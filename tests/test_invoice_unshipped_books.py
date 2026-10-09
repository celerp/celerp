# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A finalized invoice recognizes its cost of sales before the goods ship. The books check
counts that cost as given up while the goods wait on hand, through void, unvoid and
shipping."""
from __future__ import annotations

import uuid

import pytest

from stock_books import assert_settled

pytestmark = pytest.mark.asyncio


async def _lot(client, auth, sku: str, qty: float, cost_total: float) -> str:
    r = await client.post("/items", headers=auth["headers"], json={
        "sku": sku, "name": "Lot", "quantity": qty, "sell_by": "piece", "status": "available",
        "cost_total": cost_total})
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _invoice(client, auth, lot: str, sku: str, qty: float) -> str:
    r = await client.post("/docs", headers=auth["headers"], json={
        "doc_type": "invoice", "ref_id": f"INV-{uuid.uuid4().hex[:6]}",
        "line_items": [{"entity_id": lot, "sku": sku, "name": "Lot", "quantity": qty,
                        "unit_price": 40.0, "line_total": 40.0 * qty}],
        "total": 40.0 * qty})
    assert r.status_code == 200, r.text
    doc = r.json()["id"]
    r = await client.post(f"/docs/{doc}/finalize", headers=auth["headers"])
    assert r.status_code == 200, r.text
    return doc


async def _ok(client, auth, path: str, json=None) -> None:
    r = await client.post(path, headers=auth["headers"], json=json if json is not None else {})
    assert r.status_code == 200, r.text


async def test_an_unshipped_invoice_settles_through_void_unvoid_and_shipping(client, session, auth):
    sku = f"UNS-{uuid.uuid4().hex[:6]}"
    lot = await _lot(client, auth, sku, 2, 10.0)
    doc = await _invoice(client, auth, lot, sku, 2)
    await assert_settled(client, session, auth)
    await _ok(client, auth, f"/docs/{doc}/void")
    await assert_settled(client, session, auth)
    await _ok(client, auth, f"/docs/{doc}/unvoid")
    await assert_settled(client, session, auth)
    await _ok(client, auth, f"/docs/{doc}/fulfill-lines", {"line_entity_ids": [lot]})
    await assert_settled(client, session, auth)
