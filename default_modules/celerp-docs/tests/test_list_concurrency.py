# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""List line_items are read-modify-written by scan check-off and counting: a route reads the
whole array, mutates it in Python, and writes it back. Two concurrent writers on the same list
would each read the same array and the second's write would clobber the first (a lost update).
_get_list_for_update takes a row lock (SELECT ... FOR UPDATE) so the second writer blocks until
the first commits, then re-reads the committed array.

These use independent sessions on the shared engine with real commits (not the savepoint
session): a row lock is only observable across separately committed transactions."""

from __future__ import annotations

import asyncio
import types
import uuid

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from celerp.events.engine import emit_event
from celerp.models.company import Company
from celerp.models.ledger import LedgerEntry
from celerp.models.projections import Projection


async def _seed_company(factory) -> uuid.UUID:
    company_id = uuid.uuid4()
    async with factory() as s:
        s.add(Company(id=company_id, name="LockCo", slug=f"lock-{company_id.hex[:8]}"))
        await s.commit()
    return company_id


async def _seed_list(factory, company_id) -> str:
    entity_id = "list:LOCKTEST"
    async with factory() as s:
        await emit_event(
            s, company_id=company_id, entity_id=entity_id, entity_type="list",
            event_type="list.created",
            data={"list_type": "audit", "status": "finalized", "ref_id": "LOCKTEST",
                  "line_items": [{"item_id": "item:x", "sku": "X", "quantity": 1}]},
            actor_id=None, location_id=None, source="test",
            idempotency_key=str(uuid.uuid4()), metadata_={},
        )
        await s.commit()
    return entity_id


async def _cleanup(factory, company_id):
    async with factory() as s:
        await s.execute(delete(Projection).where(Projection.company_id == company_id))
        await s.execute(delete(LedgerEntry).where(LedgerEntry.company_id == company_id))
        await s.execute(delete(Company).where(Company.id == company_id))
        await s.commit()


async def _seed_typed_list(factory, company_id, entity_id, list_type, status):
    async with factory() as s:
        await emit_event(
            s, company_id=company_id, entity_id=entity_id, entity_type="list",
            event_type="list.created",
            data={"list_type": list_type, "status": status, "ref_id": entity_id.split(":")[-1],
                  "line_items": [{"item_id": "item:x", "sku": "X", "quantity": 1}]},
            actor_id=None, location_id=None, source="test",
            idempotency_key=str(uuid.uuid4()), metadata_={},
        )
        await s.commit()


# Each terminal/undo route reads the list's line_items, mutates stock, and writes the array back, so it
# must take the list row lock. Seed a status the route's early guard rejects (a fast 409): the block we
# assert happens at the FOR UPDATE loader, before that guard, so the guard's outcome is irrelevant - all
# we need is that the route reaches and acquires the lock. Without the lock (the pre-change loader) the
# route returns immediately and finishes inside the grace window.
_LOCK_CALLERS = [
    ("finalize_list", "shipment", "finalized"),  # guard: only a draft can be finalized
    ("adjust_audit", "audit", "draft"),          # guard: finalize the count before adjusting
    ("undo_audit_adjust", "audit", "draft"),     # guard: no adjustment to undo
    ("undo_write_off", "writeoff", "draft"),     # guard: no removal to undo
]


@pytest.mark.parametrize("fn_name, list_type, status", _LOCK_CALLERS)
@pytest.mark.asyncio
async def test_terminal_route_takes_list_row_lock(_db_engine, fn_name, list_type, status):
    """While one transaction holds the list row lock, the terminal/undo route on the same list blocks
    until that transaction commits. Before the fix each route loaded the row without FOR UPDATE, so it
    ran immediately against a stale line_items array (a lost update against a concurrent scan/count)."""
    from celerp_docs import routes as _routes
    from celerp_docs.routes import _get_list_for_update

    fn = getattr(_routes, fn_name)
    factory = async_sessionmaker(bind=_db_engine, class_=AsyncSession, expire_on_commit=False)
    company_id = await _seed_company(factory)
    entity_id = f"list:LOCK-{fn_name}"
    await _seed_typed_list(factory, company_id, entity_id, list_type, status)
    user = types.SimpleNamespace(id=uuid.uuid4())
    s_lock, s_route = factory(), factory()
    try:
        await _get_list_for_update(s_lock, company_id, entity_id)  # hold the row lock

        done = asyncio.Event()

        async def _run():
            try:
                await fn(entity_id, company_id=company_id, _=None, user=user, session=s_route)
            except Exception:
                pass  # a status-guard 409 still proves the route got PAST the FOR UPDATE loader
            finally:
                done.set()

        task = asyncio.create_task(_run())
        await asyncio.sleep(0.3)
        assert not done.is_set(), f"{fn_name} did not block on the list row lock"

        await s_lock.commit()  # release the lock
        await asyncio.wait_for(task, timeout=10)
        assert done.is_set()
    finally:
        await s_lock.close()
        await s_route.close()
        await _cleanup(factory, company_id)


@pytest.mark.asyncio
async def test_get_list_for_update_serializes_concurrent_writers(_db_engine):
    """While one transaction holds the list row lock, a second _get_list_for_update on the same
    list blocks until the first commits. Without the FOR UPDATE lock the second returns
    immediately and both writers derive from the same stale line_items array."""
    from celerp_docs.routes import _get_list_for_update

    factory = async_sessionmaker(bind=_db_engine, class_=AsyncSession, expire_on_commit=False)
    company_id = await _seed_company(factory)
    entity_id = await _seed_list(factory, company_id)
    s1, s2 = factory(), factory()
    try:
        await _get_list_for_update(s1, company_id, entity_id)  # s1 holds the row lock

        second_locked = asyncio.Event()

        async def _second():
            await _get_list_for_update(s2, company_id, entity_id)
            second_locked.set()

        task = asyncio.create_task(_second())
        await asyncio.sleep(0.3)
        assert not second_locked.is_set(), "second writer did not block on the list row lock"

        await s1.commit()  # release the lock
        await asyncio.wait_for(task, timeout=10)
        assert second_locked.is_set()
        await s2.commit()
    finally:
        await s1.close()
        await s2.close()
        await _cleanup(factory, company_id)


@pytest.mark.asyncio
async def test_get_list_for_update_404s_non_list(_db_engine):
    """The locking loader keeps the same entity-type guard as the plain loader: a missing or
    non-list entity is a 404, not a silently locked wrong row."""
    from fastapi import HTTPException

    from celerp_docs.routes import _get_list_for_update

    factory = async_sessionmaker(bind=_db_engine, class_=AsyncSession, expire_on_commit=False)
    company_id = await _seed_company(factory)
    try:
        async with factory() as s:
            with pytest.raises(HTTPException) as exc:
                await _get_list_for_update(s, company_id, "list:NOPE")
            assert exc.value.status_code == 404
    finally:
        await _cleanup(factory, company_id)
