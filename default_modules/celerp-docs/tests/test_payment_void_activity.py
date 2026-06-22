# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Voiding (and deleting) a payment must be tracked in the document's history, with the
amount, so the activity entry is meaningful."""
from __future__ import annotations

import uuid

import pytest


async def _register(client) -> str:
    addr = f"admin-{uuid.uuid4().hex[:8]}@payvoid.test"
    r = await client.post("/auth/register", json={"company_name": "PayVoid Co", "email": addr, "name": "A", "password": "pw"})
    assert r.status_code == 200
    return r.json()["access_token"]


def _h(t):
    return {"Authorization": f"Bearer {t}"}


async def _doc_events(client, h, doc_id):
    r = await client.get("/ledger", params={"entity_id": doc_id, "limit": 200}, headers=h)
    assert r.status_code == 200, r.text
    return r.json()["items"]


@pytest.mark.asyncio
async def test_void_payment_tracked_in_history(client):
    token = await _register(client)
    h = _h(token)
    item = (await client.post("/items", headers=h, json={"sku": "PV-ITEM", "name": "Item", "quantity": 1, "sell_by": "piece"})).json()["id"]
    doc = (await client.post("/docs", headers=h, json={"doc_type": "invoice", "line_items": [
        {"entity_id": item, "sku": "PV-ITEM", "name": "Item", "quantity": 1, "unit_price": 500, "sell_by": "piece"}],
        "total": 500,
    })).json()["id"]
    assert (await client.post(f"/docs/{doc}/finalize", headers=h)).status_code == 200

    rp = await client.post(f"/docs/{doc}/payment", headers=h, json={"amount": 500, "payment_date": "2026-06-22", "method": "cash", "bank_account": "1111"})
    assert rp.status_code == 200, rp.text

    vr = await client.post(f"/docs/{doc}/void-payment", headers=h, json={"payment_index": 0, "void_reason": "duplicate entry"})
    assert vr.status_code == 200, vr.text

    voided = [e for e in await _doc_events(client, h, doc) if e["event_type"] == "doc.payment.voided"]
    assert voided, "voiding a payment must be tracked in history"
    assert voided[0]["data"]["amount"] == 500
    assert voided[0]["data"]["void_reason"] == "duplicate entry"

    # And it renders meaningfully (label + amount), not a blank generic row.
    from ui.components.activity import detail_from_entry, event_label
    assert event_label("doc.payment.voided") == "Payment voided"
    detail = detail_from_entry(voided[0]["data"], "doc.payment.voided", "USD")
    assert "$500.00" in detail and "duplicate entry" in detail
