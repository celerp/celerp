# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

from __future__ import annotations

import uuid

import pytest


async def _register(client) -> str:
    email = f"admin-{uuid.uuid4().hex[:8]}@dash.test"
    r = await client.post("/auth/register", json={"company_name": "Dash Co", "email": email, "name": "Admin", "password": "pw"})
    assert r.status_code == 200
    return r.json()["access_token"]


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_dashboard_kpis_shape(client):
    token = await _register(client)
    await client.post("/items", headers=_h(token), json={"sku": "D-1", "name": "Item", "quantity": 1, "sell_by": "piece"})

    r = await client.get("/dashboard/kpis", headers=_h(token))
    assert r.status_code == 200
    data = r.json()

    assert set(data.keys()) == {"inventory", "sales", "purchasing", "manufacturing", "crm", "subscriptions"}
    assert {"total_items", "items_in_production", "low_stock_items"}.issubset(data["inventory"].keys())
    assert {"revenue_mtd", "ar_outstanding", "invoices_outstanding"}.issubset(data["sales"].keys())
    assert {"spend_mtd", "pending_pos", "ap_outstanding"}.issubset(data["purchasing"].keys())
    assert {"orders_in_progress", "orders_completed_mtd", "orders_overdue"}.issubset(data["manufacturing"].keys())
    assert {"total_contacts", "active_deals", "deal_value_pipeline"}.issubset(data["crm"].keys())


@pytest.mark.asyncio
async def test_dashboard_kpis_inventory_values_computed_correctly(client):
    """Regression: Cost Value and Retail Value showed 0 even with priced items.

    Root cause: /dashboard/kpis used sum(retail_price) - a per-unit price never
    multiplied by quantity - and sum(total_cost) which is only written on merges.
    Both were always 0 for normally-created items.

    Fix: compute total_value_cost = sum(qty * cost_price) and
    total_value_retail = sum(qty * retail_price) directly in /dashboard/kpis,
    excluding archived/deleted/consignment-in items (same exclusions as /items/valuation).
    """
    token = await _register(client)
    r1 = await client.post("/items", headers=_h(token), json={
        "sku": "VAL-A", "name": "Priced Item A", "quantity": 5,
        "sell_by": "piece", "cost_price": 10.0, "retail_price": 20.0,
    })
    assert r1.status_code == 200
    r2 = await client.post("/items", headers=_h(token), json={
        "sku": "VAL-B", "name": "Priced Item B", "quantity": 3,
        "sell_by": "piece", "cost_price": 5.0, "retail_price": 15.0,
    })
    assert r2.status_code == 200

    r = await client.get("/dashboard/kpis", headers=_h(token))
    assert r.status_code == 200
    inv = r.json()["inventory"]
    # 5*10 + 3*5 = 65 cost; 5*20 + 3*15 = 145 retail
    assert inv["total_value_cost"] == pytest.approx(65.0, abs=0.01), \
        f"total_value_cost should be 65.0 but got {inv.get('total_value_cost')}"
    assert inv["total_value_retail"] == pytest.approx(145.0, abs=0.01), \
        f"total_value_retail should be 145.0 but got {inv.get('total_value_retail')}"


@pytest.mark.asyncio
async def test_dashboard_inventory_valuation_non_zero_with_priced_items(client):
    """Verify /items/valuation also returns correct cost_total and retail_total."""
    token = await _register(client)
    r1 = await client.post("/items", headers=_h(token), json={
        "sku": "VAL-C", "name": "Priced Item C", "quantity": 5,
        "sell_by": "piece", "cost_price": 10.0, "retail_price": 20.0,
    })
    assert r1.status_code == 200
    r2 = await client.post("/items", headers=_h(token), json={
        "sku": "VAL-D", "name": "Priced Item D", "quantity": 3,
        "sell_by": "piece", "cost_price": 5.0, "retail_price": 15.0,
    })
    assert r2.status_code == 200

    r = await client.get("/items/valuation", headers=_h(token))
    assert r.status_code == 200
    val = r.json()
    # 5*10 + 3*5 = 65 cost; 5*20 + 3*15 = 145 retail
    assert val["cost_total"] == pytest.approx(65.0, abs=0.01), \
        f"cost_total should be 65.0 but got {val['cost_total']}"
    assert val["retail_total"] == pytest.approx(145.0, abs=0.01), \
        f"retail_total should be 145.0 but got {val['retail_total']}"


@pytest.mark.asyncio
async def test_dashboard_activity_recent_events(client):
    token = await _register(client)
    created = await client.post("/items", headers=_h(token), json={"sku": "ACT-1", "name": "Activity Item", "quantity": 2, "sell_by": "piece"})
    assert created.status_code == 200
    entity_id = created.json()["id"]

    r = await client.get("/dashboard/activity?limit=5", headers=_h(token))
    assert r.status_code == 200
    acts = r.json()["activities"]
    assert len(acts) >= 1
    assert any(a["entity_id"] == entity_id for a in acts)
    assert all({"ts", "event_type", "entity_id", "entity_type", "actor_name"}.issubset(a.keys()) for a in acts)
