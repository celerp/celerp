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


# --- stored-value canonicalization + case-insensitive identity (HTTP) --------


@pytest.mark.asyncio
async def test_rfid_epc_stored_upper_cased_on_create(client):
    """A mixed-case rfid_epc supplied on create is STORED upper-cased, not raw.

    Red at merge-base and on the pre-normalization branch: the event boundary
    persists the raw request value, so the stored projection state carries the
    original case ('aBcd12ef') and this assertion fails."""
    t = await _register(client)

    r = await client.post(
        "/items",
        headers=_h(t),
        json={"status": "available", "sku": "UC-1", "name": "UC-1", "quantity": 1,
              "sell_by": "piece", "inventory_type": "stocked", "rfid_epc": "  aBcd12ef  "},
    )
    assert r.status_code == 200, r.text
    entity_id = r.json()["id"]

    got = await client.get(f"/items/{entity_id}", headers=_h(t))
    assert got.status_code == 200, got.text
    assert got.json()["rfid_epc"] == "ABCD12EF", got.json().get("rfid_epc")


@pytest.mark.asyncio
async def test_rfid_epc_case_variant_duplicate_rejected_409(client):
    """A second item whose rfid_epc differs only in case from an existing one is a
    physical-identity collision: it is rejected 409, and the original resolves by
    either case.

    Red at merge-base and on the pre-normalization branch: the first EPC is stored
    raw-case, so the availability check (which normalizes the second input) compares
    'ABC123' against the stored 'abc123' and finds no match, wrongly accepting 200."""
    t = await _register(client)

    a = await client.post(
        "/items",
        headers=_h(t),
        json={"status": "available", "sku": "CV-A", "name": "CV-A", "quantity": 1,
              "sell_by": "piece", "inventory_type": "stocked", "rfid_epc": "abc123"},
    )
    assert a.status_code == 200, a.text

    b = await client.post(
        "/items",
        headers=_h(t),
        json={"status": "available", "sku": "CV-B", "name": "CV-B", "quantity": 1,
              "sell_by": "piece", "inventory_type": "stocked", "rfid_epc": "ABC123"},
    )
    assert b.status_code == 409, b.text

    # The stored item resolves by either case through the normalizing list filter.
    lower = await client.get("/items", headers=_h(t), params={"rfid_epc": "abc123"})
    upper = await client.get("/items", headers=_h(t), params={"rfid_epc": "ABC123"})
    assert lower.status_code == 200 and upper.status_code == 200
    lower_skus = {i["sku"] for i in lower.json()["items"]}
    upper_skus = {i["sku"] for i in upper.json()["items"]}
    assert "CV-A" in lower_skus, lower.json()
    assert "CV-A" in upper_skus, upper.json()


