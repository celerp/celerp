# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Tests for the upsert=True toggle on batch import endpoints."""

from __future__ import annotations

import uuid

import pytest

from celerp.models.accounting import UserCompany
from celerp.models.company import Company, User
from celerp.services.auth import create_access_token


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_user(company_id: uuid.UUID) -> tuple[uuid.UUID, str]:
    user_id = uuid.uuid4()
    token, _ = create_access_token(subject=str(user_id), company_id=str(company_id), role="admin")
    return user_id, token


async def _setup(session) -> tuple[uuid.UUID, uuid.UUID, str]:
    company_id = uuid.uuid4()
    user_id = uuid.uuid4()
    session.add(Company(id=company_id, name="UpsertCo", slug=f"upsertco-{company_id.hex[:8]}"))
    session.add(User(
        id=user_id,
        email=f"admin-{user_id.hex[:8]}@test.co", name="Admin",
        auth_hash="x", is_active=True,
    ))
    session.add(UserCompany(id=uuid.uuid4(), user_id=user_id, company_id=company_id, role="admin", is_active=True))
    await session.commit()
    token, _ = create_access_token(subject=str(user_id), company_id=str(company_id), role="admin")
    return company_id, user_id, token


# ---------------------------------------------------------------------------
# Items upsert
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_items_upsert_false_skips_existing(client, session):
    _, _, token = await _setup(session)
    headers = {"Authorization": f"Bearer {token}"}
    entity_id = f"item:upsert-{uuid.uuid4().hex[:8]}"
    idem = f"csv:item:upsert-test-{uuid.uuid4().hex[:8]}"
    record = {
        "entity_id": entity_id,
        "event_type": "item.created",
        "data": {"sku": f"UPSK-{idem[-6:]}", "name": "Upsert Item", "quantity": 1, "sell_by": "piece"},
        "source": "csv_import",
        "idempotency_key": idem,
    }
    payload = {"records": [record]}

    r1 = await client.post("/items/import/batch", headers=headers, json=payload)
    assert r1.status_code == 200
    assert r1.json()["created"] == 1
    assert r1.json()["skipped"] == 0
    assert r1.json()["updated"] == 0

    # Second call without upsert — should skip
    r2 = await client.post("/items/import/batch", headers=headers, json=payload)
    assert r2.status_code == 200
    assert r2.json()["created"] == 0
    assert r2.json()["skipped"] == 1
    assert r2.json()["updated"] == 0


@pytest.mark.asyncio
async def test_items_upsert_true_emits_patch(client, session):
    _, _, token = await _setup(session)
    headers = {"Authorization": f"Bearer {token}"}
    entity_id = f"item:upsert-{uuid.uuid4().hex[:8]}"
    idem = f"csv:item:upsert-test-{uuid.uuid4().hex[:8]}"
    record = {
        "entity_id": entity_id,
        "event_type": "item.created",
        "data": {"sku": f"UPSK-{idem[-6:]}", "name": "Upsert Item", "quantity": 1, "sell_by": "piece"},
        "source": "csv_import",
        "idempotency_key": idem,
    }
    # First import creates
    r1 = await client.post("/items/import/batch", headers=headers, json={"records": [record]})
    assert r1.json()["created"] == 1

    # Second import with upsert=True — should update
    r2 = await client.post("/items/import/batch", headers=headers, json={"records": [record], "upsert": True})
    assert r2.status_code == 200
    body = r2.json()
    assert body["created"] == 0
    assert body["updated"] == 1
    assert body["skipped"] == 0

    # Third call with upsert=True — should skip (upsert key already exists)
    r3 = await client.post("/items/import/batch", headers=headers, json={"records": [record], "upsert": True})
    assert r3.status_code == 200
    body3 = r3.json()
    assert body3["updated"] == 0
    assert body3["skipped"] == 1


# ---------------------------------------------------------------------------
# Docs upsert
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_docs_upsert_false_skips_existing(client, session):
    _, _, token = await _setup(session)
    headers = {"Authorization": f"Bearer {token}"}
    entity_id = f"doc:upsert-{uuid.uuid4().hex[:8]}"
    idem = f"csv:doc:invoice:upd-{uuid.uuid4().hex[:8]}"
    record = {
        "entity_id": entity_id,
        "event_type": "doc.created",
        "data": {"doc_type": "invoice", "doc_number": "UPD-001", "status": "draft", "total": 0, "line_items": []},
        "source": "csv_import",
        "idempotency_key": idem,
    }
    r1 = await client.post("/docs/import/batch", headers=headers, json={"records": [record]})
    assert r1.json() == {"created": 1, "skipped": 0, "updated": 0, "errors": []}

    r2 = await client.post("/docs/import/batch", headers=headers, json={"records": [record]})
    assert r2.json() == {"created": 0, "skipped": 1, "updated": 0, "errors": []}


