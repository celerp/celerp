# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Tests for purchasing fields on inventory items."""

from __future__ import annotations

import uuid

import pytest

_BASE = "/items"


async def _register(client) -> str:
    addr = f"purch-{uuid.uuid4().hex[:8]}@test.local"
    r = await client.post("/auth/register", json={"company_name": "PurchCo", "email": addr, "name": "Admin", "password": "pw"})
    assert r.status_code == 200
    return r.json()["access_token"]


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.anyio
async def test_create_item_with_purchasing_fields(client):
    token = await _register(client)
    resp = await client.post(
        _BASE,
        json={
            "sku": "PURCH-TEST-001",
            "name": "Purchase Field Test",
            "sell_by": "piece",
            "quantity": 100,
            "purchase_sku": "VENDOR-ABC-999",
            "purchase_name": "Vendor Product Name",
            "purchase_unit": "case",
            "purchase_conversion_factor": 24.0,
        },
        headers=_h(token),
    )
    assert resp.status_code in (200, 201), resp.text
    data = resp.json()
    eid = data.get("entity_id") or data.get("id")
    assert eid

    # Fetch and verify fields persisted in projection
    resp2 = await client.get(f"{_BASE}/{eid}", headers=_h(token))
    assert resp2.status_code == 200
    item = resp2.json()
    assert item.get("purchase_sku") == "VENDOR-ABC-999"
    assert item.get("purchase_name") == "Vendor Product Name"
    assert item.get("purchase_unit") == "case"
    assert item.get("purchase_conversion_factor") == 24.0


@pytest.mark.anyio
async def test_patch_purchasing_fields(client):
    token = await _register(client)
    resp = await client.post(
        _BASE,
        json={"sku": "PURCH-TEST-002", "name": "Patch Test", "sell_by": "piece", "quantity": 10},
        headers=_h(token),
    )
    assert resp.status_code in (200, 201)
    eid = resp.json().get("entity_id") or resp.json().get("id")

    resp2 = await client.patch(
        f"{_BASE}/{eid}",
        json={
            "fields_changed": {
                "purchase_sku": {"new": "V-SKU-123"},
                "purchase_unit": {"new": "box"},
                "purchase_conversion_factor": {"new": 12},
            }
        },
        headers=_h(token),
    )
    assert resp2.status_code == 200

    resp3 = await client.get(f"{_BASE}/{eid}", headers=_h(token))
    item = resp3.json()
    assert item.get("purchase_sku") == "V-SKU-123"
    assert item.get("purchase_unit") == "box"
    assert item.get("purchase_conversion_factor") == 12


@pytest.mark.anyio
async def test_create_item_without_purchasing_fields(client):
    """Purchasing fields are optional - items without them work fine."""
    token = await _register(client)
    resp = await client.post(
        _BASE,
        json={"sku": "PURCH-TEST-003", "name": "No Purchase Fields", "sell_by": "piece", "quantity": 5},
        headers=_h(token),
    )
    assert resp.status_code in (200, 201)
    eid = resp.json().get("entity_id") or resp.json().get("id")

    resp2 = await client.get(f"{_BASE}/{eid}", headers=_h(token))
    item = resp2.json()
    assert item.get("purchase_sku") is None
    assert item.get("purchase_conversion_factor") is None


def test_field_schema_includes_purchasing_fields():
    """Purchasing fields appear in the default field schema."""
    from celerp.services.field_schema import DEFAULT_ITEM_SCHEMA

    keys = {f["key"] for f in DEFAULT_ITEM_SCHEMA}
    assert "purchase_sku" in keys
    assert "purchase_name" in keys
    assert "purchase_unit" in keys
    assert "purchase_conversion_factor" in keys

    for f in DEFAULT_ITEM_SCHEMA:
        if f["key"].startswith("purchase_"):
            assert f["show_in_table"] is False, f"{f['key']} should be hidden in table"
            assert f["editable"] is True, f"{f['key']} should be editable"
