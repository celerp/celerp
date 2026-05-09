# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

from __future__ import annotations

import pytest


async def _register(client, email: str = "admin@mfg.test") -> str:
    r = await client.post("/auth/register", json={"company_name": "Mfg Co", "email": email, "name": "Admin", "password": "pw"})
    assert r.status_code == 200
    return r.json()["access_token"]


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_manufacturing_validation_guards(client):
    token = await _register(client)

    # no inputs guard
    r = await client.post("/manufacturing", headers=_h(token), json={"description": "No inputs", "inputs": []})
    assert r.status_code == 409

    # create valid order
    item = await client.post("/items", headers=_h(token), json={"sku": "RAW", "name": "Raw", "quantity": 5, "sell_by": "piece"})
    item_id = item.json()["id"]
    order = await client.post("/manufacturing", headers=_h(token), json={"description": "Build", "inputs": [{"item_id": item_id, "quantity": 3}], "expected_outputs": [{"sku": "FG", "name": "FG", "quantity": 1}]})
    assert order.status_code == 200
    order_id = order.json()["id"]

    # consume missing item
    r = await client.post(f"/manufacturing/{order_id}/consume", headers=_h(token), json={"item_id": "item:missing", "quantity": 1})
    assert r.status_code == 404

    # consume more than available
    r = await client.post(f"/manufacturing/{order_id}/consume", headers=_h(token), json={"item_id": item_id, "quantity": 999})
    assert r.status_code == 409

    # complete without consume all required
    r = await client.post(f"/manufacturing/{order_id}/complete", headers=_h(token), json={})
    assert r.status_code == 409

    # happy consume + complete then double-complete/cancel guards
    assert (await client.post(f"/manufacturing/{order_id}/consume", headers=_h(token), json={"item_id": item_id, "quantity": 3})).status_code == 200
    assert (await client.post(f"/manufacturing/{order_id}/complete", headers=_h(token), json={"waste_quantity": 0.5, "waste_unit": "kg"})).status_code == 200

    r = await client.post(f"/manufacturing/{order_id}/complete", headers=_h(token), json={})
    assert r.status_code == 409
    r = await client.post(f"/manufacturing/{order_id}/cancel", headers=_h(token), json={"reason": "late"})
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_manufacturing_complete_cross_entity_outputs_and_je(client):
    token = await _register(client, email="admin2@mfg.test")
    item = await client.post("/items", headers=_h(token), json={"sku": "RAW-2", "name": "Raw2", "quantity": 10, "sell_by": "piece"})
    item_id = item.json()["id"]

    order = await client.post(
        "/manufacturing",
        headers=_h(token),
        json={
            "description": "Assemble",
            "estimated_cost": 100,
            "inputs": [{"item_id": item_id, "quantity": 4}],
            "expected_outputs": [{"sku": "FG-2", "name": "FG2", "quantity": 2, "category": "fg"}],
        },
    )
    order_id = order.json()["id"]

    await client.post(f"/manufacturing/{order_id}/start", headers=_h(token))
    await client.post(f"/manufacturing/{order_id}/consume", headers=_h(token), json={"item_id": item_id, "quantity": 4})
    done = await client.post(f"/manufacturing/{order_id}/complete", headers=_h(token), json={"waste_quantity": 1})
    assert done.status_code == 200

    items = (await client.get("/items", headers=_h(token))).json()["items"]
    assert any(i.get("sku") == "FG-2" and i.get("quantity") == 2 for i in items)

    ledger = (await client.get("/ledger?entity_type=journal_entry", headers=_h(token))).json()["items"]
    je = next(e for e in ledger if order_id in (e["data"].get("memo") or ""))
    entries = je["data"]["entries"]
    accounts = [x["account"] for x in entries]
    assert accounts.count("1130") == 2  # debit FG + credit raw inventory
    assert "5100" in accounts

    debit = sum(float(x.get("debit", 0) or 0) for x in entries)
    credit = sum(float(x.get("credit", 0) or 0) for x in entries)
    assert abs(debit - credit) < 1e-6
