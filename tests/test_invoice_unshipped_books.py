# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A finalized invoice recognizes its cost of sales before the goods ship. The books check
counts that cost as given up while the goods wait on hand, through void, unvoid and
shipping; and an invoice for more than is on hand costs only the units that exist."""
from __future__ import annotations

import uuid

import pytest

from stock_books import assert_settled
from test_money_stock_and_contact_invariants import _account_net

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


async def test_an_invoice_for_more_than_is_on_hand_costs_only_what_exists(client, session, auth):
    sku = f"OVR-{uuid.uuid4().hex[:6]}"
    lot = await _lot(client, auth, sku, 1, 3.0)
    await _invoice(client, auth, lot, sku, 2)
    assert (await _account_net(session, auth["company_id"], "5100"), await _account_net(session, auth["company_id"], "1130-OB")) == (3.0, 0.0)
    await assert_settled(client, session, auth)


async def test_the_missing_unit_is_costed_when_stock_for_it_ships(client, session, auth):
    sku = f"OVR-{uuid.uuid4().hex[:6]}"
    lot = await _lot(client, auth, sku, 1, 3.0)
    doc = await _invoice(client, auth, lot, sku, 2)
    await _lot(client, auth, sku, 1, 5.0)
    await _ok(client, auth, f"/docs/{doc}/fulfill-lines", {"line_entity_ids": [lot]})
    assert (await _account_net(session, auth["company_id"], "5100"), await _account_net(session, auth["company_id"], "1130-OB")) == (8.0, 0.0)
    await assert_settled(client, session, auth)

