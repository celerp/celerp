# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Posting roles and the chart under real concurrent transactions.

Each case holds one transaction open at the point that matters, starts the other
on its own connection, waits until Postgres reports it blocked on a lock, then
lets the first commit. The only acceptable outcome is what some serial order of
the two would have produced: never a posting on an account that changed under
it, a lost scope, a loop in the hierarchy, or a deadlock.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from celerp.events.engine import emit_event
from celerp.models.company import Company
from celerp.services.account_roles import reconcile_company, resolve_many, set_role
from celerp_accounting.chart_rules import change_account
from celerp_accounting.models import Account
from celerp_accounting.routes import seed_chart_of_accounts

pytestmark = pytest.mark.asyncio


async def _seed(factory, extra: list[tuple[str, str, str | None]] = ()) -> uuid.UUID:
    company_id = uuid.uuid4()
    async with factory() as s:
        s.add(Company(id=company_id, name="RoleRace", slug=f"rr-{company_id.hex[:8]}",
                      settings={}))
        await s.flush()
        await seed_chart_of_accounts(s, company_id)
        await s.flush()
        await reconcile_company(s, company_id)
        for code, account_type, parent in extra:
            s.add(Account(id=uuid.uuid4(), company_id=company_id, code=code, name=code,
                          account_type=account_type, parent_code=parent))
        await s.commit()
    return company_id


async def _post(s, company_id, entries):
    key = uuid.uuid4().hex
    return await emit_event(
        s, company_id=company_id, entity_id=f"je:race:{key}", entity_type="journal_entry",
        event_type="acc.journal_entry.created",
        data={"ts": "2026-01-05", "memo": "Race", "entries": entries},
        actor_id=None, location_id=None, source="test", idempotency_key=key, metadata_={},
    )


async def _until_blocked(engine, task: asyncio.Task) -> None:
    for _ in range(400):
        assert not task.done(), f"the second transaction did not wait: {task.exception()!r}"
        async with engine.connect() as conn:
            waiting = (await conn.execute(text(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE datname = current_database() AND wait_event_type = 'Lock'"
            ))).scalar_one()
        if waiting:
            return
        await asyncio.sleep(0.05)
    raise AssertionError("the second transaction never blocked")


async def _second(engine, factory, first, second):
    """Run ``first`` and hold its transaction open; run ``second`` until it blocks;
    commit ``first``; return (first result, second result or exception)."""
    async with factory() as s1, factory() as s2:
        held = await first(s1)

        async def run():
            out = await second(s2)
            await s2.commit()
            return out

        task = asyncio.create_task(run())
        await _until_blocked(engine, task)
        await s1.commit()
        out = (await asyncio.gather(asyncio.wait_for(task, timeout=30), return_exceptions=True))[0]
        if isinstance(out, BaseException):
            await s2.rollback()
    return held, out


async def _row(factory, company_id, code) -> Account:
    async with factory() as s:
        return (await s.execute(select(Account).where(
            Account.company_id == company_id, Account.code == code))).scalar_one()


async def _settings(factory, company_id) -> dict:
    async with factory() as s:
        return dict((await s.get(Company, company_id)).settings or {})


def _factory(engine):
    return async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)


async def test_type_edit_waits_for_a_posting_and_then_sees_its_history(committed_engine):
    factory = _factory(committed_engine)
    cid = await _seed(factory, [("8000", "expense", "6000")])

    async def post(s):
        return await _post(s, cid, [{"account": "8000", "debit": 5}, {"account": "2110", "credit": 5}])

    async def retype(s):
        return await change_account(s, cid, "8000", account_type="cogs")

    _, out = await _second(committed_engine, factory, post, retype)
    assert isinstance(out, HTTPException) and out.status_code == 409, out
    assert (await _row(factory, cid, "8000")).account_type == "expense"


async def test_remap_waits_for_a_deactivation_and_then_refuses_the_account(committed_engine):
    factory = _factory(committed_engine)
    cid = await _seed(factory, [("8100", "asset", "1100")])

    async def deactivate(s):
        return await change_account(s, cid, "8100", is_active=False)

    async def remap(s):
        return await set_role(s, cid, "receivable", "8100")

    _, out = await _second(committed_engine, factory, deactivate, remap)
    assert isinstance(out, HTTPException) and out.status_code == 422, out
    assert (await _settings(factory, cid))["posting_roles"]["receivable"] == "1120"


async def test_deactivation_waits_for_a_remap_and_then_sees_the_new_target(committed_engine):
    factory = _factory(committed_engine)
    cid = await _seed(factory, [("8200", "asset", "1100")])

    async def remap(s):
        return await set_role(s, cid, "receivable", "8200")

    async def deactivate(s):
        return await change_account(s, cid, "8200", is_active=False)

    _, out = await _second(committed_engine, factory, remap, deactivate)
    assert isinstance(out, HTTPException) and out.status_code == 409, out
    assert (await _row(factory, cid, "8200")).is_active is True
    assert (await _settings(factory, cid))["posting_roles"]["receivable"] == "8200"


async def test_remap_during_a_posting_leaves_the_posting_on_one_mapping(committed_engine):
    factory = _factory(committed_engine)
    cid = await _seed(factory, [("8300", "asset", "1100")])

    async def recognize(s):
        targets = await resolve_many(s, cid, ["receivable", "sales_revenue"])
        await _post(s, cid, [
            {"account": targets["receivable"], "debit": 5, "account_roles": ["receivable"]},
            {"account": targets["sales_revenue"], "credit": 5, "account_roles": ["sales_revenue"]},
        ])
        return targets

    async def remap_and_deactivate_old(s):
        await set_role(s, cid, "receivable", "8300")
        return await change_account(s, cid, "1120", is_active=False)

    held, out = await _second(committed_engine, factory, recognize, remap_and_deactivate_old)
    assert held["receivable"] == "1120"
    assert not isinstance(out, BaseException), out
    assert (await _row(factory, cid, "1120")).is_active is False
    async with factory() as s:
        assert (await resolve_many(s, cid, ["receivable"])) == {"receivable": "8300"}


async def test_two_role_edits_keep_every_scope(committed_engine):
    factory = _factory(committed_engine)
    cid = await _seed(factory, [("8400", "asset", "1100"), ("8401", "asset", "1100")])

    async def first(s):
        return await set_role(s, cid, "receivable", "8400")

    async def second(s):
        return await set_role(s, cid, "receivable", "8401")

    _, out = await _second(committed_engine, factory, first, second)
    assert not isinstance(out, BaseException), out
    settings = await _settings(factory, cid)
    assert settings["posting_roles"]["receivable"] == "8401"
    assert sorted(settings["posting_role_scopes"]["receivable"]) == ["1120", "8400", "8401"]


async def test_two_moves_cannot_close_a_loop(committed_engine):
    factory = _factory(committed_engine)
    cid = await _seed(factory, [("8500", "asset", None), ("8501", "asset", None)])

    async def first(s):
        return await change_account(s, cid, "8500", parent_code="8501")

    async def second(s):
        return await change_account(s, cid, "8501", parent_code="8500")

    _, out = await _second(committed_engine, factory, first, second)
    assert isinstance(out, HTTPException) and out.status_code == 422, out
    assert (await _row(factory, cid, "8500")).parent_code == "8501"
    assert (await _row(factory, cid, "8501")).parent_code is None
