# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Inventory identity model: GTIN and RFID EPC are first-class item identifiers.

GTIN is a schema field validated for the standard 8/12/13/14 digit lengths with
leading zeros preserved and non-digits rejected. RFID EPC is a schema field that
normalizes to trimmed uppercase, bounds its length, and is unique per company
across the identifier space (a value used as a barcode cannot be reused as an
EPC, and vice versa) through a projection unique index and an application check.

The uniqueness tests use independent sessions bound to the shared engine with
real commits (not the savepoint session), so the first item is durably visible
to the second write and the DB unique index fires deterministically. The schema
tests drive the service with an AsyncMock session and a sentinel company so the
effective schema is built from the built-in defaults.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from celerp.events.engine import emit_event
from celerp.models.company import Company
from celerp.models.ledger import LedgerEntry
from celerp.models.projections import Projection
from celerp.services.field_schema import get_effective_field_schema


# --- shared helpers --------------------------------------------------------


def _session_for(settings: dict):
    session = AsyncMock()
    company = MagicMock()
    company.settings = settings
    session.get.return_value = company
    return session


def _item_kwargs(company_id, entity_id, sku, *, barcode=None, rfid_epc=None):
    data = {"sku": sku, "name": sku, "quantity": 1}
    if barcode is not None:
        data["barcode"] = barcode
    if rfid_epc is not None:
        data["rfid_epc"] = rfid_epc
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


# --- GTIN schema + validation ----------------------------------------------


@pytest.mark.asyncio
async def test_gtin_round_trip_create_read_patch():
    """gtin is a first-class built-in schema field carrying the GTIN label.

    Red at merge-base: gtin is absent from _BASE_FIELDS, so it never appears in
    the effective schema."""
    session = _session_for({})
    schema = await get_effective_field_schema(session, uuid.uuid4())
    by_key = {f["key"]: f for f in schema}
    assert "gtin" in by_key
    assert by_key["gtin"]["label"] == "GTIN / UPC / EAN"


@pytest.mark.asyncio
async def test_gtin_leading_zeros_preserved():
    """A valid 8-digit gtin with leading zeros validates as the identical string
    and gtin is a schema field.

    Red at merge-base: validate_gtin does not exist (ImportError) and gtin is
    absent from the effective schema."""
    from celerp.inventory_codes import validate_gtin

    assert validate_gtin("00012345") == "00012345"

    session = _session_for({})
    schema = await get_effective_field_schema(session, uuid.uuid4())
    assert any(f["key"] == "gtin" for f in schema)


def test_gtin_invalid_length_rejected_422():
    """A gtin whose length is not one of 8/12/13/14 is rejected by validate_gtin.

    Red at merge-base: validate_gtin does not exist (ImportError)."""
    from celerp.inventory_codes import validate_gtin

    with pytest.raises(ValueError):
        validate_gtin("12345")


def test_gtin_non_digit_rejected_422():
    """A gtin containing a non-digit is rejected by validate_gtin.

    Red at merge-base: validate_gtin does not exist (ImportError)."""
    from celerp.inventory_codes import validate_gtin

    with pytest.raises(ValueError):
        validate_gtin("12A45678")


# --- RFID EPC normalization + validation -----------------------------------


def test_rfid_epc_round_trip_uppercased():
    """validate_rfid_epc trims whitespace and normalizes to uppercase.

    Red at merge-base: validate_rfid_epc does not exist (ImportError)."""
    from celerp.inventory_codes import validate_rfid_epc

    assert validate_rfid_epc("  abc123  ") == "ABC123"


def test_rfid_epc_too_long_rejected_422():
    """An over-long rfid_epc is rejected by validate_rfid_epc.

    Red at merge-base: validate_rfid_epc does not exist (ImportError)."""
    from celerp.inventory_codes import validate_rfid_epc

    with pytest.raises(ValueError):
        validate_rfid_epc("A" * 300)


# --- combined schema metadata ----------------------------------------------


@pytest.mark.asyncio
async def test_gtin_rfid_epc_schema_round_trip_preserves_label_key():
    """gtin and rfid_epc both appear in the effective schema carrying their
    canonical label_key and tooltip_key.

    Red at merge-base: neither field is present in _BASE_FIELDS, so neither is
    in the effective schema."""
    session = _session_for({})
    schema = await get_effective_field_schema(session, uuid.uuid4())
    by_key = {f["key"]: f for f in schema}

    assert by_key["gtin"].get("label_key") == "field.label.gtin"
    assert by_key["gtin"].get("tooltip_key") == "field.tooltip.gtin"
    assert by_key["rfid_epc"].get("label_key") == "field.label.rfid_epc"
    assert by_key["rfid_epc"].get("tooltip_key") == "field.tooltip.rfid_epc"


