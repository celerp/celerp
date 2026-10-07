# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Finalizing an invoice books the cost of the lots its lines are bound to, so a lot that is
not stock the invoice can sell is refused by name and status before anything is booked."""
from __future__ import annotations

import uuid

import pytest

from stock_books import assert_settled
from test_cost_restatement import _item


async def _doc(client, auth, doc_type: str, lot: str, sku: str, qty: float = 1) -> str:
    r = await client.post("/docs", headers=auth["headers"], json={
        "doc_type": doc_type, "ref_id": f"D-{uuid.uuid4().hex[:6]}", "total": 500.0 * qty,
        "line_items": [{"sku": sku, "name": "Lot", "quantity": qty, "unit_price": 500.0, "entity_id": lot}]})
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _sold(client, auth, lot: str, sku: str) -> str:
    h = auth["headers"]
    doc = await _doc(client, auth, "invoice", lot, sku)
    assert (await client.post(f"/docs/{doc}/finalize", headers=h)).status_code == 200
    r = await client.post(f"/docs/{doc}/fulfill-lines", headers=h, json={"line_entity_ids": [lot]})
    assert r.status_code == 200, r.text
    return doc


async def _state(client, auth, entity_id: str, path: str) -> dict:
    return (await client.get(f"/{path}/{entity_id}", headers=auth["headers"])).json()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["sold", "expired"])
async def test_invoicing_a_lot_that_is_not_available_is_refused(client, session, auth, status):
    h = auth["headers"]
    sku = f"UNS-{uuid.uuid4().hex[:6]}"
    lot = await _item(client, auth, 300.0, sku=sku)
    if status == "sold":
        await _sold(client, auth, lot, sku)
    else:
        assert (await client.post(f"/items/{lot}/expire", headers=h)).status_code == 200
    await assert_settled(client, session, auth)
    doc = await _doc(client, auth, "invoice", lot, sku)
    r = await client.post(f"/docs/{doc}/finalize", headers=h)
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["message_key"] == "item.invoice_not_available", detail
    assert detail["params"] == {"sku": sku, "status": status}, detail
    assert detail["message"] == f"{sku} is {status}: only available stock can be invoiced."
    assert not (await _state(client, auth, doc, "docs")).get("finalized")
    await assert_settled(client, session, auth)


@pytest.mark.asyncio
async def test_an_invoice_converted_from_a_memo_sells_the_lot_out_on_that_memo(client, session, auth):
    h = auth["headers"]
    sku = f"UNS-{uuid.uuid4().hex[:6]}"
    lot = await _item(client, auth, 300.0, sku=sku)
    memo = await _doc(client, auth, "memo", lot, sku)
    assert (await client.post(f"/docs/{memo}/finalize", headers=h)).status_code == 200
    r = await client.post(f"/docs/{memo}/fulfill-lines", headers=h, json={"line_entity_ids": [lot]})
    assert r.status_code == 200, r.text
    assert (await _state(client, auth, lot, "items"))["status"] == "memo_out"
    r = await client.post(f"/docs/{memo}/convert", headers=h)
    assert r.status_code == 200, r.text
    invoice = r.json()["target_doc_id"]
    r = await client.post(f"/docs/{invoice}/finalize", headers=h)
    assert r.status_code == 200, r.text
    assert (await _state(client, auth, lot, "items"))["status"] == "sold"
    await assert_settled(client, session, auth)


def test_the_refusal_is_shown_in_the_users_language():
    from ui import i18n

    i18n.set_lang("de")
    try:
        text = i18n.refusal_text({"message": "x", "message_key": "item.invoice_not_available",
                                  "params": {"sku": "LOT-1", "status": "sold"}})
        assert text.startswith("LOT-1 ist ") and "in Rechnung gestellt" in text, text
        assert " sold" not in text, text
    finally:
        i18n.set_lang("en")
