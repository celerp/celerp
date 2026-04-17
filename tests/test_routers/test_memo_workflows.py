# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

from __future__ import annotations

import uuid

import pytest


async def _register(client) -> str:
    email = f"admin-{uuid.uuid4().hex[:8]}@memo.test"
    r = await client.post("/auth/register", json={"company_name": "Memo Co", "email": email, "name": "Admin", "password": "pw"})
    assert r.status_code == 200
    return r.json()["access_token"]


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_memo_to_invoice_and_items_marked_invoiced(client):
    token = await _register(client)

    item_id = (await client.post("/items", headers=_h(token), json={"sku": "M-1", "name": "Memo Item", "quantity": 1, "sell_by": "piece"})).json()["id"]
    memo_id = (await client.post("/crm/memos", headers=_h(token), json={"contact_id": "contact:1"})).json()["id"]

    assert (await client.post(f"/crm/memos/{memo_id}/items", headers=_h(token), json={"item_id": item_id, "quantity": 1})).status_code == 200
    converted = await client.post(f"/crm/memos/{memo_id}/convert-to-invoice", headers=_h(token))
    assert converted.status_code == 200

    doc_id = converted.json()["doc_id"]
    invoice = (await client.get(f"/docs/{doc_id}", headers=_h(token))).json()
    assert invoice["doc_type"] == "invoice"
    assert invoice["source_memo_id"] == memo_id

    memos = (await client.get("/crm/memos", headers=_h(token))).json()["items"]
    memo = next(m for m in memos if m["id"] == memo_id)
    assert memo["status"] == "invoiced"
    assert memo["items_invoiced"] == [item_id]


@pytest.mark.asyncio
async def test_memo_return_items_marks_available(client):
    token = await _register(client)

    item_id = (await client.post("/items", headers=_h(token), json={"sku": "M-2", "name": "Memo Return", "quantity": 1, "status": "on_memo", "sell_by": "piece"})).json()["id"]
    memo_id = (await client.post("/crm/memos", headers=_h(token), json={"contact_id": "contact:2"})).json()["id"]
    await client.post(f"/crm/memos/{memo_id}/items", headers=_h(token), json={"item_id": item_id, "quantity": 1})

    r = await client.post(f"/crm/memos/{memo_id}/return", headers=_h(token), json={"items": [{"item_id": item_id, "quantity": 1, "condition": "good"}]})
    assert r.status_code == 200

    item = (await client.get(f"/items/{item_id}", headers=_h(token))).json()
    assert item["status"] == "available"

    memos = (await client.get("/crm/memos", headers=_h(token))).json()["items"]
    memo = next(m for m in memos if m["id"] == memo_id)
    assert memo["status"] == "returned"
    assert memo["returned_items"][0]["item_id"] == item_id


@pytest.mark.asyncio
async def test_cannot_invoice_cancelled_memo(client):
    token = await _register(client)

    memo_id = (await client.post("/crm/memos", headers=_h(token), json={"contact_id": "contact:3"})).json()["id"]
    await client.post(f"/crm/memos/{memo_id}/cancel", headers=_h(token))

    r = await client.post(f"/crm/memos/{memo_id}/convert-to-invoice", headers=_h(token))
    assert r.status_code == 409
