# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Validation guards on the production-run execution endpoints (issue / receive / complete)."""
from __future__ import annotations

import uuid

import pytest


async def _register(client) -> str:
    addr = f"admin-{uuid.uuid4().hex[:8]}@mfgval.test"
    r = await client.post("/auth/register", json={"company_name": "Mfg Co", "email": addr, "name": "Admin", "password": "pw"})
    assert r.status_code == 200
    return r.json()["access_token"]


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _ring(client, token, *, ring_qty=0):
    gold = (await client.post("/items", headers=_h(token),
                              json={"status": "available", "sku": f"G-{uuid.uuid4().hex[:6]}", "name": "Gold", "quantity": 100,
                                    "sell_by": "gram", "cost_total": 8000})).json()["id"]
    ring = (await client.post("/items", headers=_h(token),
                              json={"status": "available", "sku": f"R-{uuid.uuid4().hex[:6]}", "name": "Ring", "quantity": ring_qty,
                                    "sell_by": "piece"})).json()["id"]
    await client.put(f"/manufacturing/items/{ring}/recipe", headers=_h(token),
                     json={"output_qty": 1, "components": [{"item_id": gold, "quantity": 5}], "labor": [], "overhead": []})
    return gold, ring


@pytest.mark.asyncio
async def test_create_order_requires_inputs(client):
    token = await _register(client)
    r = await client.post("/manufacturing", headers=_h(token), json={"description": "No inputs", "inputs": []})
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_receive_rejects_nonpositive_quantity(client):
    token = await _register(client)
    _gold, ring = await _ring(client, token)
    run = (await client.post(f"/manufacturing/items/{ring}/build", headers=_h(token), json={"quantity": 2})).json()["id"]
    r = await client.post(f"/manufacturing/{run}/receive", headers=_h(token), json={"quantity": 0})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_complete_with_waste_posts_balanced_je(client):
    token = await _register(client)
    _gold, ring = await _ring(client, token)
    run = (await client.post(f"/manufacturing/items/{ring}/build", headers=_h(token), json={"quantity": 2})).json()["id"]
    # Finish with scrap: 1 unit wasted out of 10 issued -> a slice of input cost hits the waste account.
    done = await client.post(f"/manufacturing/{run}/complete", headers=_h(token),
                             json={"waste_quantity": 1, "waste_unit": "gram"})
    assert done.status_code == 200

    ledger = (await client.get("/ledger?entity_type=journal_entry", headers=_h(token))).json()["items"]
    je = next(e for e in ledger if run in (e["data"].get("memo") or ""))
    entries = je["data"]["entries"]
    accounts = [x["account"] for x in entries]
    assert accounts.count("1130-P") == 2 and "5100" in accounts
    assert any(x["account"] == "5100" and float(x.get("debit", 0) or 0) > 0 for x in entries)  # waste posted
    debit = sum(float(x.get("debit", 0) or 0) for x in entries)
    credit = sum(float(x.get("credit", 0) or 0) for x in entries)
    assert abs(debit - credit) < 1e-6


@pytest.mark.asyncio
async def test_double_complete_guard(client):
    token = await _register(client)
    _gold, ring = await _ring(client, token)
    run = (await client.post(f"/manufacturing/items/{ring}/build", headers=_h(token),
                             json={"quantity": 1, "complete": True})).json()["id"]
    r = await client.post(f"/manufacturing/{run}/complete", headers=_h(token), json={})
    assert r.status_code == 409
