# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""A company reset meets the other work of the installation on real PostgreSQL connections.

Each race runs one side up to the point between its locks, starts the other until it
waits, then lets the first go on. Both sides must end the way a person would expect:
the one that came second waits, then commits or is refused with a clear reason; none is
aborted as a deadlock, and a refused side writes nothing. Around the races: work of
another company never waits for the reset; a writer that gives up waiting writes nothing;
a reset that rolls back leaves the company to the writer that waited; a lock taken in a
rolled-back savepoint no longer holds anyone up."""

from __future__ import annotations

import asyncio
import types

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError

from test_import_preflight_invariants import _factory, _seed_member_with_item, _until_blocked_or_done

pytestmark = pytest.mark.asyncio

_PLATFORM = "woocommerce"
_NAME = "RaceCo"


@pytest.fixture
def factory(committed_engine, monkeypatch):
    import celerp.db

    made = _factory(committed_engine)
    monkeypatch.setattr(celerp.db, "SessionLocal", made)
    monkeypatch.setattr(celerp.db, "engine", committed_engine)
    return made


async def _company(factory):
    company_id, user_id, entity_id = await _seed_member_with_item(factory)
    return types.SimpleNamespace(id=company_id, user_id=user_id, item=entity_id)


async def _reset(factory, company, typed_name: str = _NAME) -> dict:
    """The reset as its route runs it, in a request session of its own."""
    from celerp.routers.companies import CompanyReset, reset_company

    ctx = types.SimpleNamespace(company_id=company.id, user=types.SimpleNamespace(id=company.user_id), snonce="")
    async with factory() as session:
        return await reset_company(CompanyReset(company_name=typed_name), ctx=ctx, session=session)


def _reset_paused(monkeypatch, entered: asyncio.Event, go: asyncio.Event) -> None:
    """The reset stops once it holds the connector maintenance lock and the company for
    deletion, before it deletes anything."""
    from celerp.services import company_reset

    real = company_reset.reset

    async def paused(*args, **kwargs):
        entered.set()
        await go.wait()
        return await real(*args, **kwargs)
    monkeypatch.setattr(company_reset, "reset", paused)


async def _settled(*tasks: asyncio.Task) -> list:
    """Each side's result or exception; none may be a deadlock or still waiting."""
    results = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=10)
    from celerp.db import sqlstate
    aborted = [r for r in results if isinstance(r, DBAPIError) and sqlstate(r) == "40P01"]
    assert not aborted, f"PostgreSQL aborted a side as a deadlock: {aborted!r}"
    return results


async def _race(engine, first, second) -> list:
    """``first`` runs until it sets its event, ``second`` runs until it waits, then
    ``first`` goes on."""
    entered, go = asyncio.Event(), asyncio.Event()
    one = asyncio.create_task(first(entered, go))
    await asyncio.wait_for(entered.wait(), timeout=5)
    two = asyncio.create_task(second())
    await _until_blocked_or_done(engine, two)
    go.set()
    return await _settled(one, two)


async def _count(factory, sql: str, **params) -> int:
    async with factory() as s:
        return (await s.execute(sa.text(sql), params)).scalar_one()


async def _gone(factory, company) -> bool:
    return await _count(factory, "SELECT count(*) FROM companies WHERE id = :c", c=company.id) == 0


def _refused(result, status: int) -> None:
    from fastapi import HTTPException
    assert isinstance(result, HTTPException) and result.status_code == status, repr(result)


# -- reset against connector work ------------------------------------------------------

async def test_a_connect_waiting_for_a_reset_finds_the_company_gone(committed_engine, factory, monkeypatch):
    from celerp.connectors.ownership import ConnectorOwnershipError, claim_connector_ownership

    company = await _company(factory)

    async def reset(entered, go):
        _reset_paused(monkeypatch, entered, go)
        return await _reset(factory, company)

    async def connect():
        async with factory() as s:
            await claim_connector_ownership(s, company.id, _PLATFORM)
            await s.commit()

    done, connected = await _race(committed_engine, reset, connect)
    assert done == {"next": "start_company"}, done
    assert isinstance(connected, ConnectorOwnershipError), repr(connected)
    assert await _gone(factory, company)
    assert await _count(factory, "SELECT count(*) FROM connector_configs") == 0


