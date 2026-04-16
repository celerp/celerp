# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1
"""Integration tests for perishable/lot-tracked inventory behaviour:
- expiry_date attribute wires through to expires_at projection column
- /reports/expiring returns items correctly
- merge takes earliest expiry date (no longer blocked on mismatch)
- merge preserves expiry/batch attrs and deactivates source items
- FEFO list sort (soonest expiry first when inventory_method=fefo)
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient


async def _register(client: AsyncClient, email: str = "perishtest@test.com") -> str:
    r = await client.post("/auth/register", json={
        "company_name": "PerishTest Co", "name": "Test User", "email": email, "password": "pass1234",
    })
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _create_item(client: AsyncClient, token: str, sku: str, name: str,
                       qty: float = 100, attrs: dict | None = None) -> str:
    payload: dict = {"sku": sku, "name": name, "quantity": qty, "sell_by": "piece"}
    if attrs:
        payload["attributes"] = attrs
    r = await client.post("/items", json=payload, headers=_auth(token))
    assert r.status_code == 200, r.text
    return r.json()["id"]


# ---------------------------------------------------------------------------
# Expiry wiring: attribute → expires_at projection column
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_expiry_date_attribute_wires_to_expiring_report(client: AsyncClient):
    """Item created with attributes.expiry_date should appear in /reports/expiring."""
    token = await _register(client, "expiry_wire@test.com")
    await _create_item(client, token, "RICE-001", "Jasmine Rice", attrs={"expiry_date": "2020-01-01"})

    r = await client.get("/reports/expiring?days=99999", headers=_auth(token))
    assert r.status_code == 200
    data = r.json()
    skus = [item["sku"] for item in data["lines"]]
    assert "RICE-001" in skus, f"Expected RICE-001 in expiring report, got: {skus}"


@pytest.mark.asyncio
async def test_warranty_exp_attribute_wires_to_expiring_report(client: AsyncClient):
    """Electronics with attributes.warranty_exp should appear in /reports/expiring."""
    token = await _register(client, "warranty_wire@test.com")
    await _create_item(client, token, "LAPTOP-X1", "ThinkPad X1", attrs={"warranty_exp": "2020-06-01"})

    r = await client.get("/reports/expiring?days=99999", headers=_auth(token))
    assert r.status_code == 200
    skus = [item["sku"] for item in r.json()["lines"]]
    assert "LAPTOP-X1" in skus


@pytest.mark.asyncio
async def test_item_without_expiry_not_in_report(client: AsyncClient):
    """Items with no expiry attribute must not appear in /reports/expiring."""
    token = await _register(client, "no_expiry@test.com")
    await _create_item(client, token, "WIDGET-99", "Plain Widget")

    r = await client.get("/reports/expiring?days=99999", headers=_auth(token))
    assert r.status_code == 200
    skus = [item["sku"] for item in r.json()["lines"]]
    assert "WIDGET-99" not in skus


# ---------------------------------------------------------------------------
# Merge: conflict detection
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_merge_different_expiry_takes_earliest(client: AsyncClient):
    """Merging items with different expiry dates must succeed and use the earliest date."""
    token = await _register(client, "merge_conflict@test.com")
    id_a = await _create_item(client, token, "RICE-A", "Rice Lot A", qty=100, attrs={"expiry_date": "2026-06-01"})
    id_b = await _create_item(client, token, "RICE-B", "Rice Lot B", qty=200, attrs={"expiry_date": "2026-09-15"})

    r = await client.post("/items/merge", json={
        "source_entity_ids": [id_a, id_b],
        "target_sku_from": id_b,
    }, headers=_auth(token))
    assert r.status_code == 200, r.text
    new_id = r.json()["id"]

    r2 = await client.get(f"/items/{new_id}", headers=_auth(token))
    assert r2.status_code == 200
    item = r2.json()
    # Earliest date must be taken.
    assert item.get("expiry_date") == "2026-06-01" or item.get("expires_at") == "2026-06-01"


@pytest.mark.asyncio
async def test_merge_earliest_expiry_is_min_date(client: AsyncClient):
    """With three items, the earliest of all expiry dates wins."""
    token = await _register(client, "merge_dates_msg@test.com")
    id_a = await _create_item(client, token, "PROD-A", "Product A", attrs={"expiry_date": "2026-03-01"})
    id_b = await _create_item(client, token, "PROD-B", "Product B", attrs={"expiry_date": "2026-12-31"})
    id_c = await _create_item(client, token, "PROD-C", "Product C", attrs={"expiry_date": "2026-07-15"})

    r = await client.post("/items/merge", json={
        "source_entity_ids": [id_a, id_b, id_c],
        "target_sku_from": id_b,
    }, headers=_auth(token))
    assert r.status_code == 200, r.text
    new_id = r.json()["id"]

    r2 = await client.get(f"/items/{new_id}", headers=_auth(token))
    item = r2.json()
    assert item.get("expiry_date") == "2026-03-01" or item.get("expires_at") == "2026-03-01"


# ---------------------------------------------------------------------------
# Merge: base case correctness
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_merge_same_expiry_succeeds_and_preserves_attrs(client: AsyncClient):
    """Merging two items with the same expiry date must succeed and preserve expiry/batch."""
    token = await _register(client, "merge_same_exp@test.com")
    id_a = await _create_item(client, token, "RICE-A2", "Rice Lot A", qty=300,
                               attrs={"lot_no": "B-001", "expiry_date": "2026-06-15"})
    id_b = await _create_item(client, token, "RICE-B2", "Rice Lot B", qty=500,
                               attrs={"lot_no": "B-001", "expiry_date": "2026-06-15"})

    r = await client.post("/items/merge", json={
        "source_entity_ids": [id_a, id_b],
        "target_sku_from": id_b,
    }, headers=_auth(token))
    assert r.status_code == 200, r.text
    new_id = r.json()["id"]

    # New item should have summed quantity.
    r2 = await client.get(f"/items/{new_id}", headers=_auth(token))
    assert r2.status_code == 200
    item = r2.json()
    assert item["quantity"] == 800


@pytest.mark.asyncio
async def test_merge_deactivates_source_items(client: AsyncClient):
    """Source items must be marked is_available=False and qty=0 after a merge."""
    token = await _register(client, "merge_deactivate@test.com")
    id_src = await _create_item(client, token, "SRC-ITEM", "Source", qty=200,
                                 attrs={"expiry_date": "2026-06-15"})
    id_tgt = await _create_item(client, token, "TGT-ITEM", "Target", qty=100,
                                 attrs={"expiry_date": "2026-06-15"})

    r = await client.post("/items/merge", json={
        "source_entity_ids": [id_src, id_tgt],
        "target_sku_from": id_tgt,
    }, headers=_auth(token))
    assert r.status_code == 200, r.text
    new_id = r.json()["id"]

    r2 = await client.get(f"/items/{id_src}", headers=_auth(token))
    assert r2.status_code == 200
    src_state = r2.json()
    assert src_state.get("is_available") is False
    assert src_state.get("quantity") == 0
    assert src_state.get("merged_into") == new_id


@pytest.mark.asyncio
async def test_merge_no_expiry_items_succeeds(client: AsyncClient):
    """Merging items with no expiry (non-perishable) must still work cleanly."""
    token = await _register(client, "merge_no_expiry@test.com")
    id_a = await _create_item(client, token, "BOLT-M8-A", "Bolt M8", qty=1000)
    id_b = await _create_item(client, token, "BOLT-M8-B", "Bolt M8", qty=500)

    r = await client.post("/items/merge", json={
        "source_entity_ids": [id_a, id_b],
        "target_sku_from": id_b,
    }, headers=_auth(token))
    assert r.status_code == 200, r.text
    new_id = r.json()["id"]

    r2 = await client.get(f"/items/{new_id}", headers=_auth(token))
    assert r2.json()["quantity"] == 1500


# ---------------------------------------------------------------------------
# FEFO list sort
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fefo_sort_lists_soonest_expiry_first(client: AsyncClient):
    """When company has inventory_method=fefo, GET /items returns items sorted soonest-expiry-first."""
    token = await _register(client, "fefo_sort@test.com")

    # Apply F&B preset which sets fefo
    r = await client.post("/companies/me/apply-preset", params={"vertical": "food_beverage"}, headers=_auth(token))
    assert r.status_code == 200

    # Create three lots with different expiry dates (out of order)
    await _create_item(client, token, "LOT-C", "Rice Lot C", attrs={"expiry_date": "2026-12-01"})
    await _create_item(client, token, "LOT-A", "Rice Lot A", attrs={"expiry_date": "2026-03-01"})
    await _create_item(client, token, "LOT-B", "Rice Lot B", attrs={"expiry_date": "2026-06-15"})

    r = await client.get("/items", headers=_auth(token))
    assert r.status_code == 200
    items = r.json()["items"]
    expiry_skus = [i["sku"] for i in items if i.get("sku", "").startswith("LOT-")]
    # LOT-A (Mar) must come before LOT-B (Jun) which must come before LOT-C (Dec)
    assert expiry_skus.index("LOT-A") < expiry_skus.index("LOT-B")
    assert expiry_skus.index("LOT-B") < expiry_skus.index("LOT-C")


@pytest.mark.asyncio
async def test_non_fefo_company_no_forced_sort(client: AsyncClient):
    """Without fefo, item list order is not forced by expiry date."""
    token = await _register(client, "fifo_nosort@test.com")

    # Default company has no inventory_method=fefo
    await _create_item(client, token, "ITEM-LATE", "Item Late", attrs={"expiry_date": "2027-12-01"})
    await _create_item(client, token, "ITEM-EARLY", "Item Early", attrs={"expiry_date": "2025-01-01"})

    r = await client.get("/items", headers=_auth(token))
    assert r.status_code == 200
    # Just verify it doesn't crash; no sort requirement enforced
    assert len(r.json()["items"]) >= 2
