# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""The centralized code-allocation service (celerp_inventory.services) is the single
concurrency-safe path for minting internal SKUs/barcodes. Two concurrent creators
must not read the same sequence maximum and mint the same code: lock_item_code_namespace
takes a company-row lock (SELECT ... FOR UPDATE) that serializes every allocator in a
company, so the second waits for the first to commit and then reads the updated maximum.

These use independent sessions bound to the shared engine with real commits (not the
savepoint session): a row lock is only observable across separately committed
transactions, which a single rolled-back savepoint cannot exercise faithfully."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from celerp.events.engine import emit_event
from celerp.inventory_codes import BarcodeConflictError
from celerp.models.company import Company
from celerp.models.ledger import LedgerEntry
from celerp.models.projections import Projection


async def _seed_company(factory) -> uuid.UUID:
    company_id = uuid.uuid4()
    async with factory() as s:
        s.add(Company(id=company_id, name="AllocCo", slug=f"alloc-{company_id.hex[:8]}"))
        await s.commit()
    return company_id


async def _cleanup(factory, company_id):
    async with factory() as s:
        await s.execute(delete(Projection).where(Projection.company_id == company_id))
        await s.execute(delete(LedgerEntry).where(LedgerEntry.company_id == company_id))
        await s.execute(delete(Company).where(Company.id == company_id))
        await s.commit()


def _item_kwargs(company_id, entity_id, sku, barcode):
    return dict(
        company_id=company_id,
        entity_id=entity_id,
        entity_type="item",
        event_type="item.created",
        data={"sku": sku, "name": sku, "quantity": 1, "barcode": barcode},
        actor_id=None,
        location_id=None,
        source="test",
        idempotency_key=str(uuid.uuid4()),
        metadata_={},
    )


@pytest.mark.asyncio
async def test_lock_blocks_concurrent_namespace_access(_db_engine):
    """lock_item_code_namespace serializes: while one transaction holds the company
    lock, a second allocator blocks until the first commits. Without the FOR UPDATE
    lock the second returns immediately and both mint the same next code."""
    from celerp_inventory.services import lock_item_code_namespace

    factory = async_sessionmaker(bind=_db_engine, class_=AsyncSession, expire_on_commit=False)
    company_id = await _seed_company(factory)
    s1, s2 = factory(), factory()
    try:
        await lock_item_code_namespace(s1, company_id)  # s1 now holds the row lock

        second_acquired = asyncio.Event()

        async def _second():
            await lock_item_code_namespace(s2, company_id)
            second_acquired.set()

        task = asyncio.create_task(_second())
        await asyncio.sleep(0.3)
        assert not second_acquired.is_set(), "second allocator did not block on the company lock"

        await s1.commit()  # release the lock
        await asyncio.wait_for(task, timeout=10)
        assert second_acquired.is_set()
        await s2.commit()
    finally:
        await s1.close()
        await s2.close()
        await _cleanup(factory, company_id)


@pytest.mark.asyncio
async def test_concurrent_creates_mint_distinct_barcodes(_db_engine):
    """Two creators allocating and persisting at once get DISTINCT barcodes: the lock
    forces the second to read the first's committed code and mint the next one, so the
    barcode unique index never fires. Without the lock both read the same maximum, mint
    the same code, and the second create is rejected as a BarcodeConflictError."""
    from celerp_inventory.services import allocate_internal_codes

    factory = async_sessionmaker(bind=_db_engine, class_=AsyncSession, expire_on_commit=False)
    company_id = await _seed_company(factory)

    async def _create(tag: str) -> str:
        async with factory() as s:
            barcode = (await allocate_internal_codes(s, company_id))[0]
            await emit_event(s, **_item_kwargs(company_id, f"item:{tag}", f"sku-{tag}", barcode))
            await s.commit()
        return barcode

    try:
        codes = await asyncio.gather(_create("a"), _create("b"))
        assert len(set(codes)) == 2, f"concurrent creates minted the same barcode: {codes}"

        async with factory() as check:
            stored = {
                (await check.get(Projection, {"company_id": company_id, "entity_id": f"item:{t}"})).state["barcode"]
                for t in ("a", "b")
            }
        assert stored == set(codes)
    finally:
        await _cleanup(factory, company_id)


@pytest.mark.asyncio
async def test_allocate_internal_codes_batch_is_distinct_and_sequential(_db_engine):
    """A batch allocation returns count distinct zero-padded codes, and a later
    allocation continues past the persisted maximum rather than repeating them."""
    from celerp_inventory.services import allocate_internal_codes

    factory = async_sessionmaker(bind=_db_engine, class_=AsyncSession, expire_on_commit=False)
    company_id = await _seed_company(factory)
    try:
        async with factory() as s:
            batch = await allocate_internal_codes(s, company_id, count=3)
            assert len(set(batch)) == 3, f"batch repeated a code: {batch}"
            assert all(c.isdigit() and len(c) == 6 for c in batch), batch
            assert [int(c) for c in batch] == [int(batch[0]) + i for i in range(3)]
            # Persist the batch, then the next allocation must not re-mint any of it.
            for i, bc in enumerate(batch):
                await emit_event(s, **_item_kwargs(company_id, f"item:{i}", f"sku-{i}", bc))
            await s.commit()

        async with factory() as s:
            nxt = (await allocate_internal_codes(s, company_id))[0]
            assert nxt not in batch, f"next allocation re-minted a persisted code: {nxt}"
            assert int(nxt) == int(batch[-1]) + 1
    finally:
        await _cleanup(factory, company_id)


@pytest.mark.asyncio
async def test_assert_barcode_available_flags_taken_codes(_db_engine):
    """assert_barcode_available raises BarcodeConflictError for a barcode another item
    already holds and passes for an unused, empty, or absent barcode."""
    from celerp_inventory.services import assert_barcode_available

    factory = async_sessionmaker(bind=_db_engine, class_=AsyncSession, expire_on_commit=False)
    company_id = await _seed_company(factory)
    try:
        async with factory() as s:
            await emit_event(s, **_item_kwargs(company_id, "item:1", "A", "555001"))
            await s.commit()

        async with factory() as s:
            with pytest.raises(BarcodeConflictError):
                await assert_barcode_available(s, company_id, "555001")
            # Unused / empty / absent are all available (no raise).
            await assert_barcode_available(s, company_id, "999999")
            await assert_barcode_available(s, company_id, "")
            await assert_barcode_available(s, company_id, None)
    finally:
        await _cleanup(factory, company_id)
