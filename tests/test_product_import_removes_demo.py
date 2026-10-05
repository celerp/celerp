# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The dashboard card says: "Items marked [DEMO] are samples. Your first product
import removes them unless you have changed them." This runs a real product import
and checks that sentence is true."""
from __future__ import annotations

import uuid

import pytest


async def _owner(client) -> dict:
    r = await client.post("/auth/register", json={
        "company_name": "DemoCo", "email": f"demo-{uuid.uuid4().hex[:8]}@test.example",
        "name": "Owner", "password": "pwvalid1",
    })
    assert r.status_code == 200, r.text
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}
    r = await client.post("/companies/me/business-type", json={"vertical": "gemstones"}, headers=h)
    assert r.status_code == 200, r.text
    return h


async def _demo_by_sku(client, h) -> dict[str, dict]:
    items = (await client.get("/items", headers=h, params={"limit": 500, "status": "all"})).json()["items"]
    return {i["sku"]: i for i in items if str(i.get("sku", "")).startswith("DEMO-")}


@pytest.mark.asyncio
async def test_first_product_import_removes_untouched_demo_items_keeps_edited(client):
    h = await _owner(client)
    demo = await _demo_by_sku(client, h)
    assert len(demo) > 1, "the business type seeds several [DEMO] items"
    edited_sku = sorted(demo)[0]
    r = await client.patch(f"/items/{demo[edited_sku]['id']}", headers=h,
                           json={"fields_changed": {"name": {"old": None, "new": "My own stone"}}})
    assert r.status_code == 200, r.text

    imp = await client.post("/items/import/batch", headers=h, json={"records": [{
        "entity_id": "item:first-import-1", "event_type": "item.created", "source": "csv",
        "idempotency_key": "first-import-1",
        "data": {"sku": "MY-001", "name": "First real product", "quantity": 1, "sell_by": "piece"},
    }]})
    assert imp.status_code == 200, imp.text

    left = await _demo_by_sku(client, h)
    assert set(left) == {edited_sku}, "only the changed demo item survives the first import"
    assert left[edited_sku]["name"] == "My own stone"
    items = (await client.get("/items", headers=h, params={"limit": 500, "status": "all"})).json()["items"]
    assert "MY-001" in {i["sku"] for i in items}