@pytest.mark.asyncio
async def test_docs_upsert_true_emits_patch(client, session):
    _, _, token = await _setup(session)
    headers = {"Authorization": f"Bearer {token}"}
    entity_id = f"doc:upsert-{uuid.uuid4().hex[:8]}"
    idem = f"csv:doc:invoice:upd-{uuid.uuid4().hex[:8]}"
    record = {
        "entity_id": entity_id,
        "event_type": "doc.created",
        "data": {"doc_type": "invoice", "doc_number": "UPD-002", "status": "draft", "total": 0, "line_items": []},
        "source": "csv_import",
        "idempotency_key": idem,
    }
    r1 = await client.post("/docs/import/batch", headers=headers, json={"records": [record]})
    assert r1.json()["created"] == 1

    r2 = await client.post("/docs/import/batch", headers=headers, json={"records": [record], "upsert": True})
    assert r2.status_code == 200
    body = r2.json()
    assert body["created"] == 0
    assert body["updated"] == 1
    assert body["skipped"] == 0


# ---------------------------------------------------------------------------
# Lists upsert
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_lists_upsert_false_skips_existing(client, session):
    _, _, token = await _setup(session)
    headers = {"Authorization": f"Bearer {token}"}
    entity_id = f"list:upsert-{uuid.uuid4().hex[:8]}"
    idem = f"csv:list:upsert-{uuid.uuid4().hex[:8]}"
    record = {
        "entity_id": entity_id,
        "event_type": "list.created",
        "data": {"ref_id": "UPL-001", "status": "draft", "total": 0, "line_items": []},
        "source": "csv_import",
        "idempotency_key": idem,
    }
    r1 = await client.post("/lists/import/batch", headers=headers, json={"records": [record]})
    assert r1.json() == {"created": 1, "skipped": 0, "updated": 0, "errors": []}

    r2 = await client.post("/lists/import/batch", headers=headers, json={"records": [record]})
    assert r2.json() == {"created": 0, "skipped": 1, "updated": 0, "errors": []}


@pytest.mark.asyncio
async def test_lists_upsert_true_emits_patch(client, session):
    _, _, token = await _setup(session)
    headers = {"Authorization": f"Bearer {token}"}
    entity_id = f"list:upsert-{uuid.uuid4().hex[:8]}"
    idem = f"csv:list:upsert-{uuid.uuid4().hex[:8]}"
    record = {
        "entity_id": entity_id,
        "event_type": "list.created",
        "data": {"ref_id": "UPL-002", "status": "draft", "total": 0, "line_items": []},
        "source": "csv_import",
        "idempotency_key": idem,
    }
    r1 = await client.post("/lists/import/batch", headers=headers, json={"records": [record]})
    assert r1.json()["created"] == 1

    r2 = await client.post("/lists/import/batch", headers=headers, json={"records": [record], "upsert": True})
    assert r2.status_code == 200
    body = r2.json()
    assert body["created"] == 0
    assert body["updated"] == 1
    assert body["skipped"] == 0


# ---------------------------------------------------------------------------
# Cross-company idempotency scoping (Bug 1)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_same_idempotency_key_allowed_for_different_companies(client, session):
    """Same idempotency_key from two different companies must both succeed (Bug 1 fix)."""
    company_a_id = uuid.uuid4()
    company_b_id = uuid.uuid4()
    user_a_id = uuid.uuid4()
    user_b_id = uuid.uuid4()

    from celerp.models.accounting import UserCompany
    from celerp.models.company import Company, User
    from celerp.services.auth import create_access_token

    for cid, uid, name in [
        (company_a_id, user_a_id, "CompanyA"),
        (company_b_id, user_b_id, "CompanyB"),
    ]:
        session.add(Company(id=cid, name=name, slug=f"co-{cid.hex[:8]}"))
        session.add(User(id=uid, email=f"admin-{uid.hex[:8]}@xco.test", name="Admin", auth_hash="x", is_active=True))
        session.add(UserCompany(id=uuid.uuid4(), user_id=uid, company_id=cid, role="admin", is_active=True))
    await session.commit()

    token_a, _ = create_access_token(subject=str(user_a_id), company_id=str(company_a_id), role="admin")
    token_b, _ = create_access_token(subject=str(user_b_id), company_id=str(company_b_id), role="admin")

    shared_idem = f"csv:item:shared-key-{uuid.uuid4().hex[:8]}"
    record = {
        "entity_id": f"item:{uuid.uuid4().hex}",
        "event_type": "item.created",
        "data": {"sku": f"XSK-{shared_idem[-6:]}", "name": "Cross Co Item", "quantity": 1, "sell_by": "piece"},
        "source": "csv_import",
        "idempotency_key": shared_idem,
    }

    # Company A import
    ra = await client.post("/items/import/batch",
        headers={"Authorization": f"Bearer {token_a}"},
        json={"records": [record]})
    assert ra.status_code == 200, ra.text
    assert ra.json()["created"] == 1

    # Company B import with same key and same entity_id - should ALSO create (different scope)
    record_b = {**record, "entity_id": f"item:{uuid.uuid4().hex}"}
    rb = await client.post("/items/import/batch",
        headers={"Authorization": f"Bearer {token_b}"},
        json={"records": [record_b]})
    assert rb.status_code == 200, rb.text
    assert rb.json()["created"] == 1, f"Expected created=1, got {rb.json()}"


