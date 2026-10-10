# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""An item change and the store sync work it queues are kept or dropped together.

If queueing the store sync for an item change fails, a caller that catches the failure
and commits (a bulk loop recording one failed row and going on) keeps neither the change
nor a partial queue row.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from celerp.events.engine import emit_event
from celerp.models.company import Company
from celerp.models.connector_config import OutboundQueue

pytestmark = pytest.mark.asyncio

_ITEM = "item:atomic"


def _emit(s, company_id, event_type, data):
    return emit_event(
        s, company_id=company_id, entity_id=_ITEM, entity_type="item", event_type=event_type,
        data=data, actor_id=None, location_id=None, source="test",
        idempotency_key=str(uuid.uuid4()), metadata_={},
    )


async def _seed(factory) -> uuid.UUID:
    company_id = uuid.uuid4()
    async with factory() as s:
        s.add(Company(id=company_id, name="Atomic", slug=f"atomic-{company_id.hex[:8]}", settings={}))
        await s.flush()
        await _emit(s, company_id, "item.created", {"sku": "ATOM", "name": "Atom", "quantity": 1, "sell_by": "piece"})
        await s.commit()
    return company_id


async def _stored(engine, company_id) -> tuple:
    async with engine.connect() as conn:
        name = (await conn.execute(text(
            "SELECT state::jsonb ->> 'name' FROM projections WHERE company_id = :c AND entity_id = :e"),
            {"c": company_id, "e": _ITEM})).scalar_one_or_none()
        events = (await conn.execute(text(
            "SELECT event_type FROM ledger WHERE company_id = :c AND entity_id = :e ORDER BY id"),
            {"c": company_id, "e": _ITEM})).scalars().all()
    return name, list(events)


async def test_a_change_whose_sync_work_fails_to_queue_is_not_kept_without_it(committed_engine, monkeypatch):
    from celerp.connectors import outbound_queue

    factory = async_sessionmaker(bind=committed_engine, class_=AsyncSession, expire_on_commit=False)
    company_id = await _seed(factory)

    async def _fails_after_queueing(session, entry, *, previous_state=None):
        session.add(OutboundQueue(company_id=str(entry.company_id), connector="woocommerce",
                                  entity_type="inventory", entity_id="product:1", status="pending",
                                  retry_count=0))
        await session.flush()
        raise RuntimeError("queue unavailable")

    monkeypatch.setattr(outbound_queue, "enqueue_item_change", _fails_after_queueing)
    async with factory() as s:
        with pytest.raises(RuntimeError):
            await _emit(s, company_id, "item.updated", {"fields_changed": {"name": {"old": "Atom", "new": "Lost"}}})
        await s.commit()

    assert await _stored(committed_engine, company_id) == ("Atom", ["item.created"])
    async with committed_engine.connect() as conn:
        queued = (await conn.execute(text("SELECT count(*) FROM outbound_queue WHERE company_id = :c"),
                                     {"c": str(company_id)})).scalar_one()
    assert queued == 0
