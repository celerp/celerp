# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Tests for the inventory transform bulk action."""

from __future__ import annotations

import pytest


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
async def test_transform_child_sku_collision(client):
    token = await _token(client)
    headers = {"Authorization": f"Bearer {token}"}
    # Create an item with SKU "CHILD-SKU" first
    await client.post("/items", json={"sku": "CHILD-SKU", "name": "Existing", "quantity": 1, "sell_by": "piece"}, headers=headers)
    parent_id = await _seed_item(client, headers, sku="PARENT-2")

    r = await client.post(f"/items/{parent_id}/transform", json=_transform_payload(child_sku="CHILD-SKU"), headers=headers)
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_transform_parent_not_found(client):
    token = await _token(client)
    headers = {"Authorization": f"Bearer {token}"}

    r = await client.post("/items/item:00000000-0000-0000-0000-000000000000/transform", json=_transform_payload(), headers=headers)
    assert r.status_code == 404
