# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""To-Make allocation: in-progress supply nets demand, demand docs are FIFO-pegged to available
supply by due date, and the bulk 'Make selected' action builds each product's net shortfall."""
from __future__ import annotations

import uuid

import pytest


async def _register(client) -> str:
    addr = f"admin-{uuid.uuid4().hex[:8]}@alloc.test"
    r = await client.post("/auth/register", json={
        "company_name": "Alloc Co", "email": addr, "name": "Admin", "password": "pw"})
    assert r.status_code == 200
    return r.json()["access_token"]


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _item(client, token, sku, **kw) -> str:
    body = {"status": "available", "sku": sku, "name": sku, "sell_by": "piece", **kw}
    r = await client.post("/items", headers=_h(token), json=body)
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _recipe(client, token, item_id, components) -> None:
    r = await client.put(f"/manufacturing/items/{item_id}/recipe", headers=_h(token),
                         json={"output_qty": 1, "components": components, "labor": [], "overhead": []})
    assert r.status_code == 200, r.text


async def _finalized_invoice(client, token, item_id, sku, qty, due=None) -> str:
    body = {"doc_type": "invoice", "total": qty,
            "line_items": [{"item_id": item_id, "sku": sku, "name": sku, "quantity": qty, "unit_price": 1}]}
    if due:
        body["due_date"] = due
    inv = (await client.post("/docs", headers=_h(token), json=body)).json()
    assert (await client.post(f"/docs/{inv['id']}/finalize", headers=_h(token))).status_code == 200
    return inv["id"]


async def _board(client, token) -> dict:
    return {r["item_id"]: r for r in (await client.get("/manufacturing/to-make", headers=_h(token))).json()["items"]}


@pytest.mark.asyncio
async def test_pegging_covers_earliest_due_first(client):
    """On-hand supply is pegged to the soonest-due demand first; later demand is left short."""
    token = await _register(client)
    gold = await _item(client, token, "GOLD", quantity=100, cost_total=8000)
    ring = await _item(client, token, "RING", quantity=3)  # 3 on hand
    await _recipe(client, token, ring, [{"item_id": gold, "quantity": 1}])
    await _finalized_invoice(client, token, ring, "RING", 4, due="2026-07-01")  # earlier
    await _finalized_invoice(client, token, ring, "RING", 2, due="2026-08-01")  # later

    row = (await _board(client, token))[ring]
    assert row["demand"] == 6.0 and row["on_hand"] == 3.0 and row["in_progress"] == 0.0
    assert row["to_make"] == 3.0  # 6 demand - 3 supply
    docs = {d["due"]: d for d in row["docs"]}
    early, late = docs["2026-07-01"], docs["2026-08-01"]
    assert early["covered"] == 3.0 and early["shortfall"] == 1.0 and early["coverage"] == "partial"
    assert late["covered"] == 0.0 and late["shortfall"] == 2.0 and late["coverage"] == "short"


@pytest.mark.asyncio
async def test_in_progress_runs_reduce_to_make(client):
    """An open run's expected output counts as committed supply and nets the To-Make quantity."""
    token = await _register(client)
    gold = await _item(client, token, "G", quantity=100, cost_total=8000)
    ring = await _item(client, token, "R", quantity=0)
    await _recipe(client, token, ring, [{"item_id": gold, "quantity": 1}])
    await _finalized_invoice(client, token, ring, "R", 5)
    # Start a planned run for 2 -> in-progress supply.
    assert (await client.post(f"/manufacturing/items/{ring}/build", headers=_h(token),
                              json={"quantity": 2})).status_code == 200

    row = (await _board(client, token))[ring]
    assert row["demand"] == 5.0 and row["on_hand"] == 0.0 and row["in_progress"] == 2.0
    assert row["to_make"] == 3.0  # 5 demand - 0 on hand - 2 in progress


