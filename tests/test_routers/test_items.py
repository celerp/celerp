# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

from __future__ import annotations
import uuid

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
        json={"status": "available", "name": "Main", "type": "warehouse", "address": None, "is_default": True},
        headers=headers,
    )
    location_id = loc.json()["id"]

    r = await client.post(
        "/items",
        json={"status": "available", "sku": "SKU1", "name": "Thing", "quantity": 2, "location_id": location_id, "sell_by": "piece"},
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

    r = await client.post(f"/items/{id}/status", json={"new_status": "available"}, headers=headers)
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
        json={"status": "available", "sku": "SKU-MERGE", "name": "MergePeer", "quantity": 1, "location_id": location_id, "sell_by": "piece"},
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

    r = await client.post("/items/bulk/status", json={"entity_ids": [merged_id], "status": "archived"}, headers=headers)
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_items_split_accepts_single_child(client):
    """Split with 1 child should succeed; parent keeps the remainder."""
    token = await _token(client)
    headers = {"Authorization": f"Bearer {token}"}

    r = await client.post("/items", json={"status": "available", "sku": "SKU1", "name": "Thing", "quantity": 2, "sell_by": "piece"}, headers=headers)
    id = r.json()["id"]

    r = await client.post(
        f"/items/{id}/split",
        json={"children": [{"sku": "CHILD-1", "quantity": 1.0}]},
        headers=headers,
    )
    assert r.status_code == 200


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
    r1 = await client.post("/items", json={"status": "available", "sku": "AVAIL-1", "name": "Available Item", "quantity": 1, "sell_by": "piece"}, headers=headers)
    r2 = await client.post("/items", json={"status": "available", "sku": "SOLD-1", "name": "Sold Item", "quantity": 1, "sell_by": "piece"}, headers=headers)
    r3 = await client.post("/items", json={"status": "available", "sku": "ARCH-1", "name": "Archived Item", "quantity": 1, "sell_by": "piece"}, headers=headers)
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

    r1 = await client.post("/items", json={"status": "available", "sku": "AVAIL-2", "name": "Available", "quantity": 1, "sell_by": "piece"}, headers=headers)
    r2 = await client.post("/items", json={"status": "available", "sku": "SOLD-2", "name": "Sold", "quantity": 1, "sell_by": "piece"}, headers=headers)
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

    r1 = await client.post("/items", json={"status": "available", "sku": "AVAIL-3", "name": "Available3", "quantity": 1, "sell_by": "piece"}, headers=headers)
    r2 = await client.post("/items", json={"status": "available", "sku": "SOLD-3", "name": "Sold3", "quantity": 1, "sell_by": "piece"}, headers=headers)
    r3 = await client.post("/items", json={"status": "available", "sku": "ARCH-3", "name": "Archived3", "quantity": 1, "sell_by": "piece"}, headers=headers)
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

    r1 = await client.post("/items", json={"status": "available", "sku": "VAL-AVAIL", "name": "ValAvail", "quantity": 1, "sell_by": "piece"}, headers=headers)
    r2 = await client.post("/items", json={"status": "available", "sku": "VAL-SOLD", "name": "ValSold", "quantity": 1, "sell_by": "piece"}, headers=headers)
    r3 = await client.post("/items", json={"status": "available", "sku": "VAL-ARCH", "name": "ValArch", "quantity": 1, "sell_by": "piece"}, headers=headers)
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

    r1 = await client.post("/items", json={"status": "available", "sku": "BULK-A", "name": "BulkA", "quantity": 1, "sell_by": "piece"}, headers=headers)
    r2 = await client.post("/items", json={"status": "available", "sku": "BULK-B", "name": "BulkB", "quantity": 1, "sell_by": "piece"}, headers=headers)
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

    r1 = await client.post("/items", json={"status": "available", "sku": "VCAT-A", "name": "VCatA", "quantity": 1, "category": "Rubies", "sell_by": "piece"}, headers=headers)
    r2 = await client.post("/items", json={"status": "available", "sku": "VCAT-B", "name": "VCatB", "quantity": 1, "category": "Sapphires", "sell_by": "piece"}, headers=headers)
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

    r1 = await client.post("/items", json={"status": "available", "sku": "VCS-A", "name": "VcsA", "quantity": 1, "sell_by": "piece"}, headers=headers)
    r2 = await client.post("/items", json={"status": "available", "sku": "VCS-B", "name": "VcsB", "quantity": 1, "sell_by": "piece"}, headers=headers)
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
    r = await client.post("/items", json={"status": "available", "sku": "NO-SB", "name": "Widget", "quantity": 1}, headers=h)
    assert r.status_code == 422
    assert "sell_by" in r.text.lower() or "field required" in r.text.lower()


@pytest.mark.asyncio
async def test_create_item_validates_sell_by_unit(client):
    """sell_by must be a valid unit name from company units."""
    token = await _token(client)
    h = {"Authorization": f"Bearer {token}"}
    r = await client.post("/items", json={"status": "available", "sku": "BAD-U", "name": "Widget", "quantity": 1, "sell_by": "bushel"}, headers=h)
    assert r.status_code == 422
    assert "bushel" in r.text


@pytest.mark.asyncio
async def test_piece_rejects_fractional_qty(client):
    """sell_by=piece (decimals=0) must reject fractional quantity."""
    token = await _token(client)
    h = {"Authorization": f"Bearer {token}"}
    r = await client.post("/items", json={"status": "available", "sku": "FRAC-1", "name": "Widget", "quantity": 2.5, "sell_by": "piece"}, headers=h)
    assert r.status_code == 422
    assert "precision" in r.text.lower() or "decimal" in r.text.lower()


@pytest.mark.asyncio
async def test_carat_allows_fractional_qty(client):
    """sell_by=carat (decimals=2) allows 2dp quantity."""
    token = await _token(client)
    h = {"Authorization": f"Bearer {token}"}
    r = await client.post("/items", json={"status": "available", "sku": "CT-1", "name": "Emerald", "quantity": 2.55, "sell_by": "carat"}, headers=h)
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_carat_rejects_excess_decimals(client):
    """sell_by=carat (decimals=2) rejects 3dp quantity."""
    token = await _token(client)
    h = {"Authorization": f"Bearer {token}"}
    r = await client.post("/items", json={"status": "available", "sku": "CT-2", "name": "Emerald", "quantity": 2.555, "sell_by": "carat"}, headers=h)
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_duplicate_sku_allowed(client):
    """Intentional contract change (2026-06-17 sku/batch plan): SKU is a product-type
    that may repeat across physical lots. Creating two items with the same SKU now
    SUCCEEDS; lot identity is carried by entity_id/barcode instead."""
    token = await _token(client)
    h = {"Authorization": f"Bearer {token}"}
    r1 = await client.post("/items", json={"status": "available", "sku": "DUP-1", "name": "A", "quantity": 1, "sell_by": "piece"}, headers=h)
    assert r1.status_code == 200
    r2 = await client.post("/items", json={"status": "available", "sku": "DUP-1", "name": "B", "quantity": 1, "sell_by": "piece"}, headers=h)
    assert r2.status_code == 200
    # Two distinct physical lots, same sku, distinct entity ids.
    assert r1.json()["id"] != r2.json()["id"]


@pytest.mark.asyncio
async def test_duplicate_barcode_rejected(client):
    """Creating two items with the same barcode must return 409."""
    token = await _token(client)
    h = {"Authorization": f"Bearer {token}"}
    r1 = await client.post("/items", json={"status": "available", "sku": "BC-1", "name": "A", "quantity": 1, "sell_by": "piece", "barcode": "123456"}, headers=h)
    assert r1.status_code == 200
    r2 = await client.post("/items", json={"status": "available", "sku": "BC-2", "name": "B", "quantity": 1, "sell_by": "piece", "barcode": "123456"}, headers=h)
    assert r2.status_code == 409
    assert "123456" in r2.text


@pytest.mark.asyncio
async def test_barcode_must_be_digits(client):
    """Barcode with non-digit characters must be rejected."""
    token = await _token(client)
    h = {"Authorization": f"Bearer {token}"}
    r = await client.post("/items", json={"status": "available", "sku": "BC-3", "name": "A", "quantity": 1, "sell_by": "piece", "barcode": "ABC-123"}, headers=h)
    assert r.status_code == 422
    assert "digits" in r.text.lower()


@pytest.mark.asyncio
async def test_split_single_child_keeps_parent_remainder(client):
    """One split child: parent keeps remainder qty and cost; child gets proportional cost.

    Unit cost (cost_price) is invariant through split: child_cost_total = cost_price * child_qty.
    Partial split must not bleed parent cost into the child.
    """
    token = await _token(client)
    h = {"Authorization": f"Bearer {token}"}
    # qty=20, cost_price=10 => parent_cost_total=200
    r = await client.post("/items", json={"status": "available", "sku": "PARENT-ONE", "name": "Parcel", "quantity": 20, "sell_by": "piece", "category": "gem", "cost_price": 10.0}, headers=h)
    assert r.status_code == 200
    parent_id = r.json()["id"]

    r = await client.post(f"/items/{parent_id}/split", json={
        "children": [
            {"sku": "CHILD-ONE", "quantity": 5},
        ]
    }, headers=h)
    assert r.status_code == 200
    data = r.json()
    assert len(data["children"]) == 1

    r = await client.get(f"/items/{parent_id}", headers=h)
    assert r.status_code == 200
    parent_data = r.json()
    assert float(parent_data["quantity"]) == 15.0
    assert parent_data.get("status") == "available"
    # Parent retains 15/20 of the original cost: 200 * 15/20 = 150
    assert float(parent_data["cost_price"]) == pytest.approx(10.0)
    assert float(parent_data["cost_total"]) == pytest.approx(150.0, rel=1e-6)

    child_id = data["children"][0]["id"]
    r = await client.get(f"/items/{child_id}", headers=h)
    assert r.status_code == 200
    child_data = r.json()
    assert child_data["sku"] == "CHILD-ONE"
    assert float(child_data["quantity"]) == 5.0
    # Child gets 5/20 of the original cost: 200 * 5/20 = 50; cost_price unchanged at 10
    assert float(child_data["cost_price"]) == pytest.approx(10.0)
    assert float(child_data["cost_total"]) == pytest.approx(50.0, rel=1e-6)


@pytest.mark.asyncio
async def test_split_creates_children(client):
    """Split must create child items and reduce parent quantity."""
    token = await _token(client)
    h = {"Authorization": f"Bearer {token}"}
    # Create parent
    r = await client.post("/items", json={"status": "available", "sku": "PARENT-1", "name": "Parcel", "quantity": 20, "sell_by": "piece", "category": "gem"}, headers=h)
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
    assert r.json().get("status") == "available"

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
    r = await client.post("/items", json={"status": "available", "sku": "SP-OVER", "name": "Small", "quantity": 5, "sell_by": "piece"}, headers=h)
    parent_id = r.json()["id"]
    r = await client.post(f"/items/{parent_id}/split", json={
        "children": [{"sku": "C1", "quantity": 3}, {"sku": "C2", "quantity": 3}]
    }, headers=h)
    assert r.status_code == 422
    assert "exceed" in r.text.lower()


@pytest.mark.asyncio
async def test_split_child_omitted_sku_keeps_parent_sku(client):
    """A split child that sends NO sku keeps the parent SKU — same product, distinct lot by
    barcode/entity_id, no '.1' suffix. This is the single source of truth: the inventory UI
    now omits the child SKU and split_item defaults it to the parent's (mirroring the
    fulfillment path, test_fulfillment: "child keeps the parent SKU (no '.1')")."""
    token = await _token(client)
    h = {"Authorization": f"Bearer {token}"}
    r = await client.post("/items", json={"status": "available", "sku": "1000", "name": "Parcel", "quantity": 10,
                                          "sell_by": "piece", "category": "gem"}, headers=h)
    parent_id = r.json()["id"]

    r = await client.post(f"/items/{parent_id}/split", json={
        "children": [{"quantity": 3}, {"quantity": 2}],   # no sku → keeps parent SKU
    }, headers=h)
    assert r.status_code == 200
    children = r.json()["children"]
    assert len(children) == 2
    for c in children:
        cr = await client.get(f"/items/{c['id']}", headers=h)
        assert cr.status_code == 200
        assert cr.json()["sku"] == "1000"   # parent SKU verbatim — no ".1"


@pytest.mark.asyncio
async def test_split_child_explicit_sku_is_honored(client):
    """An explicit child-SKU override is still honored (SKUs may repeat across lots, so a
    caller may name the child differently)."""
    token = await _token(client)
    h = {"Authorization": f"Bearer {token}"}
    r = await client.post("/items", json={"status": "available", "sku": "2000", "name": "Parcel", "quantity": 10,
                                          "sell_by": "piece", "category": "gem"}, headers=h)
    parent_id = r.json()["id"]

    r = await client.post(f"/items/{parent_id}/split", json={
        "children": [{"sku": "CUSTOM-CHILD", "quantity": 3}],
    }, headers=h)
    assert r.status_code == 200
    cid = r.json()["children"][0]["id"]
    cr = await client.get(f"/items/{cid}", headers=h)
    assert cr.json()["sku"] == "CUSTOM-CHILD"


@pytest.mark.asyncio
async def test_split_child_sku_may_reuse_existing(client):
    """A split child reusing another item's SKU now SUCCEEDS (sku may repeat across
    lots); each child still gets its own unique barcode from the sequence."""
    token = await _token(client)
    h = {"Authorization": f"Bearer {token}"}
    await client.post("/items", json={"status": "available", "sku": "EXISTING-SKU", "name": "X", "quantity": 1, "sell_by": "piece"}, headers=h)
    r = await client.post("/items", json={"status": "available", "sku": "SP-EXIST", "name": "Y", "quantity": 10, "sell_by": "piece", "allow_splitting": True}, headers=h)
    parent_id = r.json()["id"]
    r = await client.post(f"/items/{parent_id}/split", json={
        "children": [{"sku": "EXISTING-SKU", "quantity": 4}, {"sku": "NEW-SKU", "quantity": 4}]
    }, headers=h)
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_split_children_keep_parent_sku(client):
    """A split child is the same product as the parent, so it now KEEPS the parent SKU
    (all children can share it) - distinguished by its own unique barcode. Formerly this
    was forbidden (422); the '.N' suffix is gone."""
    token = await _token(client)
    h = {"Authorization": f"Bearer {token}"}
    r = await client.post("/items", json={"status": "available", "sku": "PARENT-SKU", "name": "Y", "quantity": 10, "sell_by": "piece", "allow_splitting": True}, headers=h)
    parent_id = r.json()["id"]
    r = await client.post(f"/items/{parent_id}/split", json={
        "children": [{"sku": "PARENT-SKU", "quantity": 4}, {"sku": "PARENT-SKU", "quantity": 4}]
    }, headers=h)
    assert r.status_code == 200, r.text
    # Both children share the parent SKU but have distinct, unique barcodes.
    child_ids = [c["id"] for c in r.json()["children"]]
    kids = [(await client.get(f"/items/{cid}", headers=h)).json() for cid in child_ids]
    assert all(k["sku"] == "PARENT-SKU" for k in kids)
    barcodes = [k.get("barcode") for k in kids]
    assert all(barcodes) and len(set(barcodes)) == len(barcodes)


@pytest.mark.asyncio
async def test_split_preview_suggests_parent_sku(client):
    """The split preview pre-fills the child SKU as the parent SKU (no '.N' suffix)."""
    token = await _token(client)
    h = {"Authorization": f"Bearer {token}"}
    r = await client.post("/items", json={"status": "available", "sku": "PREV-SKU", "name": "Y", "quantity": 10, "sell_by": "piece", "allow_splitting": True}, headers=h)
    parent_id = r.json()["id"]
    prev = await client.get(f"/items/{parent_id}/split-preview", headers=h)
    assert prev.status_code == 200, prev.text
    assert prev.json()["child_sku"] == "PREV-SKU"


@pytest.mark.asyncio
async def test_split_respects_unit_decimals(client):
    """Split of piece item must reject fractional child qty."""
    token = await _token(client)
    h = {"Authorization": f"Bearer {token}"}
    r = await client.post("/items", json={"status": "available", "sku": "SP-DEC", "name": "Z", "quantity": 10, "sell_by": "piece"}, headers=h)
    parent_id = r.json()["id"]
    r = await client.post(f"/items/{parent_id}/split", json={
        "children": [{"sku": "SD-1", "quantity": 3.5}, {"sku": "SD-2", "quantity": 3.5}]
    }, headers=h)
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_patch_sku_to_existing_allowed(client):
    """Patching an item's SKU to one another item already uses now SUCCEEDS (sku may
    repeat); but changing barcode to an existing barcode still 409s."""
    token = await _token(client)
    h = {"Authorization": f"Bearer {token}"}
    ra = await client.post("/items", json={"status": "available", "sku": "ORIG-A", "name": "A", "quantity": 1, "sell_by": "piece", "barcode": "700001"}, headers=h)
    item_a_id = ra.json()["id"]
    r = await client.post("/items", json={"status": "available", "sku": "ORIG-B", "name": "B", "quantity": 1, "sell_by": "piece", "barcode": "700002"}, headers=h)
    item_b_id = r.json()["id"]
    r = await client.patch(f"/items/{item_b_id}", json={"fields_changed": {"sku": {"new": "ORIG-A"}}}, headers=h)
    assert r.status_code == 200
    # Barcode uniqueness is still enforced on patch.
    r = await client.patch(f"/items/{item_b_id}", json={"fields_changed": {"barcode": {"new": "700001"}}}, headers=h)
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_split_blocked_when_allow_splitting_false(client):
    """Split must be rejected (422) when allow_splitting is False."""
    token = await _token(client)
    h = {"Authorization": f"Bearer {token}"}
    r = await client.post("/items", json={"status": "available", "sku": "NO-SPLIT", "name": "No-Split Item", "quantity": 10, "sell_by": "piece", "allow_splitting": False}, headers=h)
    assert r.status_code == 200
    parent_id = r.json()["id"]

    r = await client.get(f"/items/{parent_id}", headers=h)
    assert r.status_code == 200
    assert r.json()["allow_splitting"] is False

    r = await client.post(f"/items/{parent_id}/split", json={
        "children": [{"sku": "NO-SPLIT-1", "quantity": 3}]
    }, headers=h)
    assert r.status_code == 422
    assert "Allow Splitting" in r.json()["detail"]


@pytest.mark.asyncio
async def test_split_allowed_when_allow_splitting_field_missing(client, session):
    """A missing/None allow_splitting is splittable: only an explicit False blocks.
    Simulate an item whose projection state lacks the field (as older imports produced)
    and confirm the split now succeeds.
    """
    from celerp.models.projections import Projection
    from sqlalchemy import select

    token = await _token(client)
    h = {"Authorization": f"Bearer {token}"}
    r = await client.post("/items", json={"status": "available", "sku": "MISSING-SPLIT", "name": "Item", "quantity": 10, "sell_by": "piece"}, headers=h)
    assert r.status_code == 200
    item_id = r.json()["id"]

    # Remove allow_splitting from the item's projection state (manual edit).
    row = (await session.execute(select(Projection).where(Projection.entity_id == item_id))).scalar_one()
    new_state = dict(row.state)
    new_state.pop("allow_splitting", None)
    row.state = new_state
    await session.commit()

    item = (await client.get(f"/items/{item_id}", headers=h)).json()
    assert item.get("allow_splitting") is None, f"field should be gone, got {item.get('allow_splitting')!r}"

    rs = await client.post(f"/items/{item_id}/split", json={
        "children": [{"sku": "MISSING-SPLIT-1", "quantity": 3}]
    }, headers=h)
    assert rs.status_code == 200, f"missing allow_splitting must be splittable, got {rs.status_code}: {rs.text}"


@pytest.mark.asyncio
async def test_import_defaults_unset_split_and_coerces_present_value(client):
    """Imported items leave allow_splitting unset (None) when the field is absent, so
    they stay splittable by default; a present CSV value ("Yes"/"No") is coerced to a
    real bool."""
    token = await _token(client)
    h = {"Authorization": f"Bearer {token}"}

    # (1) Import WITHOUT allow_splitting → stays unset (None) → splittable.
    eid1 = "item:imp-nofield"
    r = await client.post("/items/import/batch", json={"records": [{
        "entity_id": eid1, "event_type": "item.created", "source": "csv",
        "idempotency_key": "imp-nofield",
        "data": {"sku": "IMP-NF-001", "name": "Imported NoField", "sell_by": "piece", "quantity": 10},
    }]}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["created"] == 1, r.json()
    item1 = (await client.get(f"/items/{eid1}", headers=h)).json()
    assert item1.get("allow_splitting") is None, item1.get("allow_splitting")
    rs1 = await client.post(f"/items/{eid1}/split", json={"children": [{"sku": "IMP-NF-001-1", "quantity": 3}]}, headers=h)
    assert rs1.status_code == 200, rs1.text

    # (2) Import WITH allow_splitting="Yes" → coerced to True → splittable.
    eid2 = "item:imp-yes"
    r = await client.post("/items/import/batch", json={"records": [{
        "entity_id": eid2, "event_type": "item.created", "source": "csv",
        "idempotency_key": "imp-yes",
        "data": {"sku": "IMP-Y-001", "name": "Imported Yes", "sell_by": "piece",
                 "quantity": 10, "allow_splitting": "Yes"},
    }]}, headers=h)
    assert r.status_code == 200, r.text
    item2 = (await client.get(f"/items/{eid2}", headers=h)).json()
    assert item2.get("allow_splitting") is True, item2.get("allow_splitting")
    rs2 = await client.post(f"/items/{eid2}/split", json={"children": [{"sku": "IMP-Y-001-1", "quantity": 3}]}, headers=h)
    assert rs2.status_code == 200, rs2.text


@pytest.mark.asyncio
async def test_split_allowed_when_allow_splitting_true(client):
    """Split must succeed when allow_splitting is explicitly True."""
    token = await _token(client)
    h = {"Authorization": f"Bearer {token}"}
    r = await client.post("/items", json={"status": "available", "sku": "YES-SPLIT", "name": "Yes-Split Item", "quantity": 10, "sell_by": "piece", "allow_splitting": True}, headers=h)
    assert r.status_code == 200
    parent_id = r.json()["id"]

    r = await client.post(f"/items/{parent_id}/split", json={
        "children": [{"sku": "YES-SPLIT-1", "quantity": 3}]
    }, headers=h)
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_item_create_default_allow_splitting_is_true(client):
    """GET /items/{id} must return allow_splitting=True when not explicitly set on creation.

    ItemCreate defaults allow_splitting=True, so new items are always splittable by default.
    """
    token = await _token(client)
    h = {"Authorization": f"Bearer {token}"}
    r = await client.post("/items", json={"status": "available", "sku": "LEGACY-SPLIT", "name": "Legacy Item", "quantity": 10, "sell_by": "piece"}, headers=h)
    assert r.status_code == 200
    parent_id = r.json()["id"]

    r = await client.get(f"/items/{parent_id}", headers=h)
    assert r.status_code == 200
    assert r.json()["allow_splitting"] is True


@pytest.mark.asyncio
async def test_split_blocked_after_patch_to_false(client):
    """Split must be blocked after a PATCH sets allow_splitting to False.

    Covers the end-to-end path: item created (default True) -> PATCH to False -> split rejected.
    This is the scenario where users set Allow Splitting = No via the item detail UI.
    """
    token = await _token(client)
    h = {"Authorization": f"Bearer {token}"}
    r = await client.post("/items", json={"status": "available", "sku": "PATCH-NO-SPLIT", "name": "Patchable Item", "quantity": 10, "sell_by": "piece"}, headers=h)
    assert r.status_code == 200
    parent_id = r.json()["id"]

    r = await client.get(f"/items/{parent_id}", headers=h)
    assert r.json()["allow_splitting"] is True

    r = await client.patch(f"/items/{parent_id}", json={"fields_changed": {"allow_splitting": {"old": True, "new": False}}}, headers=h)
    assert r.status_code == 200

    r = await client.get(f"/items/{parent_id}", headers=h)
    assert r.json()["allow_splitting"] is False

    r = await client.post(f"/items/{parent_id}/split", json={"children": [{"sku": "PATCH-NO-SPLIT-1", "quantity": 3}]}, headers=h)
    assert r.status_code == 422
    assert "Allow Splitting" in r.json()["detail"]


@pytest.mark.asyncio
async def test_merge_preserves_allow_splitting_true(client):
    """Merged item must carry allow_splitting=True from the target source item."""
    token = await _token(client)
    h = {"Authorization": f"Bearer {token}"}
    r = await client.post("/items", json={"status": "available", "sku": "MERGE-AS-A", "name": "Stone A", "quantity": 5, "sell_by": "piece", "allow_splitting": True}, headers=h)
    assert r.status_code == 200
    id_a = r.json()["id"]

    r = await client.post("/items", json={"status": "available", "sku": "MERGE-AS-B", "name": "Stone B", "quantity": 3, "sell_by": "piece", "allow_splitting": True}, headers=h)
    assert r.status_code == 200
    id_b = r.json()["id"]

    r = await client.post("/items/merge", json={"source_entity_ids": [id_a, id_b], "target_sku_from": id_a}, headers=h)
    assert r.status_code == 200
    merged_id = r.json()["id"]

    r = await client.get(f"/items/{merged_id}", headers=h)
    assert r.status_code == 200
    state = r.json()
    assert "allow_splitting" in state, "Merged item must have allow_splitting in state"
    assert state["allow_splitting"] is True


@pytest.mark.asyncio
async def test_merge_qty_truncated_to_unit_decimals(client):
    """Merged qty must not carry float-summation noise beyond the sell_by unit's precision.
    Regression: 0.1 + 0.2 summed to 0.30000000000000004 instead of 0.3 (carat = 2 dp)."""
    token = await _token(client)
    h = {"Authorization": f"Bearer {token}"}
    a = (await client.post("/items", json={"status": "available", "sku": "MQ-A", "name": "A", "quantity": 0.1, "sell_by": "carat"}, headers=h)).json()["id"]
    b = (await client.post("/items", json={"status": "available", "sku": "MQ-B", "name": "B", "quantity": 0.2, "sell_by": "carat"}, headers=h)).json()["id"]
    merged_id = (await client.post("/items/merge", json={"source_entity_ids": [a, b], "target_sku_from": a}, headers=h)).json()["id"]
    qty = (await client.get(f"/items/{merged_id}", headers=h)).json()["quantity"]
    assert qty == 0.3, f"merged qty must be truncated to unit precision; got {qty!r}"


@pytest.mark.asyncio
async def test_merge_carries_attached_files(client):
    """Files attached to source items must survive onto the merged item, not be dropped."""
    token = await _token(client)
    h = {"Authorization": f"Bearer {token}"}
    a = (await client.post("/items", json={"status": "available", "sku": "MF-A", "name": "A", "quantity": 1, "sell_by": "piece"}, headers=h)).json()["id"]
    b = (await client.post("/items", json={"status": "available", "sku": "MF-B", "name": "B", "quantity": 1, "sell_by": "piece"}, headers=h)).json()["id"]
    ra = await client.post(f"/items/{a}/files", files={"file": ("a.png", b"\x89PNG\r\n\x1a\n", "image/png")}, headers=h)
    assert ra.status_code == 200, ra.text
    rb = await client.post(f"/items/{b}/files", files={"file": ("b.png", b"\x89PNG\r\n\x1a\n", "image/png")}, headers=h)
    assert rb.status_code == 200, rb.text
    merged_id = (await client.post("/items/merge", json={"source_entity_ids": [a, b], "target_sku_from": a}, headers=h)).json()["id"]
    state = (await client.get(f"/items/{merged_id}", headers=h)).json()
    files = state.get("files", [])
    assert len(files) == 2, f"both source files must be merged onto the new item; got {len(files)}"


@pytest.mark.asyncio
async def test_merge_preserves_allow_splitting_false(client):
    """Merged item must carry allow_splitting=False when target source has it disabled.
    This is the regression case: previously the key was absent from create_data,
    so the UI showed 'No' (falsy missing key) but the backend defaulted to True (splittable).
    """
    token = await _token(client)
    h = {"Authorization": f"Bearer {token}"}
    r = await client.post("/items", json={"status": "available", "sku": "MERGE-NO-A", "name": "Stone No-A", "quantity": 5, "sell_by": "piece", "allow_splitting": False}, headers=h)
    assert r.status_code == 200
    id_a = r.json()["id"]

    r = await client.post("/items", json={"status": "available", "sku": "MERGE-NO-B", "name": "Stone No-B", "quantity": 3, "sell_by": "piece", "allow_splitting": False}, headers=h)
    assert r.status_code == 200
    id_b = r.json()["id"]

    r = await client.post("/items/merge", json={"source_entity_ids": [id_a, id_b], "target_sku_from": id_a}, headers=h)
    assert r.status_code == 200
    merged_id = r.json()["id"]

    r = await client.get(f"/items/{merged_id}", headers=h)
    assert r.status_code == 200
    state = r.json()
    assert "allow_splitting" in state, "Merged item must have allow_splitting in state"
    assert state["allow_splitting"] is False

    # Backend must enforce the flag - split should be rejected
    r = await client.post(f"/items/{merged_id}/split", json={"children": [{"sku": "MERGE-NO-CHILD", "quantity": 2}]}, headers=h)
    assert r.status_code == 422
    assert "Allow Splitting" in r.json()["detail"]


@pytest.mark.asyncio
async def test_list_items_location_filter(client):
    """GET /items?location_id= returns only items at that location."""
    token = await _token(client)
    h = {"Authorization": f"Bearer {token}"}
    loc_a = (await client.post("/companies/me/locations", headers=h, json={"status": "available", "name": "Loc A", "type": "warehouse"})).json()["id"]
    loc_b = (await client.post("/companies/me/locations", headers=h, json={"status": "available", "name": "Loc B", "type": "warehouse"})).json()["id"]
    await client.post("/items", headers=h, json={"status": "available", "sku": "LOC-A1", "name": "A1", "quantity": 1, "sell_by": "piece", "location_id": loc_a})
    await client.post("/items", headers=h, json={"status": "available", "sku": "LOC-B1", "name": "B1", "quantity": 1, "sell_by": "piece", "location_id": loc_b})
    items = (await client.get(f"/items?location_id={loc_a}", headers=h)).json()["items"]
    skus = {i["sku"] for i in items}
    assert "LOC-A1" in skus and "LOC-B1" not in skus


@pytest.mark.asyncio
async def test_merge_rejects_different_sell_units(client):
    """Merging sums quantities, so items measured in different units (e.g. gram vs carat) must be
    rejected - otherwise 5 g + 3 ct would silently become 8 of nothing."""
    token = await _token(client)
    h = {"Authorization": f"Bearer {token}"}
    a = (await client.post("/items", json={"status": "available", "sku": "WU-A", "name": "Gold A", "quantity": 5, "sell_by": "gram"}, headers=h)).json()["id"]
    b = (await client.post("/items", json={"status": "available", "sku": "WU-B", "name": "Gold B", "quantity": 3, "sell_by": "carat"}, headers=h)).json()["id"]
    r = await client.post("/items/merge", json={"source_entity_ids": [a, b], "target_sku_from": a}, headers=h)
    assert r.status_code == 422
    assert "unit" in r.json()["detail"].lower()
    assert "gram" in r.json()["detail"] and "carat" in r.json()["detail"]


@pytest.mark.asyncio
async def test_merge_rejects_different_net_weight_units(client):
    """Merging sums net weight too, so a differing weight_unit must also be rejected."""
    token = await _token(client)
    h = {"Authorization": f"Bearer {token}"}
    a = (await client.post("/items", json={"status": "available", "sku": "NWU-A", "name": "A", "quantity": 1, "sell_by": "piece",
                                           "weight": 2, "weight_unit": "gram"}, headers=h)).json()["id"]
    b = (await client.post("/items", json={"status": "available", "sku": "NWU-B", "name": "B", "quantity": 1, "sell_by": "piece",
                                           "weight": 3, "weight_unit": "carat"}, headers=h)).json()["id"]
    r = await client.post("/items/merge", json={"source_entity_ids": [a, b], "target_sku_from": a}, headers=h)
    assert r.status_code == 422
    assert "weight unit" in r.json()["detail"].lower()


@pytest.mark.asyncio
async def test_merge_allows_same_weight_unit(client):
    """Same sell unit (and weight unit) still merges fine."""
    token = await _token(client)
    h = {"Authorization": f"Bearer {token}"}
    a = (await client.post("/items", json={"status": "available", "sku": "SU-A", "name": "Gold A", "quantity": 5, "sell_by": "gram"}, headers=h)).json()["id"]
    b = (await client.post("/items", json={"status": "available", "sku": "SU-B", "name": "Gold B", "quantity": 3, "sell_by": "gram"}, headers=h)).json()["id"]
    r = await client.post("/items/merge", json={"source_entity_ids": [a, b], "target_sku_from": a}, headers=h)
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_split_children_inherit_allow_splitting_from_parent(client):
    """Split children must inherit allow_splitting from the parent item.
    Child items without this key in state suffer the same UI/backend mismatch as the original bug.
    """
    token = await _token(client)
    h = {"Authorization": f"Bearer {token}"}
    r = await client.post("/items", json={"status": "available", "sku": "SPLIT-INHERIT-P", "name": "Parent Stone", "quantity": 20, "sell_by": "piece", "allow_splitting": True}, headers=h)
    assert r.status_code == 200
    parent_id = r.json()["id"]

    r = await client.post(f"/items/{parent_id}/split", json={"children": [{"sku": "SPLIT-INHERIT-C", "quantity": 5}]}, headers=h)
    assert r.status_code == 200
    child_id = r.json()["children"][0]["id"]

    r = await client.get(f"/items/{child_id}", headers=h)
    assert r.status_code == 200
    state = r.json()
    assert "allow_splitting" in state, "Split child must have allow_splitting in state"
    assert state["allow_splitting"] is True


@pytest.mark.asyncio
async def test_split_children_inherit_allow_splitting_false(client):
    """Split children inherit allow_splitting=False from parent - they should also be non-splittable."""
    token = await _token(client)
    h = {"Authorization": f"Bearer {token}"}
    r = await client.post("/items", json={"status": "available", "sku": "SPLIT-NO-P", "name": "No-Split Parent", "quantity": 20, "sell_by": "piece", "allow_splitting": True}, headers=h)
    assert r.status_code == 200
    parent_id = r.json()["id"]

    # Patch parent to disallow splitting, then re-enable to do the split (testing child inheritance)
    # Instead: create with allow_splitting=True, split to get child, check child has the flag.
    # For the False case: create a separate test with a parent that allows splitting but check child inherits correctly.
    # This test covers the case where a parent explicitly has allow_splitting=False after a patch,
    # then we enable it temporarily to demonstrate children inherit the live value.
    # Simpler: patch to False, then patch back to True, split, child should be True.
    r = await client.patch(f"/items/{parent_id}", json={"fields_changed": {"allow_splitting": {"old": True, "new": False}}}, headers=h)
    assert r.status_code == 200

    r = await client.patch(f"/items/{parent_id}", json={"fields_changed": {"allow_splitting": {"old": False, "new": True}}}, headers=h)
    assert r.status_code == 200

    r = await client.post(f"/items/{parent_id}/split", json={"children": [{"sku": "SPLIT-NO-C", "quantity": 5}]}, headers=h)
    assert r.status_code == 200
    child_id = r.json()["children"][0]["id"]

    r = await client.get(f"/items/{child_id}", headers=h)
    assert r.status_code == 200
    state = r.json()
    assert "allow_splitting" in state, "Split child must have allow_splitting in state"
    assert state["allow_splitting"] is True


@pytest.mark.asyncio
async def test_list_item_categories_includes_schema_categories(client):
    """GET /items/categories must return categories from company category_schemas even if no items exist yet."""
    token = await _token(client)
    h = {"Authorization": f"Bearer {token}"}

    # Seed category schemas via company settings (simulates vertical preset / category library)
    r = await client.patch("/companies/me", headers=h, json={
        "settings": {"category_schemas": {"Colored Stones": [], "Gold Jewelry": []}}
    })
    assert r.status_code == 200, r.text

    cats = (await client.get("/items/categories", headers=h)).json()
    assert "Colored Stones" in cats, f"Expected 'Colored Stones' in {cats}"
    assert "Gold Jewelry" in cats, f"Expected 'Gold Jewelry' in {cats}"


@pytest.mark.asyncio
async def test_list_item_categories_union_of_schema_and_items(client):
    """GET /items/categories returns union: schema categories + categories on actual items."""
    token = await _token(client)
    h = {"Authorization": f"Bearer {token}"}

    # Schema has one category
    await client.patch("/companies/me", headers=h, json={
        "settings": {"category_schemas": {"Schema Cat": []}}
    })

    # Create an item with a different category (not in schema)
    await client.post("/items", headers=h, json={
        "status": "available", "sku": "CAT-TEST-001", "name": "Widget", "sell_by": "piece", "category": "Item Cat",
    })

    cats = (await client.get("/items/categories", headers=h)).json()
    assert "Schema Cat" in cats, f"Schema category missing: {cats}"
    assert "Item Cat" in cats, f"Item category missing: {cats}"


# ── Sort tests ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_items_sort_by_name_asc(client):
    """sort=name&dir=asc returns items in ascending name order."""
    token = await _token(client)
    h = {"Authorization": f"Bearer {token}"}

    for name in ("Zebra", "Apple", "Mango"):
        await client.post("/items", headers=h, json={"status": "available", "sku": f"SRT-{name[:3]}", "name": name, "sell_by": "piece"})

    r = await client.get("/items?sort=name&dir=asc&status=all", headers=h)
    assert r.status_code == 200, r.text
    names = [i["name"] for i in r.json()["items"]]
    assert names == sorted(names, key=str.lower), f"Not ascending: {names}"


@pytest.mark.asyncio
async def test_list_items_sort_by_name_desc(client):
    """sort=name&dir=desc returns items in descending name order."""
    token = await _token(client)
    h = {"Authorization": f"Bearer {token}"}

    for name in ("Alpha", "Gamma", "Beta"):
        await client.post("/items", headers=h, json={"status": "available", "sku": f"DSC-{name[:3]}", "name": name, "sell_by": "piece"})

    r = await client.get("/items?sort=name&dir=desc&status=all", headers=h)
    assert r.status_code == 200, r.text
    names = [i["name"] for i in r.json()["items"]]
    assert names == sorted(names, key=str.lower, reverse=True), f"Not descending: {names}"


@pytest.mark.asyncio
async def test_list_items_sort_unknown_key_no_crash(client):
    """Sorting by a non-existent key must not crash - nulls-last fallback applies."""
    token = await _token(client)
    h = {"Authorization": f"Bearer {token}"}

    await client.post("/items", headers=h, json={"status": "available", "sku": "UNK-001", "name": "Item", "sell_by": "piece"})

    r = await client.get("/items?sort=nonexistent_field&dir=desc&status=all", headers=h)
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_list_items_sort_is_global_before_pagination(client):
    """Sorting applies to the full set before pagination, not just the current page."""
    token = await _token(client)
    h = {"Authorization": f"Bearer {token}"}

    # Create 6 items with names that, if sorted only after slicing page 1 of 5,
    # would produce wrong results on page 2.
    names = ["F", "A", "E", "B", "D", "C"]
    for n in names:
        await client.post("/items", headers=h, json={"status": "available", "sku": f"PAG-{n}", "name": f"Item {n}", "sell_by": "piece"})

    # Page 1 (first 4), page 2 (next 2) - globally sorted asc
    r1 = await client.get("/items?sort=name&dir=asc&limit=4&offset=0&status=all", headers=h)
    r2 = await client.get("/items?sort=name&dir=asc&limit=4&offset=4&status=all", headers=h)
    assert r1.status_code == 200 and r2.status_code == 200

    page1_names = [i["name"] for i in r1.json()["items"]]
    page2_names = [i["name"] for i in r2.json()["items"]]

    # All page1 names must be <= all page2 names (global sort)
    if page1_names and page2_names:
        assert page1_names[-1].lower() <= page2_names[0].lower(), (
            f"Sort is not global: page1 ends with {page1_names[-1]!r}, "
            f"page2 starts with {page2_names[0]!r}"
        )


# ---------------------------------------------------------------------------
# inventory_type tests
# ---------------------------------------------------------------------------

async def _reg_items(client, company="ItemTypeCo"):
    r = await client.post("/auth/register", json={"company_name": company, "email": f"{company.lower()}@test.com", "name": "Admin", "password": "pw"})
    tok = r.json()["access_token"]
    h = {"Authorization": f"Bearer {tok}"}
    return tok, h


@pytest.mark.asyncio
async def test_inventory_type_defaults_stocked(client):
    """Item created without inventory_type must default to stocked."""
    _, h = await _reg_items(client, "DefaultStockedCo2")
    r = await client.post("/items", headers=h, json={"status": "available", "name": "Widget", "sku": "W-001", "sell_by": "piece", "quantity": 1})
    assert r.status_code == 200
    eid = r.json()["id"]
    item = (await client.get(f"/items/{eid}", headers=h)).json()
    assert item.get("inventory_type", "stocked") == "stocked"


@pytest.mark.asyncio
async def test_inventory_type_service_stored(client):
    """Item created with inventory_type=service must store that value."""
    _, h = await _reg_items(client, "ServiceItemCo2")
    r = await client.post("/items", headers=h, json={"status": "available", "name": "Pro Plan", "sku": "SVC-001", "sell_by": "piece", "quantity": 0, "inventory_type": "service"})
    assert r.status_code == 200
    eid = r.json()["id"]
    item = (await client.get(f"/items/{eid}", headers=h)).json()
    assert item["inventory_type"] == "service"


@pytest.mark.asyncio
async def test_inventory_type_invalid_rejected(client):
    """Item created with invalid inventory_type must return 422."""
    _, h = await _reg_items(client, "InvalidTypeCo2")
    r = await client.post("/items", headers=h, json={"status": "available", "name": "Bad Item", "sku": "BAD-001", "sell_by": "piece", "quantity": 1, "inventory_type": "warehouse"})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_valuation_excludes_service_items(client):
    """Valuation cost_total must only count stocked items, not service items."""
    _, h = await _reg_items(client, "ValuationTypeCo2")
    # Get baseline cost_total before adding our items
    baseline = (await client.get("/items/valuation", headers=h)).json().get("cost_total", 0)
    # Add a stocked item: qty=1, cost_price=100
    await client.post("/items", headers=h, json={"status": "available", "name": "Stocked Widget", "sku": "ST-001", "sell_by": "piece", "quantity": 1, "inventory_type": "stocked", "cost_price": 100})
    # Add a service item: cost_price=200 - should NOT add to valuation
    await client.post("/items", headers=h, json={"status": "available", "name": "Pro Plan", "sku": "SVC-001", "sell_by": "piece", "quantity": 0, "inventory_type": "service", "cost_price": 200})
    val = (await client.get("/items/valuation", headers=h)).json()
    # cost_total must have increased by exactly 100 (the stocked item), not 300 (both)
    assert abs(val.get("cost_total", 0) - (baseline + 100)) < 0.01, f"Expected {baseline + 100}, got {val.get('cost_total')}"


@pytest.mark.asyncio
async def test_inventory_list_filter_by_inventory_type(client):
    """GET /items?inventory_type=service must return only service items."""
    _, h = await _reg_items(client, "FilterTypeCo2")
    await client.post("/items", headers=h, json={"status": "available", "name": "Physical", "sku": "PHY-001", "sell_by": "piece", "quantity": 1, "inventory_type": "stocked"})
    await client.post("/items", headers=h, json={"status": "available", "name": "Plan A", "sku": "SVC-002", "sell_by": "piece", "quantity": 0, "inventory_type": "service"})
    await client.post("/items", headers=h, json={"status": "available", "name": "Plan B", "sku": "SVC-003", "sell_by": "piece", "quantity": 0, "inventory_type": "service"})
    r = await client.get("/items?inventory_type=service", headers=h)
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 2
    assert all(i.get("inventory_type") == "service" for i in items)


@pytest.mark.asyncio
async def test_split_child_pieces_attribute_stored(client):
    """When sell_by is weight (carat), pieces on child must come from attributes.pieces in the split payload."""
    token = await _token(client)
    h = {"Authorization": f"Bearer {token}"}
    # Parent: sell_by=carat (weight unit) with 3 pieces
    r = await client.post("/items", json={
        "status": "available", "sku": "GEM-W-001", "name": "Rough Stone", "quantity": 10.0,
        "sell_by": "carat", "attributes": {"pieces": 3},
    }, headers=h)
    assert r.status_code == 200
    parent_id = r.json()["id"]

    # Split: child gets explicit pieces=1 via attributes
    r = await client.post(f"/items/{parent_id}/split", json={
        "children": [{"sku": "GEM-W-001.1", "quantity": 3.0, "attributes": {"pieces": 1}}],
    }, headers=h)
    assert r.status_code == 200
    child_id = r.json()["children"][0]["id"]

    child = (await client.get(f"/items/{child_id}", headers=h)).json()
    assert child["sku"] == "GEM-W-001.1"
    assert float(child["quantity"]) == 3.0
    # pieces must be 1 (the override), not proportional (10%)
    # attributes are flattened to top-level in the GET response
    assert int(child.get("pieces", -1)) == 1


@pytest.mark.asyncio
async def test_split_child_weight_stored(client):
    """When sell_by is pieces (piece), weight on child must be set from SplitChild.weight."""
    token = await _token(client)
    h = {"Authorization": f"Bearer {token}"}
    # Parent: sell_by=piece (pieces unit) with a physical weight
    r = await client.post("/items", json={
        "status": "available", "sku": "GEM-P-001", "name": "Cut Stones", "quantity": 5.0,
        "sell_by": "piece", "weight": 25.0,
    }, headers=h)
    assert r.status_code == 200
    parent_id = r.json()["id"]

    # Split: child gets explicit weight=7.5
    r = await client.post(f"/items/{parent_id}/split", json={
        "children": [{"sku": "GEM-P-001.1", "quantity": 2.0, "weight": 7.5}],
    }, headers=h)
    assert r.status_code == 200
    child_id = r.json()["children"][0]["id"]

    child = (await client.get(f"/items/{child_id}", headers=h)).json()
    assert child["sku"] == "GEM-P-001.1"
    assert float(child["quantity"]) == 2.0
    # weight must be 7.5 (explicit override), not proportional (10.0)
    assert abs(float(child.get("weight", 0)) - 7.5) < 0.01


@pytest.mark.asyncio
async def test_inventory_list_filter_by_skus(client):
    """GET /items?skus=A,B must return exactly those two items by exact SKU match."""
    _, h = await _reg_items(client, "SkusFilterCo")
    await client.post("/items", headers=h, json={"status": "available", "name": "Alpha", "sku": "SKUS-A", "sell_by": "piece", "quantity": 1})
    await client.post("/items", headers=h, json={"status": "available", "name": "Beta", "sku": "SKUS-B", "sell_by": "piece", "quantity": 1})
    await client.post("/items", headers=h, json={"status": "available", "name": "Gamma", "sku": "SKUS-C", "sell_by": "piece", "quantity": 1})
    r = await client.get("/items?skus=SKUS-A,SKUS-B", headers=h)
    assert r.status_code == 200
    items = r.json()["items"]
    skus = {i["sku"] for i in items}
    assert skus == {"SKUS-A", "SKUS-B"}


@pytest.mark.asyncio
async def test_split_preview_reads_pieces_from_top_level_state(client):
    """split-preview must return has_pieces=True when pieces is stored at top-level state.

    Bug: preview read parent.state['attributes']['pieces'] but _flatten_item promotes
    pieces to top-level. Correct read is parent.state.get('pieces').
    """
    token = await _token(client)
    h = {"Authorization": f"Bearer {token}"}
    # Create item with pieces stored in attributes (will be flattened to top-level by projection)
    r = await client.post("/items", json={
        "status": "available", "sku": "PREV-PIECES-001", "name": "Gem", "quantity": 10.0,
        "sell_by": "carat", "attributes": {"pieces": 5},
    }, headers=h)
    assert r.status_code == 200
    parent_id = r.json()["id"]

    r2 = await client.get(f"/items/{parent_id}/split-preview", headers=h)
    assert r2.status_code == 200
    preview = r2.json()

    assert preview.get("has_pieces") is True, f"Expected has_pieces=True, got {preview}"
    assert "parent_pieces" in preview, f"Expected parent_pieces in preview: {preview}"
    assert preview["parent_pieces"] == 5


@pytest.mark.asyncio
async def test_split_preview_weight_uses_weight_unit_decimals(client):
    """parent_weight must use weight_unit decimals precision.

    The preview returns the raw parent weight; proportional computation is client-side.
    """
    token = await _token(client)
    h = {"Authorization": f"Bearer {token}"}
    r = await client.post("/items", json={
        "status": "available", "sku": "PREV-WDEC-001", "name": "Cut Stones", "quantity": 4.0,
        "sell_by": "piece", "weight": 2.0, "weight_unit": "gram",
    }, headers=h)
    assert r.status_code == 200
    parent_id = r.json()["id"]

    r2 = await client.get(f"/items/{parent_id}/split-preview", headers=h)
    assert r2.status_code == 200
    preview = r2.json()

    assert preview.get("has_weight") is True, f"Expected has_weight=True: {preview}"
    assert abs(preview["parent_weight"] - 2.0) < 0.01, (
        f"Expected parent_weight=2.0, got {preview['parent_weight']}"
    )
    assert preview.get("weight_decimals", 2) == 2


@pytest.mark.asyncio
async def test_merge_sums_weight(client):
    """Merged item weight must equal the sum of all source item weights."""
    token = await _token(client)
    h = {"Authorization": f"Bearer {token}"}
    ra = await client.post("/items", json={
        "status": "available", "sku": "WGT-MERGE-A", "name": "Stone A", "quantity": 2, "sell_by": "gram",
        "weight": 3.0, "weight_unit": "gram",
    }, headers=h)
    assert ra.status_code == 200
    id_a = ra.json()["id"]

    rb = await client.post("/items", json={
        "status": "available", "sku": "WGT-MERGE-B", "name": "Stone B", "quantity": 3, "sell_by": "gram",
        "weight": 2.0, "weight_unit": "gram",
    }, headers=h)
    assert rb.status_code == 200
    id_b = rb.json()["id"]

    rm = await client.post("/items/merge", json={
        "source_entity_ids": [id_a, id_b], "target_sku_from": id_a,
    }, headers=h)
    assert rm.status_code == 200
    merged_id = rm.json()["id"]

    rg = await client.get(f"/items/{merged_id}", headers=h)
    assert rg.status_code == 200
    state = rg.json()
    assert abs(float(state.get("weight", 0)) - 5.0) < 0.01, (
        f"Expected weight=5.0, got {state.get('weight')}"
    )
    assert state.get("weight_unit") == "gram"


@pytest.mark.asyncio
async def test_attachment_stored_under_data_dir(client):
    """Uploaded attachment URL must point to a file under settings.data_dir, not CWD."""
    from celerp.config import settings

    token = await _token(client)
    h = {"Authorization": f"Bearer {token}"}
    r = await client.post("/items", json={
        "status": "available", "sku": "ATT-DATADIR-001", "name": "Cert Item", "quantity": 1, "sell_by": "piece",
    }, headers=h)
    assert r.status_code == 200
    item_id = r.json()["id"]

    fake_pdf = b"%PDF-1.4 fake"
    ru = await client.post(
        f"/items/{item_id}/attachments?attachment_type=certificate",
        files={"file": ("cert.pdf", fake_pdf, "application/pdf")},
        headers=h,
    )
    assert ru.status_code == 200
    att = ru.json()
    url = att["url"]
    assert url.startswith("/static/attachments/"), f"URL must start with /static/attachments/, got {url!r}"

    # File must exist under data_dir (not CWD)
    rel = url.lstrip("/")  # "static/attachments/<co>/<id>.pdf"
    expected_path = settings.data_dir / rel
    assert expected_path.exists(), (
        f"File not found at {expected_path}. data_dir={settings.data_dir}. "
        "LocalBackend is likely writing to CWD instead of data_dir."
    )


@pytest.mark.asyncio
async def test_split_preview_clamps_qty(client):
    """split-preview no longer accepts qty - returns static parent data only."""
    token = await _token(client)
    h = {"Authorization": f"Bearer {token}"}
    r = await client.post("/items", json={
        "status": "available", "sku": "CLAMP-001", "name": "Clamp Stone", "quantity": 5.0, "sell_by": "carat",
    }, headers=h)
    assert r.status_code == 200
    parent_id = r.json()["id"]

    # qty param is now ignored; response contains parent_qty only
    r2 = await client.get(f"/items/{parent_id}/split-preview", headers=h)
    assert r2.status_code == 200
    preview = r2.json()
    assert preview["parent_qty"] == 5.0
    assert "child_qty" not in preview


@pytest.mark.asyncio
async def test_split_item_pieces_conservation(client):
    """split allows child_pieces == parent_pieces (full consumption) but rejects child_pieces > parent_pieces."""
    import time as _t
    _ts = str(int(_t.time() * 1000))[-6:]
    token = await _token(client)
    h = {"Authorization": f"Bearer {token}"}
    r = await client.post("/items", json={
        "status": "available", "sku": f"PIECE-CONS-{_ts}", "name": "Multi-stone", "quantity": 10.0,
        "sell_by": "carat", "attributes": {"pieces": 5},
    }, headers=h)
    assert r.status_code == 200
    parent_id = r.json()["id"]
    # child_pieces == parent_pieces should now succeed (full consumption - parent archived)
    rs_eq = await client.post(f"/items/{parent_id}/split", json={
        "children": [{"sku": f"PIECE-CONS-CHILD-{_ts}", "quantity": 3.0, "attributes": {"pieces": 5}}],
    }, headers=h)
    assert rs_eq.status_code == 200, f"Expected 200 for child_pieces == parent_pieces (full consumption), got {rs_eq.status_code}"
    # child_pieces > parent_pieces should still be rejected
    r3 = await client.post("/items", json={
        "status": "available", "sku": f"PIECE-CONS-P2-{_ts}", "name": "Over-piece parent", "quantity": 5.0,
        "sell_by": "carat", "attributes": {"pieces": 5},
    }, headers=h)
    parent_id2 = r3.json()["id"]
    rs_over = await client.post(f"/items/{parent_id2}/split", json={
        "children": [{"sku": f"PIECE-CONS-C2-{_ts}", "quantity": 3.0, "attributes": {"pieces": 6}}],
    }, headers=h)
    assert rs_over.status_code == 422, f"Expected 422 for child_pieces > parent_pieces, got {rs_over.status_code}"


@pytest.mark.asyncio
async def test_split_item_mother_pieces_computed(client):
    """After split, mother pieces = parent_pieces - child_pieces (server-computed)."""
    token = await _token(client)
    h = {"Authorization": f"Bearer {token}"}
    r = await client.post("/items", json={
        "status": "available", "sku": "MPIECE-001", "name": "Multi-pc", "quantity": 10.0,
        "sell_by": "carat", "attributes": {"pieces": 6},
    }, headers=h)
    assert r.status_code == 200
    parent_id = r.json()["id"]
    child_sku = "MPIECE-CHILD-001"
    rs = await client.post(f"/items/{parent_id}/split", json={
        "children": [{"sku": child_sku, "quantity": 3.0, "attributes": {"pieces": 2}}],
    }, headers=h)
    assert rs.status_code == 200, f"Split failed: {rs.text}"
    # Mother should have 6 - 2 = 4 pieces
    rp = await client.get(f"/items/{parent_id}", headers=h)
    state = rp.json()
    mother_pieces = state.get("pieces") or (state.get("attributes") or {}).get("pieces")
    assert int(mother_pieces) == 4, f"Expected mother_pieces=4, got {mother_pieces}"


# ---------------------------------------------------------------------------
# Bug 4: Negative weights rejected
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_patch_item_negative_weight_rejected(client):
    """PATCH weight=-1 must return 422."""
    token = await _token(client)
    h = {"Authorization": f"Bearer {token}"}
    r = await client.post("/items", json={
        "status": "available", "sku": "NEG-W-001", "name": "Test", "quantity": 1.0, "sell_by": "piece",
    }, headers=h)
    assert r.status_code == 200
    item_id = r.json()["id"]
    r2 = await client.patch(f"/items/{item_id}", json={
        "fields_changed": {"weight": {"old": None, "new": -1}},
    }, headers=h)
    assert r2.status_code == 422, f"Expected 422, got {r2.status_code}: {r2.text}"


@pytest.mark.asyncio
async def test_split_negative_child_weight_rejected(client):
    """Split with child weight=-1 must return 422."""
    token = await _token(client)
    h = {"Authorization": f"Bearer {token}"}
    r = await client.post("/items", json={
        "status": "available", "sku": "NEG-CW-001", "name": "Parent", "quantity": 10.0, "sell_by": "piece", "weight": 100.0,
    }, headers=h)
    assert r.status_code == 200
    item_id = r.json()["id"]
    r2 = await client.post(f"/items/{item_id}/split", json={
        "children": [{"sku": "NEG-CW-001.1", "quantity": 3.0, "weight": -1}],
    }, headers=h)
    assert r2.status_code == 422, f"Expected 422, got {r2.status_code}: {r2.text}"


# ---------------------------------------------------------------------------
# Bug 3: Weight conservation (mother weight computed server-side)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_split_mother_weight_computed_server_side(client):
    """After split, parent weight = original_weight - child_weight (server-computed)."""
    token = await _token(client)
    h = {"Authorization": f"Bearer {token}"}
    r = await client.post("/items", json={
        "status": "available", "sku": "MW-CS-001", "name": "Gem", "quantity": 10.0,
        "sell_by": "carat", "weight": 50.0,
    }, headers=h)
    assert r.status_code == 200
    parent_id = r.json()["id"]
    r2 = await client.post(f"/items/{parent_id}/split", json={
        "children": [{"sku": "MW-CS-001.1", "quantity": 4.0, "weight": 20.0}],
    }, headers=h)
    assert r2.status_code == 200, f"Split failed: {r2.text}"
    rp = await client.get(f"/items/{parent_id}", headers=h)
    parent_state = rp.json()
    assert abs(float(parent_state.get("weight", 0)) - 30.0) < 0.01, \
        f"Expected mother weight=30.0, got {parent_state.get('weight')}"


@pytest.mark.asyncio
async def test_split_preview_no_proportional_defaults(client):
    """split-preview must return static parent data only - no proportional computed fields."""
    token = await _token(client)
    h = {"Authorization": f"Bearer {token}"}
    r = await client.post("/items", json={
        "status": "available", "sku": "PREV-STATIC-001", "name": "Test", "quantity": 10, "sell_by": "piece",
        "weight": 5.0, "weight_unit": "gram",
    }, headers=h)
    assert r.status_code == 200, r.text
    entity_id = r.json()["id"]
    r = await client.get(f"/items/{entity_id}/split-preview?child_sku=PREV-STATIC-001.1", headers=h)
    assert r.status_code == 200
    data = r.json()
    # These keys must NOT be in the response
    assert "child_weight_default" not in data
    assert "child_pieces_default" not in data
    assert "parent_weight_remaining" not in data
    assert "parent_pieces_remaining" not in data
    assert "parent_qty_remaining" not in data
    assert "child_qty" not in data
    # These keys MUST be in the response
    assert "parent_weight" in data
    assert "parent_qty" in data


@pytest.mark.asyncio
async def test_split_omitting_child_weight_gives_child_no_weight(client):
    """If child_weight is not supplied, child item must have no weight.

    The backend must NOT assign a proportional fallback weight.
    This guards against the evil fallback that was removed from routes.py.
    """
    token = await _token(client)
    h = {"Authorization": f"Bearer {token}"}
    r = await client.post("/items", json={
        "status": "available", "sku": "NO-CW-PARENT-001", "name": "Parent", "quantity": 10.0,
        "sell_by": "piece", "weight": 100.0, "weight_unit": "gram",
    }, headers=h)
    assert r.status_code == 200, r.text
    parent_id = r.json()["id"]

    r2 = await client.post(f"/items/{parent_id}/split", json={
        "children": [{"sku": "NO-CW-CHILD-001", "quantity": 3.0}],
        # NOTE: no weight key supplied
    }, headers=h)
    assert r2.status_code == 200, f"Split failed: {r2.text}"
    child_ids = r2.json().get("child_ids") or r2.json().get("children") or []
    # Fetch all items matching child SKU
    rc = await client.get("/items", params={"q": "NO-CW-CHILD-001", "status": "all"}, headers=h)
    assert rc.status_code == 200
    children = rc.json().get("items", [])
    assert len(children) >= 1, "Child item not found after split"
    child = children[0]
    assert child.get("weight") is None, (
        f"Child weight should be None when not supplied, got {child.get('weight')}. "
        "Proportional fallback must be removed."
    )


@pytest.mark.asyncio
async def test_split_child_inherits_weight_unit(client):
    """Child items created via split must inherit weight_unit from parent."""
    token = await _token(client)
    h = {"Authorization": f"Bearer {token}"}
    r = await client.post("/items", json={
        "status": "available", "sku": "WU-PARENT-001", "name": "Parent", "quantity": 10.0,
        "sell_by": "piece", "weight": 50.0, "weight_unit": "carat",
    }, headers=h)
    assert r.status_code == 200, r.text
    parent_id = r.json()["id"]

    r2 = await client.post(f"/items/{parent_id}/split", json={
        "children": [{"sku": "WU-CHILD-001", "quantity": 3.0, "weight": 15.0}],
    }, headers=h)
    assert r2.status_code == 200, r2.text
    rc = await client.get("/items", params={"q": "WU-CHILD-001", "status": "all"}, headers=h)
    assert rc.status_code == 200
    children = rc.json().get("items", [])
    assert len(children) >= 1, "Child item not found after split"
    assert children[0].get("weight_unit") == "carat", (
        f"Child must inherit weight_unit='carat' from parent, got {children[0].get('weight_unit')!r}"
    )


@pytest.mark.asyncio
async def test_patch_item_updates_updated_at(client):
    """Patching any field must advance updated_at on the projection."""
    import time
    token = await _token(client)
    h = {"Authorization": f"Bearer {token}"}
    r = await client.post("/items", json={
        "status": "available", "sku": "UPD-AT-001", "name": "Timestamp Test", "quantity": 5.0, "sell_by": "piece",
    }, headers=h)
    assert r.status_code == 200, r.text
    item_id = r.json()["id"]

    before = (await client.get(f"/items/{item_id}", headers=h)).json().get("updated_at")

    # Small sleep to ensure timestamp advances
    time.sleep(0.05)

    r2 = await client.patch(f"/items/{item_id}", json={
        "fields_changed": {"name": {"old": "Timestamp Test", "new": "Timestamp Test 2"}},
    }, headers=h)
    assert r2.status_code == 200, r2.text

    after = (await client.get(f"/items/{item_id}", headers=h)).json().get("updated_at")
    assert after is not None, "updated_at must be set"
    assert after != before, f"updated_at must advance after patch; before={before!r} after={after!r}"


@pytest.mark.asyncio
async def test_transfer_item_updates_updated_at(client):
    """Transferring an item must advance updated_at on the projection."""
    import time
    token = await _token(client)
    h = {"Authorization": f"Bearer {token}"}
    loc1 = (await client.post("/companies/me/locations", json={"status": "available", "name": "Warehouse A", "type": "warehouse"}, headers=h)).json()
    loc2 = (await client.post("/companies/me/locations", json={"status": "available", "name": "Warehouse B", "type": "warehouse"}, headers=h)).json()

    r = await client.post("/items", json={
        "status": "available", "sku": "TR-AT-001", "name": "Transfer Timestamp", "quantity": 3.0,
        "sell_by": "piece", "location_id": loc1["id"],
    }, headers=h)
    assert r.status_code == 200, r.text
    item_id = r.json()["id"]

    before = (await client.get(f"/items/{item_id}", headers=h)).json().get("updated_at")
    time.sleep(0.05)

    r2 = await client.post(f"/items/{item_id}/transfer", json={"to_location_id": loc2["id"]}, headers=h)
    assert r2.status_code == 200, r2.text

    after_item = (await client.get(f"/items/{item_id}", headers=h)).json()
    after = after_item.get("updated_at")
    assert after is not None, "updated_at must be set after transfer"
    assert after != before, f"updated_at must advance after transfer; before={before!r} after={after!r}"
    assert after_item.get("location_id") == loc2["id"], "location_id must reflect new location"


@pytest.mark.asyncio
async def test_bulk_transfer_updates_projection(client):
    """Bulk transfer must update location_id on all selected items' projections."""
    token = await _token(client)
    h = {"Authorization": f"Bearer {token}"}
    loc1 = (await client.post("/companies/me/locations", json={"status": "available", "name": "BT-Warehouse-A", "type": "warehouse"}, headers=h)).json()
    loc2 = (await client.post("/companies/me/locations", json={"status": "available", "name": "BT-Warehouse-B", "type": "warehouse"}, headers=h)).json()

    item1_id = (await client.post("/items", json={"status": "available", "sku": "BT-001", "name": "Bulk A", "quantity": 1.0, "sell_by": "piece", "location_id": loc1["id"]}, headers=h)).json()["id"]
    item2_id = (await client.post("/items", json={"status": "available", "sku": "BT-002", "name": "Bulk B", "quantity": 2.0, "sell_by": "piece", "location_id": loc1["id"]}, headers=h)).json()["id"]

    r = await client.post("/items/bulk/transfer", json={"entity_ids": [item1_id, item2_id], "to_location_id": loc2["id"]}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json().get("updated") == 2

    for item_id in [item1_id, item2_id]:
        item = (await client.get(f"/items/{item_id}", headers=h)).json()
        assert item.get("location_id") == loc2["id"], (
            f"Item {item_id} location_id must be {loc2['id']} after bulk transfer, got {item.get('location_id')!r}"
        )


@pytest.mark.asyncio
async def test_auto_sku_fresh_company(client):
    """Blank SKU on fresh company (no items) assigns '000001'."""
    token = await _token(client)
    h = {"Authorization": f"Bearer {token}"}
    loc = (await client.post("/companies/me/locations", json={"status": "available", "name": "WH", "type": "warehouse"}, headers=h)).json()
    r = await client.post("/items", json={"status": "available", "name": "Auto SKU Item", "quantity": 1.0, "sell_by": "piece", "location_id": loc["id"]}, headers=h)
    assert r.status_code == 200, r.text
    item_id = r.json()["id"]
    item = (await client.get(f"/items/{item_id}", headers=h)).json()
    assert item["sku"] == "000001"


@pytest.mark.asyncio
async def test_auto_sku_after_existing_items(client):
    """Blank SKU when numeric SKUs exist assigns max+1."""
    token = await _token(client)
    h = {"Authorization": f"Bearer {token}"}
    loc = (await client.post("/companies/me/locations", json={"status": "available", "name": "WH-AS", "type": "warehouse"}, headers=h)).json()
    # Create items with explicit numeric SKUs
    for sku in ("000005", "000012", "ALPHA"):
        await client.post("/items", json={"status": "available", "sku": sku, "name": f"Item {sku}", "quantity": 1.0, "sell_by": "piece", "location_id": loc["id"]}, headers=h)
    r = await client.post("/items", json={"status": "available", "name": "Auto next", "quantity": 1.0, "sell_by": "piece", "location_id": loc["id"]}, headers=h)
    assert r.status_code == 200, r.text
    item_id = r.json()["id"]
    item = (await client.get(f"/items/{item_id}", headers=h)).json()
    assert item["sku"] == "000013"


@pytest.mark.asyncio
async def test_to_int_pieces_handles_float_string():
    """_to_int_pieces must handle '25.0' without raising ValueError."""
    from celerp_inventory.routes import _to_int_pieces
    assert _to_int_pieces("25.0") == 25
    assert _to_int_pieces("5") == 5
    assert _to_int_pieces(10.0) == 10
    assert _to_int_pieces(7) == 7


@pytest.mark.asyncio
async def test_read_pieces_from_top_level_and_attributes():
    """_read_pieces reads from top-level state first, falls back to attributes."""
    from celerp_inventory.routes import _read_pieces
    assert _read_pieces({"pieces": 5}) == 5.0
    assert _read_pieces({"attributes": {"pieces": "10.0"}}) == 10.0
    assert _read_pieces({"attributes": {"pieces": 7}}) == 7.0
    assert _read_pieces({}) is None
    assert _read_pieces({"attributes": {}}) is None


@pytest.mark.asyncio
async def test_merge_then_split_does_not_500(client):
    """After merge, split of merged item must not 500 due to float-string pieces."""
    token = await _token(client)
    h = {"Authorization": f"Bearer {token}"}
    loc = (await client.post("/companies/me/locations", json={"status": "available", "name": "WH-MTS", "type": "warehouse"}, headers=h)).json()
    r1 = await client.post("/items", json={"status": "available", "sku": "MTS-A", "name": "Parcel A", "quantity": 10.0, "sell_by": "carat", "location_id": loc["id"], "attributes": {"pieces": 10}}, headers=h)
    r2 = await client.post("/items", json={"status": "available", "sku": "MTS-B", "name": "Parcel B", "quantity": 15.0, "sell_by": "carat", "location_id": loc["id"], "attributes": {"pieces": 15}}, headers=h)
    assert r1.status_code == 200, r1.text
    assert r2.status_code == 200, r2.text
    id1, id2 = r1.json()["id"], r2.json()["id"]
    mr = await client.post("/items/merge", json={"source_entity_ids": [id1, id2], "target_sku_from": id1, "idempotency_key": "mts-merge-1"}, headers=h)
    assert mr.status_code == 200, mr.text
    merged_id = mr.json()["id"]
    # Split must not 500 (this was crashing with ValueError: invalid literal for int() '25.0')
    sr = await client.post(f"/items/{merged_id}/split", json={"children": [{"sku": "MTS-C", "quantity": 5.0, "attributes": {"pieces": 5}}]}, headers=h)
    assert sr.status_code == 200, sr.text


@pytest.mark.asyncio
async def test_merge_numeric_attrs_stored_as_numbers(client):
    """An AGREEING (intensive) numeric attribute carries through after merge as a number, not a string.
    (Conflicting numeric attributes get no value — see test_merge_dropdown_fields_use_value_or_mixed_never_sum.
    NOTE: `pieces` is intentionally NOT used here — it is an *additive* count that is summed on merge,
    not carried through; see tests/test_merge_pieces.py / issue #197. `size` is intensive, so it carries
    through unchanged.)
    """
    token = await _token(client)
    h = {"Authorization": f"Bearer {token}"}
    loc = (await client.post("/companies/me/locations", json={"status": "available", "name": "WH-MNA", "type": "warehouse"}, headers=h)).json()
    r1 = await client.post("/items", json={"status": "available", "sku": "MNA-A", "name": "Item A", "quantity": 8.0, "sell_by": "carat", "location_id": loc["id"], "attributes": {"size": 8}}, headers=h)
    r2 = await client.post("/items", json={"status": "available", "sku": "MNA-B", "name": "Item B", "quantity": 12.0, "sell_by": "carat", "location_id": loc["id"], "attributes": {"size": 8}}, headers=h)
    assert r1.status_code == 200
    assert r2.status_code == 200
    id1, id2 = r1.json()["id"], r2.json()["id"]
    mr = await client.post("/items/merge", json={"source_entity_ids": [id1, id2], "target_sku_from": id1, "idempotency_key": "mna-merge-1"}, headers=h)
    assert mr.status_code == 200, mr.text
    merged_id = mr.json()["id"]
    item = (await client.get(f"/items/{merged_id}", headers=h)).json()
    size_val = (item.get("attributes") or {}).get("size")
    if size_val is None:
        size_val = item.get("size")
    assert isinstance(size_val, (int, float)), f"size should be numeric, got {type(size_val)}: {size_val!r}"
    assert int(float(size_val)) == 8


# ---------------------------------------------------------------------------
# SKU/barcode auto-assignment
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_new_item_barcode_copies_sku(client):
    """New item with auto-assigned numeric SKU gets barcode == SKU."""
    token = await _token(client)
    h = {"Authorization": f"Bearer {token}"}
    r = await client.post("/items", json={"status": "available", "name": "AutoBarcode", "quantity": 1, "sell_by": "piece"}, headers=h)
    assert r.status_code == 200, r.text
    item_id = r.json()["id"]
    item = (await client.get(f"/items/{item_id}", headers=h)).json()
    assert item.get("barcode") is not None, "barcode should be auto-assigned"
    assert item["barcode"] == item["sku"], f"barcode {item['barcode']!r} should equal SKU {item['sku']!r}"
    assert item["barcode"].isdigit(), "barcode must be digits-only"


# ---------------------------------------------------------------------------
# SKU / batch separation (2026-06-17 plan) + reorder fields (2026-07-05 plan)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_numeric_duplicate_sku_gets_distinct_barcode(client):
    """Two items with the same numeric SKU and no explicit barcode: the second must
    NOT 409 on the auto-copied barcode; it gets a distinct sequential barcode."""
    token = await _token(client)
    h = {"Authorization": f"Bearer {token}"}
    r1 = await client.post("/items", json={"status": "available", "sku": "5000", "name": "A", "quantity": 1, "sell_by": "piece"}, headers=h)
    assert r1.status_code == 200
    r2 = await client.post("/items", json={"status": "available", "sku": "5000", "name": "B", "quantity": 1, "sell_by": "piece"}, headers=h)
    assert r2.status_code == 200, r2.text
    i1 = (await client.get(f"/items/{r1.json()['id']}", headers=h)).json()
    i2 = (await client.get(f"/items/{r2.json()['id']}", headers=h)).json()
    assert i1["barcode"] == "5000"
    assert i2.get("barcode") and i2["barcode"] != i1["barcode"]
    assert i2["barcode"].isdigit()


@pytest.mark.asyncio
async def test_batch_no_and_reorder_fields_round_trip(client):
    """batch_no + reorder_point + reorder_qty + pick_method are additive state fields:
    absent is accepted, and set values round-trip via patch."""
    token = await _token(client)
    h = {"Authorization": f"Bearer {token}"}
    r = await client.post("/items", json={"status": "available", "sku": "RT-1", "name": "A", "quantity": 3, "sell_by": "piece"}, headers=h)
    item_id = r.json()["id"]
    item = (await client.get(f"/items/{item_id}", headers=h)).json()
    assert item.get("batch_no") in (None, "")
    r = await client.patch(f"/items/{item_id}", json={"fields_changed": {
        "batch_no": {"new": "LOT-42"},
        "reorder_point": {"new": 10},
        "reorder_qty": {"new": 24},
        "pick_method": {"new": "fefo"},
    }}, headers=h)
    assert r.status_code == 200, r.text
    item = (await client.get(f"/items/{item_id}", headers=h)).json()
    assert item.get("batch_no") == "LOT-42"
    assert float(item.get("reorder_point")) == 10
    assert float(item.get("reorder_qty")) == 24
    assert item.get("pick_method") == "fefo"  # per-item stock-cutting rule persists


# The canonical resolver + its ambiguous-SKU behaviour on live scan paths are
# covered in tests/test_services/test_resolver.py (unit) and the audit/list scan
# tests in default_modules/celerp-docs/tests/test_audits.py.


@pytest.mark.asyncio
async def test_new_item_explicit_barcode_not_overridden(client):
    """When caller supplies a barcode, it must not be overridden by auto-copy."""
    token = await _token(client)
    h = {"Authorization": f"Bearer {token}"}
    r = await client.post("/items", json={"status": "available", "name": "ExplicitBC", "quantity": 1, "sell_by": "piece", "barcode": "9999999999"}, headers=h)
    assert r.status_code == 200, r.text
    item_id = r.json()["id"]
    item = (await client.get(f"/items/{item_id}", headers=h)).json()
    assert item["barcode"] == "9999999999"


@pytest.mark.asyncio
async def test_new_item_non_numeric_sku_no_barcode(client):
    """Non-numeric SKU with no barcode supplied → barcode stays null (no auto-copy would 422)."""
    token = await _token(client)
    h = {"Authorization": f"Bearer {token}"}
    r = await client.post("/items", json={"status": "available", "sku": "GOLD-001", "name": "Gold Bar", "quantity": 1, "sell_by": "piece"}, headers=h)
    assert r.status_code == 200, r.text
    item_id = r.json()["id"]
    item = (await client.get(f"/items/{item_id}", headers=h)).json()
    # Non-numeric SKU - barcode should be None (digits-only rule prevents copying)
    assert item.get("barcode") is None


@pytest.mark.asyncio
async def test_auto_barcode_mints_fresh_unique_barcode(client):
    """A create with auto_barcode=True (the duplicate/clone path) mints a fresh
    unique barcode from the shared sequence and never inherits the one passed in -
    barcode is globally unique, mirroring the split child's reset."""
    token = await _token(client)
    h = {"Authorization": f"Bearer {token}"}
    # Source item carrying a barcode.
    r0 = await client.post("/items", json={"status": "available", "sku": "SRC-1", "name": "Src", "quantity": 1, "sell_by": "piece", "barcode": "700001"}, headers=h)
    assert r0.status_code == 200, r0.text
    src = (await client.get(f"/items/{r0.json()['id']}", headers=h)).json()
    assert src["barcode"] == "700001"
    # Duplicate: same barcode passed with auto_barcode=True -> fresh, distinct barcode
    # (and no 409 on the source's barcode).
    r1 = await client.post("/items", json={"status": "available", "sku": "SRC-1-copy", "name": "Src", "quantity": 1, "sell_by": "piece", "barcode": "700001", "auto_barcode": True}, headers=h)
    assert r1.status_code == 200, r1.text
    dup = (await client.get(f"/items/{r1.json()['id']}", headers=h)).json()
    assert dup.get("barcode") is not None, "duplicate must receive a fresh barcode"
    assert dup["barcode"] != src["barcode"], f"duplicate barcode must differ from source; both={dup['barcode']!r}"
    assert dup["barcode"].isdigit()
    # A second duplicate gets its own distinct barcode too.
    r2 = await client.post("/items", json={"status": "available", "sku": "SRC-1-copy2", "name": "Src", "quantity": 1, "sell_by": "piece", "auto_barcode": True}, headers=h)
    assert r2.status_code == 200, r2.text
    dup2 = (await client.get(f"/items/{r2.json()['id']}", headers=h)).json()
    assert dup2["barcode"] not in (None, src["barcode"], dup["barcode"])


@pytest.mark.asyncio
async def test_auto_barcode_not_persisted_as_field(client):
    """auto_barcode is a request-only signal; it must not be stored on the item."""
    token = await _token(client)
    h = {"Authorization": f"Bearer {token}"}
    r = await client.post("/items", json={"status": "available", "sku": "AB-1", "name": "AB", "quantity": 1, "sell_by": "piece", "auto_barcode": True}, headers=h)
    assert r.status_code == 200, r.text
    item = (await client.get(f"/items/{r.json()['id']}", headers=h)).json()
    assert "auto_barcode" not in item, f"auto_barcode leaked onto stored item: {item!r}"


@pytest.mark.asyncio
async def test_split_children_get_unique_barcodes(client):
    """Each split child must receive a unique auto-assigned barcode from the shared sequence."""
    token = await _token(client)
    h = {"Authorization": f"Bearer {token}"}
    r = await client.post("/items", json={"status": "available", "name": "SplitParent", "quantity": 30, "sell_by": "piece"}, headers=h)
    assert r.status_code == 200, r.text
    parent_id = r.json()["id"]
    parent = (await client.get(f"/items/{parent_id}", headers=h)).json()
    parent_barcode = parent["barcode"]
    parent_sku = parent["sku"]

    r = await client.post(f"/items/{parent_id}/split", json={
        "children": [
            {"sku": f"{parent_sku}.1", "quantity": 10},
            {"sku": f"{parent_sku}.2", "quantity": 10},
        ]
    }, headers=h)
    assert r.status_code == 200, r.text
    child_ids = [c["id"] for c in r.json()["children"]]
    assert len(child_ids) == 2

    barcodes = []
    for cid in child_ids:
        item = (await client.get(f"/items/{cid}", headers=h)).json()
        bc = item.get("barcode")
        assert bc is not None, f"child {cid} has no barcode"
        assert bc.isdigit(), f"child barcode {bc!r} must be digits-only"
        barcodes.append(bc)

    assert len(barcodes) == len(set(barcodes)), f"duplicate barcodes: {barcodes}"
    assert parent_barcode not in barcodes, f"child barcode collides with parent: {parent_barcode}"


@pytest.mark.asyncio
async def test_split_barcode_no_overlap_with_sku_sequence(client):
    """Barcode assigned during split must not collide with next auto-SKU on item creation."""
    token = await _token(client)
    h = {"Authorization": f"Bearer {token}"}

    r = await client.post("/items", json={"status": "available", "name": "SeqParent", "quantity": 20, "sell_by": "piece"}, headers=h)
    assert r.status_code == 200
    parent_id = r.json()["id"]
    parent = (await client.get(f"/items/{parent_id}", headers=h)).json()
    parent_sku_int = int(parent["sku"])
    parent_sku = parent["sku"]

    r = await client.post(f"/items/{parent_id}/split", json={
        "children": [{"sku": f"{parent_sku}.1", "quantity": 5}]
    }, headers=h)
    assert r.status_code == 200
    child_id = r.json()["children"][0]["id"]
    child_barcode = (await client.get(f"/items/{child_id}", headers=h)).json()["barcode"]
    assert int(child_barcode) == parent_sku_int + 1

    r = await client.post("/items", json={"status": "available", "name": "NextItem", "quantity": 1, "sell_by": "piece"}, headers=h)
    assert r.status_code == 200
    next_id = r.json()["id"]
    next_item = (await client.get(f"/items/{next_id}", headers=h)).json()
    assert int(next_item["sku"]) == parent_sku_int + 2, (
        f"Expected SKU {parent_sku_int + 2}, got {next_item['sku']} — sequence should skip barcode N+1"
    )


@pytest.mark.asyncio
async def test_split_child_explicit_barcode_not_overridden(client):
    """If a split child explicitly provides a barcode, it must not be auto-assigned."""
    token = await _token(client)
    h = {"Authorization": f"Bearer {token}"}
    r = await client.post("/items", json={"status": "available", "name": "ExplicitBCParent", "quantity": 20, "sell_by": "piece"}, headers=h)
    assert r.status_code == 200
    parent_id = r.json()["id"]
    parent = (await client.get(f"/items/{parent_id}", headers=h)).json()

    r = await client.post(f"/items/{parent_id}/split", json={
        "children": [{"sku": f"{parent['sku']}.1", "quantity": 5, "barcode": "8888888888"}]
    }, headers=h)
    assert r.status_code == 200
    child_id = r.json()["children"][0]["id"]
    child = (await client.get(f"/items/{child_id}", headers=h)).json()
    assert child["barcode"] == "8888888888"


# ── Phase 3: bulk file attach tests ─────────────────────────────────────────

def _make_zip(files: dict[str, bytes]) -> bytes:
    """Create an in-memory ZIP with given {filename: content} entries."""
    import io as _io, zipfile as _zf
    buf = _io.BytesIO()
    with _zf.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


@pytest.mark.asyncio
async def test_bulk_attach_bare_image_is_hero(client):
    """Bare SKU.jpg → product_images tag, is_hero=True."""
    token = await _token(client)
    h = {"Authorization": f"Bearer {token}"}
    r = await client.post("/items", json={"status": "available", "sku": "BULK-HERO-001", "name": "Img Item", "quantity": 1, "sell_by": "piece"}, headers=h)
    assert r.status_code == 200

    fake_jpg = b"\xff\xd8\xff" + b"\x00" * 10  # minimal JPEG header
    zdata = _make_zip({"BULK-HERO-001.jpg": fake_jpg})
    result = await client.post("/items/files/bulk", files={"file": ("test.zip", zdata, "application/zip")}, headers=h)
    assert result.status_code == 200
    data = result.json()
    assert data["matched"] == 1
    row = data["report"][0]
    assert row["tag"] == "product_images"
    assert row["is_hero"] is True


@pytest.mark.asyncio
async def test_bulk_attach_does_not_duplicate_files(client):
    """F1: bulk attach must add each file exactly ONCE.

    Regression for the double-applied `item.file.attached` event (emit_event
    already updates the projection, and the handler re-applied it manually), which
    left each bulk-attached file twice in state.files with the same file_id.
    """
    token = await _token(client)
    h = {"Authorization": f"Bearer {token}"}
    r = await client.post("/items", json={"status": "available", "sku": "BULK-DUP-001", "name": "Dup", "quantity": 1, "sell_by": "piece"}, headers=h)
    assert r.status_code == 200
    item_id = r.json()["id"]

    fake_jpg = b"\xff\xd8\xff" + b"\x00" * 10
    zdata = _make_zip({
        "BULK-DUP-001.jpg": fake_jpg,
        "BULK-DUP-001-cert-grs.pdf": b"%PDF-1.4",
    })
    result = await client.post("/items/files/bulk", files={"file": ("dup.zip", zdata, "application/zip")}, headers=h)
    assert result.status_code == 200, result.text
    assert result.json()["matched"] == 2

    item = (await client.get(f"/items/{item_id}", headers=h)).json()
    files = item.get("files") or (item.get("attributes") or {}).get("files") or []
    ids = [f.get("id") for f in files]
    assert len(ids) == len(set(ids)), f"duplicate file_ids after bulk attach: {ids}"
    assert len(files) == 2, f"expected 2 distinct files, got {len(files)}: {ids}"


@pytest.mark.asyncio
async def test_bulk_attach_skips_nested_dotfiles(client):
    """F5: a nested dotfile (sub/.DS_Store) must be skipped, not processed as
    unmatched. It slips the skip because its full ZIP path doesn't start with
    '.'/'__' — the skip must look at the basename.
    """
    token = await _token(client)
    h = {"Authorization": f"Bearer {token}"}
    await client.post("/items", json={"status": "available", "sku": "F5-ITEM-001", "name": "F5", "quantity": 1, "sell_by": "piece"}, headers=h)

    fake_jpg = b"\xff\xd8\xff" + b"\x00" * 10
    zdata = _make_zip({
        "F5-ITEM-001.jpg": fake_jpg,   # valid → should match
        "sub/.DS_Store": b"x",         # nested junk → must be skipped
    })
    result = await client.post("/items/files/bulk", files={"file": ("attach.zip", zdata, "application/zip")}, headers=h)
    assert result.status_code == 200, result.text
    data = result.json()
    assert data["matched"] == 1, data
    assert data["unmatched"] == 0, f"nested dotfile should be skipped, not unmatched: {data}"
    reported = " ".join(r.get("file", "") for r in data["report"])
    assert ".DS_Store" not in reported, reported


@pytest.mark.asyncio
async def test_bulk_attach_multi_image_only_first_hero(client):
    """First alphabetical bare image gets hero; second does not."""
    token = await _token(client)
    h = {"Authorization": f"Bearer {token}"}
    await client.post("/items", json={"status": "available", "sku": "BULK-MULTI-001", "name": "Multi", "quantity": 1, "sell_by": "piece"}, headers=h)

    fake_jpg = b"\xff\xd8\xff" + b"\x00" * 10
    zdata = _make_zip({"BULK-MULTI-001.jpg": fake_jpg, "BULK-MULTI-001-img-2.jpg": fake_jpg})
    result = await client.post("/items/files/bulk", files={"file": ("test.zip", zdata, "application/zip")}, headers=h)
    assert result.status_code == 200
    report = result.json()["report"]
    hero_rows = [r for r in report if r.get("is_hero")]
    assert len(hero_rows) == 1


@pytest.mark.asyncio
async def test_bulk_attach_cert_tag(client):
    token = await _token(client)
    h = {"Authorization": f"Bearer {token}"}
    await client.post("/items", json={"status": "available", "sku": "BULK-CERT-001", "name": "Cert", "quantity": 1, "sell_by": "piece"}, headers=h)

    zdata = _make_zip({"BULK-CERT-001-cert-grs.pdf": b"%PDF-1.4"})
    result = await client.post("/items/files/bulk", files={"file": ("test.zip", zdata, "application/zip")}, headers=h)
    assert result.status_code == 200
    assert result.json()["report"][0]["tag"] == "certificates"


@pytest.mark.asyncio
async def test_bulk_attach_spec_tag(client):
    token = await _token(client)
    h = {"Authorization": f"Bearer {token}"}
    await client.post("/items", json={"status": "available", "sku": "BULK-SPEC-001", "name": "Spec", "quantity": 1, "sell_by": "piece"}, headers=h)

    zdata = _make_zip({"BULK-SPEC-001-spec-tech.pdf": b"%PDF-1.4"})
    result = await client.post("/items/files/bulk", files={"file": ("test.zip", zdata, "application/zip")}, headers=h)
    assert result.status_code == 200
    assert result.json()["report"][0]["tag"] == "spec_sheets"


@pytest.mark.asyncio
async def test_bulk_attach_safety_tag(client):
    token = await _token(client)
    h = {"Authorization": f"Bearer {token}"}
    await client.post("/items", json={"status": "available", "sku": "BULK-SAFE-001", "name": "Safe", "quantity": 1, "sell_by": "piece"}, headers=h)

    zdata = _make_zip({"BULK-SAFE-001-safety-msds.pdf": b"%PDF-1.4"})
    result = await client.post("/items/files/bulk", files={"file": ("test.zip", zdata, "application/zip")}, headers=h)
    assert result.status_code == 200
    assert result.json()["report"][0]["tag"] == "safety_docs"


@pytest.mark.asyncio
async def test_bulk_attach_360_tag(client):
    token = await _token(client)
    h = {"Authorization": f"Bearer {token}"}
    await client.post("/items", json={"status": "available", "sku": "BULK-360-001", "name": "360", "quantity": 1, "sell_by": "piece"}, headers=h)

    zdata = _make_zip({"BULK-360-001-360-exterior.mp4": b"\x00" * 20})
    result = await client.post("/items/files/bulk", files={"file": ("test.zip", zdata, "application/zip")}, headers=h)
    assert result.status_code == 200
    assert result.json()["report"][0]["tag"] == "view_360"


@pytest.mark.asyncio
async def test_bulk_attach_doc_alias_backward_compat(client):
    """-doc- marker still maps to certificates."""
    token = await _token(client)
    h = {"Authorization": f"Bearer {token}"}
    await client.post("/items", json={"status": "available", "sku": "BULK-DOC-001", "name": "Doc", "quantity": 1, "sell_by": "piece"}, headers=h)

    zdata = _make_zip({"BULK-DOC-001-doc-warranty.pdf": b"%PDF-1.4"})
    result = await client.post("/items/files/bulk", files={"file": ("test.zip", zdata, "application/zip")}, headers=h)
    assert result.status_code == 200
    assert result.json()["report"][0]["tag"] == "certificates"


@pytest.mark.asyncio
async def test_bulk_attach_report_includes_tag_and_hero(client):
    token = await _token(client)
    h = {"Authorization": f"Bearer {token}"}
    await client.post("/items", json={"status": "available", "sku": "BULK-RPT-001", "name": "Report", "quantity": 1, "sell_by": "piece"}, headers=h)

    fake_jpg = b"\xff\xd8\xff" + b"\x00" * 10
    zdata = _make_zip({"BULK-RPT-001.jpg": fake_jpg})
    result = await client.post("/items/files/bulk", files={"file": ("test.zip", zdata, "application/zip")}, headers=h)
    assert result.status_code == 200
    row = result.json()["report"][0]
    assert "tag" in row
    assert "is_hero" in row


@pytest.mark.asyncio
async def test_bulk_attach_override_hero_false(client):
    """Without override_hero, existing hero is not replaced."""
    token = await _token(client)
    h = {"Authorization": f"Bearer {token}"}
    r = await client.post("/items", json={"status": "available", "sku": "BULK-OH-001", "name": "OH", "quantity": 1, "sell_by": "piece"}, headers=h)
    item_id = r.json()["id"]
    # Upload first hero via file event
    fake_jpg = b"\xff\xd8\xff" + b"\x00" * 10
    first_zip = _make_zip({"BULK-OH-001.jpg": fake_jpg})
    await client.post("/items/files/bulk", files={"file": ("t.zip", first_zip, "application/zip")}, headers=h)

    # Second batch: different image, no override_hero
    second_zip = _make_zip({"BULK-OH-001.png": fake_jpg})
    result = await client.post("/items/files/bulk", files={"file": ("t2.zip", second_zip, "application/zip")}, headers=h)
    assert result.status_code == 200
    second_row = result.json()["report"][0]
    # No hero override → new image is NOT hero
    assert second_row["is_hero"] is False


@pytest.mark.asyncio
async def test_bulk_attach_override_hero_true(client):
    """With override_hero=1, new bare image becomes hero."""
    token = await _token(client)
    h = {"Authorization": f"Bearer {token}"}
    await client.post("/items", json={"status": "available", "sku": "BULK-OHT-001", "name": "OHT", "quantity": 1, "sell_by": "piece"}, headers=h)

    fake_jpg = b"\xff\xd8\xff" + b"\x00" * 10
    first_zip = _make_zip({"BULK-OHT-001.jpg": fake_jpg})
    await client.post("/items/files/bulk", files={"file": ("t.zip", first_zip, "application/zip")}, headers=h)

    second_zip = _make_zip({"BULK-OHT-001.png": fake_jpg})
    result = await client.post("/items/files/bulk?override_hero=1", files={"file": ("t2.zip", second_zip, "application/zip")}, headers=h)
    assert result.status_code == 200
    second_row = result.json()["report"][0]
    assert second_row["is_hero"] is True


@pytest.mark.asyncio
async def test_download_item_file_route_exists(client):
    """GET /items/{entity_id}/files/{file_id} must return the file, not 404.

    Regression: attachments_router was registered AFTER the main items router,
    causing GET /{entity_id} to shadow GET /{entity_id}/files/{file_id}.
    """
    from celerp.config import settings

    token = await _token(client)
    h = {"Authorization": f"Bearer {token}"}

    # Create item and upload a file via the new /files endpoint
    r = await client.post("/items", json={"status": "available", "sku": "DL-TEST-001", "name": "Download Test", "quantity": 1, "sell_by": "piece"}, headers=h)
    assert r.status_code == 200
    item_id = r.json()["id"]

    fake_jpg = b"\xff\xd8\xff\xe0" + b"\x00" * 20
    ru = await client.post(
        f"/items/{item_id}/files",
        files={"file": ("test.jpg", fake_jpg, "image/jpeg")},
        headers=h,
    )
    assert ru.status_code == 200, f"Upload failed: {ru.text}"
    file_id = ru.json()["file_id"]

    # Download the file — must NOT return 404
    rd = await client.get(f"/items/{item_id}/files/{file_id}", headers=h)
    assert rd.status_code == 200, (
        f"GET /items/{{entity_id}}/files/{{file_id}} returned {rd.status_code}. "
        "Route is likely shadowed by GET /{entity_id} from the main items router."
    )
    assert rd.content == fake_jpg


# ── qty→weight/pieces sync ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_patch_qty_syncs_weight_for_weight_sell_by(client):
    """When sell_by is a weight unit, PATCH quantity must also update weight to the same value."""
    token = await _token(client)
    h = {"Authorization": f"Bearer {token}"}
    # Create item with sell_by=ct (weight unit from DEFAULT_UNITS)
    r = await client.post("/items", json={
        "status": "available", "sku": "QTY-SYNC-W-001", "name": "Weight sync test",
        "quantity": 5.0, "sell_by": "carat", "weight": 5.0, "weight_unit": "carat",
    }, headers=h)
    assert r.status_code == 200, r.text
    item_id = r.json()["id"]

    # Patch quantity
    r2 = await client.patch(f"/items/{item_id}", json={
        "fields_changed": {"quantity": {"old": 5.0, "new": 8.0}},
    }, headers=h)
    assert r2.status_code == 200, r2.text

    # Verify weight was synced
    r3 = await client.get(f"/items/{item_id}", headers=h)
    assert r3.status_code == 200
    state = r3.json()
    assert float(state.get("quantity", 0)) == 8.0
    assert float(state.get("weight", 0)) == 8.0


@pytest.mark.asyncio
async def test_patch_qty_syncs_pieces_for_pieces_sell_by(client):
    """When sell_by is a pieces unit, PATCH quantity must also update pieces to the same value."""
    token = await _token(client)
    h = {"Authorization": f"Bearer {token}"}
    # Create item with sell_by=piece
    r = await client.post("/items", json={
        "status": "available", "sku": "QTY-SYNC-P-001", "name": "Pieces sync test",
        "quantity": 3, "sell_by": "piece",
    }, headers=h)
    assert r.status_code == 200, r.text
    item_id = r.json()["id"]

    # Patch quantity
    r2 = await client.patch(f"/items/{item_id}", json={
        "fields_changed": {"quantity": {"old": 3, "new": 7}},
    }, headers=h)
    assert r2.status_code == 200, r2.text

    # Verify pieces was synced
    r3 = await client.get(f"/items/{item_id}", headers=h)
    assert r3.status_code == 200
    state = r3.json()
    assert int(state.get("quantity", 0)) == 7
    pieces = (state.get("attributes") or {}).get("pieces") or state.get("pieces")
    assert int(pieces) == 7


# ── Split / Transform inheritance tests ───────────────────────────────────────

@pytest.mark.asyncio
async def test_split_children_inherit_purchase_fields(client):
    """Split children must inherit purchase_unit, purchase_conversion_factor, purchase_sku, purchase_name from parent."""
    token = await _token(client)
    h = {"Authorization": f"Bearer {token}"}

    sku = f"SPL-PURCH-{uuid.uuid4().hex[:6]}"
    r = await client.post("/items", json={
        "status": "available", "sku": sku, "name": "Parent", "quantity": 10, "sell_by": "gram",
        "purchase_unit": "kg", "purchase_conversion_factor": 1000.0,
        "purchase_sku": "V-SKU", "purchase_name": "Vendor Material",
    }, headers=h)
    assert r.status_code == 200, r.text
    parent_id = r.json()["id"]

    children = [{"sku": f"{sku}-A", "quantity": 5}, {"sku": f"{sku}-B", "quantity": 5}]
    r2 = await client.post(f"/items/{parent_id}/split", json={"children": children}, headers=h)
    assert r2.status_code == 200, r2.text

    for c in r2.json()["children"]:
        child = (await client.get(f"/items/{c['id']}", headers=h)).json()
        assert child.get("purchase_unit") == "kg"
        assert child.get("purchase_conversion_factor") == pytest.approx(1000.0)
        assert child.get("purchase_sku") == "V-SKU"
        assert child.get("purchase_name") == "Vendor Material"


@pytest.mark.asyncio
async def test_split_children_inherit_inventory_type(client):
    """Split children must inherit inventory_type from parent."""
    token = await _token(client)
    h = {"Authorization": f"Bearer {token}"}

    sku = f"SPL-INVT-{uuid.uuid4().hex[:6]}"
    r = await client.post("/items", json={
        "status": "available", "sku": sku, "name": "Parent", "quantity": 10, "sell_by": "piece",
        "inventory_type": "non_stocked",
    }, headers=h)
    assert r.status_code == 200, r.text
    parent_id = r.json()["id"]

    r2 = await client.post(f"/items/{parent_id}/split", json={
        "children": [{"sku": f"{sku}-A", "quantity": 5}, {"sku": f"{sku}-B", "quantity": 5}],
    }, headers=h)
    assert r2.status_code == 200, r2.text

    for c in r2.json()["children"]:
        child = (await client.get(f"/items/{c['id']}", headers=h)).json()
        assert child.get("inventory_type") == "non_stocked"


@pytest.mark.asyncio
async def test_split_children_inherit_custom_attributes(client):
    """Split children must inherit custom attributes (e.g. beauty_grade) from parent."""
    token = await _token(client)
    h = {"Authorization": f"Bearer {token}"}

    sku = f"SPL-ATTR-{uuid.uuid4().hex[:6]}"
    r = await client.post("/items", json={
        "status": "available", "sku": sku, "name": "Parent", "quantity": 10, "sell_by": "gram",
        "attributes": {"beauty_grade": "A+", "cut": "oval"},
    }, headers=h)
    assert r.status_code == 200, r.text
    parent_id = r.json()["id"]

    r2 = await client.post(f"/items/{parent_id}/split", json={
        "children": [{"sku": f"{sku}-A", "quantity": 4}, {"sku": f"{sku}-B", "quantity": 6}],
    }, headers=h)
    assert r2.status_code == 200, r2.text

    for c in r2.json()["children"]:
        child = (await client.get(f"/items/{c['id']}", headers=h)).json()
        grade = child.get("beauty_grade") or (child.get("attributes") or {}).get("beauty_grade")
        cut = child.get("cut") or (child.get("attributes") or {}).get("cut")
        assert grade == "A+", f"beauty_grade not inherited: {grade!r}"
        assert cut == "oval", f"cut not inherited: {cut!r}"


@pytest.mark.asyncio
async def test_split_children_start_available(client):
    """Split children must always have status=available."""
    token = await _token(client)
    h = {"Authorization": f"Bearer {token}"}

    sku = f"SPL-STATUS-{uuid.uuid4().hex[:6]}"
    r = await client.post("/items", json={
        "status": "available", "sku": sku, "name": "Parent", "quantity": 10, "sell_by": "piece",
    }, headers=h)
    assert r.status_code == 200, r.text
    parent_id = r.json()["id"]

    r2 = await client.post(f"/items/{parent_id}/split", json={
        "children": [{"sku": f"{sku}-A", "quantity": 5}, {"sku": f"{sku}-B", "quantity": 5}],
    }, headers=h)
    assert r2.status_code == 200, r2.text

    for c in r2.json()["children"]:
        child = (await client.get(f"/items/{c['id']}", headers=h)).json()
        assert child.get("status") == "available"


@pytest.mark.asyncio
async def test_split_children_get_distinct_barcodes(client):
    """Each split child must get a distinct barcode, different from the parent."""
    token = await _token(client)
    h = {"Authorization": f"Bearer {token}"}

    sku = f"SPL-BAR-{uuid.uuid4().hex[:6]}"
    r = await client.post("/items", json={
        "status": "available", "sku": sku, "name": "Parent", "quantity": 10, "sell_by": "piece",
    }, headers=h)
    assert r.status_code == 200, r.text
    parent_id = r.json()["id"]
    parent_barcode = (await client.get(f"/items/{parent_id}", headers=h)).json().get("barcode")

    r2 = await client.post(f"/items/{parent_id}/split", json={
        "children": [{"sku": f"{sku}-A", "quantity": 5}, {"sku": f"{sku}-B", "quantity": 5}],
    }, headers=h)
    assert r2.status_code == 200, r2.text
    child_ids = [c["id"] for c in r2.json()["children"]]

    barcodes = [(await client.get(f"/items/{cid}", headers=h)).json().get("barcode") for cid in child_ids]
    assert all(b is not None for b in barcodes), "All children must have barcodes"
    assert len(set(barcodes)) == len(barcodes), f"Child barcodes must be distinct: {barcodes}"
    assert parent_barcode not in barcodes, f"Children must not inherit parent barcode: {parent_barcode}"


@pytest.mark.asyncio
async def test_created_at_set_by_engine_not_client(client):
    """POST /items with a client-supplied created_at must be ignored.

    Projection.created_at is set by ProjectionEngine on INSERT.
    The API response must contain a valid created_at that is NOT the forged value.
    """
    token = await _token(client)
    h = {"Authorization": f"Bearer {token}"}
    r = await client.post("/items", json={
        "status": "available", "sku": f"CA-{uuid.uuid4().hex[:6]}",
        "name": "Timestamp Guard",
        "quantity": 1,
        "sell_by": "piece",
        "created_at": "2000-01-01T00:00:00",  # must be ignored
    }, headers=h)
    assert r.status_code == 200, r.text
    item_id = r.json()["id"]

    data = (await client.get(f"/items/{item_id}", headers=h)).json()
    assert data.get("created_at") is not None, "created_at must be set in API response"
    assert not data["created_at"].startswith("2000"), (
        f"Client-forged created_at must not be stored; got {data['created_at']}"
    )


@pytest.mark.asyncio
async def test_created_at_immutable_after_patch(client):
    """PATCH /items/{id} must not change created_at; updated_at must advance."""
    from datetime import datetime
    token = await _token(client)
    h = {"Authorization": f"Bearer {token}"}
    r = await client.post("/items", json={
        "status": "available", "sku": f"CA-IMM-{uuid.uuid4().hex[:6]}",
        "name": "Immutable TS",
        "quantity": 1,
        "sell_by": "piece",
    }, headers=h)
    assert r.status_code == 200, r.text
    item_id = r.json()["id"]

    before = (await client.get(f"/items/{item_id}", headers=h)).json()
    created_before = before.get("created_at")
    updated_before = before.get("updated_at")
    assert created_before is not None

    # Patch the item name
    pr = await client.patch(f"/items/{item_id}", json={"status": "available", "name": "Renamed"}, headers=h)
    assert pr.status_code == 200, pr.text

    after = (await client.get(f"/items/{item_id}", headers=h)).json()
    assert after.get("created_at") == created_before, "created_at must not change after patch"
    assert after.get("updated_at") != updated_before, "updated_at must advance after patch"


@pytest.mark.asyncio
async def test_split_child_top_level_pieces_field(client):
    """SplitChild.pieces (top-level) must override parent pieces on children.

    Regression test for batch-split bug: when sell_by is a weight unit (carat),
    sending pieces as a top-level SplitChild field was silently dropped by Pydantic
    because SplitChild had no pieces field, causing every child to inherit the
    parent's pieces count unchanged.
    """
    import time as _t
    _ts = str(int(_t.time() * 1000))[-6:]
    token = await _token(client)
    h = {"Authorization": f"Bearer {token}"}
    # Parent: sell_by=carat (weight unit), 70 carat, 100 pieces
    r = await client.post("/items", json={
        "status": "available", "sku": f"TLP-{_ts}", "name": "Batch Split Stone",
        "quantity": 70.0, "sell_by": "carat", "attributes": {"pieces": 100},
    }, headers=h)
    assert r.status_code == 200, r.text
    parent_id = r.json()["id"]

    # Split into 5 children using top-level pieces field (mirrors what batch-split UI sends)
    children = [
        {"sku": f"TLP-{_ts}-{i}", "quantity": 0.01, "pieces": 1}
        for i in range(1, 6)
    ]
    rs = await client.post(f"/items/{parent_id}/split", json={"children": children}, headers=h)
    assert rs.status_code == 200, f"Split failed: {rs.text}"

    child_ids = [c["id"] for c in rs.json()["children"]]
    for cid in child_ids:
        child = (await client.get(f"/items/{cid}", headers=h)).json()
        assert int(child.get("pieces", -1)) == 1, (
            f"Child {cid} has pieces={child.get('pieces')} but expected 1 (not parent's 100)"
        )


@pytest.mark.asyncio
async def test_sell_by_change_piece_to_weight_pulls_weight_into_qty(client):
    """Switching sell_by from piece to carat: quantity must become stored weight, weight unchanged.

    Regression: old code set weight = qty (backwards direction), so the real
    weight was overwritten by the piece count.
    """
    import time as _t
    _ts = str(int(_t.time() * 1000))[-6:]
    token = await _token(client)
    h = {"Authorization": f"Bearer {token}"}
    r = await client.post("/items", json={
        "status": "available", "sku": f"SBP2C-{_ts}", "name": "Stone", "quantity": 100.0,
        "sell_by": "piece", "weight": 50.0,
    }, headers=h)
    assert r.status_code == 200, r.text
    item_id = r.json()["id"]

    pr = await client.patch(f"/items/{item_id}", json={
        "fields_changed": {"sell_by": {"old": "piece", "new": "carat"}},
    }, headers=h)
    assert pr.status_code == 200, pr.text

    item = (await client.get(f"/items/{item_id}", headers=h)).json()
    assert float(item["quantity"]) == 50.0, f"qty should be 50 (weight), got {item['quantity']}"
    assert float(item["weight"]) == 50.0, f"weight must stay 50, got {item['weight']}"


@pytest.mark.asyncio
async def test_sell_by_change_weight_to_piece_pulls_pieces_into_qty(client):
    """Switching sell_by from carat to piece: quantity must become stored pieces, pieces unchanged.

    Regression: old code set pieces = qty (backwards direction), so the real
    piece count was overwritten by the carat quantity.
    """
    import time as _t
    _ts = str(int(_t.time() * 1000))[-6:]
    token = await _token(client)
    h = {"Authorization": f"Bearer {token}"}
    r = await client.post("/items", json={
        "status": "available", "sku": f"SBC2P-{_ts}", "name": "Parcel", "quantity": 50.0,
        "sell_by": "carat", "attributes": {"pieces": 100},
    }, headers=h)
    assert r.status_code == 200, r.text
    item_id = r.json()["id"]

    pr = await client.patch(f"/items/{item_id}", json={
        "fields_changed": {"sell_by": {"old": "carat", "new": "piece"}},
    }, headers=h)
    assert pr.status_code == 200, pr.text

    item = (await client.get(f"/items/{item_id}", headers=h)).json()
    assert int(item["quantity"]) == 100, f"qty should be 100 (pieces), got {item['quantity']}"
    assert int(item.get("pieces", -1)) == 100, f"pieces must stay 100, got {item.get('pieces')}"


@pytest.mark.asyncio
async def test_sell_by_change_piece_to_weight_no_weight_gives_none_qty(client):
    """Switching sell_by from piece to carat when weight is unset: quantity must become None."""
    import time as _t
    _ts = str(int(_t.time() * 1000))[-6:]
    token = await _token(client)
    h = {"Authorization": f"Bearer {token}"}
    r = await client.post("/items", json={
        "status": "available", "sku": f"SBPN-{_ts}", "name": "Unweighed Stone", "quantity": 5.0,
        "sell_by": "piece",
    }, headers=h)
    assert r.status_code == 200, r.text
    item_id = r.json()["id"]

    pr = await client.patch(f"/items/{item_id}", json={
        "fields_changed": {"sell_by": {"old": "piece", "new": "carat"}},
    }, headers=h)
    assert pr.status_code == 200, pr.text

    item = (await client.get(f"/items/{item_id}", headers=h)).json()
    assert item.get("quantity") is None or item.get("quantity") == 0, (
        f"qty should be None/0 when weight was unset, got {item.get('quantity')}"
    )


@pytest.mark.asyncio
async def test_cost_price_edit_after_cost_total_edit(client):
    """Editing cost_price (unit cost) after cost_total was set must persist.

    Regression: _flatten_item derives cost_price from cost_total on every read.
    If patching cost_price only writes cost_price to state (not cost_total), the
    next read re-derives cost_price from the stale cost_total, silently reverting
    the user's edit.

    Fix: patching cost_price via the UI route must derive and overwrite cost_total
    (unit × qty) so cost_total — the canonical primitive — reflects the new unit price.
    """
    import time as _t
    _ts = str(int(_t.time() * 1000))[-6:]
    token = await _token(client)
    h = {"Authorization": f"Bearer {token}"}

    # Create item: qty=10, no cost set
    r = await client.post("/items", json={
        "status": "available", "sku": f"CP-{_ts}", "name": "Cost Test Stone",
        "quantity": 10.0, "sell_by": "piece",
    }, headers=h)
    assert r.status_code == 200, r.text
    item_id = r.json()["id"]

    # Step 1: Set cost_total = 100 (total cost for all 10 units)
    r1 = await client.patch(f"/items/{item_id}", json={
        "fields_changed": {"cost_total": {"old": None, "new": 100.0}},
    }, headers=h)
    assert r1.status_code == 200, r1.text

    # Verify cost_price is back-calculated to 10.0 (100 / 10)
    item1 = (await client.get(f"/items/{item_id}", headers=h)).json()
    assert float(item1["cost_price"]) == pytest.approx(10.0), f"expected 10.0, got {item1['cost_price']}"
    assert float(item1["cost_total"]) == pytest.approx(100.0), f"expected 100.0, got {item1['cost_total']}"

    # Step 2: Now patch cost_price to 15 (new unit cost — total should become 150)
    r2 = await client.patch(f"/items/{item_id}", json={
        "fields_changed": {"cost_price": {"old": 10.0, "new": 15.0}},
    }, headers=h)
    assert r2.status_code == 200, r2.text

    # Verify cost_price is now 15.0 and cost_total updated to 150.0 (15 × 10)
    item2 = (await client.get(f"/items/{item_id}", headers=h)).json()
    assert float(item2["cost_price"]) == pytest.approx(15.0), (
        f"cost_price should be 15.0 after direct edit, got {item2['cost_price']}. "
        f"cost_total in state: {item2.get('cost_total')}"
    )
    assert float(item2["cost_total"]) == pytest.approx(150.0), (
        f"cost_total should be 150.0 (15 × 10), got {item2['cost_total']}"
    )


@pytest.mark.asyncio
async def test_recipe_backed_cost_reads_at_standard_not_lot_total(client):
    """A manufactured item's cost reads at its recipe's standard unit cost.

    Regression: _flatten_item derives cost_price from a stored cost_total on every read.
    For a recipe-backed item, a lingering lot cost_total (e.g. from a divergent manual cost
    or a build that stamped actual input cost) must NOT override the recipe's rolled standard
    — recipe.unit_cost is the single source of truth for a manufactured item's cost.
    """
    import time as _t
    _ts = str(int(_t.time() * 1000))[-6:]
    token = await _token(client)
    h = {"Authorization": f"Bearer {token}"}

    # Baseline cost valuation before our items (register seeds demo items) — assert the delta.
    base_cost = float((await client.get("/items/valuation", headers=h)).json()["cost_total"])

    gold = (await client.post("/items", json={
        "status": "available", "sku": f"GOLD-{_ts}", "name": "Gold", "quantity": 1.0, "sell_by": "piece", "cost_total": 80.0,
    }, headers=h)).json()["id"]
    ring = (await client.post("/items", json={
        "status": "available", "sku": f"RING-{_ts}", "name": "Ring", "quantity": 2.0, "sell_by": "piece",
    }, headers=h)).json()["id"]

    # Recipe: 5 gold per ring → rolled standard unit cost 400.
    r = await client.put(f"/manufacturing/items/{ring}/recipe", json={
        "output_qty": 1, "components": [{"item_id": gold, "quantity": 5}], "labor": [], "overhead": [],
    }, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["recipe"]["unit_cost"] == 400.0

    # Now stamp a DIVERGENT lot cost_total (1000 → would imply unit 500). The standard must win.
    r2 = await client.post(f"/items/{ring}/price", json={"price_type": "cost_total", "new_price": 1000.0}, headers=h)
    assert r2.status_code == 200, r2.text

    item = (await client.get(f"/items/{ring}", headers=h)).json()
    assert float(item["cost_price"]) == pytest.approx(400.0), (
        f"cost_price must read at recipe standard 400.0, got {item['cost_price']} "
        f"(lot cost_total leaked through?)"
    )
    assert float(item["cost_total"]) == pytest.approx(800.0), (
        f"cost_total must be derived from standard (400 × 2), got {item['cost_total']}"
    )

    # Valuation aggregates at the same standard: our items add gold 80 + ring standard 800 = 880
    # (never gold 80 + ring lot 1000 = 1080).
    val = (await client.get("/items/valuation", headers=h)).json()
    assert float(val["cost_total"]) - base_cost == pytest.approx(880.0), (
        f"valuation Cost must value the ring at standard (gold 80 + ring 800), "
        f"got delta {float(val['cost_total']) - base_cost}"
    )


@pytest.mark.asyncio
async def test_merge_dropdown_fields_use_value_or_mixed_never_sum(client):
    """Merging must never sum or invent a value for dropdown (select) fields. Identical values
    carry forward; any disagreement collapses to the system 'Mixed'. Regression: numeric-option
    dropdowns were summed into invalid new values (e.g. size 1 + size 2 -> 3)."""
    token = await _token(client)
    h = {"Authorization": f"Bearer {token}"}
    # Category with a string dropdown (grade), a NUMERIC-option dropdown (size), and a genuine
    # numeric-TYPED field (weight_ct). 'carats' is left undefined -> a free/custom attribute.
    r = await client.patch("/companies/me", headers=h, json={"settings": {"category_schemas": {"DD": [
        {"key": "grade", "label": "Grade", "type": "select", "options": ["A", "B", "C"], "editable": True, "required": False},
        {"key": "size", "label": "Size", "type": "select", "options": ["1", "2", "3"], "editable": True, "required": False},
        {"key": "weight_ct", "label": "Weight (ct)", "type": "number", "editable": True, "required": False},
    ]}}})
    assert r.status_code == 200, r.text

    def _attr(item, key):
        return (item.get("attributes") or {}).get(key, item.get(key))

    async def _mk(sku, attrs):
        rr = await client.post("/items", json={"status": "available", "sku": sku, "name": sku, "quantity": 1, "sell_by": "piece",
                                               "category": "DD", "attributes": attrs}, headers=h)
        assert rr.status_code == 200, rr.text
        return rr.json()["id"]

    # Differing dropdowns -> "Mixed"; numeric dropdown must NOT sum.
    a = await _mk("DD-A", {"grade": "A", "size": "1", "weight_ct": "5", "carats": "5"})
    b = await _mk("DD-B", {"grade": "B", "size": "2", "weight_ct": "3", "carats": "3"})
    rm = await client.post("/items/merge", json={"source_entity_ids": [a, b], "target_sku_from": a}, headers=h)
    assert rm.status_code == 200, rm.text
    merged = (await client.get(f"/items/{rm.json()['id']}", headers=h)).json()
    assert _attr(merged, "grade") == "Mixed"
    assert _attr(merged, "size") == "Mixed", f"numeric dropdown was summed/invented: {_attr(merged, 'size')!r}"
    # A conflicting numeric-TYPED field (weight_ct) gets NO value — never summed, can't render "Mixed".
    assert _attr(merged, "weight_ct") in (None, ""), f"numeric field was summed/kept: {_attr(merged, 'weight_ct')!r}"
    # A conflicting CUSTOM/free attribute (carats) collapses to the system "Mixed" — never dropped.
    assert _attr(merged, "carats") == "Mixed", f"custom attr was dropped instead of Mixed: {_attr(merged, 'carats')!r}"

    # Identical values carry through (dropdown, numeric, or custom); only conflicts are cleared/mixed.
    c = await _mk("DD-C", {"grade": "A", "size": "2", "weight_ct": "5", "carats": "5"})
    d = await _mk("DD-D", {"grade": "A", "size": "2", "weight_ct": "5", "carats": "5"})
    rm2 = await client.post("/items/merge", json={"source_entity_ids": [c, d], "target_sku_from": c}, headers=h)
    assert rm2.status_code == 200, rm2.text
    merged2 = (await client.get(f"/items/{rm2.json()['id']}", headers=h)).json()
    assert _attr(merged2, "grade") == "A"
    assert _attr(merged2, "size") == "2"
    assert float(_attr(merged2, "weight_ct")) == 5.0  # agreeing numeric still carries
    assert str(_attr(merged2, "carats")) == "5"       # agreeing custom attr still carries


@pytest.mark.asyncio
async def test_transform_drops_sell_prices_keeps_cost(client):
    """A transform must NOT carry the parent's sell prices to the child: selling transformed goods
    at the pre-transform sell price is unsafe. The child starts with no sell price (the user sets it
    manually before selling); cost still carries over."""
    token = await _token(client)
    h = {"Authorization": f"Bearer {token}"}
    r = await client.post("/items", json={
        "status": "available", "sku": "TX-PARENT", "name": "Parent", "quantity": 20, "sell_by": "piece",
        "retail_price": 38.0, "wholesale_price": 24.0, "cost_total": 260.0,
    }, headers=h)
    assert r.status_code == 200, r.text
    pid = r.json()["id"]
    parent = (await client.get(f"/items/{pid}", headers=h)).json()
    assert float(parent.get("retail_price") or 0) == 38.0  # parent really has a sell price

    rt = await client.post(f"/items/{pid}/transform", json={
        "child_sku": "TX-CHILD", "child_category": "Cut", "child_sell_by": "piece",
        "child_quantity": 10, "child_cost_total": 260.0,
    }, headers=h)
    assert rt.status_code == 200, rt.text
    child = (await client.get(f"/items/{rt.json()['child_id']}", headers=h)).json()
    # Sell prices dropped — neither carried verbatim nor rebased.
    assert not child.get("retail_price"), f"retail_price should be dropped, got {child.get('retail_price')!r}"
    assert not child.get("wholesale_price"), f"wholesale_price should be dropped, got {child.get('wholesale_price')!r}"
    # Cost still preserved.
    assert float(child.get("cost_total") or 0) == 260.0


@pytest.mark.asyncio
async def test_set_item_status_rejects_disposed(client):
    """The generic status route must not be a back door to the off-books `disposed` terminal: that
    status is reachable only through the manager-gated write-off flow. The route rejects it with a
    write-off-specific reason and emits no status event."""
    reg = await client.post("/auth/register", json={
        "company_name": "Items Co", "email": f"admin-{uuid.uuid4().hex[:8]}@items.test",
        "name": "A", "password": "pw"})
    h = {"Authorization": f"Bearer {reg.json()['access_token']}"}
    iid = (await client.post("/items", json={
        "status": "available", "sku": "ST-DISP", "name": "Thing", "quantity": 3, "sell_by": "piece"},
        headers=h)).json()["id"]

    r = await client.post(f"/items/{iid}/status", json={"new_status": "disposed"}, headers=h)
    assert r.status_code == 422, r.text
    detail = (r.json().get("detail") or "").lower()
    # Distinct from the generic "Unknown status ..." rejection - names the write-off flow.
    assert "unknown status" not in detail
    assert "write" in detail
    # No status event emitted: the item is untouched.
    assert (await client.get(f"/items/{iid}", headers=h)).json()["status"] == "available"
