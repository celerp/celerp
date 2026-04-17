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
