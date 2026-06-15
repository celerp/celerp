# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""API tests for POST /manufacturing/items/{id}/build (the repurposed new-order flow)."""
from __future__ import annotations

import uuid

import pytest


async def _register(client) -> str:
    addr = f"admin-{uuid.uuid4().hex[:8]}@build.test"
    r = await client.post("/auth/register", json={"company_name": "Build Co", "email": addr, "name": "Admin", "password": "pw"})
    assert r.status_code == 200
    return r.json()["access_token"]


def _h(t):
    return {"Authorization": f"Bearer {t}"}


async def _item(client, token, sku, **kw):
    body = {"sku": sku, "name": sku, "quantity": kw.pop("quantity", 1), "sell_by": "piece", **kw}
    return (await client.post("/items", headers=_h(token), json=body)).json()["id"]


@pytest.mark.asyncio
async def test_build_creates_planned_run_with_expanded_inputs(client) -> None:
    token = await _register(client)
    gold = await _item(client, token, "GOLD", quantity=100, cost_total=8000)
    ring = await _item(client, token, "RING")
    await client.put(f"/manufacturing/items/{ring}/recipe", headers=_h(token),
                     json={"output_qty": 1, "components": [{"item_id": gold, "quantity": 5}], "labor": [], "overhead": []})
    r = await client.post(f"/manufacturing/items/{ring}/build", headers=_h(token), json={"quantity": 3})
    assert r.status_code == 200
    order = (await client.get(f"/manufacturing/{r.json()['id']}", headers=_h(token))).json()
    # A bare build leaves a Planned run (issued_qty 0, nothing received yet).
    assert order["status"] == "planned" and order["received_qty"] == 0.0
    assert order["inputs"] == [{"item_id": gold, "quantity": 15.0, "issued_qty": 0.0}]
    assert order["output_item_id"] == ring
    assert order["expected_outputs"][0]["sku"] == "RING" and order["expected_outputs"][0]["quantity"] == 3.0


@pytest.mark.asyncio
async def test_build_complete_one_tap_increments_splittable_sku(client) -> None:
    """allow_splitting default true: one-tap build increments the existing product, no nameless item."""
    token = await _register(client)
    gold = await _item(client, token, "GOLD2", quantity=100, cost_total=8000)
    ring = await _item(client, token, "RING2", quantity=4)
    await client.put(f"/manufacturing/items/{ring}/recipe", headers=_h(token),
                     json={"output_qty": 1, "components": [{"item_id": gold, "quantity": 5}], "labor": [], "overhead": []})
    r = await client.post(f"/manufacturing/items/{ring}/build", headers=_h(token), json={"quantity": 2, "complete": True})
    assert r.status_code == 200
    assert (await client.get(f"/manufacturing/{r.json()['id']}", headers=_h(token))).json()["status"] == "completed"
    assert (await client.get(f"/items/{ring}", headers=_h(token))).json()["quantity"] == 6  # 4 + 2, same SKU
    assert (await client.get(f"/items/{gold}", headers=_h(token))).json()["quantity"] == 90  # 100 - 10
    skus = [i.get("sku") for i in (await client.get("/items", headers=_h(token))).json()["items"]]
    assert skus.count("RING2") == 1  # restocked in place — no second/throwaway RING2 entry


@pytest.mark.asyncio
async def test_build_complete_one_tap_new_lot_for_non_splittable(client) -> None:
    """allow_splitting false: one-tap build creates a discrete lot under the product."""
    token = await _register(client)
    gold = await _item(client, token, "GOLD3", quantity=100, cost_total=8000)
    ring = await _item(client, token, "RING3", quantity=0, allow_splitting=False)
    await client.put(f"/manufacturing/items/{ring}/recipe", headers=_h(token),
                     json={"output_qty": 1, "components": [{"item_id": gold, "quantity": 5}], "labor": [], "overhead": []})
    await client.post(f"/manufacturing/items/{ring}/build", headers=_h(token), json={"quantity": 2, "complete": True})
    rings = [i for i in (await client.get("/items", headers=_h(token))).json()["items"] if i.get("sku") == "RING3"]
    assert len(rings) == 2
    lot = next(i for i in rings if i["id"] != ring)
    assert lot["quantity"] == 2 and lot["parent_item_id"] == ring


@pytest.mark.asyncio
async def test_build_no_recipe_422(client) -> None:
    token = await _register(client)
    raw = await _item(client, token, "RAW", cost_total=5)
    r = await client.post(f"/manufacturing/items/{raw}/build", headers=_h(token), json={"quantity": 1})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_build_zero_qty_422(client) -> None:
    token = await _register(client)
    gold = await _item(client, token, "G2", cost_total=5)
    ring = await _item(client, token, "R2")
    await client.put(f"/manufacturing/items/{ring}/recipe", headers=_h(token),
                     json={"output_qty": 1, "components": [{"item_id": gold, "quantity": 1}], "labor": [], "overhead": []})
    r = await client.post(f"/manufacturing/items/{ring}/build", headers=_h(token), json={"quantity": 0})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_build_unknown_404(client) -> None:
    token = await _register(client)
    r = await client.post("/manufacturing/items/item:nope/build", headers=_h(token), json={"quantity": 1})
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_list_orders_search_dates_and_created_at(client) -> None:
    token = await _register(client)
    gold = await _item(client, token, "LGOLD", quantity=100, cost_total=100)
    ring = await _item(client, token, "LRING")
    pin = await _item(client, token, "LPIN")
    for fg in (ring, pin):
        r = await client.put(f"/manufacturing/items/{fg}/recipe", headers=_h(token),
                             json={"output_qty": 1, "components": [{"item_id": gold, "quantity": 1}], "labor": [], "overhead": []})
        assert r.status_code == 200
    assert (await client.post(f"/manufacturing/items/{ring}/build", headers=_h(token), json={"quantity": 1})).status_code == 200
    assert (await client.post(f"/manufacturing/items/{pin}/build", headers=_h(token), json={"quantity": 2})).status_code == 200

    everything = (await client.get("/manufacturing", headers=_h(token))).json()["items"]
    assert len(everything) == 2
    # Newest first, and created_at is returned (GDR: newer rows on top).
    assert all(o.get("created_at") for o in everything)
    assert everything[0]["created_at"] >= everything[1]["created_at"]

    # q matches the output SKU and the description ("Build N x SKU").
    hits = (await client.get("/manufacturing", headers=_h(token), params={"q": "LRING"})).json()["items"]
    assert len(hits) == 1 and hits[0]["expected_outputs"][0]["sku"] == "LRING"

    # Date range: everything today; nothing in the far past.
    today = everything[0]["created_at"][:10]
    assert (await client.get("/manufacturing", headers=_h(token),
                             params={"date_from": today, "date_to": today})).json()["total"] == 2
    assert (await client.get("/manufacturing", headers=_h(token),
                             params={"date_to": "2000-01-01"})).json()["total"] == 0
