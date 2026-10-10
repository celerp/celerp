# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Finishing a migration and reconciling a chart under real concurrent transactions.

Two users (or two app workers at startup) can finish the same migration, edit a
role, or reconcile the same company at the same instant. The allowed result is
always a serial old-or-new mapping: exactly one finish wins, accounts are added
once, and a role is never mapped to a target that stopped being valid while the
confirm was in flight.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import select

from celerp.models.company import Company
from celerp.models.migration import MigrationRun
from celerp.services import migrations, posting_readiness
from celerp.services.account_roles import PostingRoleError, reconcile_company, resolve_many, set_role
from celerp_accounting.import_service import create_chart_account
from celerp_accounting.models import Account
from celerp_accounting.routes import seed_chart_of_accounts
from test_posting_roles_finalize import _CHOICES
from test_posting_roles_migration_sinks import _SOURCE_CHART, _import_chart, _staged_context
from ui.i18n import t
from test_posting_roles_race_pg import _factory, _second, _settings

pytestmark = pytest.mark.asyncio


async def _staged_run(factory, monkeypatch, extra_chart_rows=()) -> tuple[uuid.UUID, uuid.UUID]:
    """A migration run ready to finalize, committed so two connections both see it."""
    async def _verified(session, run):
        return {"blockers": 0}

    monkeypatch.setattr(migrations, "_verification", _verified)
    async with factory() as s:
        context = await _staged_context(s)
        await _import_chart(context, [*_SOURCE_CHART, *extra_chart_rows])
        run = await s.get(MigrationRun, context.run_id)
        run.status = migrations._S.READY_TO_FINALIZE.value
        await s.commit()
        return context.company_id, context.run_id


async def _finalize(s, run_id, choices):
    run = await s.get(MigrationRun, run_id)
    return await migrations.finalize(s, run, choices)


async def test_two_concurrent_finalizes_leave_exactly_one_completion(committed_engine, monkeypatch):
    # finalize() commits internally (success or refusal), so there is no window where
    # one call's transaction stays open for a harness to release on cue, unlike
    # change_account/set_role in test_posting_roles_race_pg.py. Both calls are started
    # together as real tasks on their own connections; whichever's lock_company wins
    # the race completes the run, and Postgres makes the other wait for that commit
    # before it re-reads the row and refuses.
    factory = _factory(committed_engine)
    cid, run_id = await _staged_run(factory, monkeypatch)

    async def finalize_once(s):
        return await _finalize(s, run_id, _CHOICES)

    async with factory() as sa, factory() as sb:
        ra, rb = await asyncio.gather(
            asyncio.wait_for(finalize_once(sa), timeout=30),
            asyncio.wait_for(finalize_once(sb), timeout=30),
            return_exceptions=True,
        )

    results = [ra, rb]
    completed = [r for r in results if not isinstance(r, BaseException)]
    refused = [r for r in results if isinstance(r, BaseException)]
    assert len(completed) == 1 and len(refused) == 1, results
    assert completed[0].status == migrations._S.COMPLETED.value
    assert isinstance(refused[0], migrations.MigrationError) and refused[0].status_code == 409
    assert refused[0].detail == t("migration.err_cannot_finalize", "en", status=t("migration.status.completed", "en"))

    async with factory() as s:
        company = await s.get(Company, cid)
        codes = set((await s.execute(select(Account.code).where(Account.company_id == cid))).scalars())
    assert company.is_active and not company.is_migration_staged
    # The loser waited, saw the run already completed, and added nothing of its own.
    assert len(codes) == len(_SOURCE_CHART) + len(_CHOICES["add_accounts"])
    assert {"6950", "1111", "4300", "6970"} <= codes
    roles = company.settings["posting_roles"]
    assert roles["sales_revenue"] == "400" and roles["general_expense"] == "6950"


