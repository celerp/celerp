# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""API tests for PUT /manufacturing/items/{id}/recipe."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from celerp.models.ledger import LedgerEntry


async def _register(client) -> str:
    addr = f"admin-{uuid.uuid4().hex[:8]}@recipe.test"
    r = await client.post("/auth/register", json={"company_name": "Recipe Co", "email": addr, "name": "Admin", "password": "pw"})
    assert r.status_code == 200
    return r.json()["access_token"]


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _item(client, token, sku, **kw) -> str:
    body = {"sku": sku, "name": kw.pop("name", sku), "quantity": kw.pop("quantity", 0), "sell_by": "piece", **kw}
    r = await client.post("/items", headers=_h(token), json=body)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _recipe(components, **kw):
    return {"output_qty": kw.get("output_qty", 1), "components": components, "labor": kw.get("labor", []), "overhead": kw.get("overhead", [])}


@pytest.mark.asyncio
async def test_put_recipe_round_trip_and_cost(client) -> None:
    token = await _register(client)
    raw = await _item(client, token, "RAW", quantity=10, cost_total=50)  # unit 5
    fg = await _item(client, token, "FG")

    body = _recipe(
        [{"item_id": raw, "sku": "RAW", "quantity": 3, "unit": "piece"}],
        labor=[{"operation": "Assemble", "hours": 1, "rate": 20}],
        overhead=[{"description": "Packaging", "amount": 5}],
    )
    r = await client.put(f"/manufacturing/items/{fg}/recipe", headers=_h(token), json=body)
    assert r.status_code == 200, r.text
    # 3*5 materials + 20 labor + 5 overhead = 40
    assert r.json()["recipe"]["unit_cost"] == 40.0

    got = (await client.get(f"/items/{fg}", headers=_h(token))).json()
    assert got["recipe"]["components"][0]["item_id"] == raw
    assert got["recipe"]["materials_cost"] == 15.0
    assert got["recipe"]["unit_cost"] == 40.0


@pytest.mark.asyncio
async def test_put_recipe_resolves_component_unit_from_sell_by(client) -> None:
    token = await _register(client)
    raw = await _item(client, token, "GRAM", quantity=10, cost_total=50, sell_by="gram")
    fg = await _item(client, token, "FGU")
    # No unit sent — the server must derive it from the component item's sell unit.
    r = await client.put(f"/manufacturing/items/{fg}/recipe", headers=_h(token), json=_recipe([{"item_id": raw, "quantity": 3}]))
    assert r.status_code == 200, r.text
    got = (await client.get(f"/items/{fg}", headers=_h(token))).json()
    assert got["recipe"]["components"][0]["unit"] == "gram"


@pytest.mark.asyncio
async def test_put_recipe_unknown_component_422(client, session) -> None:
    token = await _register(client)
    fg = await _item(client, token, "FG2")
    r = await client.put(f"/manufacturing/items/{fg}/recipe", headers=_h(token),
                         json=_recipe([{"item_id": "item:does-not-exist", "quantity": 1}]))
    assert r.status_code == 422
    # No event emitted on rejection.
    rows = (await session.execute(select(LedgerEntry).where(LedgerEntry.entity_id == fg, LedgerEntry.event_type == "item.recipe.set"))).scalars().all()
    assert rows == []


@pytest.mark.asyncio
async def test_put_recipe_self_reference_422(client) -> None:
    token = await _register(client)
    fg = await _item(client, token, "FG3")
    r = await client.put(f"/manufacturing/items/{fg}/recipe", headers=_h(token),
                         json=_recipe([{"item_id": fg, "quantity": 1}]))
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_put_recipe_cycle_422(client) -> None:
    token = await _register(client)
    a = await _item(client, token, "A")
    b = await _item(client, token, "B")
    # B uses A
    assert (await client.put(f"/manufacturing/items/{b}/recipe", headers=_h(token),
                            json=_recipe([{"item_id": a, "quantity": 1}]))).status_code == 200
    # A uses B → cycle
    r = await client.put(f"/manufacturing/items/{a}/recipe", headers=_h(token),
                         json=_recipe([{"item_id": b, "quantity": 1}]))
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_put_recipe_full_replace_single_state(client) -> None:
    token = await _register(client)
    x = await _item(client, token, "X")
    y = await _item(client, token, "Y")
    fg = await _item(client, token, "FG4")
    await client.put(f"/manufacturing/items/{fg}/recipe", headers=_h(token),
                     json=_recipe([{"item_id": x, "quantity": 1}, {"item_id": y, "quantity": 1}]))
    await client.put(f"/manufacturing/items/{fg}/recipe", headers=_h(token),
                     json=_recipe([{"item_id": x, "quantity": 2}]))
    got = (await client.get(f"/items/{fg}", headers=_h(token))).json()
    assert [c["item_id"] for c in got["recipe"]["components"]] == [x]
    assert got["recipe"]["components"][0]["quantity"] == 2


@pytest.mark.asyncio
async def test_put_recipe_unknown_item_404(client) -> None:
    token = await _register(client)
    r = await client.put("/manufacturing/items/item:nope/recipe", headers=_h(token), json=_recipe([]))
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_put_recipe_unauth_401(client) -> None:
    r = await client.put("/manufacturing/items/item:x/recipe", json=_recipe([]))
    assert r.status_code in (401, 403)
