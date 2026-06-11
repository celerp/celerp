# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""API tests: create manufacturing orders from a document (one per recipe-bearing line) + JIT summary."""
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


@pytest.mark.asyncio
async def test_one_order_per_recipe_line_others_skipped(client) -> None:
    token = await _register(client)
    gold = await _item(client, token, "GOLD", quantity=100, cost_total=8000)
    ring = await _item(client, token, "RING")
    widget = await _item(client, token, "WIDGET")  # no recipe
    await _recipe(client, token, ring, [{"item_id": gold, "quantity": 5}])

    doc = await _doc(client, token, [
        {"item_id": ring, "sku": "RING", "name": "Ring", "quantity": 2, "unit_price": 100},
        {"item_id": widget, "sku": "WIDGET", "name": "Widget", "quantity": 1, "unit_price": 50},
    ])

    r = await client.post(f"/manufacturing/documents/{doc}/orders", headers=_h(token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["created_count"] == 1
    assert body["skipped_count"] == 1
    assert body["skipped"][0]["reason"] == "no recipe defined"

    # The created order's inputs must equal expand_recipe: gold x (5 * 2) = 10.
    order_id = body["created"][0]["order_id"]
    order = (await client.get(f"/manufacturing/{order_id}", headers=_h(token))).json()
    assert order["inputs"] == [{"item_id": gold, "quantity": 10.0}]
    assert order["expected_outputs"][0]["sku"] == "RING"
    assert order["expected_outputs"][0]["quantity"] == 2.0
    assert order["source_doc_id"] == doc


@pytest.mark.asyncio
async def test_reinvoke_is_idempotent(client) -> None:
    token = await _register(client)
    gold = await _item(client, token, "GOLD2", quantity=100, cost_total=8000)
    ring = await _item(client, token, "RING2")
    await _recipe(client, token, ring, [{"item_id": gold, "quantity": 3}])
    doc = await _doc(client, token, [{"item_id": ring, "sku": "RING2", "name": "Ring", "quantity": 4, "unit_price": 1}])

    first = (await client.post(f"/manufacturing/documents/{doc}/orders", headers=_h(token))).json()
    assert first["created_count"] == 1
    second = (await client.post(f"/manufacturing/documents/{doc}/orders", headers=_h(token))).json()
    assert second["created_count"] == 0
    assert second["skipped"][0]["reason"] == "order already created"
    # Still exactly one order overall.
    orders = (await client.get("/manufacturing", headers=_h(token))).json()["items"]
    assert len([o for o in orders if o.get("source_doc_id") == doc]) == 1


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


@pytest.mark.asyncio
async def test_manufacture_unknown_doc_404(client) -> None:
    token = await _register(client)
    r = await client.post("/manufacturing/documents/doc:nope/orders", headers=_h(token))
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_manufacture_from_list(client) -> None:
    token = await _register(client)
    gold = await _item(client, token, "GOLDL", quantity=100, cost_total=8000)
    ring = await _item(client, token, "RINGL")
    await _recipe(client, token, ring, [{"item_id": gold, "quantity": 5}])
    lst = await _doc(client, token, [{"item_id": ring, "sku": "RINGL", "name": "Ring", "quantity": 2, "unit_price": 1}], doc_type="list")
    r = await client.post(f"/manufacturing/documents/{lst}/orders", headers=_h(token))
    assert r.status_code == 200 and r.json()["created_count"] == 1