async def test_a_role_set_between_preview_and_confirm_blocks_then_refuses_the_stale_choice(
    committed_engine, monkeypatch,
):
    factory = _factory(committed_engine)
    cid, run_id = await _staged_run(
        factory, monkeypatch, extra_chart_rows=[("sales2", "410", "Other sales", "revenue", None)])

    async def set_sales_revenue_to_410(s):
        return await set_role(s, cid, "sales_revenue", "410")

    async def confirm_the_stale_preview(s):
        # The preview the user is acting on offered "400"; by the time they confirm,
        # the role has already been pointed elsewhere.
        choices = {**_CHOICES, "roles": {**_CHOICES["roles"], "sales_revenue": "400"}}
        return await _finalize(s, run_id, choices)

    held, out = await _second(committed_engine, factory, set_sales_revenue_to_410, confirm_the_stale_preview)
    assert isinstance(out, migrations.MigrationError) and out.status_code == 409
    assert "Sales revenue is already set to account 410" in out.detail["message"]

    async with factory() as s:
        company = await s.get(Company, cid)
        run = await s.get(MigrationRun, run_id)
    assert company.settings["posting_roles"]["sales_revenue"] == "410"
    assert company.is_migration_staged
    assert run.status == migrations._S.READY_TO_FINALIZE.value


async def test_an_account_added_between_preview_and_confirm_is_refused_not_overwritten(
    committed_engine, monkeypatch,
):
    factory = _factory(committed_engine)
    cid, run_id = await _staged_run(factory, monkeypatch)

    async with factory() as s:
        rows = await posting_readiness.readiness(s, cid)
    proposal = next(r for r in rows if r["role"] == "general_expense")["proposal"]
    assert proposal["code"] == "6950"  # what the preview the user is acting on offered

    # Between preview and confirm, something else takes that exact code - apply_choices
    # re-reads the chart under its own lock at confirm time rather than trusting the
    # preview, so this must be a separate committed transaction, not a held-open one.
    async with factory() as s:
        await create_chart_account(s, cid, code="6950", name="Owner's draw", account_type="equity",
                                   parent_code=None)
        await s.commit()

    async with factory() as s:
        with pytest.raises(migrations.MigrationError) as exc:
            await _finalize(s, run_id, _CHOICES)
    assert exc.value.status_code == 409
    assert "Account code 6950 is already in use" in exc.value.detail["message"]

    async with factory() as s:
        company = await s.get(Company, cid)
        account = (await s.execute(select(Account).where(
            Account.company_id == cid, Account.code == "6950"))).scalar_one()
    assert account.name == "Owner's draw" and account.account_type == "equity"
    assert "general_expense" not in (company.settings.get("posting_roles") or {})
    assert company.is_migration_staged


async def test_two_concurrent_startup_reconciles_map_each_role_once(committed_engine):
    factory = _factory(committed_engine)
    cid = uuid.uuid4()
    async with factory() as s:
        s.add(Company(id=cid, name="StartupRace", slug=f"sr-{cid.hex[:8]}", settings={}))
        await s.flush()
        await seed_chart_of_accounts(s, cid)
        # A role chosen before either worker's reconcile runs must survive both of them.
        await set_role(s, cid, "general_expense", "6100")
        await s.commit()

    async def reconcile_once(s):
        return await reconcile_company(s, cid)

    await _second(committed_engine, factory, reconcile_once, reconcile_once)

    settings = await _settings(factory, cid)
    assert settings["posting_roles"]["receivable"] == "1120"
    assert settings["posting_role_scopes"]["receivable"] == ["1120"]
    assert settings["posting_roles"]["general_expense"] == "6100"
    assert settings["posting_role_scopes"]["general_expense"] == ["6100"]


async def test_a_posting_never_sees_a_half_reconciled_role(committed_engine):
    factory = _factory(committed_engine)
    cid = uuid.uuid4()
    async with factory() as s:
        s.add(Company(id=cid, name="ReconcileRead", slug=f"rc-{cid.hex[:8]}", settings={}))
        await s.flush()
        await seed_chart_of_accounts(s, cid)
        await s.commit()

    # reconcile_company and a posting's resolve_many share no lock: reconcile takes the
    # company row and the seeded accounts FOR SHARE, and resolving a role only takes the
    # same FOR SHARE on its target account, so there is nothing for the second transaction
    # to block on. What is under test is that Postgres's read-committed isolation, not an
    # application lock, still keeps the two from ever producing a half-applied mapping.
    async with factory() as s1, factory() as s2:
        await reconcile_company(s1, cid)  # flushed, not committed: "receivable" -> "1120" exists only here

        with pytest.raises(PostingRoleError):
            await resolve_many(s2, cid, ["receivable"])  # a second, real connection, run while s1 is open

        await s1.commit()

    async with factory() as s3:
        resolved = await resolve_many(s3, cid, ["receivable"])
    assert resolved == {"receivable": "1120"}
