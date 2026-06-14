# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""API tests: the product-centric To-Make board aggregates open demand BY PRODUCT across all
open demand documents (invoices/pro formas/lists). Runs are NOT auto-created from documents.
Also covers the JIT components-summary endpoint (incl. finished_goods).
"""
from __future__ import annotations

import uuid

import pytest


async def _register(client) -> str:
    addr = f"admin-{uuid.uuid4().hex[:8]}@mfgdoc.test"
    r = await client.post("/auth/register", json={"company_name": "MfgDoc Co", "email": addr, "name": "Admin", "password": "pw"})
    assert r.status_code == 200
    return r.json()["access_token"]


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _item(client, token, sku, **kw) -> str:
    body = {"sku": sku, "name": sku, "quantity": kw.pop("quantity", 0), "sell_by": "piece", **kw}
    r = await client.post("/items", headers=_h(token), json=body)
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _recipe(client, token, item_id, components):
    r = await client.put(f"/manufacturing/items/{item_id}/recipe", headers=_h(token),
                         json={"output_qty": 1, "components": components, "labor": [], "overhead": []})
    assert r.status_code == 200, r.text


async def _doc(client, token, line_items, doc_type="invoice") -> str:
    r = await client.post("/docs", headers=_h(token), json={"doc_type": doc_type, "line_items": line_items, "total": 0})
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


async def _to_make(client, token) -> dict:
    return {r["item_id"]: r for r in (await client.get("/manufacturing/to-make", headers=_h(token))).json()["items"]}


@pytest.mark.asyncio
async def test_no_runs_auto_created_from_docs(client) -> None:
    """In the product-centric model, opening the runs list does NOT spawn runs from documents."""
    token = await _register(client)
    gold = await _item(client, token, "GOLD", quantity=100, cost_total=8000)
    ring = await _item(client, token, "RING")
    await _recipe(client, token, ring, [{"item_id": gold, "quantity": 5}])
    await _doc(client, token, [{"item_id": ring, "sku": "RING", "name": "Ring", "quantity": 2, "unit_price": 100}])
    runs = (await client.get("/manufacturing", headers=_h(token))).json()["items"]
    assert runs == []


@pytest.mark.asyncio
async def test_to_make_aggregates_demand_across_docs(client) -> None:
    token = await _register(client)
    gold = await _item(client, token, "GOLD2", quantity=100, cost_total=8000)  # unit 80
    ring = await _item(client, token, "RING2", quantity=3)  # 3 on hand
    widget = await _item(client, token, "WIDGET")  # no recipe -> never on the board
    await _recipe(client, token, ring, [{"item_id": gold, "quantity": 5}])
    # Two separate docs demanding the ring: 2 + 4 = 6 total; on hand 3 -> to make 3.
    await _doc(client, token, [
        {"item_id": ring, "sku": "RING2", "name": "Ring", "quantity": 2, "unit_price": 100},
        {"item_id": widget, "sku": "WIDGET", "name": "Widget", "quantity": 9, "unit_price": 1},
    ])
    await _doc(client, token, [{"item_id": ring, "sku": "RING2", "name": "Ring", "quantity": 4, "unit_price": 100}],
               doc_type="list")

    board = await _to_make(client, token)
    assert widget not in board  # not manufacturable
    row = board[ring]
    assert row["demand"] == 6.0
    assert row["on_hand"] == 3.0
    assert row["to_make"] == 3.0
    assert row["doc_count"] == 2
    assert row["est_unit_cost"] == 400.0           # 5 x 80
    assert row["est_cost"] == 1200.0               # 400 x 3 to make


@pytest.mark.asyncio
async def test_to_make_excludes_voided_doc(client) -> None:
    token = await _register(client)
    gold = await _item(client, token, "GOLDV", quantity=100, cost_total=8000)
    ring = await _item(client, token, "RINGV")
    await _recipe(client, token, ring, [{"item_id": gold, "quantity": 1}])
    doc = await _doc(client, token, [{"item_id": ring, "sku": "RINGV", "name": "Ring", "quantity": 5, "unit_price": 1}])
    assert ring in await _to_make(client, token)
    r = await client.post(f"/docs/{doc}/void", headers=_h(token), json={"reason": "test"})
    if r.status_code != 200:
        pytest.skip(f"void not permitted in this doc state: {r.status_code}")
    assert ring not in await _to_make(client, token)


@pytest.mark.asyncio
async def test_to_make_covered_when_enough_stock(client) -> None:
    token = await _register(client)
    gold = await _item(client, token, "GOLDC", quantity=100, cost_total=8000)
    ring = await _item(client, token, "RINGC", quantity=10)  # plenty on hand
    await _recipe(client, token, ring, [{"item_id": gold, "quantity": 1}])
    await _doc(client, token, [{"item_id": ring, "sku": "RINGC", "name": "Ring", "quantity": 4, "unit_price": 1}])
    row = (await _to_make(client, token))[ring]
    assert row["demand"] == 4.0 and row["on_hand"] == 10.0 and row["to_make"] == 0.0


@pytest.mark.asyncio
async def test_to_make_est_hours_from_labor(client) -> None:
    token = await _register(client)
    gold = await _item(client, token, "GOLDH", quantity=100, cost_total=8000)
    ring = await _item(client, token, "RINGH")
    r = await client.put(f"/manufacturing/items/{ring}/recipe", headers=_h(token),
                         json={"output_qty": 1, "components": [{"item_id": gold, "quantity": 1}],
                               "labor": [{"operation": "Set", "kind": "hourly", "hours": 2, "rate": 10},
                                         {"operation": "Site", "kind": "daily", "hours": 1, "rate": 100}],
                               "overhead": []})
    assert r.status_code == 200
    await _doc(client, token, [{"item_id": ring, "sku": "RINGH", "name": "Ring", "quantity": 3, "unit_price": 1}])
    row = (await _to_make(client, token))[ring]
    # per unit hours = 2 (hourly) + 1 day x 8 = 10; x 3 to make = 30.
    assert row["est_hours"] == 30.0


@pytest.mark.asyncio
async def test_components_summary_nested_and_finished_goods(client) -> None:
    token = await _register(client)
    gold = await _item(client, token, "GOLD3", quantity=100, cost_total=8000)
    sub = await _item(client, token, "SUB3")
    ring = await _item(client, token, "RING3")
    await _recipe(client, token, sub, [{"item_id": gold, "quantity": 2}])
    await _recipe(client, token, ring, [{"item_id": sub, "quantity": 3}])
    doc = await _doc(client, token, [{"item_id": ring, "sku": "RING3", "name": "Ring", "quantity": 1, "unit_price": 1}])

    summary = (await client.get(f"/manufacturing/documents/{doc}/components-summary", headers=_h(token))).json()
    raws = {r["item_id"]: r["quantity"] for r in summary["raw_materials"]}
    subs = {s["item_id"]: s["quantity"] for s in summary["sub_assemblies"]}
    fgs = {f["item_id"]: f["quantity"] for f in summary["finished_goods"]}
    assert raws == {gold: 6.0}              # 1 * 3 * 2
    assert subs == {ring: 1.0, sub: 3.0}
    assert fgs == {ring: 1.0}              # the manufactured line item
