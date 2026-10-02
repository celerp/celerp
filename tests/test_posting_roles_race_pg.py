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
from celerp.services.company_lock import locked_company
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


# --- Structural chart changes meet one at a time ---------------------------------------


async def _create(s, cid, code, parent, account_type="asset"):
    from celerp_accounting.import_service import create_chart_account

    acc = await create_chart_account(s, cid, code=code, name=code, account_type=account_type,
                                     parent_code=parent)
    await s.flush()
    return acc.code


async def _children(factory, cid, parent) -> list[str]:
    async with factory() as s:
        return sorted((await s.execute(select(Account.code).where(
            Account.company_id == cid, Account.parent_code == parent))).scalars().all())


async def test_a_child_added_while_its_parent_is_switched_off_is_refused(committed_engine):
    factory = _factory(committed_engine)
    cid = await _seed(factory, [("8600", "asset", None)])

    async def deactivate(s):
        return await change_account(s, cid, "8600", is_active=False)

    _, out = await _second(committed_engine, factory, deactivate, lambda s: _create(s, cid, "8601", "8600"))
    assert isinstance(out, HTTPException) and out.status_code == 422, out
    assert await _children(factory, cid, "8600") == []


async def test_a_parent_switched_off_while_a_child_is_added_is_refused(committed_engine):
    factory = _factory(committed_engine)
    cid = await _seed(factory, [("8610", "asset", None)])

    async def deactivate(s):
        return await change_account(s, cid, "8610", is_active=False)

    _, out = await _second(committed_engine, factory, lambda s: _create(s, cid, "8611", "8610"), deactivate)
    assert isinstance(out, HTTPException) and out.status_code == 409, out
    assert (await _row(factory, cid, "8610")).is_active is True
    assert await _children(factory, cid, "8610") == ["8611"]


async def test_an_account_moved_while_its_new_parent_is_retyped_is_refused(committed_engine):
    factory = _factory(committed_engine)
    cid = await _seed(factory, [("8620", "asset", None), ("8621", "asset", None)])

    async def retype(s):
        return await change_account(s, cid, "8620", account_type="liability")

    async def move(s):
        return await change_account(s, cid, "8621", parent_code="8620")

    _, out = await _second(committed_engine, factory, retype, move)
    assert isinstance(out, HTTPException) and out.status_code == 422, out
    assert (await _row(factory, cid, "8621")).parent_code is None


async def test_a_parent_retyped_while_an_account_moves_under_it_is_refused(committed_engine):
    factory = _factory(committed_engine)
    cid = await _seed(factory, [("8630", "asset", None), ("8631", "asset", None)])

    async def move(s):
        return await change_account(s, cid, "8631", parent_code="8630")

    async def retype(s):
        return await change_account(s, cid, "8630", account_type="liability")

    _, out = await _second(committed_engine, factory, move, retype)
    assert isinstance(out, HTTPException) and out.status_code == 422, out
    assert (await _row(factory, cid, "8630")).account_type == "asset"


@pytest.mark.parametrize("change", ["create", "move"])
async def test_a_child_placed_while_a_role_moves_onto_its_parent_is_refused(committed_engine, change):
    factory = _factory(committed_engine)
    cid = await _seed(factory, [("8640", "asset", "1100"), ("8641", "asset", None)])

    async def remap(s):
        return await set_role(s, cid, "receivable", "8640")

    async def place(s):
        if change == "create":
            return await _create(s, cid, "8642", "8640")
        return await change_account(s, cid, "8641", parent_code="8640")

    _, out = await _second(committed_engine, factory, remap, place)
    assert isinstance(out, HTTPException) and out.status_code == 422, out
    assert await _children(factory, cid, "8640") == []
    assert (await _settings(factory, cid))["posting_roles"]["receivable"] == "8640"


@pytest.mark.parametrize("change", ["create", "move"])
async def test_a_role_moved_onto_a_parent_while_a_child_is_placed_is_refused(committed_engine, change):
    factory = _factory(committed_engine)
    cid = await _seed(factory, [("8650", "asset", "1100"), ("8651", "asset", None)])

    async def place(s):
        if change == "create":
            return await _create(s, cid, "8652", "8650")
        return await change_account(s, cid, "8651", parent_code="8650")

    async def remap(s):
        return await set_role(s, cid, "receivable", "8650")

    _, out = await _second(committed_engine, factory, place, remap)
    assert isinstance(out, HTTPException) and out.status_code == 422, out
    assert "header account" in out.detail
    assert (await _settings(factory, cid))["posting_roles"]["receivable"] == "1120"


# --- Startup mapping meets a chart change one at a time --------------------------------


@pytest.mark.parametrize("first", ["reconcile", "child"])
async def test_startup_mapping_and_a_new_child_never_leave_a_role_on_a_header(committed_engine, first):
    factory = _factory(committed_engine)
    cid = await _seed(factory)
    async with factory() as s:
        company = await locked_company(s, cid)
        roles = dict(company.settings["posting_roles"])
        roles.pop("receivable")
        company.settings = {**company.settings, "posting_roles": roles}
        await s.commit()

    steps = {"reconcile": lambda s: reconcile_company(s, cid), "child": lambda s: _create(s, cid, "1121", "1120")}
    order = [first, "child" if first == "reconcile" else "reconcile"]
    _, out = await _second(committed_engine, factory, steps[order[0]], steps[order[1]])
    mapped = (await _settings(factory, cid))["posting_roles"].get("receivable")
    children = await _children(factory, cid, "1120")
    assert not (mapped == "1120" and children), (mapped, children, out)
    if first == "reconcile":
        assert mapped == "1120" and children == []
        assert isinstance(out, HTTPException) and out.status_code == 422, out
    else:
        assert children == ["1121"] and mapped is None
        assert not isinstance(out, BaseException), out