# ---------------------------------------------------------------------------
# Server-side 500-record limit enforcement
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_batch_import_501_records_returns_422(client, session):
    """POST /items/import/batch with 501 records must return 422.

    BatchImportRequest.records has max_length=500. Sending 501 records
    must be rejected by Pydantic validation before any DB work happens.
    """
    company_id, user_id, token = await _setup(session)
    headers = {"Authorization": f"Bearer {token}"}

    records = [
        {
            "entity_id": f"item:{uuid.uuid4().hex}",
            "event_type": "item.created",
            "data": {"sku": f"LIMIT-{i:04d}", "name": f"Item {i}", "quantity": 1, "sell_by": "piece"},
            "source": "csv_import",
            "idempotency_key": f"csv:item:limit-{i:04d}",
        }
        for i in range(501)
    ]
    r = await client.post(
        "/items/import/batch",
        headers=headers,
        json={"records": records},
    )
    assert r.status_code == 422, f"Expected 422, got {r.status_code}: {r.text}"


# ---------------------------------------------------------------------------
# Import field enforcement (Issues 1-4 fixes)
# ---------------------------------------------------------------------------

def _item_record(data: dict, sku_suffix: str | None = None) -> dict:
    """Build a minimal BatchImportRequest record dict."""
    suffix = sku_suffix or uuid.uuid4().hex[:8]
    return {
        "entity_id": f"item:{uuid.uuid4()}",
        "event_type": "item.created",
        "data": data,
        "source": "csv_import",
        "idempotency_key": f"csv:item:field-test-{suffix}",
    }


