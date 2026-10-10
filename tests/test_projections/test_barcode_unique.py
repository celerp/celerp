# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Physical-code uniqueness at the event boundary: a write that INTRODUCES a barcode or
RFID / EPC another item in the company already holds is rejected as a conflict, while
a duplicate already present in the data never blocks unrelated edits and can be
resolved by moving one holder to a fresh code. Imports and connectors record the
source system's codes as given; the resolver then reports the duplicate.

These use independent sessions bound to the shared engine with real commits (not the
savepoint session), so the first item is durably visible to the second write."""

from __future__ import annotations

import functools
import uuid

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from celerp.events.engine import connector_upsert, emit_event
from celerp.inventory_codes import BarcodeConflictError, RfidEpcConflictError
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


def _update_kwargs(company_id, entity_id, barcode=None, *, field="barcode", value=None):
    return dict(
        company_id=company_id,
        entity_id=entity_id,
        entity_type="item",
        event_type="item.updated",
        data={"fields_changed": {field: {"new": barcode if value is None else value}}},
        actor_id=None,
        location_id=None,
        source="test",
        idempotency_key=str(uuid.uuid4()),
        metadata_={},
    )


@pytest.mark.asyncio
async def test_update_to_taken_barcode_raises_conflict(_db_engine):
    """Changing an existing item's barcode to one another item already holds is
    rejected as a BarcodeConflictError from the UPDATE applier, not swallowed as a
    500 at the outer commit. The check covers updates, not only the first insert."""
    factory = async_sessionmaker(bind=_db_engine, class_=AsyncSession, expire_on_commit=False)
    company_id = uuid.uuid4()
    try:
        async with factory() as s:
            s.add(Company(id=company_id, name="BU", slug=f"bu-{company_id.hex[:8]}"))
            await s.flush()
            await emit_event(s, **_item_kwargs(company_id, "item:1", "A", "12345"))
            await emit_event(s, **_item_kwargs(company_id, "item:2", "B", "67890"))
            await s.commit()

        async with factory() as s:
            with pytest.raises(BarcodeConflictError):
                await emit_event(s, **_update_kwargs(company_id, "item:2", "12345"))
            await s.rollback()

        async with factory() as s:
            # item:2 kept its original barcode; the conflicting update never landed.
            assert (await s.get(Projection, {"company_id": company_id, "entity_id": "item:2"})).state["barcode"] == "67890"
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


async def _company(factory, name):
    cid = uuid.uuid4()
    async with factory() as s:
        s.add(Company(id=cid, name=name, slug=f"{name.lower()}-{cid.hex[:8]}"))
        await s.commit()
    return cid


async def _resolve(factory, company_id, code):
    from celerp_inventory.routes import resolve_item_by_code

    async with factory() as s:
        return await resolve_item_by_code(s, company_id, code)


@pytest.mark.asyncio
async def test_barcode_and_rfid_share_one_namespace(_db_engine):
    """A value held as one item's barcode cannot become another item's RFID / EPC, and
    the reverse, on create or update."""
    factory = async_sessionmaker(bind=_db_engine, class_=AsyncSession, expire_on_commit=False)
    company_id = await _company(factory, "XF")
    try:
        async with factory() as s:
            await emit_event(s, **_item_kwargs(company_id, "item:1", "A", "30001"))
            await emit_event(s, **_item_kwargs(company_id, "item:2", "B", "67890"))
            await s.commit()

        async with factory() as s:
            with pytest.raises(RfidEpcConflictError):
                await emit_event(s, **_update_kwargs(company_id, "item:2", field="rfid_epc", value="30001"))
            await s.rollback()

        async with factory() as s:
            kwargs = _item_kwargs(company_id, "item:3", "C")
            kwargs["data"]["rfid_epc"] = "67890"
            with pytest.raises(RfidEpcConflictError):
                await emit_event(s, **kwargs)
            await s.rollback()
    finally:
        await _cleanup(factory, [company_id])


@pytest.mark.asyncio
async def test_existing_duplicate_is_grandfathered_and_can_be_resolved(_db_engine):
    """Two items that already share a barcode (older data, an import, a connector) stay
    editable: an unrelated edit to either succeeds, moving one to a fresh code succeeds
    and heals the lookup, and a third item still cannot claim the shared code."""
    factory = async_sessionmaker(bind=_db_engine, class_=AsyncSession, expire_on_commit=False)
    company_id = await _company(factory, "GF")
    try:
        async with factory() as s:
            await emit_event(s, **_item_kwargs(company_id, "item:a", "A", "7508"))
            await emit_event(
                s, preserve_external_code_conflicts=True,
                **_item_kwargs(company_id, "item:b", "B", "7508"),
            )
            await emit_event(s, **_item_kwargs(company_id, "item:c", "C", "7509"))
            await s.commit()
        assert (await _resolve(factory, company_id, "7508")).duplicate_physical

        async with factory() as s:
            await emit_event(s, **_update_kwargs(company_id, "item:a", field="name", value="Renamed"))
            await s.commit()

        async with factory() as s:
            await emit_event(s, **_update_kwargs(company_id, "item:a", "7510"))
            await s.commit()

        async with factory() as s:
            with pytest.raises(BarcodeConflictError):
                await emit_event(s, **_update_kwargs(company_id, "item:c", "7508"))
            await s.rollback()

        assert (await _resolve(factory, company_id, "7508")).one.entity_id == "item:b"
        assert (await _resolve(factory, company_id, "7510")).one.entity_id == "item:a"
    finally:
        await _cleanup(factory, [company_id])


@pytest.mark.asyncio
async def test_connector_records_duplicate_barcodes_and_update_heals_lookup(_db_engine):
    """A connector sync whose platform holds the same barcode on two products records
    both; the resolver reports the duplicate, and a later sync that changes one
    product's barcode resolves it."""
    factory = async_sessionmaker(bind=_db_engine, class_=AsyncSession, expire_on_commit=False)
    company_id = await _company(factory, "CN")

    from celerp_inventory.services import update_item_from_connector

    async def _sync(idem_key, sku, barcode):
        async with factory() as s:
            outcome = await connector_upsert(
                s, company_id=company_id, entity_type="item", event_type="item.created",
                idem_key=idem_key, data={"sku": sku, "name": sku, "quantity": 1, "barcode": barcode},
                update=functools.partial(update_item_from_connector, company_id=company_id),
            )
            await s.commit()
        return outcome

    try:
        assert await _sync("p1", "A", "7508") == "created"
        assert await _sync("p2", "B", "7508") == "created"
        res = await _resolve(factory, company_id, "7508")
        assert res.duplicate_physical and len(res.matches) == 2

        assert await _sync("p1", "A", "7510") == "updated"
        assert (await _resolve(factory, company_id, "7508")).one.entity_id == "item:p2"
        assert (await _resolve(factory, company_id, "7510")).one.entity_id == "item:p1"
    finally:
        await _cleanup(factory, [company_id])


