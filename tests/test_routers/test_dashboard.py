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

    Registration seeds demo items, so we capture the baseline before adding our
    own items and assert the delta rather than an absolute total.
    """
    token = await _register(client)

    # Capture baseline (includes demo-seeded items)
    r0 = await client.get("/dashboard/kpis", headers=_h(token))
    assert r0.status_code == 200
    inv0 = r0.json()["inventory"]
    base_cost = inv0.get("total_value_cost", 0)
    base_retail = inv0.get("total_value_retail", 0)

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
    # 5*10 + 3*5 = 65 cost added; 5*20 + 3*15 = 145 retail added
    delta_cost = inv["total_value_cost"] - base_cost
    delta_retail = inv["total_value_retail"] - base_retail
    assert delta_cost == pytest.approx(65.0, abs=0.01), \
        f"cost delta should be 65.0 but got {delta_cost}"
    assert delta_retail == pytest.approx(145.0, abs=0.01), \
        f"retail delta should be 145.0 but got {delta_retail}"


@pytest.mark.asyncio
async def test_dashboard_inventory_valuation_non_zero_with_priced_items(client):
    """Verify /items/valuation returns correct cost_total and retail_total.

    Uses delta approach: registration seeds demo items so we compare before/after.
    """
    token = await _register(client)

    # Capture baseline
    r0 = await client.get("/items/valuation", headers=_h(token))
    assert r0.status_code == 200
    base_cost = r0.json()["cost_total"]
    base_retail = r0.json()["retail_total"]

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
    # 5*10 + 3*5 = 65 cost added; 5*20 + 3*15 = 145 retail added
    delta_cost = val["cost_total"] - base_cost
    delta_retail = val["retail_total"] - base_retail
    assert delta_cost == pytest.approx(65.0, abs=0.01), \
        f"cost delta should be 65.0 but got {delta_cost}"
    assert delta_retail == pytest.approx(145.0, abs=0.01), \
        f"retail delta should be 145.0 but got {delta_retail}"


@pytest.mark.asyncio
async def test_dashboard_valuation_prices_in_attributes(client):
    """Regression: demo-seeded items store prices inside attributes dict.

    The projection handler must promote *_price fields from attributes to
    top-level state so valuation stats read them correctly.
    """
    from celerp_inventory.projections import apply_item_event

    state: dict = {}
    data = {
        "sku": "SEED-1",
        "name": "Seeded Item",
        "quantity": 4,
        "status": "available",
        "sell_by": "piece",
        "attributes": {"cost_price": 25.0, "retail_price": 60.0},
    }
    state = apply_item_event(state, "item.created", data)

    assert state.get("cost_price") == 25.0, "cost_price must be promoted from attributes to top-level"
    assert state.get("retail_price") == 60.0, "retail_price must be promoted from attributes to top-level"
    # Prices must NOT be removed from attributes (don't break _flatten_item)
    assert state["attributes"]["cost_price"] == 25.0


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
