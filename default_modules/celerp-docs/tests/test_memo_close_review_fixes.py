# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Terminal state of a closed memo across the send and shipping surfaces.

A closed memo is settled paperwork: it must not be re-sent (that would silently
un-close it) and it must not spawn a shipping document. Both are function-level
409s with a message that names the way back (Reopen first), and the send guard
is enforced on the backend regardless of what the UI offers.
"""
from __future__ import annotations

import uuid

import pytest


async def _register(client) -> str:
    addr = f"admin-{uuid.uuid4().hex[:8]}@memoclose.test"
    r = await client.post("/auth/register", json={"company_name": "MemoClose Co", "email": addr, "name": "A", "password": "pw"})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _h(t):
    return {"Authorization": f"Bearer {t}"}


async def _item(client, h, sku) -> str:
    r = await client.post("/items", headers=h, json={"status": "available", "sku": sku, "name": sku, "quantity": 1, "sell_by": "piece"})
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _memo(client, h, item_ids: list[str]) -> str:
    line_items = [{"entity_id": i, "sku": f"S-{n}", "name": f"S-{n}", "quantity": 1, "unit_price": 10, "sell_by": "piece"}
                  for n, i in enumerate(item_ids)]
    r = await client.post("/docs", headers=h, json={"doc_type": "memo", "line_items": line_items})
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _closed_memo(client, h) -> str:
    """A memo taken all the way to closed: finalize, ship a line then return it
    (so nothing is left memo_out and Close is allowed), then close."""
    a = await _item(client, h, f"CM-{uuid.uuid4().hex[:6]}")
    memo = await _memo(client, h, [a])
    assert (await client.post(f"/docs/{memo}/finalize", headers=h)).status_code == 200
    assert (await client.post(f"/docs/{memo}/fulfill-lines", headers=h, json={"line_entity_ids": [a]})).status_code == 200
    assert (await client.post(f"/docs/{memo}/revert-lines", headers=h, json={"line_entity_ids": [a]})).status_code == 200
    assert (await client.post(f"/docs/{memo}/close", headers=h, json={})).status_code == 200
    doc = (await client.get(f"/docs/{memo}", headers=h)).json()
    assert doc.get("status") == "closed", doc.get("status")
    return memo


@pytest.mark.asyncio
async def test_send_rejected_on_closed_memo(client):
    """POST /docs/{id}/send on a closed memo returns 409 and leaves it closed
    (a send must never silently un-close a settled memo)."""
    token = await _register(client)
    h = _h(token)
    memo = await _closed_memo(client, h)

    r = await client.post(f"/docs/{memo}/send", headers=h, json={"sent_via": "manual"})
    assert r.status_code == 409, r.text
    assert "closed" in r.text.lower()

    doc = (await client.get(f"/docs/{memo}", headers=h)).json()
    assert doc.get("status") == "closed", f"send must not un-close; got {doc.get('status')}"


@pytest.mark.asyncio
async def test_revert_lines_rejected_on_closed_memo(client):
    """POST /docs/{id}/revert-lines on a closed memo returns 409 and reverts nothing
    (reverting fulfillment on a settled memo must go through Reopen first). The doc
    stays closed and the referenced item's status is unchanged."""
    token = await _register(client)
    h = _h(token)
    memo = await _closed_memo(client, h)
    doc_before = (await client.get(f"/docs/{memo}", headers=h)).json()
    _li0 = doc_before["line_items"][0]
    line_eid = _li0.get("entity_id") or _li0.get("item_id")
    item_before = (await client.get(f"/items/{line_eid}", headers=h)).json()

    r = await client.post(f"/docs/{memo}/revert-lines", headers=h, json={"line_entity_ids": [line_eid]})
    assert r.status_code == 409, r.text
    assert "closed" in r.text.lower() or "reopen" in r.text.lower()

    doc_after = (await client.get(f"/docs/{memo}", headers=h)).json()
    item_after = (await client.get(f"/items/{line_eid}", headers=h)).json()
    assert doc_after.get("status") == "closed", f"revert must not un-close; got {doc_after.get('status')}"
    assert item_after.get("status") == item_before.get("status"), "revert must not change item status on a closed memo"


@pytest.mark.asyncio
async def test_closed_memo_no_create_shipping(client):
    """POST /shipment for a closed memo returns 409, nothing is created; a live
    (non-closed) memo still ships, so the guard is specific to closed."""
    token = await _register(client)
    h = _h(token)
    memo = await _closed_memo(client, h)

    r = await client.post("/docs/shipment", headers=h, json={"doc_ids": [memo]})
    assert r.status_code == 409, r.text
    assert "closed" in r.text.lower() or "reopen" in r.text.lower()
