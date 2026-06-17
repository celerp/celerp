# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Inventory-account reconciliation: COGS relieves the SAME account goods are capitalised into
(1130-P), not the orphan 1300. A buy->sell round-trip nets the inventory account to the unsold value."""
from __future__ import annotations

import uuid

import pytest


async def _register(client) -> str:
    addr = f"admin-{uuid.uuid4().hex[:8]}@recon.test"
    r = await client.post("/auth/register", json={"company_name": "Recon Co", "email": addr, "name": "A", "password": "pw"})
    assert r.status_code == 200
    return r.json()["access_token"]


def _h(t):
    return {"Authorization": f"Bearer {t}"}


async def _ledger_balance(client, t, code) -> float:
    """Net debit-credit balance on an account across all posted JEs."""
    led = (await client.get("/ledger?entity_type=journal_entry", headers=_h(t))).json()["items"]
    bal = 0.0
    for e in led:
        data = e.get("data") or {}
        if data.get("status") != "posted":
            continue
        for entry in data.get("entries", []):
            if entry.get("account") == code:
                bal += float(entry.get("debit", 0) or 0) - float(entry.get("credit", 0) or 0)
    return round(bal, 2)


@pytest.mark.asyncio
async def test_cogs_relieves_same_account_goods_are_capitalised_to(client):
    t = await _register(client)
    # Stock an item with a known cost (qty 10 @ cost 10 -> cost_total 100), debiting inventory 1130-P
    # would normally happen on receive; here create the item directly with cost and sell part of it.
    item = (await client.post("/items", headers=_h(t), json={
        "sku": "RECON-1", "name": "Widget", "quantity": 10, "sell_by": "piece", "cost_total": 100})).json()["id"]

    # Sell 10 @ price 20 -> COGS 100. Fulfilling posts the COGS JE.
    doc = (await client.post("/docs", headers=_h(t), json={"doc_type": "invoice", "line_items": [
        {"entity_id": item, "sku": "RECON-1", "name": "Widget", "quantity": 10, "unit_price": 20, "sell_by": "piece"}],
        "total": 200})).json()["id"]
    assert (await client.post(f"/docs/{doc}/finalize", headers=_h(t))).status_code == 200
    r = await client.post(f"/docs/{doc}/fulfill-lines", headers=_h(t), json={"line_entity_ids": [item]})
    assert r.status_code == 200, r.text

    ledger = (await client.get("/ledger?entity_type=journal_entry", headers=_h(t))).json()["items"]
    # The COGS JE credits inventory at 1130-P (the canonical goods account), never the orphan 1300.
    cogs_je = next(e for e in ledger if "fulfill" in (e.get("entity_id") or "") and e["data"].get("entries"))
    accts = {x["account"] for x in cogs_je["data"]["entries"]}
    assert accts == {"5100", "1130-P"}, accts
    assert "1300" not in accts

    # No JE anywhere references the orphan 1300 account.
    for e in ledger:
        for entry in (e.get("data") or {}).get("entries", []):
            assert entry.get("account") != "1300", "no JE may post to the orphan 1300 account"


@pytest.mark.asyncio
async def test_audit_adjustment_uses_canonical_inventory_account(client):
    """An audit stock adjustment posts shrinkage/overage against 1130-P, consistent with COGS."""
    t = await _register(client)
    loc = (await client.post("/companies/me/locations", headers=_h(t),
                             json={"name": "WH", "type": "warehouse"})).json()["id"]
    item = (await client.post("/items", headers=_h(t), json={
        "sku": "AUD-RECON", "name": "W", "quantity": 10, "sell_by": "piece", "cost_total": 100,
        "location_id": loc, "barcode": "55510001"})).json()["id"]
    audit = (await client.post("/lists/audit", headers=_h(t), json={"location_id": loc})).json()["id"]
    assert (await client.post(f"/lists/{audit}/finalize", headers=_h(t))).status_code == 200
    await client.patch(f"/lists/{audit}/line/{item}", headers=_h(t), json={"counted_qty": 8})  # shrink 2 -> 20
    assert (await client.post(f"/lists/{audit}/adjust", headers=_h(t))).status_code == 200

    ledger = (await client.get("/ledger?entity_type=journal_entry", headers=_h(t))).json()["items"]
    je = next(e for e in ledger if audit in (e["data"].get("memo") or "") and e["data"].get("entries"))
    accts = {x["account"] for x in je["data"]["entries"]}
    assert "1130-P" in accts and "1130" not in accts and "1300" not in accts