def _status_kwargs(company_id, entity_id, new_status):
    return dict(
        company_id=company_id,
        entity_id=entity_id,
        entity_type="item",
        event_type="item.status.set",
        data={"new_status": new_status},
        actor_id=None,
        location_id=None,
        source="test",
        idempotency_key=str(uuid.uuid4()),
        metadata_={},
    )


@pytest.mark.asyncio
async def test_reactivating_merged_item_checks_its_codes(_db_engine):
    """A merged item does not resolve by its codes, so returning it to a live status
    introduces them again: rejected while another live item holds the code, allowed
    once the code is free. Moving a live holder to merged is always allowed."""
    factory = async_sessionmaker(bind=_db_engine, class_=AsyncSession, expire_on_commit=False)
    company_id = await _company(factory, "RM")
    try:
        async with factory() as s:
            await emit_event(s, **_item_kwargs(company_id, "item:a", "A", "7508"))
            await emit_event(
                s, **_item_kwargs(company_id, "item:b", "B", "7508"),
                preserve_external_code_conflicts=True,
            )
            await emit_event(s, **_status_kwargs(company_id, "item:b", "merged"))
            await s.commit()
        assert (await _resolve(factory, company_id, "7508")).one.entity_id == "item:a"

        async with factory() as s:
            with pytest.raises(BarcodeConflictError):
                await emit_event(s, **_status_kwargs(company_id, "item:b", "available"))
            await s.rollback()
        assert (await _resolve(factory, company_id, "7508")).one.entity_id == "item:a"

        async with factory() as s:
            await emit_event(s, **_status_kwargs(company_id, "item:a", "merged"))
            await s.commit()
        async with factory() as s:
            # Two merged holders: B's code is still held by A's history, so it stays
            # rejected rather than re-issued.
            with pytest.raises(BarcodeConflictError):
                await emit_event(s, **_status_kwargs(company_id, "item:b", "available"))
            await s.rollback()
        async with factory() as s:
            await emit_event(s, **_update_kwargs(company_id, "item:a", "7599"))
            await emit_event(s, **_status_kwargs(company_id, "item:b", "available"))
            await s.commit()
        assert (await _resolve(factory, company_id, "7508")).one.entity_id == "item:b"
    finally:
        await _cleanup(factory, [company_id])


@pytest.mark.asyncio
async def test_boundary_rereads_item_changed_after_it_was_loaded(_db_engine):
    """The availability check reads the committed item under the namespace lock, not a
    copy the writing session loaded before another writer changed it. Otherwise a code
    the item no longer holds looks unchanged and is written back as a duplicate."""
    factory = async_sessionmaker(bind=_db_engine, class_=AsyncSession, expire_on_commit=False)
    company_id = await _company(factory, "SR")
    try:
        async with factory() as s:
            await emit_event(s, **_item_kwargs(company_id, "item:a", "A", "100"))
            await s.commit()
        async with factory() as stale:
            cached = await stale.get(Projection, (company_id, "item:a"))
            assert cached.state["barcode"] == "100"
            async with factory() as other:
                await emit_event(other, **_update_kwargs(company_id, "item:a", "200"))
                await emit_event(other, **_item_kwargs(company_id, "item:c", "C", "100"))
                await other.commit()
            with pytest.raises(BarcodeConflictError):
                await emit_event(stale, **_update_kwargs(company_id, "item:a", "100"))
            await stale.rollback()
        assert (await _resolve(factory, company_id, "100")).one.entity_id == "item:c"
    finally:
        await _cleanup(factory, [company_id])


def test_status_change_on_live_item_does_not_take_code_namespace():
    """Only a merged item can gain resolvable codes from a status event. A live item's
    status change leaves the namespace lock alone, so callers that already hold item row
    locks never take the company lock after them."""
    from celerp.events.engine import _touches_physical_codes

    live = {"status": "available", "barcode": "123"}
    assert not _touches_physical_codes(live, "item.status.set", {"new_status": "sold"})
    assert not _touches_physical_codes(live, "item.status.set", {"new_status": "merged"})
    merged = {"status": "merged", "barcode": "123"}
    assert _touches_physical_codes(merged, "item.status.set", {"new_status": "available"})
    assert _touches_physical_codes(live, "item.updated", {"fields_changed": {"barcode": "456"}})