@pytest.mark.asyncio
async def test_make_work_orders_links_to_order_at_shortfall(client):
    """Making a demand line creates a work order at the line's shortfall, linked 1:1 to its order."""
    token = await _register(client)
    gold = await _item(client, token, "G2", quantity=100, cost_total=8000)
    ring = await _item(client, token, "R2", quantity=1)  # 1 on hand
    await _recipe(client, token, ring, [{"item_id": gold, "quantity": 1}])
    await _finalized_invoice(client, token, ring, "R2", 4)  # demand 4 - on hand 1 -> shortfall 3
    board = await _board(client, token)
    doc_id = board[ring]["docs"][0]["doc_id"]
    assert board[ring]["to_make"] == 3.0

    res = (await client.post("/manufacturing/to-make/make", headers=_h(token),
                             json={"lines": [{"item_id": ring, "doc_id": doc_id}]})).json()
    assert len(res["created"]) == 1 and res["created"][0]["quantity"] == 3.0 and res["skipped"] == []
    run = (await client.get(f"/manufacturing/{res['created'][0]['run_id']}", headers=_h(token))).json()
    assert run["output_item_id"] == ring
    assert run["source_doc_id"] == doc_id  # the 1:1 link back to the order

    board = await _board(client, token)
    assert board[ring]["in_progress"] == 3.0 and board[ring]["to_make"] == 0.0


@pytest.mark.asyncio
async def test_auto_create_work_orders_on_finalize(client):
    """With the company setting on, finalizing an order auto-creates a linked work order per line."""
    token = await _register(client)
    await client.patch("/companies/me", headers=_h(token),
                       json={"settings": {"manufacturing": {"auto_create_work_orders": True}}})
    gold = await _item(client, token, "GA", quantity=100, cost_total=8000)
    ring = await _item(client, token, "RA", quantity=0)
    await _recipe(client, token, ring, [{"item_id": gold, "quantity": 1}])
    inv = (await client.post("/docs", headers=_h(token), json={"doc_type": "invoice", "total": 2,
           "line_items": [{"item_id": ring, "sku": "RA", "name": "Ring", "quantity": 2, "unit_price": 1}]})).json()

    await client.post(f"/docs/{inv['id']}/finalize", headers=_h(token))  # fires the auto-create hook

    runs = [r for r in (await client.get("/manufacturing", headers=_h(token))).json()["items"]
            if r.get("output_item_id") == ring]
    assert len(runs) == 1
    assert runs[0]["source_doc_id"] == inv["id"] and runs[0]["expected_outputs"][0]["quantity"] == 2.0


@pytest.mark.asyncio
async def test_no_auto_create_when_setting_off(client):
    """With the setting off (default), finalizing creates no work orders."""
    token = await _register(client)
    gold = await _item(client, token, "GO", quantity=100, cost_total=8000)
    ring = await _item(client, token, "RO", quantity=0)
    await _recipe(client, token, ring, [{"item_id": gold, "quantity": 1}])
    inv = (await client.post("/docs", headers=_h(token), json={"doc_type": "invoice", "total": 2,
           "line_items": [{"item_id": ring, "sku": "RO", "name": "Ring", "quantity": 2, "unit_price": 1}]})).json()

    await client.post(f"/docs/{inv['id']}/finalize", headers=_h(token))

    runs = [r for r in (await client.get("/manufacturing", headers=_h(token))).json()["items"]
            if r.get("output_item_id") == ring]
    assert runs == []


