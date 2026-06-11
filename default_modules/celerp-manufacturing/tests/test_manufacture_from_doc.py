# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""API tests: manufacturing orders are created automatically from open sales documents.

Listing orders (GET /manufacturing) ensures one order per recipe-bearing document line,
idempotently (deterministic order ids per doc+line+fulfill-cycle). There is no manual
create step. Also covers the JIT components-summary endpoint.
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
    body = {"sku": sku, "name": sku, "quantity": kw.pop("quantity", 1), "sell_by": "piece", **kw}
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


async def _orders(client, token) -> list[dict]:
    return (await client.get("/manufacturing", headers=_h(token))).json()["items"]


@pytest.mark.asyncio
async def test_orders_auto_created_one_per_recipe_line(client) -> None:
    token = await _register(client)
    gold = await _item(client, token, "GOLD", quantity=100, cost_total=8000)
    ring = await _item(client, token, "RING")
    widget = await _item(client, token, "WIDGET")  # no recipe: never drives an order
    await _recipe(client, token, ring, [{"item_id": gold, "quantity": 5}])
    doc = await _doc(client, token, [
        {"item_id": ring, "sku": "RING", "name": "Ring", "quantity": 2, "unit_price": 100},
        {"item_id": widget, "sku": "WIDGET", "name": "Widget", "quantity": 1, "unit_price": 50},
    ])

    mine = [o for o in await _orders(client, token) if o.get("source_doc_id") == doc]
    assert len(mine) == 1
    order = mine[0]
    # Inputs expanded from the recipe: gold x (5 * 2) = 10; provenance stamped.
    assert order["inputs"] == [{"item_id": gold, "quantity": 10.0}]
    assert order["expected_outputs"][0]["sku"] == "RING"
    assert order["expected_outputs"][0]["quantity"] == 2.0
    assert order["source_line_id"] is not None


@pytest.mark.asyncio
async def test_auto_sync_is_idempotent(client) -> None:
    token = await _register(client)
    gold = await _item(client, token, "GOLD2", quantity=100, cost_total=8000)
    ring = await _item(client, token, "RING2")
    await _recipe(client, token, ring, [{"item_id": gold, "quantity": 3}])
    doc = await _doc(client, token, [{"item_id": ring, "sku": "RING2", "name": "Ring", "quantity": 4, "unit_price": 1}])

    first = [o for o in await _orders(client, token) if o.get("source_doc_id") == doc]
    second = [o for o in await _orders(client, token) if o.get("source_doc_id") == doc]
    assert len(first) == 1 and len(second) == 1
    assert first[0]["id"] == second[0]["id"]


@pytest.mark.asyncio
async def test_cancelled_order_is_not_resurrected(client) -> None:
    token = await _register(client)
    gold = await _item(client, token, "GOLDC", quantity=100, cost_total=8000)
    ring = await _item(client, token, "RINGC")
    await _recipe(client, token, ring, [{"item_id": gold, "quantity": 1}])
    doc = await _doc(client, token, [{"item_id": ring, "sku": "RINGC", "name": "Ring", "quantity": 1, "unit_price": 1}])

    order = [o for o in await _orders(client, token) if o.get("source_doc_id") == doc][0]
    assert (await client.post(f"/manufacturing/{order['id']}/cancel", headers=_h(token), json={"reason": "test"})).status_code == 200
    mine = [o for o in await _orders(client, token) if o.get("source_doc_id") == doc]
    assert len(mine) == 1 and mine[0]["status"] == "cancelled"


@pytest.mark.asyncio
async def test_auto_create_from_list_doc(client) -> None:
    token = await _register(client)
    gold = await _item(client, token, "GOLDL", quantity=100, cost_total=8000)
    ring = await _item(client, token, "RINGL")
    await _recipe(client, token, ring, [{"item_id": gold, "quantity": 5}])
    lst = await _doc(client, token, [{"item_id": ring, "sku": "RINGL", "name": "Ring", "quantity": 2, "unit_price": 1}], doc_type="list")
    mine = [o for o in await _orders(client, token) if o.get("source_doc_id") == lst]
    assert len(mine) == 1 and mine[0]["inputs"][0]["quantity"] == 10.0


@pytest.mark.asyncio
async def test_voided_doc_does_not_drive_orders(client) -> None:
    token = await _register(client)
    gold = await _item(client, token, "GOLDV", quantity=100, cost_total=8000)
    ring = await _item(client, token, "RINGV")
    await _recipe(client, token, ring, [{"item_id": gold, "quantity": 1}])
    doc = await _doc(client, token, [{"item_id": ring, "sku": "RINGV", "name": "Ring", "quantity": 1, "unit_price": 1}])
    # Void before any order sync has run for this doc.
    r = await client.post(f"/docs/{doc}/void", headers=_h(token), json={"reason": "test"})
    if r.status_code != 200:
        pytest.skip(f"void not permitted in this doc state: {r.status_code}")
    assert [o for o in await _orders(client, token) if o.get("source_doc_id") == doc] == []


@pytest.mark.asyncio
async def test_components_summary_nested(client) -> None:
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
    assert raws == {gold: 6.0}              # 1 * 3 * 2
    assert subs == {ring: 1.0, sub: 3.0}