async def test_a_reset_waiting_for_a_connect_is_refused_until_it_is_disconnected(committed_engine, factory):
    from celerp.connectors.ownership import claim_connector_ownership

    company = await _company(factory)

    async def connect(entered, go):
        async with factory() as s:
            await claim_connector_ownership(s, company.id, _PLATFORM)
            entered.set()
            await go.wait()
            await s.commit()

    async def reset():
        return await _reset(factory, company)

    connected, refused = await _race(committed_engine, connect, reset)
    assert connected is None
    _refused(refused, 409)
    assert not await _gone(factory, company)
    assert await _count(factory, "SELECT count(*) FROM connector_configs WHERE company_id = :c",
                        c=str(company.id)) == 1


async def test_connector_work_holding_the_runtime_fence_finishes_before_the_reset(committed_engine, factory):
    """A sync of a connector the company does not own yet (shared runtime fence, a run
    recorded for the company) against the reset, which waits for it and then removes the run."""
    from celerp.connectors.ownership import lock_connector_operation
    from celerp.models.sync_run import SyncRun

    company = await _company(factory)

    async def sync(entered, go):
        from datetime import datetime, timezone
        async with factory() as s:
            assert await lock_connector_operation(s, company.id, _PLATFORM) is None
            now = datetime.now(timezone.utc)
            s.add(SyncRun(company_id=str(company.id), connector=_PLATFORM, entity="orders", direction="inbound",
                          started_at=now, finished_at=now, status="success"))
            await s.flush()
            entered.set()
            await go.wait()
            await s.commit()

    async def reset():
        return await _reset(factory, company)

    synced, done = await _race(committed_engine, sync, reset)
    assert synced is None and done == {"next": "start_company"}, (synced, done)
    assert await _gone(factory, company)
    assert await _count(factory, "SELECT count(*) FROM sync_runs") == 0


# -- reset against notices ------------------------------------------------------------

async def test_a_notice_written_under_the_company_lock_is_removed_by_the_reset_that_waited(
        committed_engine, factory):
    from celerp.notifications import service
    from celerp.services.company_lock import lock_company

    company = await _company(factory)

    async def notify(entered, go):
        async with factory() as s:
            await lock_company(s, company.id)
            await service.notify_standing(s, company.id, "accounting", "Standing", "Body")
            entered.set()
            await go.wait()
            await s.commit()

    async def reset():
        return await _reset(factory, company)

    notified, done = await _race(committed_engine, notify, reset)
    assert notified is None and done == {"next": "start_company"}, (notified, done)
    assert await _gone(factory, company)
    assert await _count(factory, "SELECT count(*) FROM notifications") == 0


async def test_a_notice_waiting_for_a_reset_writes_nothing_once_the_company_is_gone(
        committed_engine, factory, monkeypatch):
    """A new background notice for the company commits while the reset holds it: the notice
    waits for the reset, finds the company gone and writes nothing, never a deadlock."""
    from celerp.notifications import service

    company = await _company(factory)

    async def reset(entered, go):
        _reset_paused(monkeypatch, entered, go)
        return await _reset(factory, company)

    async def notify():
        async with factory() as s:
            await service.notify_standing(s, company.id, "accounting", "Standing", "After")
            await s.commit()

    done, notified = await _race(committed_engine, reset, notify)
    assert done == {"next": "start_company"} and notified is None, (done, notified)
    assert await _gone(factory, company)
    assert await _count(factory, "SELECT count(*) FROM notifications") == 0


# -- another company, lock timeout, rollback, savepoint --------------------------------

async def _edit_and_notify(factory, company, *, lock_timeout: str | None = None) -> None:
    """An item edit of ``company`` and a notice in one transaction."""
    from celerp.notifications import service
    from celerp.services.company_lock import lock_projections

    async with factory() as s:
        if lock_timeout:
            await s.execute(sa.text(f"SET LOCAL lock_timeout = '{lock_timeout}'"))
        [row] = (await lock_projections(s, company.id, [company.item])).values()
        row.state = {**row.state, "name": "Edited"}
        await service.notify_standing(s, company.id, "inventory", "Edited", "Body")
        await s.commit()


async def _name(factory, company) -> str | None:
    async with factory() as s:
        return await s.scalar(sa.text(
            "SELECT state->>'name' FROM projections WHERE company_id = :c AND entity_id = :e"),
            {"c": company.id, "e": company.item})


