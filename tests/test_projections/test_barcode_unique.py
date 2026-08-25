# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""The projection barcode unique index is the final defense: a write that reaches the
applier with a barcode already used by another item in the company is rejected as a
BarcodeConflictError (not swallowed as a projection primary-key race).

These use independent sessions bound to the shared engine with real commits (not the
savepoint session), so the first item is durably visible to the second write and the
DB unique index fires deterministically - the constraint is a property of committed
rows, which a single rolled-back savepoint transaction cannot exercise faithfully."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from celerp.events.engine import emit_event
from celerp.inventory_codes import BarcodeConflictError
from celerp.models.company import Company
from celerp.models.ledger import LedgerEntry
from celerp.models.projections import Projection


def _item_kwargs(company_id, entity_id, sku, barcode=None):
    data = {"sku": sku, "name": sku, "quantity": 1}
    if barcode is not None:
        data["barcode"] = barcode
    return dict(
        company_id=company_id,
        entity_id=entity_id,
        entity_type="item",
        event_type="item.created",
        data=data,
        actor_id=None,
        location_id=None,
        source="test",
        idempotency_key=str(uuid.uuid4()),
        metadata_={},
    )


async def _cleanup(factory, company_ids):
    async with factory() as s:
        for cid in company_ids:
            await s.execute(delete(Projection).where(Projection.company_id == cid))
            await s.execute(delete(LedgerEntry).where(LedgerEntry.company_id == cid))
            await s.execute(delete(Company).where(Company.id == cid))
        await s.commit()


@pytest.mark.asyncio
async def test_duplicate_barcode_in_company_raises_conflict(_db_engine):
    factory = async_sessionmaker(bind=_db_engine, class_=AsyncSession, expire_on_commit=False)
    company_id = uuid.uuid4()
    try:
        async with factory() as s:
            s.add(Company(id=company_id, name="BC", slug=f"bc-{company_id.hex[:8]}"))
            await s.flush()
            await emit_event(s, **_item_kwargs(company_id, "item:1", "A", "12345"))
            await s.commit()

        async with factory() as s:
            with pytest.raises(BarcodeConflictError):
                await emit_event(s, **_item_kwargs(company_id, "item:2", "B", "12345"))
            await s.rollback()

        async with factory() as s:
            # The conflicting projection was never created.
            assert await s.get(Projection, {"company_id": company_id, "entity_id": "item:2"}) is None
    finally:
        await _cleanup(factory, [company_id])


@pytest.mark.asyncio
async def test_same_barcode_across_companies_is_allowed(_db_engine):
    factory = async_sessionmaker(bind=_db_engine, class_=AsyncSession, expire_on_commit=False)
    c1, c2 = uuid.uuid4(), uuid.uuid4()
    try:
        async with factory() as s:
            s.add(Company(id=c1, name="C1", slug=f"c1-{c1.hex[:8]}"))
            s.add(Company(id=c2, name="C2", slug=f"c2-{c2.hex[:8]}"))
            await s.flush()
            await emit_event(s, **_item_kwargs(c1, "item:1", "A", "12345"))
            await emit_event(s, **_item_kwargs(c2, "item:1", "A", "12345"))
            await s.commit()

        async with factory() as s:
            assert (await s.get(Projection, {"company_id": c1, "entity_id": "item:1"})).state["barcode"] == "12345"
            assert (await s.get(Projection, {"company_id": c2, "entity_id": "item:1"})).state["barcode"] == "12345"
    finally:
        await _cleanup(factory, [c1, c2])


@pytest.mark.asyncio
async def test_empty_and_absent_barcodes_do_not_collide(_db_engine):
    factory = async_sessionmaker(bind=_db_engine, class_=AsyncSession, expire_on_commit=False)
    company_id = uuid.uuid4()
    try:
        async with factory() as s:
            s.add(Company(id=company_id, name="EB", slug=f"eb-{company_id.hex[:8]}"))
            await s.flush()
            await emit_event(s, **_item_kwargs(company_id, "item:1", "A", ""))
            await emit_event(s, **_item_kwargs(company_id, "item:2", "B", ""))
            await emit_event(s, **_item_kwargs(company_id, "item:3", "C"))
            await s.commit()

        async with factory() as s:
            for eid in ("item:1", "item:2", "item:3"):
                assert await s.get(Projection, {"company_id": company_id, "entity_id": eid}) is not None
    finally:
        await _cleanup(factory, [company_id])