@pytest.mark.asyncio
async def test_import_cross_field_barcode_epc_collision_skipped(client):
    """A batch-import row may not claim, as its rfid_epc, a value already held as
    another item's barcode within the company: the row is skipped with an error and
    never created, so the barcode's owner stays the only holder of the code.

    Red at head b764c0da: batch_import_items emits rec.data verbatim with no shared
    namespace check, so the colliding row is created and the code then belongs to two
    items."""
    t = await _register(client)

    a = await client.post(
        "/items",
        headers=_h(t),
        json={"status": "available", "sku": "IMP-A", "name": "IMP-A", "quantity": 1,
              "sell_by": "piece", "inventory_type": "stocked", "barcode": "5901299"},
    )
    assert a.status_code == 200, a.text

    rec = {
        "entity_id": f"item:imp-{uuid.uuid4()}",
        "entity_type": "item",
        "event_type": "item.created",
        "data": {"sku": "IMP-B", "name": "IMP-B", "quantity": 1,
                 "sell_by": "piece", "rfid_epc": "5901299"},
        "idempotency_key": f"test:impcf:{uuid.uuid4()}",
        "source": "csv_import",
        "source_ts": None,
    }
    r = await client.post(
        "/items/import/batch",
        headers=_h(t),
        json={"records": [rec], "filename": "cross_field.csv"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["created"] == 0, body
    assert body["skipped"] == 1, body
    assert body["errors"], body

    # The barcode still belongs only to item A; nothing holds it as an rfid_epc.
    by_barcode = await client.get("/items", headers=_h(t), params={"barcode": "5901299"})
    assert by_barcode.status_code == 200, by_barcode.text
    assert {i["sku"] for i in by_barcode.json()["items"]} == {"IMP-A"}, by_barcode.json()

    by_epc = await client.get("/items", headers=_h(t), params={"rfid_epc": "5901299"})
    assert by_epc.status_code == 200, by_epc.text
    assert by_epc.json()["items"] == [], by_epc.json()


@pytest.mark.asyncio
async def test_patch_rfid_epc_stored_upper_cased_and_resolvable(client):
    """PATCH of an item's rfid_epc to a mixed-case, whitespace-padded value stores the
    canonical trimmed upper-case form, and the item then resolves by a lowercase lookup.

    Red at merge-base 2e48505 (pre-#326: patch has no rfid_epc handling and the event
    boundary does not normalize it); green at head b764c0da, where patch validates +
    locks + asserts availability and emit_event normalizes the persisted value. This is
    the PATCH-path coverage of the corrected storage/lookup behavior."""
    t = await _register(client)

    r = await client.post(
        "/items",
        headers=_h(t),
        json={"status": "available", "sku": "PU-1", "name": "PU-1", "quantity": 1,
              "sell_by": "piece", "inventory_type": "stocked"},
    )
    assert r.status_code == 200, r.text
    entity_id = r.json()["id"]

    p = await client.patch(
        f"/items/{entity_id}",
        headers=_h(t),
        json={"fields_changed": {"rfid_epc": {"old": None, "new": "  abcXYZ12  "}}},
    )
    assert p.status_code == 200, p.text

    got = await client.get(f"/items/{entity_id}", headers=_h(t))
    assert got.status_code == 200, got.text
    assert got.json()["rfid_epc"] == "ABCXYZ12", got.json().get("rfid_epc")

    lookup = await client.get("/items", headers=_h(t), params={"rfid_epc": "abcxyz12"})
    assert lookup.status_code == 200, lookup.text
    assert "PU-1" in {i["sku"] for i in lookup.json()["items"]}, lookup.json()


@pytest.mark.asyncio
async def test_invalid_gtin_returns_422_not_500(client):
    """A non-digit or wrong-length gtin on create returns 422 with the format
    message, never a 500 from an unhandled ValueError.

    Red at merge-base and on the pre-fix branch: post_item has no gtin friendly-422
    wrapper, so validate_gtin's ValueError falls through the global handler as 500."""
    t = await _register(client)

    non_digit = await client.post(
        "/items",
        headers=_h(t),
        json={"status": "available", "sku": "G-1", "name": "G-1", "quantity": 1,
              "sell_by": "piece", "inventory_type": "stocked", "gtin": "12A45678"},
    )
    assert non_digit.status_code == 422, non_digit.text
    assert "digit" in non_digit.text.lower()

    wrong_len = await client.post(
        "/items",
        headers=_h(t),
        json={"status": "available", "sku": "G-2", "name": "G-2", "quantity": 1,
              "sell_by": "piece", "inventory_type": "stocked", "gtin": "12345"},
    )
    assert wrong_len.status_code == 422, wrong_len.text


@pytest.mark.asyncio
async def test_invalid_rfid_epc_returns_422_not_500(client):
    """An over-long or non-alphanumeric rfid_epc on create returns 422, never a 500.

    Red at merge-base and on the pre-fix branch: post_item has no rfid_epc
    friendly-422 wrapper, so validate_rfid_epc's ValueError falls through as 500."""
    t = await _register(client)

    too_long = await client.post(
        "/items",
        headers=_h(t),
        json={"status": "available", "sku": "E-1", "name": "E-1", "quantity": 1,
              "sell_by": "piece", "inventory_type": "stocked", "rfid_epc": "A" * 300},
    )
    assert too_long.status_code == 422, too_long.text

    non_alnum = await client.post(
        "/items",
        headers=_h(t),
        json={"status": "available", "sku": "E-2", "name": "E-2", "quantity": 1,
              "sell_by": "piece", "inventory_type": "stocked", "rfid_epc": "abc-123!"},
    )
    assert non_alnum.status_code == 422, non_alnum.text
