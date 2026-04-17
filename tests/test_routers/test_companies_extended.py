# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""
Extended coverage for celerp/routers/companies.py.

Targets: locations PATCH, BOM CRUD, taxes batch import, payment terms batch import,
         error paths (invalid UUID, 404, 409, invalid rate/days, missing name).
"""
from __future__ import annotations

import os
import uuid as _uuid

os.environ.setdefault("ALLOW_INSECURE_JWT", "true")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import pytest


# ── helper ────────────────────────────────────────────────────────────────────

async def _register(client, suffix: str) -> dict:
    r = await client.post(
        "/auth/register",
        json={
            "company_name": f"Co {suffix}",
            "email": f"admin_{suffix}@test.com",
            "name": "Admin",
            "password": "pw123",
        },
    )
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _tax_record(name: str, rate, tax_type: str = "both", idem: str | None = None) -> dict:
    return {
        "entity_id": "company",
        "event_type": "tax.import",
        "source": "import",
        "idempotency_key": idem or f"tax-{name.lower().replace(' ', '-')}",
        "data": {"name": name, "rate": rate, "tax_type": tax_type},
    }


def _pt_record(name: str, days, idem: str | None = None) -> dict:
    return {
        "entity_id": "company",
        "event_type": "pt.import",
        "source": "import",
        "idempotency_key": idem or f"pt-{name.lower().replace(' ', '-')}",
        "data": {"name": name, "days": days},
    }


# ── Locations ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_location_patch(client):
    headers = await _register(client, "loc_patch")

    # Create
    r = await client.post(
        "/companies/me/locations",
        json={"name": "Warehouse A", "type": "warehouse", "is_default": True},
        headers=headers,
    )
    assert r.status_code == 200
    loc_id = r.json()["id"]

    # Patch name
    r = await client.patch(
        f"/companies/me/locations/{loc_id}",
        json={"name": "Warehouse B"},
        headers=headers,
    )
    assert r.status_code == 200
    assert r.json()["name"] == "Warehouse B"

    # Verify updated
    r = await client.get("/companies/me/locations", headers=headers)
    names = [loc["name"] for loc in r.json()["items"]]
    assert "Warehouse B" in names


@pytest.mark.asyncio
async def test_location_patch_invalid_uuid(client):
    headers = await _register(client, "loc_bad_uuid")
    r = await client.patch(
        "/companies/me/locations/not-a-uuid",
        json={"name": "X"},
        headers=headers,
    )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_location_patch_not_found(client):
    headers = await _register(client, "loc_404")
    r = await client.patch(
        f"/companies/me/locations/{_uuid.uuid4()}",
        json={"name": "X"},
        headers=headers,
    )
    assert r.status_code == 404


# ── BOM CRUD ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_bom_full_crud(client):
    headers = await _register(client, "bom_crud")

    # Empty list
    r = await client.get("/companies/me/boms", headers=headers)
    assert r.status_code == 200
    assert r.json()["total"] == 0

    # Create
    bom_payload = {
        "bom_id": "bom-001",
        "name": "Gold Ring BOM",
        "output_item": "ring-gold",
        "inputs": [{"item_id": "gold-bar", "qty": 5}],
        "outputs": [{"item_id": "ring-gold", "qty": 1}],
        "is_active": True,
    }
    r = await client.post("/companies/me/boms", json=bom_payload, headers=headers)
    assert r.status_code == 200
    assert r.json()["bom_id"] == "bom-001"

    # Get by id
    r = await client.get("/companies/me/boms/bom-001", headers=headers)
    assert r.status_code == 200
    assert r.json()["name"] == "Gold Ring BOM"

    # Patch
    r = await client.patch(
        "/companies/me/boms/bom-001",
        json={"name": "Gold Ring BOM v2"},
        headers=headers,
    )
    assert r.status_code == 200
    assert r.json()["ok"] is True

    # Delete (soft)
    r = await client.delete("/companies/me/boms/bom-001", headers=headers)
    assert r.status_code == 200
    assert r.json()["ok"] is True


@pytest.mark.asyncio
async def test_bom_duplicate_409(client):
    headers = await _register(client, "bom_409")
    payload = {"bom_id": "bom-dup", "name": "Dup BOM", "inputs": [], "outputs": []}
    await client.post("/companies/me/boms", json=payload, headers=headers)
    r = await client.post("/companies/me/boms", json=payload, headers=headers)
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_bom_get_not_found(client):
    headers = await _register(client, "bom_get_404")
    r = await client.get("/companies/me/boms/nonexistent", headers=headers)
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_bom_patch_not_found(client):
    headers = await _register(client, "bom_patch_404")
    r = await client.patch("/companies/me/boms/nonexistent", json={"name": "x"}, headers=headers)
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_bom_delete_not_found(client):
    headers = await _register(client, "bom_del_404")
    r = await client.delete("/companies/me/boms/nonexistent", headers=headers)
    assert r.status_code == 404


# ── Taxes batch import ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_taxes_batch_import_creates(client):
    headers = await _register(client, "tax_import")

    r = await client.post(
        "/companies/me/taxes/import/batch",
        json={"records": [
            _tax_record("Custom GST 10%", 10.0),       # not in defaults
            _tax_record("WHT 3%", 3.0, "sales"),
        ]},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["created"] == 2
    assert body["skipped"] == 0
    assert body["errors"] == []


@pytest.mark.asyncio
async def test_taxes_batch_import_skips_duplicate(client):
    headers = await _register(client, "tax_dup")

    payload = {"records": [_tax_record("VAT 7%", 7.0, idem="vat7-unique")]}
    await client.post("/companies/me/taxes/import/batch", json=payload, headers=headers)
    # Second import with same name (different idem key - duplicate is name-based)
    payload2 = {"records": [_tax_record("VAT 7%", 7.0, idem="vat7-unique-2")]}
    r = await client.post("/companies/me/taxes/import/batch", json=payload2, headers=headers)

    assert r.status_code == 200
    body = r.json()
    assert body["skipped"] == 1
    assert body["created"] == 0


@pytest.mark.asyncio
async def test_taxes_batch_import_invalid_rate(client):
    headers = await _register(client, "tax_bad_rate")

    r = await client.post(
        "/companies/me/taxes/import/batch",
        json={"records": [_tax_record("Bad Tax", "notanumber")]},
        headers=headers,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["created"] == 0
    assert len(body["errors"]) == 1
    assert "Invalid rate" in body["errors"][0]


@pytest.mark.asyncio
async def test_taxes_batch_import_missing_name(client):
    headers = await _register(client, "tax_no_name")

    r = await client.post(
        "/companies/me/taxes/import/batch",
        json={"records": [{
            "entity_id": "company",
            "event_type": "tax.import",
            "source": "import",
            "idempotency_key": "tax-noname-1",
            "data": {"rate": 5.0, "tax_type": "sales"},
        }]},
        headers=headers,
    )
    assert r.status_code == 200
    body = r.json()
    assert len(body["errors"]) == 1
    assert "Missing name" in body["errors"][0]


@pytest.mark.asyncio
async def test_taxes_batch_import_invalid_tax_type(client):
    headers = await _register(client, "tax_bad_type")

    r = await client.post(
        "/companies/me/taxes/import/batch",
        json={"records": [_tax_record("Weird Tax", 5.0, "weird")]},
        headers=headers,
    )
    assert r.status_code == 200
    body = r.json()
    assert len(body["errors"]) == 1
    assert "Invalid tax_type" in body["errors"][0]


# ── Payment terms batch import ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_payment_terms_batch_import_creates(client):
    headers = await _register(client, "pt_import")

    r = await client.post(
        "/companies/me/payment-terms/import/batch",
        json={"records": [
            _pt_record("Net 120", 120),   # not in defaults
            _pt_record("Net 180", 180),
        ]},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["created"] == 2
    assert body["errors"] == []


@pytest.mark.asyncio
async def test_payment_terms_batch_import_skips_duplicate(client):
    headers = await _register(client, "pt_dup")

    payload = {"records": [_pt_record("Net 45", 45, idem="pt45-a")]}
    await client.post("/companies/me/payment-terms/import/batch", json=payload, headers=headers)
    payload2 = {"records": [_pt_record("Net 45", 45, idem="pt45-b")]}
    r = await client.post("/companies/me/payment-terms/import/batch", json=payload2, headers=headers)

    assert r.status_code == 200
    assert r.json()["skipped"] == 1


@pytest.mark.asyncio
async def test_payment_terms_batch_import_invalid_days(client):
    headers = await _register(client, "pt_bad_days")

    r = await client.post(
        "/companies/me/payment-terms/import/batch",
        json={"records": [_pt_record("Bad Terms", "not-a-number")]},
        headers=headers,
    )
    assert r.status_code == 200
    body = r.json()
    assert len(body["errors"]) == 1
    assert "Invalid days" in body["errors"][0]


@pytest.mark.asyncio
async def test_payment_terms_batch_import_missing_name(client):
    headers = await _register(client, "pt_no_name")

    r = await client.post(
        "/companies/me/payment-terms/import/batch",
        json={"records": [{
            "entity_id": "company",
            "event_type": "pt.import",
            "source": "import",
            "idempotency_key": "pt-noname-1",
            "data": {"days": 15},
        }]},
        headers=headers,
    )
    assert r.status_code == 200
    assert "Missing name" in r.json()["errors"][0]


# ── /health routes ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_health_ready(client):
    r = await client.get("/health/ready")
    assert r.status_code == 200
    assert r.json()["db"] == "ok"


@pytest.mark.asyncio
async def test_health_system(client):
    r = await client.get("/health/system")
    assert r.status_code == 200
    assert isinstance(r.json(), dict)


# ── /connectors sync success path ─────────────────────────────────────────────

_FAKE_SESSION_TOKEN = "test-session-token-abc123"


@pytest.fixture(autouse=False)
def patch_session_token():
    import celerp.gateway.state as gw_state
    gw_state.set_session_token(_FAKE_SESSION_TOKEN)
    yield
    gw_state.set_session_token("")


@pytest.mark.asyncio
async def test_connector_sync_success(client, patch_session_token):
    """POST /connectors/shopify/sync with mocked connector returns 200 SyncResponse."""
    from unittest.mock import AsyncMock, patch
    from celerp.connectors.base import SyncResult, SyncEntity, SyncDirection

    base_headers = await _register(client, "conn_sync")
    headers = {**base_headers, "X-Session-Token": _FAKE_SESSION_TOKEN}

    mock_result = SyncResult(
        entity=SyncEntity.PRODUCTS,
        direction=SyncDirection.INBOUND,
        created=3,
        updated=0,
        skipped=1,
        errors=[],
    )

    with patch("celerp.connectors.shopify.ShopifyConnector.sync_products", new=AsyncMock(return_value=mock_result)):
        r = await client.post(
            "/connectors/shopify/sync",
            headers=headers,
            json={"entity": "products", "access_token": "tok", "store_handle": "test.myshopify.com"},
        )

    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["created"] == 3
    assert body["skipped"] == 1
