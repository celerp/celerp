# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A finalized invoice recognizes its cost of sales before the goods ship. The books check
counts that cost as given up while the goods wait on hand, through void, unvoid and
shipping; and an invoice for more than is on hand costs only the units that exist. Goods on hand count as given up at most
once, and only while they are still on hand, so the check never hides a real gap."""
from __future__ import annotations

import uuid

import pytest

from celerp.services.lot_origin import stock_off_books
from stock_books import assert_settled
from test_money_stock_and_contact_invariants import _account_net

pytestmark = pytest.mark.asyncio


async def _lot(client, auth, sku: str, qty: float, cost_total: float) -> str:
    r = await client.post("/items", headers=auth["headers"], json={
        "sku": sku, "name": "Lot", "quantity": qty, "sell_by": "piece", "status": "available",
        "cost_total": cost_total})
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _invoice(client, auth, lot: str, sku: str, qty: float, *, ship: bool = False) -> str:
    r = await client.post("/docs", headers=auth["headers"], json={
        "doc_type": "invoice", "ref_id": f"INV-{uuid.uuid4().hex[:6]}",
        "line_items": [{"entity_id": lot, "sku": sku, "name": "Lot", "quantity": qty,
                        "unit_price": 40.0, "line_total": 40.0 * qty}],
        "total": 40.0 * qty})
    assert r.status_code == 200, r.text
    doc = r.json()["id"]
    r = await client.post(f"/docs/{doc}/finalize", headers=auth["headers"])
    assert r.status_code == 200, r.text
    if ship:
        await _ok(client, auth, f"/docs/{doc}/fulfill-lines", {"line_entity_ids": [lot]})
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



async def _books(session, auth) -> tuple[float, float]:
    return (await _account_net(session, auth["company_id"], "5100"),
            await _account_net(session, auth["company_id"], "1130-OB"))


async def _gaps(session, auth) -> list[dict]:
    session.expire_all()
    return [f for f in await stock_off_books(session, auth["company_id"]) if f["kind"] == "stock_gap"]


async def test_the_books_check_reports_a_lot_an_unshipped_invoice_costed_and_another_shipped(client, session, auth):
    """An invoice for 2 costs lot A (1 at 10) and lot B (1 at 30); B then ships on another
    invoice. Only A is still on hand, so only A's 10 counts as given up: the books check
    reports the 30 B was costed twice."""
    sku = f"SIB-{uuid.uuid4().hex[:6]}"
    a = await _lot(client, auth, sku, 1, 10.0)
    b = await _lot(client, auth, sku, 1, 30.0)
    await _invoice(client, auth, a, sku, 2)
    await _invoice(client, auth, b, sku, 1, ship=True)
    on_hand = 1 * 10.0  # lot A
    given_up = 1 * 10.0  # lot A, costed on the first invoice and still on hand
    books = (await _books(session, auth))[1]
    assert books == 40.0 - 40.0 - 30.0
    assert await _gaps(session, auth) == [
        {"kind": "stock_gap", "account": "1130-OB", "books": books, "stock": on_hand - given_up}]
    with pytest.raises(AssertionError):
        await assert_settled(client, session, auth)


async def test_the_books_check_counts_a_lot_two_invoices_costed_once(client, session, auth):
    """2 units at 10 each costed on an invoice, voided, costed on a second invoice, and the
    first restored: the lot's 20 is given up once, so the books check reports the second 20."""
    sku = f"DBL-{uuid.uuid4().hex[:6]}"
    lot = await _lot(client, auth, sku, 2, 20.0)
    one = await _invoice(client, auth, lot, sku, 2)
    await _ok(client, auth, f"/docs/{one}/void")
    await _invoice(client, auth, lot, sku, 2)
    await _ok(client, auth, f"/docs/{one}/unvoid")
    on_hand = 2 * 10.0
    given_up = 2 * 10.0  # at most what the lot holds, however many invoices costed it
    books = (await _books(session, auth))[1]
    assert books == 20.0 - 20.0 - 20.0
    assert await _gaps(session, auth) == [
        {"kind": "stock_gap", "account": "1130-OB", "books": books, "stock": on_hand - given_up}]
    with pytest.raises(AssertionError):
        await assert_settled(client, session, auth)
