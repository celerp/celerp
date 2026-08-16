# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Bill posting (P2): landed-cost charge lines route to their clearing accounts (freight/insurance/
duty -> 1130-FRT/INS/DTY, non-recoverable import VAT -> 1130-IVT, recoverable import VAT -> 1150);
doc-level shipping debits freight clearing so the JE balances. See freight-tracking-plan."""
from __future__ import annotations

import uuid

import pytest


async def _register(client) -> str:
    addr = f"admin-{uuid.uuid4().hex[:8]}@frtje.test"
    r = await client.post("/auth/register", json={"company_name": "FrtJE Co", "email": addr, "name": "A", "password": "pw"})
    assert r.status_code == 200
    return r.json()["access_token"]


def _h(t):
    return {"Authorization": f"Bearer {t}"}


async def _item(client, t, sku, *, inv="stocked", kind=None, recoverable=None, qty=0):
    body = {"status": "available", "sku": sku, "name": sku, "quantity": qty, "sell_by": "piece", "inventory_type": inv}
    if kind:
        body["landed_cost_kind"] = kind
    if recoverable is not None:
        body["recoverable"] = recoverable
    r = await client.post("/items", headers=_h(t), json=body)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _line(eid, sku, qty, price):
    return {"entity_id": eid, "sku": sku, "name": sku, "quantity": qty, "unit_price": price,
            "line_total": qty * price, "sell_by": "piece"}


@pytest.mark.asyncio
async def test_bill_routes_landed_lines_and_balances(client):
    t = await _register(client)
    goods = await _item(client, t, "GOODS-1", qty=10)
    frt = await _item(client, t, "FREIGHT-1", inv="freight", kind="freight")
    duty = await _item(client, t, "DUTY-1", inv="freight", kind="duty")
    vat_rec = await _item(client, t, "IVAT-REC", inv="freight", kind="import_vat", recoverable=True)
    vat_non = await _item(client, t, "IVAT-NON", inv="freight", kind="import_vat", recoverable=False)

    line_items = [
        _line(goods, "GOODS-1", 10, 10),   # 100 -> 1130-P
        _line(frt, "FREIGHT-1", 1, 50),    # 50  -> 1130-FRT
        _line(duty, "DUTY-1", 1, 20),      # 20  -> 1130-DTY
        _line(vat_rec, "IVAT-REC", 1, 30), # 30  -> 1150 (recoverable, not capitalised)
        _line(vat_non, "IVAT-NON", 1, 15), # 15  -> 1130-IVT
    ]
    shipping = 25                          # 25  -> 1130-FRT (doc-level inbound freight)
    total = 100 + 50 + 20 + 30 + 15 + shipping

    bill = (await client.post("/docs", headers=_h(t), json={
        "doc_type": "bill", "line_items": line_items, "shipping": shipping, "total": total})).json()["id"]
    assert (await client.post(f"/docs/{bill}/finalize", headers=_h(t))).status_code == 200, "finalize"

    ledger = (await client.get("/ledger?entity_type=journal_entry", headers=_h(t))).json()["items"]
    je = next(e for e in ledger if bill in (e.get("entity_id") or "") and e["data"].get("entries"))
    entries = je["data"]["entries"]

    # Balanced.
    debit = sum(float(x.get("debit", 0) or 0) for x in entries)
    credit = sum(float(x.get("credit", 0) or 0) for x in entries)
    assert abs(debit - credit) < 1e-6, f"unbalanced: {debit} vs {credit}"

    # Per-account debits routed correctly.
    by_acct: dict[str, float] = {}
    for x in entries:
        by_acct[x["account"]] = by_acct.get(x["account"], 0.0) + float(x.get("debit", 0) or 0)
    assert by_acct.get("1130-P") == 100
    assert by_acct.get("1130-FRT") == 75   # 50 freight line + 25 doc shipping
    assert by_acct.get("1130-DTY") == 20
    assert by_acct.get("1130-IVT") == 15
    assert by_acct.get("1150") == 30       # recoverable import VAT to input-VAT receivable
    # AP credited the full bill total.
    ap_credit = sum(float(x.get("credit", 0) or 0) for x in entries if x["account"] == "2110")
    assert ap_credit == total


@pytest.mark.asyncio
async def test_import_vat_default_capitalises_when_unspecified(client):
    """An import_vat line with no explicit recoverable flag defaults to capitalising (1130-IVT) -
    the safe default for non-recoverable jurisdictions."""
    t = await _register(client)
    goods = await _item(client, t, "GOODS-V", qty=1)
    vat = await _item(client, t, "IVAT-DEFAULT", inv="freight", kind="import_vat")  # no recoverable flag
    line_items = [_line(goods, "GOODS-V", 1, 100), _line(vat, "IVAT-DEFAULT", 1, 7)]
    bill = (await client.post("/docs", headers=_h(t), json={
        "doc_type": "bill", "line_items": line_items, "total": 107})).json()["id"]
    assert (await client.post(f"/docs/{bill}/finalize", headers=_h(t))).status_code == 200
    ledger = (await client.get("/ledger?entity_type=journal_entry", headers=_h(t))).json()["items"]
    je = next(e for e in ledger if bill in (e.get("entity_id") or "") and e["data"].get("entries"))
    by_acct = {x["account"]: float(x.get("debit", 0) or 0) for x in je["data"]["entries"]}
    assert by_acct.get("1130-IVT") == 7 and "1150" not in by_acct


@pytest.mark.asyncio
async def test_new_company_chart_has_clearing_accounts(client):
    """New companies are seeded with the landed-cost clearing accounts."""
    t = await _register(client)
    accts = (await client.get("/accounting/chart", headers=_h(t))).json()
    codes = {a["code"] for a in accts["items"]}
    assert {"1130-FRT", "1130-INS", "1130-DTY", "1130-IVT"} <= codes


@pytest.mark.asyncio
async def test_bill_shipping_only_balances(client):
    """A bill carrying only doc-level shipping still posts a balanced JE (closes the legacy gap)."""
    t = await _register(client)
    goods = await _item(client, t, "GOODS-S", qty=5)
    line_items = [_line(goods, "GOODS-S", 5, 10)]   # 50 -> 1130-P
    total = 50 + 12                                  # + shipping 12
    bill = (await client.post("/docs", headers=_h(t), json={
        "doc_type": "bill", "line_items": line_items, "shipping": 12, "total": total})).json()["id"]
    assert (await client.post(f"/docs/{bill}/finalize", headers=_h(t))).status_code == 200

    ledger = (await client.get("/ledger?entity_type=journal_entry", headers=_h(t))).json()["items"]
    je = next(e for e in ledger if bill in (e.get("entity_id") or "") and e["data"].get("entries"))
    entries = je["data"]["entries"]
    debit = sum(float(x.get("debit", 0) or 0) for x in entries)
    credit = sum(float(x.get("credit", 0) or 0) for x in entries)
    assert abs(debit - credit) < 1e-6, f"unbalanced: {debit} vs {credit}"
    frt = sum(float(x.get("debit", 0) or 0) for x in entries if x["account"] == "1130-FRT")
    assert frt == 12
