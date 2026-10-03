# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Creating a production run and deleting one of its items at the same moment settle in
one order, never both half way.

Runs on real PostgreSQL across two connections. The first transaction holds its commit
while the second starts; whichever holds the locks first wins:

- delete first: the run waits, then finds the item gone and is refused, leaving nothing;
- run first: the Delete waits for the run to be saved, then finds the item named by the
  run and deletes nothing.

Delete only removes a draft that nothing depends on, so the item here is a draft.
"""

from __future__ import annotations

import asyncio
import types
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from celerp.events.engine import emit_event
from celerp.models.accounting import UserCompany
from celerp.models.company import Company, Location, User
from celerp.models.ledger import LedgerEntry
from celerp.models.projections import Projection
from celerp_inventory import routes as inventory
from celerp_manufacturing import routes as mfg

pytestmark = pytest.mark.asyncio

_PART = "item:part"


async def _seed(factory):
    company_id, user_id = uuid.uuid4(), uuid.uuid4()
    async with factory() as s:
        s.add(Company(id=company_id, name="Runs", slug=f"runs-{company_id.hex[:8]}", settings={}))
        s.add(User(id=user_id, email=f"a-{user_id.hex[:8]}@example.test", name="Admin",
                   auth_hash="x", is_active=True))
        await s.flush()
        s.add(Location(id=uuid.uuid4(), company_id=company_id, name="Main", type="warehouse", is_default=True))
        s.add(UserCompany(user_id=user_id, company_id=company_id, role="admin", is_active=True))
        await emit_event(
            s, company_id=company_id, entity_id=_PART, entity_type="item", event_type="item.created",
            data={"sku": "PART", "name": "Part", "quantity": 5, "sell_by": "piece", "status": "draft"},
            actor_id=user_id, location_id=None, source="test", idempotency_key=str(uuid.uuid4()),
        )
        await s.commit()
    return company_id, types.SimpleNamespace(id=user_id)


def _create(s, company_id, user):
    payload = mfg.MfgOrderCreate(description="Run", inputs=[mfg.MfgInput(item_id=_PART, quantity=1)])
    return mfg.create_order(payload, company_id=company_id, user=user, _=None, session=s)


def _delete(s, company_id, user):
    return inventory.bulk_delete(inventory.BulkDeleteBody(entity_ids=[_PART]), company_id=company_id,
                                 _=None, user=user, session=s)


async def _until_waiting_or_done(engine, task: asyncio.Task) -> None:
    for _ in range(400):
        if task.done():
            return
        async with engine.connect() as conn:
            waiting = (await conn.execute(text(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE datname = current_database() AND wait_event_type = 'Lock'"
            ))).scalar_one()
        if waiting:
            return
        await asyncio.sleep(0.05)
    raise AssertionError("the second transaction neither waited nor finished")


async def _race(engine, factory, company_id, user, first, second):
    """``first`` runs up to its commit and holds it; ``second`` starts and waits on the
    lock; then ``first`` commits. Returns each side's result or exception."""
    reached, release = asyncio.Event(), asyncio.Event()

    async def _run(action, hold):
        async with factory() as s:
            if hold:
                real = s.commit

                async def _held():
                    reached.set()
                    await release.wait()
                    await real()
                s.commit = _held
            try:
                return await action(s, company_id, user)
            except HTTPException as exc:
                await s.rollback()
                return exc

    one = asyncio.create_task(_run(first, True))
    await asyncio.wait_for(reached.wait(), 30)
    two = asyncio.create_task(_run(second, False))
    await _until_waiting_or_done(engine, two)
    assert not two.done(), "the second transaction did not wait for the first"
    release.set()
    return await asyncio.wait_for(one, 30), await asyncio.wait_for(two, 30)


async def _state(factory, company_id):
    async with factory() as s:
        part = await s.get(Projection, {"company_id": company_id, "entity_id": _PART})
        runs = (await s.execute(select(Projection).where(
            Projection.company_id == company_id, Projection.entity_type == "mfg_order"))).scalars().all()
        run_events = (await s.execute(select(func.count()).select_from(LedgerEntry).where(
            LedgerEntry.company_id == company_id, LedgerEntry.entity_type == "mfg_order"))).scalar_one()
    return part, runs, run_events


async def test_delete_first_refuses_the_waiting_run(committed_engine):
    factory = async_sessionmaker(bind=committed_engine, class_=AsyncSession, expire_on_commit=False)
    company_id, user = await _seed(factory)

    deleted, created = await _race(committed_engine, factory, company_id, user, _delete, _create)

    assert deleted == {"deleted": 1}
    assert isinstance(created, HTTPException) and created.status_code == 422, created
    assert _PART in created.detail
    part, runs, run_events = await _state(factory, company_id)
    assert part is None and runs == [] and run_events == 0


async def test_run_first_is_saved_and_the_delete_waits_then_refuses(committed_engine):
    factory = async_sessionmaker(bind=committed_engine, class_=AsyncSession, expire_on_commit=False)
    company_id, user = await _seed(factory)

    created, deleted = await _race(committed_engine, factory, company_id, user, _create, _delete)

    assert not isinstance(created, HTTPException), created
    assert isinstance(deleted, HTTPException) and deleted.status_code == 409, deleted
    part, runs, run_events = await _state(factory, company_id)
    assert part is not None and part.state["status"] == "draft"
    assert [r.state["inputs"][0]["item_id"] for r in runs] == [_PART] and run_events == 1
