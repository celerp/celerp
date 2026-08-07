# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Tests for the inventory transform bulk action."""

from __future__ import annotations

import uuid

import pytest

from test_helpers import invite_user


async def _token(client) -> str:
    r = await client.post(
        "/auth/register",
        json={"company_name": "Acme", "email": "admin@acme.com", "name": "Admin", "password": "pw"},
    )
    return r.json()["access_token"]


async def _seed_item(client, headers, sku="PARENT-SKU", qty=10.0, cost_price=100.0, sell_by="piece", category="Raw") -> str:
    r = await client.post(
        "/items",
        json={"sku": sku, "name": "Parent Item", "quantity": qty, "sell_by": sell_by, "category": category, "cost_price": cost_price},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _transform_payload(**overrides) -> dict:
    base = {
        "child_sku": "CHILD-SKU",
        "child_category": "Processed",
        "child_sell_by": "gram",
        "child_quantity": 8.0,
        "child_cost_total": 1000.0,
    }
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_transform_basic(client):
    token = await _token(client)
    headers = {"Authorization": f"Bearer {token}"}
    parent_id = await _seed_item(client, headers)

    r = await client.post(f"/items/{parent_id}/transform", json=_transform_payload(), headers=headers)
    assert r.status_code == 200, r.text
    data = r.json()
    assert "child_id" in data
    assert data["child_sku"] == "CHILD-SKU"

    # Parent archived with qty preserved (audit record)
    parent = await client.get(f"/items/{parent_id}", headers=headers)
    assert parent.status_code == 200
    p = parent.json()
    assert float(p["quantity"]) == 10.0  # original qty preserved
    assert p["status"] == "archived"

    # Child created with new category and sell_by
    child = await client.get(f"/items/{data['child_id']}", headers=headers)
    assert child.status_code == 200
    c = child.json()
    assert c["category"] == "Processed"
    assert c["sell_by"] == "gram"


@pytest.mark.asyncio
async def test_transform_cost_formula_zero_loss(client):
    token = await _token(client)
    headers = {"Authorization": f"Bearer {token}"}
    parent_id = await _seed_item(client, headers, qty=1.0, cost_price=500.0)

    r = await client.post(
        f"/items/{parent_id}/transform",
        json=_transform_payload(child_quantity=1.0, child_cost_total=500.0),
        headers=headers,
    )
    assert r.status_code == 200
    child_id = r.json()["child_id"]
    child = (await client.get(f"/items/{child_id}", headers=headers)).json()
    assert float(child["cost_price"]) == pytest.approx(500.0)


@pytest.mark.asyncio
async def test_transform_cost_formula_20pct(client):
    token = await _token(client)
    headers = {"Authorization": f"Bearer {token}"}
    # parent: qty=1, cost_price=100 => parent_cost_total=100
    parent_id = await _seed_item(client, headers, qty=1.0, cost_price=100.0)

    # child_cost_total = 125.0 (100 / (1 - 0.20)); child_qty=1
    r = await client.post(
        f"/items/{parent_id}/transform",
        json=_transform_payload(child_quantity=1.0, child_cost_total=125.0),
        headers=headers,
    )
    assert r.status_code == 200
    child_id = r.json()["child_id"]
    child = (await client.get(f"/items/{child_id}", headers=headers)).json()
    assert float(child["cost_price"]) == pytest.approx(125.0)


@pytest.mark.asyncio
async def test_transform_cost_manual_override(client):
    token = await _token(client)
    headers = {"Authorization": f"Bearer {token}"}
    parent_id = await _seed_item(client, headers, qty=1.0, cost_price=100.0)

    r = await client.post(
        f"/items/{parent_id}/transform",
        json=_transform_payload(child_quantity=2.0, child_cost_total=200.0),
        headers=headers,
    )
    assert r.status_code == 200
    child_id = r.json()["child_id"]
    child = (await client.get(f"/items/{child_id}", headers=headers)).json()
    # cost_price = 200 / 2 = 100
    assert float(child["cost_price"]) == pytest.approx(100.0)


@pytest.mark.asyncio
async def test_transform_sell_by_changed(client):
    token = await _token(client)
    headers = {"Authorization": f"Bearer {token}"}
    parent_id = await _seed_item(client, headers, sell_by="piece")

    r = await client.post(
        f"/items/{parent_id}/transform",
        json=_transform_payload(child_sell_by="gram"),
        headers=headers,
    )
    assert r.status_code == 200
    child = (await client.get(f"/items/{r.json()['child_id']}", headers=headers)).json()
    assert child["sell_by"] == "gram"


@pytest.mark.asyncio
async def test_transform_category_changed(client):
    token = await _token(client)
    headers = {"Authorization": f"Bearer {token}"}
    parent_id = await _seed_item(client, headers, category="Raw")

    r = await client.post(
        f"/items/{parent_id}/transform",
        json=_transform_payload(child_category="Refined"),
        headers=headers,
    )
    assert r.status_code == 200
    child = (await client.get(f"/items/{r.json()['child_id']}", headers=headers)).json()
    assert child["category"] == "Refined"

    # Parent category unchanged
    parent = (await client.get(f"/items/{parent_id}", headers=headers)).json()
    assert parent.get("category") == "Raw"


@pytest.mark.asyncio
async def test_transform_parent_fully_consumed(client):
    token = await _token(client)
    headers = {"Authorization": f"Bearer {token}"}
    parent_id = await _seed_item(client, headers)

    r = await client.post(f"/items/{parent_id}/transform", json=_transform_payload(), headers=headers)
    assert r.status_code == 200

    parent = (await client.get(f"/items/{parent_id}", headers=headers)).json()
    assert float(parent["quantity"]) == 10.0  # original qty preserved
    assert parent["status"] == "archived"


@pytest.mark.asyncio
async def test_transform_audit_event(client):
    token = await _token(client)
    headers = {"Authorization": f"Bearer {token}"}
    parent_id = await _seed_item(client, headers, qty=2.0, cost_price=50.0)

    r = await client.post(
        f"/items/{parent_id}/transform",
        json=_transform_payload(child_quantity=2.0, child_cost_total=111.11),
        headers=headers,
    )
    assert r.status_code == 200
    child_id = r.json()["child_id"]

    # Fetch transform event from ledger
    events_r = await client.get(f"/ledger", params={"entity_id": parent_id, "event_type": "item.transform"}, headers=headers)
    assert events_r.status_code == 200
    events = events_r.json()["items"]
    transform_events = [e for e in events if e.get("event_type") == "item.transform"]
    assert len(transform_events) == 1
    d = transform_events[0]["data"]
    assert d["child_id"] == child_id
    assert d["child_sku"] == "CHILD-SKU"
    assert d["parent_cost_total"] == pytest.approx(100.0)
    assert d["child_cost_total"] == pytest.approx(111.11)


@pytest.mark.asyncio
async def test_transform_child_sku_may_reuse_existing(client):
    """Intentional contract change (2026-06-17 sku/batch plan): a transform child may
    reuse a SKU another item already uses - SKUs are product-types that repeat across
    physical lots (identity is entity_id/barcode). Formerly a 409; now succeeds."""
    token = await _token(client)
    headers = {"Authorization": f"Bearer {token}"}
    # Create an item with SKU "CHILD-SKU" first
    await client.post("/items", json={"sku": "CHILD-SKU", "name": "Existing", "quantity": 1, "sell_by": "piece"}, headers=headers)
    parent_id = await _seed_item(client, headers, sku="PARENT-2")

    r = await client.post(f"/items/{parent_id}/transform", json=_transform_payload(child_sku="CHILD-SKU"), headers=headers)
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_transform_parent_not_found(client):
    token = await _token(client)
    headers = {"Authorization": f"Bearer {token}"}

    r = await client.post("/items/item:00000000-0000-0000-0000-000000000000/transform", json=_transform_payload(), headers=headers)
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_transform_rejects_invalid_sell_by(client):
    """transform_item must reject child_sell_by not in the company unit map."""
    token = await _token(client)
    headers = {"Authorization": f"Bearer {token}"}
    parent_id = await _seed_item(client, headers, sku=f"TRANSFORM-UNIT-{uuid.uuid4().hex[:6]}")

    payload = _transform_payload()
    payload["child_sell_by"] = "invalid_unit_that_does_not_exist"

    r = await client.post(f"/items/{parent_id}/transform", json=payload, headers=headers)
    assert r.status_code == 422, f"Expected 422 for invalid sell_by, got {r.status_code}: {r.text}"
    assert "invalid_unit_that_does_not_exist" in r.text


@pytest.mark.asyncio
async def test_transform_cost_set_via_pricing_event(client):
    """transform_item must set child cost via item.pricing.set, not item.created data."""
    token = await _token(client)
    headers = {"Authorization": f"Bearer {token}"}
    parent_id = await _seed_item(client, headers, sku=f"TRANSFORM-COST-{uuid.uuid4().hex[:6]}", cost_price=100.0)

    r = await client.post(f"/items/{parent_id}/transform", json=_transform_payload(child_cost_total=75.0), headers=headers)
    assert r.status_code == 200, r.text
    child_id = r.json()["child_id"]

    # Verify cost_total is reflected in child projection
    child = (await client.get(f"/items/{child_id}", headers=headers)).json()
    assert child.get("cost_total") == pytest.approx(75.0), f"Child cost_total should be 75.0, got {child.get('cost_total')}"


@pytest.mark.asyncio
async def test_transform_inherits_purchase_fields(client):
    """Child must inherit purchase_unit, purchase_conversion_factor, purchase_sku, purchase_name from parent."""
    token = await _token(client)
    headers = {"Authorization": f"Bearer {token}"}

    # Create parent with purchase fields
    sku = f"TR-PURCH-{uuid.uuid4().hex[:6]}"
    r = await client.post("/items", json={
        "sku": sku, "name": "Parent with purchase info",
        "quantity": 10, "sell_by": "gram", "category": "Raw",
        "purchase_unit": "kg", "purchase_conversion_factor": 1000.0,
        "purchase_sku": "VENDOR-SKU-001", "purchase_name": "Raw Material Bulk",
    }, headers=headers)
    assert r.status_code == 200, r.text
    parent_id = r.json()["id"]

    r2 = await client.post(f"/items/{parent_id}/transform", json=_transform_payload(
        child_sku=f"TR-PURCH-CHILD-{uuid.uuid4().hex[:6]}",
    ), headers=headers)
    assert r2.status_code == 200, r2.text
    child_id = r2.json()["child_id"]

    child = (await client.get(f"/items/{child_id}", headers=headers)).json()
    assert child.get("purchase_unit") == "kg", f"purchase_unit not inherited: {child.get('purchase_unit')}"
    assert child.get("purchase_conversion_factor") == pytest.approx(1000.0)
    assert child.get("purchase_sku") == "VENDOR-SKU-001"
    assert child.get("purchase_name") == "Raw Material Bulk"


@pytest.mark.asyncio
async def test_transform_inherits_inventory_type(client):
    """Child must inherit inventory_type from parent."""
    token = await _token(client)
    headers = {"Authorization": f"Bearer {token}"}

    sku = f"TR-INVTYPE-{uuid.uuid4().hex[:6]}"
    r = await client.post("/items", json={
        "sku": sku, "name": "Service item",
        "quantity": 0, "sell_by": "piece", "inventory_type": "service",
    }, headers=headers)
    assert r.status_code == 200, r.text
    parent_id = r.json()["id"]

    r2 = await client.post(f"/items/{parent_id}/transform", json=_transform_payload(
        child_sku=f"TR-INVTYPE-CHILD-{uuid.uuid4().hex[:6]}",
        child_sell_by="piece",
        child_quantity=1,
    ), headers=headers)
    assert r2.status_code == 200, r2.text
    child_id = r2.json()["child_id"]

    child = (await client.get(f"/items/{child_id}", headers=headers)).json()
    assert child.get("inventory_type") == "service"


@pytest.mark.asyncio
async def test_transform_inherits_custom_attributes(client):
    """Child must inherit custom category attributes (e.g. beauty_grade) from parent."""
    token = await _token(client)
    headers = {"Authorization": f"Bearer {token}"}

    sku = f"TR-ATTR-{uuid.uuid4().hex[:6]}"
    r = await client.post("/items", json={
        "sku": sku, "name": "Gem with grade",
        "quantity": 5, "sell_by": "gram", "category": "Gem",
        "attributes": {"beauty_grade": "AAA", "origin": "Burma"},
    }, headers=headers)
    assert r.status_code == 200, r.text
    parent_id = r.json()["id"]

    r2 = await client.post(f"/items/{parent_id}/transform", json=_transform_payload(
        child_sku=f"TR-ATTR-CHILD-{uuid.uuid4().hex[:6]}",
    ), headers=headers)
    assert r2.status_code == 200, r2.text
    child_id = r2.json()["child_id"]

    child = (await client.get(f"/items/{child_id}", headers=headers)).json()
    assert child.get("beauty_grade") == "AAA" or (child.get("attributes") or {}).get("beauty_grade") == "AAA"
    assert child.get("origin") == "Burma" or (child.get("attributes") or {}).get("origin") == "Burma"


@pytest.mark.asyncio
async def test_transform_child_starts_available(client):
    """Child status must always be available regardless of parent status."""
    token = await _token(client)
    headers = {"Authorization": f"Bearer {token}"}

    parent_id = await _seed_item(client, headers, sku=f"TR-STATUS-{uuid.uuid4().hex[:6]}")

    r = await client.post(f"/items/{parent_id}/transform", json=_transform_payload(
        child_sku=f"TR-STATUS-CHILD-{uuid.uuid4().hex[:6]}",
    ), headers=headers)
    assert r.status_code == 200, r.text
    child_id = r.json()["child_id"]

    child = (await client.get(f"/items/{child_id}", headers=headers)).json()
    assert child.get("status") == "available", f"Child status should be available, got {child.get('status')}"


@pytest.mark.asyncio
async def test_transform_child_barcode_distinct_from_parent(client):
    """Child must receive a fresh barcode, not inherit the parent's barcode."""
    token = await _token(client)
    headers = {"Authorization": f"Bearer {token}"}

    parent_id = await _seed_item(client, headers, sku=f"TR-BAR-{uuid.uuid4().hex[:6]}")
    parent = (await client.get(f"/items/{parent_id}", headers=headers)).json()
    parent_barcode = parent.get("barcode")

    r = await client.post(f"/items/{parent_id}/transform", json=_transform_payload(
        child_sku=f"TR-BAR-CHILD-{uuid.uuid4().hex[:6]}",
    ), headers=headers)
    assert r.status_code == 200, r.text
    child_id = r.json()["child_id"]

    child = (await client.get(f"/items/{child_id}", headers=headers)).json()
    child_barcode = child.get("barcode")
    assert child_barcode is not None, "Child must have a barcode"
    assert child_barcode != parent_barcode, f"Child barcode must differ from parent; both={child_barcode!r}"


@pytest.mark.asyncio
async def test_transform_preserves_parent_cost_when_restricted(client, session):
    # A user without view_inventory_costs (operator has edit_inventory but not the
    # cost permission) transforms without submitting a cost - the child must inherit
    # the parent's cost, never be zeroed or rejected for the missing field.
    admin_h = {"Authorization": f"Bearer {await _token(client)}"}
    op_tok = await invite_user(client, session, admin_h, "operator@example.com", "operator")
    op_h = {"Authorization": f"Bearer {op_tok}"}
    # parent: qty=1, cost_price=100 => parent_cost_total=100
    parent_id = await _seed_item(client, admin_h, qty=1.0, cost_price=100.0)

    payload = _transform_payload(child_quantity=1.0)
    payload.pop("child_cost_total")  # restricted UI hides cost, so the field is absent
    r = await client.post(f"/items/{parent_id}/transform", json=payload, headers=op_h)
    assert r.status_code == 200, r.text
    child_id = r.json()["child_id"]
    # Read as admin (the operator cannot see cost); parent cost preserved on the child.
    child = (await client.get(f"/items/{child_id}", headers=admin_h)).json()
    assert float(child["cost_price"]) == pytest.approx(100.0)


@pytest.mark.asyncio
async def test_transform_ignores_crafted_cost_from_restricted(client, session):
    # A restricted user who crafts a cost directly into the request body must not be
    # able to set it: the endpoint ignores the submitted value and preserves parent cost.
    admin_h = {"Authorization": f"Bearer {await _token(client)}"}
    op_tok = await invite_user(client, session, admin_h, "operator@example.com", "operator")
    op_h = {"Authorization": f"Bearer {op_tok}"}
    parent_id = await _seed_item(client, admin_h, qty=1.0, cost_price=100.0)

    r = await client.post(
        f"/items/{parent_id}/transform",
        json=_transform_payload(child_quantity=1.0, child_cost_total=999999.0),
        headers=op_h,
    )
    assert r.status_code == 200, r.text
    child_id = r.json()["child_id"]
    child = (await client.get(f"/items/{child_id}", headers=admin_h)).json()
    assert float(child["cost_price"]) == pytest.approx(100.0)
    assert float(child["cost_price"]) != pytest.approx(999999.0)
