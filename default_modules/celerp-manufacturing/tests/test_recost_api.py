# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""API tests for recipe cost: auto-applied cost_price + mark-to-market re-cost of dependents.

The recipe rolls on every save and the rolled unit cost is applied to cost_price automatically
(no manual recalculate / apply step). An item's OWN recipe still goes stale when a *component*
reprices (nothing re-saves the parent) — that is what recost-dependents fixes, in one click.
"""
from __future__ import annotations

import uuid

import pytest


async def _register(client) -> str:
    addr = f"admin-{uuid.uuid4().hex[:8]}@recost.test"
    r = await client.post("/auth/register", json={"company_name": "Recost Co", "email": addr, "name": "Admin", "password": "pw"})
    assert r.status_code == 200
    return r.json()["access_token"]


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _item(client, token, sku, **kw) -> str:
    body = {"sku": sku, "name": sku, "quantity": kw.pop("quantity", 1), "sell_by": "piece", **kw}
    r = await client.post("/items", headers=_h(token), json=body)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _recipe(components):
    return {"output_qty": 1, "components": components, "labor": [], "overhead": []}


async def _set_recipe(client, token, item_id, components):
    r = await client.put(f"/manufacturing/items/{item_id}/recipe", headers=_h(token), json=_recipe(components))
    assert r.status_code == 200, r.text
    return r.json()


async def _set_cost(client, token, item_id, cost_total):
    r = await client.post(f"/items/{item_id}/price", headers=_h(token), json={"price_type": "cost_total", "new_price": cost_total})
    assert r.status_code == 200, r.text


async def _item_state(client, token, item_id) -> dict:
    return (await client.get(f"/items/{item_id}", headers=_h(token))).json()


async def _unit_cost(client, token, item_id) -> float:
    return (await _item_state(client, token, item_id))["recipe"]["unit_cost"]


@pytest.mark.asyncio
async def test_set_recipe_auto_applies_cost_price(client) -> None:
    token = await _register(client)
    gold = await _item(client, token, "GOLD", quantity=1, cost_total=80)  # unit 80
    ring = await _item(client, token, "RING")  # no stock — standard cost must still apply
    await _set_recipe(client, token, ring, [{"item_id": gold, "quantity": 5}])
    st = await _item_state(client, token, ring)
    # Rolled unit cost is applied to cost_price automatically — no manual "apply" step.
    assert st["recipe"]["unit_cost"] == 400.0
    assert st["cost_price"] == 400.0


@pytest.mark.asyncio
async def test_set_recipe_annotates_component_line_costs(client) -> None:
    token = await _register(client)
    gold = await _item(client, token, "GOLDC", quantity=1, cost_total=80)  # unit 80
    ring = await _item(client, token, "RINGC")
    res = await _set_recipe(client, token, ring, [{"item_id": gold, "quantity": 5}])
    comp = res["recipe"]["components"][0]
    # Each component carries its catalog unit cost + extended line cost (for the UI, read-only).
    assert comp["unit_cost"] == 80.0
    assert comp["line_cost"] == 400.0


@pytest.mark.asyncio
async def test_component_reprice_is_stale_until_recost(client) -> None:
    token = await _register(client)
    gold = await _item(client, token, "GOLDS", quantity=1, cost_total=80)
    ring = await _item(client, token, "RINGS")
    await _set_recipe(client, token, ring, [{"item_id": gold, "quantity": 5}])
    assert await _unit_cost(client, token, ring) == 400.0

    await _set_cost(client, token, gold, 100)  # gold reprices
    # The parent's own recipe is deterministic-stale (nothing re-saved it):
    assert await _unit_cost(client, token, ring) == 400.0

    r = await client.post(f"/manufacturing/items/{gold}/recost-dependents", headers=_h(token))
    assert r.status_code == 200 and r.json()["count"] == 1
    st = await _item_state(client, token, ring)
    assert st["recipe"]["unit_cost"] == 500.0
    assert st["cost_price"] == 500.0  # recost also flows through to cost_price


@pytest.mark.asyncio
async def test_recost_dependents_marks_to_market_nested(client) -> None:
    token = await _register(client)
    gold = await _item(client, token, "GOLD2", quantity=1, cost_total=80)
    sub = await _item(client, token, "SUB")
    ring = await _item(client, token, "RING2")
    await _set_recipe(client, token, sub, [{"item_id": gold, "quantity": 2}])   # 160
    await _set_recipe(client, token, ring, [{"item_id": sub, "quantity": 3}])    # 480
    assert await _unit_cost(client, token, sub) == 160.0
    assert await _unit_cost(client, token, ring) == 480.0

    await _set_cost(client, token, gold, 100)
    r = await client.post(f"/manufacturing/items/{gold}/recost-dependents", headers=_h(token))
    assert r.status_code == 200
    assert r.json()["count"] == 2  # sub + ring

    assert await _unit_cost(client, token, sub) == 200.0    # 2 * 100
    assert await _unit_cost(client, token, ring) == 600.0   # 3 * 200


@pytest.mark.asyncio
async def test_recost_dependents_none(client) -> None:
    token = await _register(client)
    lonely = await _item(client, token, "LONELY", quantity=1, cost_total=10)
    r = await client.post(f"/manufacturing/items/{lonely}/recost-dependents", headers=_h(token))
    assert r.status_code == 200 and r.json()["count"] == 0
