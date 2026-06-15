# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Work Centers (relational CRUD, like locations) and Manufacturing settings effects.

Settings live under company.settings["manufacturing"]: hours_per_day feeds the To-Make est-hours
column, and require_issued_before_complete gates run completion.
"""
from __future__ import annotations

import uuid

import pytest


async def _register(client) -> str:
    addr = f"admin-{uuid.uuid4().hex[:8]}@wc.test"
    r = await client.post("/auth/register", json={"company_name": "WC Co", "email": addr, "name": "Admin", "password": "pw"})
    assert r.status_code == 200
    return r.json()["access_token"]


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# --- Work Centers CRUD ------------------------------------------------------

@pytest.mark.asyncio
async def test_work_center_crud(client):
    token = await _register(client)
    # Empty list.
    assert (await client.get("/manufacturing/work-centers", headers=_h(token))).json()["items"] == []

    # Create.
    wc = (await client.post("/manufacturing/work-centers", headers=_h(token),
                            json={"name": "Polishing Bench", "labor_rate": 45.0})).json()
    wc_id = wc["id"]
    items = (await client.get("/manufacturing/work-centers", headers=_h(token))).json()["items"]
    assert len(items) == 1 and items[0]["name"] == "Polishing Bench" and items[0]["labor_rate"] == 45.0

    # Duplicate name rejected.
    assert (await client.post("/manufacturing/work-centers", headers=_h(token),
                              json={"name": "Polishing Bench"})).status_code == 409
    # Blank name rejected.
    assert (await client.post("/manufacturing/work-centers", headers=_h(token),
                              json={"name": "  "})).status_code == 422

    # Patch.
    assert (await client.patch(f"/manufacturing/work-centers/{wc_id}", headers=_h(token),
                               json={"name": "Bench 1", "labor_rate": 50.0})).status_code == 200
    items = (await client.get("/manufacturing/work-centers", headers=_h(token))).json()["items"]
    assert items[0]["name"] == "Bench 1" and items[0]["labor_rate"] == 50.0

    # Delete.
    assert (await client.delete(f"/manufacturing/work-centers/{wc_id}", headers=_h(token))).status_code == 200
    assert (await client.get("/manufacturing/work-centers", headers=_h(token))).json()["items"] == []
    # Patch / delete on a missing one -> 404.
    assert (await client.patch(f"/manufacturing/work-centers/{wc_id}", headers=_h(token),
                               json={"name": "x"})).status_code == 404
    assert (await client.delete(f"/manufacturing/work-centers/{wc_id}", headers=_h(token))).status_code == 404


# --- Settings: hours_per_day feeds est-hours --------------------------------

async def _ring_with_daily_labor(client, token):
    gold = (await client.post("/items", headers=_h(token),
                              json={"sku": f"G-{uuid.uuid4().hex[:6]}", "name": "Gold", "quantity": 100,
                                    "sell_by": "gram", "cost_total": 8000})).json()["id"]
    ring = (await client.post("/items", headers=_h(token),
                              json={"sku": f"R-{uuid.uuid4().hex[:6]}", "name": "Ring", "quantity": 0,
                                    "sell_by": "piece"})).json()["id"]
    await client.put(f"/manufacturing/items/{ring}/recipe", headers=_h(token),
                     json={"output_qty": 1, "components": [{"item_id": gold, "quantity": 1}],
                           "labor": [{"operation": "Cast", "kind": "daily", "hours": 1, "rate": 100}], "overhead": []})
    await client.post("/docs", headers=_h(token), json={"doc_type": "invoice", "line_items": [
        {"item_id": ring, "sku": "R", "name": "Ring", "quantity": 1, "unit_price": 1}], "total": 1})
    return ring


async def _est_hours(client, token, ring) -> float:
    board = {r["item_id"]: r for r in (await client.get("/manufacturing/to-make", headers=_h(token))).json()["items"]}
    return board[ring]["est_hours"]


@pytest.mark.asyncio
async def test_hours_per_day_setting_drives_est_hours(client):
    token = await _register(client)
    ring = await _ring_with_daily_labor(client, token)
    # Default 8 hours/day: 1 day x 8 = 8 est hours for 1 to make.
    assert await _est_hours(client, token, ring) == 8.0
    # Raise the setting to 10 -> 1 day x 10 = 10.
    assert (await client.patch("/companies/me", headers=_h(token),
                               json={"settings": {"manufacturing": {"hours_per_day": 10}}})).status_code == 200
    assert await _est_hours(client, token, ring) == 10.0


# --- Settings: require_issued_before_complete -------------------------------

@pytest.mark.asyncio
async def test_require_issued_before_complete_setting(client):
    token = await _register(client)
    gold = (await client.post("/items", headers=_h(token),
                              json={"sku": "GREQ", "name": "Gold", "quantity": 100, "sell_by": "gram",
                                    "cost_total": 8000})).json()["id"]
    ring = (await client.post("/items", headers=_h(token),
                              json={"sku": "RREQ", "name": "Ring", "quantity": 0, "sell_by": "piece"})).json()["id"]
    await client.put(f"/manufacturing/items/{ring}/recipe", headers=_h(token),
                     json={"output_qty": 1, "components": [{"item_id": gold, "quantity": 5}], "labor": [], "overhead": []})

    # With the guard on, completing a run whose components are not issued is rejected.
    assert (await client.patch("/companies/me", headers=_h(token),
                               json={"settings": {"manufacturing": {"require_issued_before_complete": True}}})).status_code == 200
    run = (await client.post(f"/manufacturing/items/{ring}/build", headers=_h(token), json={"quantity": 1})).json()["id"]
    assert (await client.post(f"/manufacturing/{run}/complete", headers=_h(token), json={})).status_code == 409
    # Issue first, then completion succeeds.
    assert (await client.post(f"/manufacturing/{run}/issue", headers=_h(token))).status_code == 200
    assert (await client.post(f"/manufacturing/{run}/complete", headers=_h(token), json={})).status_code == 200