@pytest.mark.asyncio
async def test_import_missing_sell_by_counted_as_error(client, session):
    """Batch import with no sell_by must return 200 with created=0, errors non-empty.

    post_item() raises 422 when sell_by is absent. The per-record try/except
    must capture it and append to errors — not bubble a 422 from the endpoint.
    created + skipped must equal len(records).
    """
    _, _, token = await _setup(session)
    headers = {"Authorization": f"Bearer {token}"}

    record = _item_record({"name": "No Unit Item", "sku": f"NO-UNIT-{uuid.uuid4().hex[:6]}"})
    r = await client.post("/items/import/batch", headers=headers, json={"records": [record]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["created"] == 0
    assert body["skipped"] == 1
    assert len(body["errors"]) >= 1
    assert body["created"] + body["skipped"] == 1


@pytest.mark.asyncio
async def test_import_invalid_sell_by_counted_as_error(client, session):
    """Batch import with a sell_by value not in company units must surface in errors.

    Company has no custom units so DEFAULT_UNITS apply ("piece", "carat", etc.).
    "pcs" is not a valid unit name — post_item must raise 422.
    Per-record try/except must capture it; endpoint returns 200.
    """
    _, _, token = await _setup(session)
    headers = {"Authorization": f"Bearer {token}"}

    record = _item_record({
        "name": "Bad Unit Item",
        "sku": f"BAD-UNIT-{uuid.uuid4().hex[:6]}",
        "sell_by": "pcs",  # invalid — "piece" is correct
    })
    r = await client.post("/items/import/batch", headers=headers, json={"records": [record]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["created"] == 0
    assert body["skipped"] == 1
    assert len(body["errors"]) >= 1


@pytest.mark.asyncio
async def test_import_status_stripped_always_available(client, session):
    """Batch import with status=memo_out must create item with status=available.

    The backend strips status from rec.data before calling post_item, so the
    item always lands in the default available state regardless of CSV content.
    """
    from celerp.models.projections import Projection
    from sqlalchemy import select as _select

    company_id, _, token = await _setup(session)
    headers = {"Authorization": f"Bearer {token}"}

    record = _item_record({
        "name": "Status Test Item",
        "sku": f"STATUS-{uuid.uuid4().hex[:6]}",
        "sell_by": "piece",
        "status": "memo_out",  # must be stripped
    })
    r = await client.post("/items/import/batch", headers=headers, json={"records": [record]})
    assert r.status_code == 200, r.text
    assert r.json()["created"] == 1

    entity_id = record["entity_id"]
    proj = (await session.execute(
        _select(Projection).where(
            Projection.entity_id == entity_id,
            Projection.company_id == company_id,
        )
    )).scalars().first()
    assert proj is not None
    assert proj.state.get("status") == "available"


@pytest.mark.asyncio
async def test_import_timestamps_stripped_system_generated(client, session):
    """Batch import with user-supplied timestamps must store valid ISO timestamps.

    The backend strips created_at/updated_at from rec.data; post_item backfills
    them via setdefault(key, now_iso). Neither field should be empty or contain
    the user-supplied garbage value.
    """
    from celerp.models.projections import Projection
    from sqlalchemy import select as _select
    from datetime import datetime

    company_id, _, token = await _setup(session)
    headers = {"Authorization": f"Bearer {token}"}

    record = _item_record({
        "name": "Timestamp Test Item",
        "sku": f"TS-{uuid.uuid4().hex[:6]}",
        "sell_by": "piece",
        "created_at": "BANGKOK",      # must be stripped
        "updated_at": "not-a-date",   # must be stripped
    })
    r = await client.post("/items/import/batch", headers=headers, json={"records": [record]})
    assert r.status_code == 200, r.text
    assert r.json()["created"] == 1

    entity_id = record["entity_id"]
    proj = (await session.execute(
        _select(Projection).where(
            Projection.entity_id == entity_id,
            Projection.company_id == company_id,
        )
    )).scalars().first()
    assert proj is not None
    state = proj.state
    for field in ("created_at", "updated_at"):
        value = state.get(field)
        assert value is not None, f"{field} must not be None"
        assert value != "BANGKOK" and value != "not-a-date", f"{field} must not be user value"
        # Must be a parseable ISO timestamp
        datetime.fromisoformat(value.replace("Z", "+00:00"))


@pytest.mark.asyncio
async def test_import_spec_required_fields():
    """_IMPORT_SPEC.required must contain exactly {name, sell_by}.

    location_name and sku are not truly required (auto-resolved / auto-assigned).
    sell_by is required because post_item raises 422 without it.
    """
    from ui.routes.inventory import _IMPORT_SPEC

    assert "location_name" not in _IMPORT_SPEC.required
    assert "sku" not in _IMPORT_SPEC.required
    assert "name" in _IMPORT_SPEC.required
    assert "sell_by" in _IMPORT_SPEC.required


# ---------------------------------------------------------------------------
# Issue 1: ill-formed CSV handling
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_import_malformed_csv_shows_error(client):
    """CSV with extra columns beyond the header (causes None key in DictReader rows)
    must return a clean upload error, not a 500."""
    from httpx import AsyncClient
    from httpx._transports.asgi import ASGITransport
    from ui.app import app as ui_app

    async with AsyncClient(
        transport=ASGITransport(app=ui_app),
        base_url="http://ui",
        follow_redirects=False,
    ) as c:
        # More columns than header → DictReader emits None key for overflow columns
        malformed = b"name,sell_by\nfoo,piece,extra_unexpected_col\n"
        r = await c.post(
            "/inventory/import/preview",
            cookies={"celerp_token": "dummy-token-for-test"},
            files={"csv_file": ("items.csv", malformed, "text/csv")},
        )
    assert r.status_code == 200
    body = r.text
    assert "unexpected error" not in body.lower()
    # Must show a user-friendly CSV error, not fall through to the column mapping step
    assert "more columns than" in body or "valid CSV" in body or "upload" in body.lower()


@pytest.mark.asyncio
async def test_import_none_fieldnames_shows_error(client):
    """CSV that causes DictReader to emit None fieldnames must return a clean error,
    not propagate to a 500."""
    from httpx import AsyncClient
    from httpx._transports.asgi import ASGITransport
    from ui.app import app as ui_app

    async with AsyncClient(
        transport=ASGITransport(app=ui_app),
        base_url="http://ui",
        follow_redirects=False,
    ) as c:
        # A CSV where the header row is empty / blank triggers None fieldnames
        empty_header = b"\n\nname,sell_by\ntest,piece\n"
        r = await c.post(
            "/inventory/import/preview",
            cookies={"celerp_token": "dummy-token-for-test"},
            files={"csv_file": ("items.csv", empty_header, "text/csv")},
        )
    assert r.status_code == 200
    body = r.text
    assert "unexpected error" not in body.lower()
