# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

from __future__ import annotations

import pytest


async def _token(client) -> str:
    r = await client.post(
        "/auth/register",
        json={"company_name": "Acme", "email": "admin@acme.com", "name": "Admin", "password": "pw"},
    )
    return r.json()["access_token"]


@pytest.mark.asyncio
async def test_items_happy_path(client):
    token = await _token(client)
    headers = {"Authorization": f"Bearer {token}"}

    loc = await client.post(
        "/companies/me/locations",
        json={"name": "Main", "type": "warehouse", "address": None, "is_default": True},
        headers=headers,
    )
    location_id = loc.json()["id"]

    r = await client.post(
        "/items",
        json={"sku": "SKU1", "name": "Thing", "quantity": 2, "location_id": location_id, "sell_by": "piece"},
        headers=headers,
    )
    assert r.status_code == 200
    id = r.json()["id"]

    r = await client.get("/items", headers=headers)
    assert r.status_code == 200
    assert any(i["id"] == id for i in r.json()["items"])

    r = await client.get(f"/items/{id}", headers=headers)
    assert r.status_code == 200
    assert r.json()["name"] == "Thing"

    r = await client.patch(
        f"/items/{id}",
        json={"fields_changed": {"name": {"old": "Thing", "new": "Thing2"}}},
        headers=headers,
    )
    assert r.status_code == 200

    r = await client.post(f"/items/{id}/price", json={"price_type": "price", "new_price": 10}, headers=headers)
    assert r.status_code == 200

    r = await client.post(f"/items/{id}/status", json={"new_status": "active"}, headers=headers)
    assert r.status_code == 200

    r = await client.post(f"/items/{id}/reserve", json={"quantity": 1.5}, headers=headers)
    assert r.status_code == 200

    r = await client.post(f"/items/{id}/unreserve", json={"quantity": 0.5}, headers=headers)
    assert r.status_code == 200

    r = await client.post(f"/items/{id}/adjust", json={"new_qty": 99}, headers=headers)
    assert r.status_code == 200

    r = await client.post(f"/items/{id}/transfer", json={"to_location_id": location_id}, headers=headers)
    assert r.status_code == 200

    r = await client.post(
        f"/items/{id}/split",
        json={"children": [{"sku": "CHILD-A", "quantity": 1.0}, {"sku": "CHILD-B", "quantity": 1.0}]},
        headers=headers,
    )
    assert r.status_code == 200

    # Create a second item so merge has 2 real sources
    r2 = await client.post(
        "/items",
        json={"sku": "SKU-MERGE", "name": "MergePeer", "quantity": 1, "location_id": location_id, "sell_by": "piece"},
        headers=headers,
    )
    merge_peer_id = r2.json()["id"]
    r = await client.post(
        "/items/merge",
        json={"source_entity_ids": [id, merge_peer_id], "target_sku_from": id},
        headers=headers,
    )
    assert r.status_code == 200
    merged_id = r.json()["id"]

    r = await client.post(f"/items/{merged_id}/expire", headers=headers)
    assert r.status_code == 200

    r = await client.post(f"/items/{merged_id}/dispose", headers=headers)
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_items_split_rejects_mismatch_lengths(client):
    """Split with only 1 child should return 422."""
    token = await _token(client)
    headers = {"Authorization": f"Bearer {token}"}

    r = await client.post("/items", json={"sku": "SKU1", "name": "Thing", "quantity": 2, "sell_by": "piece"}, headers=headers)
    id = r.json()["id"]

    # Only 1 child — requires at least 2
    r = await client.post(
        f"/items/{id}/split",
        json={"children": [{"sku": "CHILD-1", "quantity": 1.0}]},
        headers=headers,
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_items_requires_auth(client):
    r = await client.get("/items")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_list_items_default_excludes_sold_and_archived(client):
    """Default GET /items must exclude sold + archived items."""
    token = await _token(client)
    headers = {"Authorization": f"Bearer {token}"}

    # Create three items
    r1 = await client.post("/items", json={"sku": "AVAIL-1", "name": "Available Item", "quantity": 1, "sell_by": "piece"}, headers=headers)
    r2 = await client.post("/items", json={"sku": "SOLD-1", "name": "Sold Item", "quantity": 1, "sell_by": "piece"}, headers=headers)
    r3 = await client.post("/items", json={"sku": "ARCH-1", "name": "Archived Item", "quantity": 1, "sell_by": "piece"}, headers=headers)
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r3.status_code == 200

    avail_id = r1.json()["id"]
    sold_id = r2.json()["id"]
    arch_id = r3.json()["id"]

    # Set statuses
    await client.post(f"/items/{sold_id}/status", json={"new_status": "sold"}, headers=headers)
    await client.post(f"/items/{arch_id}/status", json={"new_status": "archived"}, headers=headers)

    # Default list: must include available, exclude sold + archived
    r = await client.get("/items", headers=headers)
    assert r.status_code == 200
    ids = {i["id"] for i in r.json()["items"]}
    assert avail_id in ids
    assert sold_id not in ids
    assert arch_id not in ids


@pytest.mark.asyncio
async def test_list_items_status_filter_sold(client):
    """GET /items?status=sold returns only sold items."""
    token = await _token(client)
    headers = {"Authorization": f"Bearer {token}"}

    r1 = await client.post("/items", json={"sku": "AVAIL-2", "name": "Available", "quantity": 1, "sell_by": "piece"}, headers=headers)
    r2 = await client.post("/items", json={"sku": "SOLD-2", "name": "Sold", "quantity": 1, "sell_by": "piece"}, headers=headers)
    avail_id = r1.json()["id"]
    sold_id = r2.json()["id"]

    await client.post(f"/items/{sold_id}/status", json={"new_status": "sold"}, headers=headers)

    r = await client.get("/items?status=sold", headers=headers)
    assert r.status_code == 200
    ids = {i["id"] for i in r.json()["items"]}
    assert sold_id in ids
    assert avail_id not in ids


@pytest.mark.asyncio
async def test_list_items_status_all_shows_everything(client):
    """GET /items?status=all returns sold + archived + available."""
    token = await _token(client)
    headers = {"Authorization": f"Bearer {token}"}

    r1 = await client.post("/items", json={"sku": "AVAIL-3", "name": "Available3", "quantity": 1, "sell_by": "piece"}, headers=headers)
    r2 = await client.post("/items", json={"sku": "SOLD-3", "name": "Sold3", "quantity": 1, "sell_by": "piece"}, headers=headers)
    r3 = await client.post("/items", json={"sku": "ARCH-3", "name": "Archived3", "quantity": 1, "sell_by": "piece"}, headers=headers)
    avail_id = r1.json()["id"]
    sold_id = r2.json()["id"]
    arch_id = r3.json()["id"]

    await client.post(f"/items/{sold_id}/status", json={"new_status": "sold"}, headers=headers)
    await client.post(f"/items/{arch_id}/status", json={"new_status": "archived"}, headers=headers)

    r = await client.get("/items?status=all", headers=headers)
    assert r.status_code == 200
    ids = {i["id"] for i in r.json()["items"]}
    assert avail_id in ids
    assert sold_id in ids
    assert arch_id in ids


@pytest.mark.asyncio
async def test_valuation_excludes_sold_and_archived(client):
    """GET /items/valuation must exclude sold + archived from counts and totals."""
    token = await _token(client)
    headers = {"Authorization": f"Bearer {token}"}

    r1 = await client.post("/items", json={"sku": "VAL-AVAIL", "name": "ValAvail", "quantity": 1, "sell_by": "piece"}, headers=headers)
    r2 = await client.post("/items", json={"sku": "VAL-SOLD", "name": "ValSold", "quantity": 1, "sell_by": "piece"}, headers=headers)
    r3 = await client.post("/items", json={"sku": "VAL-ARCH", "name": "ValArch", "quantity": 1, "sell_by": "piece"}, headers=headers)
    assert r1.status_code == 200 and r2.status_code == 200 and r3.status_code == 200

    sold_id = r2.json()["id"]
    arch_id = r3.json()["id"]

    await client.post(f"/items/{sold_id}/status", json={"new_status": "sold"}, headers=headers)
    await client.post(f"/items/{arch_id}/status", json={"new_status": "archived"}, headers=headers)

    r = await client.get("/items/valuation", headers=headers)
    assert r.status_code == 200
    data = r.json()

    # Get total items via all-status listing to compare
    r_all = await client.get("/items?status=all", headers=headers)
    total_all = r_all.json()["total"]

    # active_item_count must be less than total (sold + archived excluded)
    assert data["active_item_count"] == data["item_count"]
    assert data["active_item_count"] == total_all - 2  # 2 items hidden (sold + archived)


@pytest.mark.asyncio
async def test_bulk_archive(client):
    """POST /items/bulk/status with status=archived hides items from default view."""
    token = await _token(client)
    headers = {"Authorization": f"Bearer {token}"}

    r1 = await client.post("/items", json={"sku": "BULK-A", "name": "BulkA", "quantity": 1, "sell_by": "piece"}, headers=headers)
    r2 = await client.post("/items", json={"sku": "BULK-B", "name": "BulkB", "quantity": 1, "sell_by": "piece"}, headers=headers)
    id1 = r1.json()["id"]
    id2 = r2.json()["id"]

    r = await client.post(
        "/items/bulk/status",
        json={"entity_ids": [id1, id2], "status": "archived"},
        headers=headers,
    )
    assert r.status_code == 200
    assert r.json()["updated"] == 2

    # Default list should no longer include them
    r = await client.get("/items", headers=headers)
    assert r.status_code == 200
    ids = {i["id"] for i in r.json()["items"]}
    assert id1 not in ids
    assert id2 not in ids

    # ?status=archived shows them
    r = await client.get("/items?status=archived", headers=headers)
    assert r.status_code == 200
    ids_arch = {i["id"] for i in r.json()["items"]}
    assert id1 in ids_arch
    assert id2 in ids_arch



@pytest.mark.asyncio
async def test_valuation_category_filter(client):
    """GET /items/valuation?category= scopes count_by_status and totals to that category."""
    token = await _token(client)
    headers = {"Authorization": f"Bearer {token}"}

    r1 = await client.post("/items", json={"sku": "VCAT-A", "name": "VCatA", "quantity": 1, "category": "Rubies", "sell_by": "piece"}, headers=headers)
    r2 = await client.post("/items", json={"sku": "VCAT-B", "name": "VCatB", "quantity": 1, "category": "Sapphires", "sell_by": "piece"}, headers=headers)
    assert r1.status_code == 200 and r2.status_code == 200
    id1 = r1.json()["id"]

    await client.post(f"/items/{id1}/status", json={"new_status": "available"}, headers=headers)

    r = await client.get("/items/valuation?category=Rubies", headers=headers)
    assert r.status_code == 200
    data = r.json()
    # Scoped count matches items in Rubies only
    assert data["active_item_count"] == 1
    # category_counts is always global (both categories visible in tabs)
    assert "Rubies" in data["category_counts"]
    assert "Sapphires" in data["category_counts"]
    # count_by_status present and covers the scoped item
    assert "available" in data["count_by_status"]
    assert data["count_by_status"]["available"] == 1


@pytest.mark.asyncio
async def test_valuation_count_by_status(client):
    """GET /items/valuation returns count_by_status for all active statuses."""
    token = await _token(client)
    headers = {"Authorization": f"Bearer {token}"}

    r1 = await client.post("/items", json={"sku": "VCS-A", "name": "VcsA", "quantity": 1, "sell_by": "piece"}, headers=headers)
    r2 = await client.post("/items", json={"sku": "VCS-B", "name": "VcsB", "quantity": 1, "sell_by": "piece"}, headers=headers)
    assert r1.status_code == 200 and r2.status_code == 200
    id2 = r2.json()["id"]

    await client.post(f"/items/{id2}/status", json={"new_status": "reserved"}, headers=headers)

    r = await client.get("/items/valuation", headers=headers)
    assert r.status_code == 200
    cbs = r.json()["count_by_status"]
    assert cbs.get("available", 0) >= 1
    assert cbs.get("reserved", 0) >= 1
    # Sold/archived not counted (hidden statuses)
    assert "sold" not in cbs
    assert "archived" not in cbs


# ---------------------------------------------------------------------------
# sell_by refactor, SKU/barcode uniqueness, split fix tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_item_requires_sell_by(client):
    """POST /items without sell_by must return 422."""
    token = await _token(client)
    h = {"Authorization": f"Bearer {token}"}
    r = await client.post("/items", json={"sku": "NO-SB", "name": "Widget", "quantity": 1}, headers=h)
    assert r.status_code == 422
    assert "sell_by" in r.text.lower() or "field required" in r.text.lower()


@pytest.mark.asyncio
async def test_create_item_validates_sell_by_unit(client):
    """sell_by must be a valid unit name from company units."""
    token = await _token(client)
    h = {"Authorization": f"Bearer {token}"}
    r = await client.post("/items", json={"sku": "BAD-U", "name": "Widget", "quantity": 1, "sell_by": "bushel"}, headers=h)
    assert r.status_code == 422
    assert "bushel" in r.text


@pytest.mark.asyncio
async def test_piece_rejects_fractional_qty(client):
    """sell_by=piece (decimals=0) must reject fractional quantity."""
    token = await _token(client)
    h = {"Authorization": f"Bearer {token}"}
    r = await client.post("/items", json={"sku": "FRAC-1", "name": "Widget", "quantity": 2.5, "sell_by": "piece"}, headers=h)
    assert r.status_code == 422
    assert "precision" in r.text.lower() or "decimal" in r.text.lower()


@pytest.mark.asyncio
async def test_carat_allows_fractional_qty(client):
    """sell_by=carat (decimals=2) allows 2dp quantity."""
    token = await _token(client)
    h = {"Authorization": f"Bearer {token}"}
    r = await client.post("/items", json={"sku": "CT-1", "name": "Emerald", "quantity": 2.55, "sell_by": "carat"}, headers=h)
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_carat_rejects_excess_decimals(client):
    """sell_by=carat (decimals=2) rejects 3dp quantity."""
    token = await _token(client)
    h = {"Authorization": f"Bearer {token}"}
    r = await client.post("/items", json={"sku": "CT-2", "name": "Emerald", "quantity": 2.555, "sell_by": "carat"}, headers=h)
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_duplicate_sku_rejected(client):
    """Creating two items with the same SKU must return 409."""
    token = await _token(client)
    h = {"Authorization": f"Bearer {token}"}
    r1 = await client.post("/items", json={"sku": "DUP-1", "name": "A", "quantity": 1, "sell_by": "piece"}, headers=h)
    assert r1.status_code == 200
    r2 = await client.post("/items", json={"sku": "DUP-1", "name": "B", "quantity": 1, "sell_by": "piece"}, headers=h)
    assert r2.status_code == 409
    assert "DUP-1" in r2.text


@pytest.mark.asyncio
async def test_duplicate_barcode_rejected(client):
    """Creating two items with the same barcode must return 409."""
    token = await _token(client)
    h = {"Authorization": f"Bearer {token}"}
    r1 = await client.post("/items", json={"sku": "BC-1", "name": "A", "quantity": 1, "sell_by": "piece", "barcode": "123456"}, headers=h)
    assert r1.status_code == 200
    r2 = await client.post("/items", json={"sku": "BC-2", "name": "B", "quantity": 1, "sell_by": "piece", "barcode": "123456"}, headers=h)
    assert r2.status_code == 409
    assert "123456" in r2.text


@pytest.mark.asyncio
async def test_barcode_must_be_digits(client):
    """Barcode with non-digit characters must be rejected."""
    token = await _token(client)
    h = {"Authorization": f"Bearer {token}"}
    r = await client.post("/items", json={"sku": "BC-3", "name": "A", "quantity": 1, "sell_by": "piece", "barcode": "ABC-123"}, headers=h)
    assert r.status_code == 422
    assert "digits" in r.text.lower()


@pytest.mark.asyncio
async def test_split_creates_children(client):
    """Split must create child items and reduce parent quantity."""
    token = await _token(client)
    h = {"Authorization": f"Bearer {token}"}
    # Create parent
    r = await client.post("/items", json={"sku": "PARENT-1", "name": "Parcel", "quantity": 20, "sell_by": "piece", "category": "gem"}, headers=h)
    assert r.status_code == 200
    parent_id = r.json()["id"]

    # Split off 5 + 5
    r = await client.post(f"/items/{parent_id}/split", json={
        "children": [
            {"sku": "CHILD-1", "quantity": 5},
            {"sku": "CHILD-2", "quantity": 5},
        ]
    }, headers=h)
    assert r.status_code == 200
    data = r.json()
    assert len(data["children"]) == 2

    # Verify parent qty reduced
    r = await client.get(f"/items/{parent_id}", headers=h)
    assert r.status_code == 200
    assert float(r.json()["quantity"]) == 10.0

    # Verify parent is still available
    assert r.json().get("is_available", True) is True

    # Verify children exist
    child_1_id = data["children"][0]["id"]
    child_2_id = data["children"][1]["id"]
    r1 = await client.get(f"/items/{child_1_id}", headers=h)
    assert r1.status_code == 200
    assert r1.json()["sku"] == "CHILD-1"
    assert float(r1.json()["quantity"]) == 5.0
    assert r1.json()["category"] == "gem"
    assert r1.json()["sell_by"] == "piece"

    r2 = await client.get(f"/items/{child_2_id}", headers=h)
    assert r2.status_code == 200
    assert r2.json()["sku"] == "CHILD-2"


@pytest.mark.asyncio
async def test_split_qty_exceeds_parent_rejected(client):
    """Split with child qty > parent qty must be rejected."""
    token = await _token(client)
    h = {"Authorization": f"Bearer {token}"}
    r = await client.post("/items", json={"sku": "SP-OVER", "name": "Small", "quantity": 5, "sell_by": "piece"}, headers=h)
    parent_id = r.json()["id"]
    r = await client.post(f"/items/{parent_id}/split", json={
        "children": [{"sku": "C1", "quantity": 3}, {"sku": "C2", "quantity": 3}]
    }, headers=h)
    assert r.status_code == 422
    assert "exceed" in r.text.lower()


@pytest.mark.asyncio
async def test_split_child_sku_must_be_unique(client):
    """Split child with existing SKU must be rejected."""
    token = await _token(client)
    h = {"Authorization": f"Bearer {token}"}
    await client.post("/items", json={"sku": "EXISTING-SKU", "name": "X", "quantity": 1, "sell_by": "piece"}, headers=h)
    r = await client.post("/items", json={"sku": "SP-EXIST", "name": "Y", "quantity": 10, "sell_by": "piece"}, headers=h)
    parent_id = r.json()["id"]
    r = await client.post(f"/items/{parent_id}/split", json={
        "children": [{"sku": "EXISTING-SKU", "quantity": 4}, {"sku": "NEW-SKU", "quantity": 4}]
    }, headers=h)
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_split_respects_unit_decimals(client):
    """Split of piece item must reject fractional child qty."""
    token = await _token(client)
    h = {"Authorization": f"Bearer {token}"}
    r = await client.post("/items", json={"sku": "SP-DEC", "name": "Z", "quantity": 10, "sell_by": "piece"}, headers=h)
    parent_id = r.json()["id"]
    r = await client.post(f"/items/{parent_id}/split", json={
        "children": [{"sku": "SD-1", "quantity": 3.5}, {"sku": "SD-2", "quantity": 3.5}]
    }, headers=h)
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_patch_sku_to_existing_rejected(client):
    """Changing SKU to an existing one must return 409."""
    token = await _token(client)
    h = {"Authorization": f"Bearer {token}"}
    await client.post("/items", json={"sku": "ORIG-A", "name": "A", "quantity": 1, "sell_by": "piece"}, headers=h)
    r = await client.post("/items", json={"sku": "ORIG-B", "name": "B", "quantity": 1, "sell_by": "piece"}, headers=h)
    item_b_id = r.json()["id"]
    r = await client.patch(f"/items/{item_b_id}", json={"fields_changed": {"sku": {"new": "ORIG-A"}}}, headers=h)
    assert r.status_code == 409