@pytest.mark.asyncio
async def test_make_work_orders_complete_one_tap(client):
    """complete=True issues components, receives output and closes each work order in one call."""
    token = await _register(client)
    gold = await _item(client, token, "GC", quantity=100, cost_total=8000)
    ring = await _item(client, token, "RC", quantity=0)
    await _recipe(client, token, ring, [{"item_id": gold, "quantity": 1}])
    await _finalized_invoice(client, token, ring, "RC", 3)
    doc_id = (await _board(client, token))[ring]["docs"][0]["doc_id"]

    res = (await client.post("/manufacturing/to-make/make", headers=_h(token),
                             json={"lines": [{"item_id": ring, "doc_id": doc_id}], "complete": True})).json()
    assert len(res["created"]) == 1
    run_id = res["created"][0]["run_id"]
    assert (await client.get(f"/manufacturing/{run_id}", headers=_h(token))).json()["status"] == "completed"
    # Output restocked, components consumed.
    assert (await client.get(f"/items/{ring}", headers=_h(token))).json()["quantity"] == 3.0
    assert (await client.get(f"/items/{gold}", headers=_h(token))).json()["quantity"] == 97.0


@pytest.mark.asyncio
async def test_requirements_aggregates_components(client):
    """The requirements pick list explodes recipes to raw materials for the selected shortfall."""
    token = await _register(client)
    gold = await _item(client, token, "GR", quantity=0, cost_total=8000)
    ring = await _item(client, token, "RR", quantity=0)
    await _recipe(client, token, ring, [{"item_id": gold, "quantity": 5}])
    await _finalized_invoice(client, token, ring, "RR", 4)  # to_make 4 -> 20 gold

    data = (await client.post("/manufacturing/to-make/requirements", headers=_h(token),
                              json={"item_ids": [ring]})).json()
    assert any(p["item_id"] == ring and p["quantity"] == 4.0 for p in data["products"])
    raw = {r["item_id"]: r for r in data["raw_materials"]}
    assert gold in raw and raw[gold]["quantity"] == 20.0


@pytest.mark.asyncio
async def test_bulk_run_action_start_then_complete(client):
    """The bulk run-action endpoint advances many runs at once; invalid-state runs are skipped."""
    token = await _register(client)
    gold = await _item(client, token, "GB", quantity=100, cost_total=8000)
    ring = await _item(client, token, "RB", quantity=0)
    await _recipe(client, token, ring, [{"item_id": gold, "quantity": 1}])
    r1 = (await client.post(f"/manufacturing/items/{ring}/build", headers=_h(token), json={"quantity": 1})).json()["id"]
    r2 = (await client.post(f"/manufacturing/items/{ring}/build", headers=_h(token), json={"quantity": 2})).json()["id"]

    started = (await client.post("/manufacturing/bulk-action", headers=_h(token),
                                 json={"run_ids": [r1, r2], "action": "start"})).json()
    assert set(started["done"]) == {r1, r2}
    for rid in (r1, r2):
        assert (await client.get(f"/manufacturing/{rid}", headers=_h(token))).json()["status"] == "in_progress"

    completed = (await client.post("/manufacturing/bulk-action", headers=_h(token),
                                   json={"run_ids": [r1, r2], "action": "complete"})).json()
    assert set(completed["done"]) == {r1, r2}
    assert (await client.get(f"/items/{ring}", headers=_h(token))).json()["quantity"] == 3.0

    # Re-completing closed runs is skipped, not an error.
    again = (await client.post("/manufacturing/bulk-action", headers=_h(token),
                               json={"run_ids": [r1], "action": "complete"})).json()
    assert again["done"] == [] and len(again["skipped"]) == 1


@pytest.mark.asyncio
async def test_make_work_orders_skips_fully_covered(client):
    """A demand line already covered by stock has nothing to make and is skipped."""
    token = await _register(client)
    gold = await _item(client, token, "G3", quantity=100, cost_total=8000)
    ring = await _item(client, token, "R3", quantity=10)  # plenty on hand
    await _recipe(client, token, ring, [{"item_id": gold, "quantity": 1}])
    await _finalized_invoice(client, token, ring, "R3", 4)  # covered by on-hand
    doc_id = (await _board(client, token))[ring]["docs"][0]["doc_id"]

    res = (await client.post("/manufacturing/to-make/make", headers=_h(token),
                             json={"lines": [{"item_id": ring, "doc_id": doc_id}]})).json()
    assert res["created"] == [] and len(res["skipped"]) == 1
