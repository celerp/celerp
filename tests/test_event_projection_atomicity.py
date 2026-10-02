# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""An event is recorded only together with its effect on the projection.

A change to an item that is gone is refused with 404. A caller that catches that
refusal and commits (a bulk loop recording one failed row and going on) must not
leave the refused event in the ledger, and a rebuild must never turn such an event
into a ghost item.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from celerp.events.engine import emit_event
from celerp.models.company import Company
from celerp.models.ledger import LedgerEntry
from celerp.projections.engine import ProjectionEngine
from celerp.services.item_erasure import erase_items

pytestmark = pytest.mark.asyncio

_ITEM = "item:atomic"


def _emit(s, company_id, event_type, data, key=None):
    return emit_event(
        s, company_id=company_id, entity_id=_ITEM, entity_type="item", event_type=event_type,
        data=data, actor_id=None, location_id=None, source="test",
        idempotency_key=key or str(uuid.uuid4()), metadata_={},
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


async def _rebuild(factory, company_id) -> None:
    async with factory() as s:
        await ProjectionEngine.rebuild(s, company_id)
        await s.commit()


async def test_a_refused_change_caught_and_committed_leaves_no_event_and_no_ghost(committed_engine):
    factory = async_sessionmaker(bind=committed_engine, class_=AsyncSession, expire_on_commit=False)
    company_id = await _seed(factory)
    async with factory() as s:
        await erase_items(s, company_id, [_ITEM])
        await s.commit()

    async with factory() as s:
        # A bulk caller: records the failed row and commits everything else it did.
        with pytest.raises(HTTPException) as refused:
            await _emit(s, company_id, "item.file.attached", {
                "entity_id": _ITEM, "entity_type": "item", "file_id": "f1",
                "filename": "a.png", "mime": "image/png", "size": 1,
            })
        assert refused.value.status_code == 404
        await s.commit()

    assert await _stored(committed_engine, company_id) == (None, [])
    await _rebuild(factory, company_id)
    assert await _stored(committed_engine, company_id) == (None, [])


async def test_rebuild_does_not_turn_a_stray_item_event_into_an_item(committed_engine):
    """A ledger written before the refusal existed may hold a change to an item with no
    birth; replaying it must not create an item from the change alone."""
    factory = async_sessionmaker(bind=committed_engine, class_=AsyncSession, expire_on_commit=False)
    company_id = await _seed(factory)
    async with factory() as s:
        await erase_items(s, company_id, [_ITEM])
        s.add(LedgerEntry(
            company_id=company_id, entity_id=_ITEM, entity_type="item", event_type="item.updated",
            data={"fields_changed": {"name": {"old": "Atom", "new": "Ghost"}}},
            actor_id=None, location_id=None, source="test",
            idempotency_key=str(uuid.uuid4()), metadata_={},
        ))
        await s.commit()

    await _rebuild(factory, company_id)

    assert await _stored(committed_engine, company_id) == (None, ["item.updated"])


async def test_a_repeated_key_returns_the_original_and_applies_nothing_twice(committed_engine):
    """The idempotency dedup still returns the original event, keeps the caller's other
    events, and does not apply the repeat to the projection."""
    factory = async_sessionmaker(bind=committed_engine, class_=AsyncSession, expire_on_commit=False)
    company_id = await _seed(factory)
    key = str(uuid.uuid4())
    async with factory() as s:
        first = await _emit(s, company_id, "item.updated", {"fields_changed": {"name": {"old": "Atom", "new": "One"}}}, key)
        await s.commit()

    async with factory() as s:
        sibling = await _emit(s, company_id, "item.updated", {"fields_changed": {"name": {"old": "One", "new": "Two"}}})
        repeat = await _emit(s, company_id, "item.updated", {"fields_changed": {"name": {"old": "Two", "new": "Three"}}}, key)
        await s.commit()

    assert repeat.id == first.id
    assert repeat.was_deduped is True
    assert sibling.id != first.id
    assert await _stored(committed_engine, company_id) == ("Two", ["item.created", "item.updated", "item.updated"])
