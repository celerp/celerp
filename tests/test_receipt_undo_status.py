# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Undoing a bill's receipt puts the bill back where its receipt found it.

A draft bill whose goods came in before it was issued is still a draft once the
receipt is undone: it has not been issued and books no payable. An issued bill goes
back to what its payments make it, so a paid bill stays paid.
"""
from __future__ import annotations

import uuid

import pytest

from test_helpers import default_location_id

pytestmark = pytest.mark.asyncio


async def _owner(client) -> dict:
    r = await client.post("/auth/register", json={
        "company_name": "Bills Co", "email": f"a-{uuid.uuid4().hex[:8]}@bills.test", "name": "Admin",
        "password": "pwvalid1"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def _bill(client, h: dict, sku: str) -> str:
    r = await client.post("/docs", headers=h, json={"doc_type": "bill", "line_items": [
        {"sku": sku, "name": "Widget", "quantity": 2, "unit_price": 50, "line_total": 100, "receive_as": "stock"}],
        "subtotal": 100, "tax": 0, "total": 100})
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _post(client, h: dict, path: str, body: dict | None = None) -> dict:
    r = await client.post(path, headers=h, json=body or {})
    assert r.status_code == 200, r.text
    return r.json()


async def _receive_and_undo(client, h: dict, bill: str, sku: str) -> dict:
    loc = await default_location_id(client, h)
    await _post(client, h, f"/docs/{bill}/receive", {"location_id": loc, "received_items": [
        {"po_line_index": 0, "sku": sku, "name": "Widget", "quantity_received": 2, "receive_as": "stock"}]})
    assert (await client.get(f"/docs/{bill}", headers=h)).json()["status"] == "received"
    r = await client.delete(f"/docs/{bill}/receive", headers=h)
    assert r.status_code == 200, r.text
    return (await client.get(f"/docs/{bill}", headers=h)).json()


async def test_undoing_a_receipt_on_a_draft_bill_leaves_it_a_draft(client):
    h = await _owner(client)
    bill = await _bill(client, h, "RU-DRAFT")

    doc = await _receive_and_undo(client, h, bill, "RU-DRAFT")

    assert doc["status"] == "draft"
    assert not doc.get("finalized")
    await _post(client, h, f"/docs/{bill}/finalize")
    doc = (await client.get(f"/docs/{bill}", headers=h)).json()
    assert (doc["status"], doc["finalized"]) == ("final", True)


async def test_undoing_a_receipt_on_an_issued_bill_leaves_it_final(client):
    h = await _owner(client)
    bill = await _bill(client, h, "RU-FINAL")
    await _post(client, h, f"/docs/{bill}/finalize")

    doc = await _receive_and_undo(client, h, bill, "RU-FINAL")

    assert doc["status"] == "final"


async def test_undoing_a_receipt_on_a_paid_bill_leaves_it_paid(client):
    h = await _owner(client)
    bill = await _bill(client, h, "RU-PAID")
    await _post(client, h, f"/docs/{bill}/finalize")
    await _post(client, h, f"/docs/{bill}/payment", {"payment_date": "2026-01-15", "amount": 100, "bank_account": "1111"})
    assert (await client.get(f"/docs/{bill}", headers=h)).json()["status"] == "paid"

    doc = await _receive_and_undo(client, h, bill, "RU-PAID")

    assert doc["status"] == "paid"
