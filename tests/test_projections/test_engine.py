# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

from __future__ import annotations

import uuid

import pytest

from celerp.events.engine import emit_event
from celerp.models.company import Company
from celerp.models.projections import Projection
from celerp.projections.engine import ProjectionEngine


@pytest.mark.asyncio
async def test_projection_apply_and_rebuild(session):
    company_id = uuid.uuid4()
    # Postgres enforces ledger.company_id → companies; seed a real company first.
    session.add(Company(id=company_id, name="ProjCo", slug=f"projco-{company_id.hex[:8]}"))
    await session.flush()

    e1 = await emit_event(
        session,
        company_id=company_id,
        entity_id="item:1",
        entity_type="item",
        event_type="item.created",
        data={"sku": "S", "name": "A", "quantity": 1},
        actor_id=None,
        location_id=None,
        source="test",
        idempotency_key=str(uuid.uuid4()),
        metadata_={},
    )
    await session.commit()

    proj = await session.get(Projection, {"company_id": company_id, "entity_id": "item:1"})
    assert proj is not None
    assert proj.state["name"] == "A"
    assert proj.version == e1.id

    await emit_event(
        session,
        company_id=company_id,
        entity_id="item:1",
        entity_type="item",
        event_type="item.updated",
        data={"fields_changed": {"name": {"old": "A", "new": "B"}}},
        actor_id=None,
        location_id=None,
        source="test",
        idempotency_key=str(uuid.uuid4()),
        metadata_={},
    )
    await session.commit()

    proj2 = await session.get(Projection, {"company_id": company_id, "entity_id": "item:1"})
    assert proj2.state["name"] == "B"

    # rebuild is deterministic
    await ProjectionEngine.rebuild(session, company_id=company_id)
    await session.commit()
    proj3 = await session.get(Projection, {"company_id": company_id, "entity_id": "item:1"})
    assert proj3.state["name"] == "B"


@pytest.mark.asyncio
async def test_concurrent_events_on_one_entity_keep_both_writes(_db_engine):
    """Two events landing at once on the same entity must serialize on the
    projection row, so the second applier bases its state on the first's
    committed write. Without the row lock both read the same base state and
    whichever commit lands last silently drops the other's field, even though
    the ledger holds both events."""
    import asyncio

    from sqlalchemy import delete
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from celerp.models.ledger import LedgerEntry

    factory = async_sessionmaker(bind=_db_engine, class_=AsyncSession, expire_on_commit=False)
    company_id = uuid.uuid4()
    entity_id = f"item:{uuid.uuid4().hex[:8]}"

    def _event_kwargs(event_type, data):
        return dict(
            company_id=company_id,
            entity_id=entity_id,
            entity_type="item",
            event_type=event_type,
            data=data,
            actor_id=None,
            location_id=None,
            source="test",
            idempotency_key=str(uuid.uuid4()),
            metadata_={},
        )

    async with factory() as seed:
        seed.add(Company(id=company_id, name="RaceCo", slug=f"raceco-{company_id.hex[:8]}"))
        await seed.flush()
        await emit_event(seed, **_event_kwargs("item.created", {"sku": "R", "name": "A", "quantity": 1}))
        await seed.commit()

    s1, s2 = factory(), factory()
    try:
        # First edit holds the projection row until its transaction commits.
        await emit_event(s1, **_event_kwargs("item.updated", {"fields_changed": {"quantity": {"old": 1, "new": 7}}}))

        async def _second_edit():
            # Routes read the projection unlocked for validation before emitting
            # (e.g. the inventory quantity check); the engine must not let that
            # cached copy stand in for the locked read.
            await s2.get(Projection, {"company_id": company_id, "entity_id": entity_id})
            await emit_event(s2, **_event_kwargs("item.updated", {"fields_changed": {"name": {"old": "A", "new": "B"}}}))
            await s2.commit()

        task = asyncio.create_task(_second_edit())
        await asyncio.sleep(0.3)  # let the second edit reach the projection row
        await s1.commit()
        await asyncio.wait_for(task, timeout=10)

        async with factory() as check:
            proj = await check.get(Projection, {"company_id": company_id, "entity_id": entity_id})
            assert proj.state["quantity"] == 7, "concurrent edit dropped the first write"
            assert proj.state["name"] == "B", "concurrent edit dropped the second write"
    finally:
        await s1.close()
        await s2.close()
        async with factory() as cleanup:
            await cleanup.execute(delete(LedgerEntry).where(LedgerEntry.company_id == company_id))
            await cleanup.execute(delete(Projection).where(Projection.company_id == company_id))
            await cleanup.execute(delete(Company).where(Company.id == company_id))
            await cleanup.commit()
