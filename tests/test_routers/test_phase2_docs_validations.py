# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

from __future__ import annotations

import pytest


async def _register(client, email: str = "admin@docs.test") -> str:
    r = await client.post("/auth/register", json={"company_name": "Docs Co", "email": email, "name": "Admin", "password": "pw"})
    assert r.status_code == 200
    return r.json()["access_token"]


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _create_invoice(client, token: str, total: float = 107, status: str = "draft") -> str:
    r = await client.post(
        "/docs",
        headers=_h(token),
        json={"doc_type": "invoice", "contact_id": "contact:1", "line_items": [{"name": "A", "quantity": 1, "unit_price": 100, "line_total": 100}], "subtotal": 100, "tax": 7, "total": total, "status": status},
    )
    assert r.status_code == 200
    return r.json()["id"]


@pytest.mark.asyncio
async def test_docs_validation_guards_and_edge_cases(client):
    token = await _register(client)

    # payment on draft rejected
    inv = await _create_invoice(client, token)
    r = await client.post(f"/docs/{inv}/payment", headers=_h(token), json={"amount": 1})
    assert r.status_code == 409

    # finalize then edit rejected
    assert (await client.post(f"/docs/{inv}/send", headers=_h(token), json={})).status_code == 200
    assert (await client.post(f"/docs/{inv}/finalize", headers=_h(token))).status_code == 200
    r = await client.patch(f"/docs/{inv}", headers=_h(token), json={"fields_changed": {"notes": {"old": None, "new": "x"}}})
    assert r.status_code == 409

    # payment exceeding outstanding rejected
    r = await client.post(f"/docs/{inv}/payment", headers=_h(token), json={"amount": 108})
    assert r.status_code == 409

    # valid payment then void after paid rejected
    assert (await client.post(f"/docs/{inv}/payment", headers=_h(token), json={"amount": 107})).status_code == 200
    r = await client.post(f"/docs/{inv}/void", headers=_h(token), json={"reason": "x"})
    assert r.status_code == 409

    # finalize void doc rejected
    inv2 = await _create_invoice(client, token, total=50)
    assert (await client.post(f"/docs/{inv2}/void", headers=_h(token), json={"reason": "oops"})).status_code == 200
    r = await client.post(f"/docs/{inv2}/finalize", headers=_h(token))
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_quotation_convert_expired_rejected_and_non_expired_allowed(client):
    token = await _register(client, email="admin2@docs.test")

    expired = await client.post(
        "/docs",
        headers=_h(token),
        json={"doc_type": "quotation", "contact_id": "contact:1", "line_items": [], "subtotal": 0, "tax": 0, "total": 0, "valid_until": "2000-01-01"},
    )
    q_old = expired.json()["id"]
    r = await client.post(f"/docs/{q_old}/convert", headers=_h(token))
    assert r.status_code == 409

    ok = await client.post(
        "/docs",
        headers=_h(token),
        json={"doc_type": "quotation", "contact_id": "contact:1", "line_items": [{"name": "A", "quantity": 1, "unit_price": 10, "line_total": 10}], "subtotal": 10, "tax": 0, "total": 10, "valid_until": "2999-01-01"},
    )
    q_new = ok.json()["id"]
    r2 = await client.post(f"/docs/{q_new}/convert", headers=_h(token))
    assert r2.status_code == 200
    target = r2.json()["target_doc_id"]
    created = await client.get(f"/docs/{target}", headers=_h(token))
    assert created.status_code == 200
    assert created.json()["doc_type"] == "invoice"


@pytest.mark.asyncio
async def test_credit_note_validation_total_not_exceed_original(client):
    token = await _register(client, email="admin3@docs.test")
    inv = await _create_invoice(client, token, total=100)

    bad = await client.post(
        "/docs",
        headers=_h(token),
        json={"doc_type": "credit_note", "original_doc_id": inv, "reason": "return", "line_items": [], "subtotal": 0, "tax": 0, "total": 101},
    )
    assert bad.status_code == 409

    good = await client.post(
        "/docs",
        headers=_h(token),
        json={"doc_type": "credit_note", "original_doc_id": inv, "reason": "return", "line_items": [], "subtotal": 0, "tax": 0, "total": 40},
    )
    assert good.status_code == 200
    inv_state = (await client.get(f"/docs/{inv}", headers=_h(token))).json()
    assert inv_state["amount_outstanding"] == 60


@pytest.mark.asyncio
async def test_auto_je_entries_balanced_and_account_codes(client):
    token = await _register(client, email="admin4@docs.test")
    inv = await _create_invoice(client, token, total=107)
    await client.post(f"/docs/{inv}/send", headers=_h(token), json={})
    await client.post(f"/docs/{inv}/finalize", headers=_h(token))
    await client.post(f"/docs/{inv}/payment", headers=_h(token), json={"amount": 107})

    ledger = (await client.get("/ledger?entity_type=journal_entry", headers=_h(token))).json()["items"]
    assert len(ledger) >= 2
    # Find finalize JE and payment JE
    finalize = next(e for e in ledger if "finalized" in (e["data"].get("memo") or ""))
    payment = next(e for e in ledger if "payment" in (e["data"].get("memo") or ""))

    for je in (finalize, payment):
        entries = je["data"]["entries"]
        debit = sum(float(x.get("debit", 0) or 0) for x in entries)
        credit = sum(float(x.get("credit", 0) or 0) for x in entries)
        assert abs(debit - credit) < 1e-6

    fin_accounts = {x["account"] for x in finalize["data"]["entries"]}
    pay_accounts = {x["account"] for x in payment["data"]["entries"]}
    assert fin_accounts == {"1120", "4100", "2120"}
    assert pay_accounts == {"1110", "1120"}


@pytest.mark.asyncio
async def test_po_receive_creates_inventory_or_adjusts_and_je(client):
    token = await _register(client, email="admin5@docs.test")
    item = await client.post("/items", headers=_h(token), json={"sku": "EXIST", "name": "Existing", "quantity": 1, "sell_by": "piece"})
    item_id = item.json()["id"]

    po = await client.post(
        "/docs",
        headers=_h(token),
        json={"doc_type": "purchase_order", "contact_id": "contact:sup", "line_items": [{"quantity": 2}, {"quantity": 3}], "subtotal": 50, "tax": 0, "total": 50},
    )
    po_id = po.json()["id"]

    loc = await client.post(
        "/companies/me/locations",
        headers=_h(token),
        json={"name": "Receiving", "type": "warehouse", "is_default": True},
    )
    location_id = loc.json()["id"]

    rec = await client.post(
        f"/docs/{po_id}/receive",
        headers=_h(token),
        json={
            "location_id": location_id,
            "received_items": [
                {"po_line_index": 0, "item_id": item_id, "quantity_received": 2},
                {"po_line_index": 1, "quantity_received": 3, "sku": "NEW-1", "name": "New Item"},
            ],
        },
    )
    assert rec.status_code == 200

    updated = (await client.get(f"/items/{item_id}", headers=_h(token))).json()
    assert updated["quantity"] == 3
    po_state = (await client.get(f"/docs/{po_id}", headers=_h(token))).json()
    assert po_state["status"] in {"received", "partially_received"}

    ledger = (await client.get("/ledger?entity_type=journal_entry", headers=_h(token))).json()["items"]
    po_je = next(e for e in ledger if po_id in (e["data"].get("memo") or ""))
    codes = {x["account"] for x in po_je["data"]["entries"]}
    assert codes == {"1130", "2110"}
