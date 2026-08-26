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