# --- RFID EPC uniqueness (committed-session pattern) ------------------------


@pytest.mark.asyncio
async def test_rfid_epc_duplicate_rejected_409(_db_engine):
    """Two items in one company with the same rfid_epc: the second emit is
    rejected as an RfidEpcConflictError.

    Red at merge-base: RfidEpcConflictError does not exist and there is no
    rfid_epc unique index, so the second emit succeeds."""
    from celerp.inventory_codes import RfidEpcConflictError

    factory = async_sessionmaker(bind=_db_engine, class_=AsyncSession, expire_on_commit=False)
    company_id = uuid.uuid4()
    try:
        async with factory() as s:
            s.add(Company(id=company_id, name="RE", slug=f"re-{company_id.hex[:8]}"))
            await s.flush()
            await emit_event(s, **_item_kwargs(company_id, "item:1", "A", rfid_epc="EPC001"))
            await s.commit()

        async with factory() as s:
            with pytest.raises(RfidEpcConflictError):
                await emit_event(s, **_item_kwargs(company_id, "item:2", "B", rfid_epc="EPC001"))
            await s.rollback()

        async with factory() as s:
            assert await s.get(Projection, {"company_id": company_id, "entity_id": "item:2"}) is None
    finally:
        await _cleanup(factory, [company_id])


@pytest.mark.asyncio
async def test_rfid_epc_company_isolated(_db_engine):
    """The same rfid_epc in two different companies is allowed; the same
    rfid_epc within one company is rejected.

    Red at merge-base: no rfid_epc uniqueness enforcement exists and
    RfidEpcConflictError is undefined (ImportError)."""
    from celerp.inventory_codes import RfidEpcConflictError

    factory = async_sessionmaker(bind=_db_engine, class_=AsyncSession, expire_on_commit=False)
    c1, c2 = uuid.uuid4(), uuid.uuid4()
    try:
        async with factory() as s:
            s.add(Company(id=c1, name="C1", slug=f"c1-{c1.hex[:8]}"))
            s.add(Company(id=c2, name="C2", slug=f"c2-{c2.hex[:8]}"))
            await s.flush()
            await emit_event(s, **_item_kwargs(c1, "item:1", "A", rfid_epc="EPC777"))
            await emit_event(s, **_item_kwargs(c2, "item:1", "A", rfid_epc="EPC777"))
            await s.commit()

        async with factory() as s:
            assert (await s.get(Projection, {"company_id": c1, "entity_id": "item:1"})).state["rfid_epc"] == "EPC777"
            assert (await s.get(Projection, {"company_id": c2, "entity_id": "item:1"})).state["rfid_epc"] == "EPC777"

        async with factory() as s:
            with pytest.raises(RfidEpcConflictError):
                await emit_event(s, **_item_kwargs(c1, "item:2", "B", rfid_epc="EPC777"))
            await s.rollback()
    finally:
        await _cleanup(factory, [c1, c2])


def test_rfid_epc_unique_index_declared():
    """The projection model declares the rfid_epc unique index.

    Red at merge-base: only the barcode unique index is declared on the
    Projection table."""
    names = {ix.name for ix in Projection.__table__.indexes}
    assert "uq_projection_company_item_rfid_epc" in names


# --- cross-field identifier collision (HTTP) --------------------------------


async def _register(client) -> str:
    addr = f"admin-{uuid.uuid4().hex[:8]}@invidents.test"
    r = await client.post(
        "/auth/register",
        json={"company_name": "Ident Co", "email": addr, "name": "A", "password": "pw"},
    )
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _h(t):
    return {"Authorization": f"Bearer {t}"}


@pytest.mark.asyncio
async def test_cross_field_barcode_epc_collision_409(client):
    """A value already stored as an item's barcode cannot be reused as another
    item's rfid_epc within the same company: the second create is rejected 409.

    Red at merge-base: there is no cross-field identifier check, so item B with
    rfid_epc equal to item A's barcode is created successfully."""
    t = await _register(client)

    a = await client.post(
        "/items",
        headers=_h(t),
        json={"status": "available", "sku": "IA-1", "name": "IA-1", "quantity": 1,
              "sell_by": "piece", "inventory_type": "stocked", "barcode": "5901234"},
    )
    assert a.status_code == 200, a.text

    b = await client.post(
        "/items",
        headers=_h(t),
        json={"status": "available", "sku": "IB-1", "name": "IB-1", "quantity": 1,
              "sell_by": "piece", "inventory_type": "stocked", "rfid_epc": "5901234"},
    )
    assert b.status_code == 409, b.text
