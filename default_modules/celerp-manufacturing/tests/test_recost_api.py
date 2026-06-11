# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""API tests for recalculate + mark-to-market re-cost of dependents."""
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


async def _unit_cost(client, token, item_id) -> float:
    return (await client.get(f"/items/{item_id}", headers=_h(token))).json()["recipe"]["unit_cost"]


@pytest.mark.asyncio
async def test_recalculate_reflects_changed_component_cost(client) -> None:
    token = await _register(client)
    gold = await _item(client, token, "GOLD", quantity=1, cost_total=80)  # unit 80
    ring = await _item(client, token, "RING")
    await _set_recipe(client, token, ring, [{"item_id": gold, "quantity": 5}])
    assert await _unit_cost(client, token, ring) == 400.0

    await _set_cost(client, token, gold, 100)  # gold reprices
    # Stale until recalculated (no automatic cascade — deterministic):
    assert await _unit_cost(client, token, ring) == 400.0

    r = await client.post(f"/manufacturing/items/{ring}/recalculate", headers=_h(token))
    assert r.status_code == 200
    assert r.json()["recipe"]["unit_cost"] == 500.0
    assert await _unit_cost(client, token, ring) == 500.0


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


@pytest.mark.asyncio
async def test_recalculate_no_recipe_422(client) -> None:
    token = await _register(client)
    raw = await _item(client, token, "RAWX", quantity=1, cost_total=5)
    r = await client.post(f"/manufacturing/items/{raw}/recalculate", headers=_h(token))
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_recalculate_unknown_404(client) -> None:
    token = await _register(client)
    r = await client.post("/manufacturing/items/item:nope/recalculate", headers=_h(token))
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_apply_cost_sets_cost_price(client) -> None:
    token = await _register(client)
    gold = await _item(client, token, "GOLDA", quantity=1, cost_total=80)  # unit 80
    ring = await _item(client, token, "RINGA")  # no stock — standard cost must still persist
    await _set_recipe(client, token, ring, [{"item_id": gold, "quantity": 5}])  # unit 400

    r = await client.post(f"/manufacturing/items/{ring}/apply-cost", headers=_h(token))
    assert r.status_code == 200 and r.json()["cost_price"] == 400.0
    assert (await client.get(f"/items/{ring}", headers=_h(token))).json()["cost_price"] == 400.0


@pytest.mark.asyncio
async def test_apply_cost_no_recipe_422(client) -> None:
    token = await _register(client)
    raw = await _item(client, token, "RAWA", cost_total=5)
    r = await client.post(f"/manufacturing/items/{raw}/apply-cost", headers=_h(token))
    assert r.status_code == 422