async def test_another_company_is_never_held_up_by_a_reset(factory, monkeypatch):
    alpha, beta = await _company(factory), await _company(factory)
    entered, go = asyncio.Event(), asyncio.Event()
    _reset_paused(monkeypatch, entered, go)
    reset = asyncio.create_task(_reset(factory, alpha))
    await asyncio.wait_for(entered.wait(), timeout=5)
    try:
        await _edit_and_notify(factory, beta, lock_timeout="200ms")
    finally:
        go.set()
    [done] = await _settled(reset)
    assert done == {"next": "start_company"}, done
    assert await _name(factory, beta) == "Edited"
    assert await _count(factory, "SELECT count(*) FROM notifications WHERE company_id = :c", c=beta.id) == 1


async def test_a_writer_that_stops_waiting_for_a_reset_writes_nothing(factory, monkeypatch):
    from celerp.db import sqlstate

    company = await _company(factory)
    entered, go = asyncio.Event(), asyncio.Event()
    _reset_paused(monkeypatch, entered, go)
    reset = asyncio.create_task(_reset(factory, company))
    await asyncio.wait_for(entered.wait(), timeout=5)
    try:
        with pytest.raises(DBAPIError) as gave_up:
            await _edit_and_notify(factory, company, lock_timeout="200ms")
        assert sqlstate(gave_up.value) == "55P03"
        assert await _name(factory, company) == "Original"
    finally:
        go.set()
    [done] = await _settled(reset)
    assert done == {"next": "start_company"}, done
    assert await _gone(factory, company)


async def test_a_writer_waiting_for_a_reset_that_rolls_back_finds_the_company_unchanged(
        committed_engine, factory, monkeypatch):
    company = await _company(factory)

    async def reset(entered, go):
        _reset_paused(monkeypatch, entered, go)
        return await _reset(factory, company, typed_name="Not the name")

    async def edit():
        await _edit_and_notify(factory, company)

    refused, edited = await _race(committed_engine, reset, edit)
    _refused(refused, 422)
    assert edited is None
    assert await _name(factory, company) == "Edited"
    assert await _count(factory, "SELECT count(*) FROM notifications WHERE company_id = :c", c=company.id) == 1


async def test_a_lock_taken_in_a_rolled_back_savepoint_holds_no_one_up(factory):
    from celerp.services.company_lock import lock_company

    company = await _company(factory)
    async with factory() as held:
        await held.execute(sa.text("SELECT 1"))
        savepoint = await held.begin_nested()
        await lock_company(held, company.id)
        await savepoint.rollback()
        # The outer transaction is still open; PostgreSQL released the savepoint's lock.
        await _edit_and_notify(factory, company, lock_timeout="200ms")
        await held.commit()
    assert await _name(factory, company) == "Edited"


# -- a store link (company, link namespace, item) against the reset ---------------------

async def _link(factory, company) -> None:
    import celerp_inventory.services as svc
    async with factory() as s:
        await svc.set_external_link(s, company.id, company.item, _PLATFORM,
                                    {"product_id": "77", "variation_id": None, "sku": "HOLD-1",
                                     "sync_enabled": True})
        await s.commit()


async def test_a_store_link_change_finishes_before_the_reset_that_waited(committed_engine, factory, monkeypatch):
    import celerp_inventory.services as svc

    company = await _company(factory)
    await _link(factory, company)
    real = svc.set_external_link

    async def stop(entered, go):
        async def paused(*args, **kwargs):
            entered.set()
            await go.wait()
            return await real(*args, **kwargs)
        monkeypatch.setattr(svc, "set_external_link", paused)
        async with factory() as s:
            await svc.set_external_link_state(s, company.id, company.item, _PLATFORM, sync_enabled=False)
            await s.commit()

    async def reset():
        return await _reset(factory, company)

    stopped, done = await _race(committed_engine, stop, reset)
    assert stopped is None and done == {"next": "start_company"}, (stopped, done)
    assert await _gone(factory, company)


async def test_a_store_link_change_waiting_for_a_reset_finds_the_item_gone(committed_engine, factory, monkeypatch):
    import celerp_inventory.services as svc

    company = await _company(factory)
    await _link(factory, company)

    async def reset(entered, go):
        _reset_paused(monkeypatch, entered, go)
        return await _reset(factory, company)

    async def stop():
        async with factory() as s:
            await svc.set_external_link_state(s, company.id, company.item, _PLATFORM, sync_enabled=False)
            await s.commit()

    done, stopped = await _race(committed_engine, reset, stop)
    assert done == {"next": "start_company"}, done
    assert not isinstance(stopped, DBAPIError), repr(stopped)
    assert await _gone(factory, company)
