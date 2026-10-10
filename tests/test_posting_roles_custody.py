# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Goods a document holds are resolved through that document.

A lot out on memo is at the customer under the memo: Archive and Expire refuse it and
name the document to resolve first, and the memo cannot be closed while it is out.
Returning it or converting the memo into an invoice resolves it, and the lot's
ordinary lifecycle then works again. A sold lot can still be archived to tidy the
catalog.
"""
from __future__ import annotations

import pytest

from stock_books import assert_books_carry_stock
from test_cost_restatement import _state
from test_helpers import sell_item
from test_posting_roles_kept_stock import _ROUTES, _available, _ok

pytestmark = pytest.mark.asyncio

_RESOLVE = "resolve the document"


async def _on_memo(session, client, auth) -> tuple[str, str]:
    lot = await _available(client, auth, 100.0)
    memo = (await _ok(client, auth, "POST", "/docs", {"doc_type": "memo", "line_items": [
        {"entity_id": lot, "name": "Lot", "quantity": 1, "unit_price": 150.0, "sell_by": "piece"}]}))["id"]
    await _ok(client, auth, "POST", f"/docs/{memo}/finalize")
    await _ok(client, auth, "POST", f"/docs/{memo}/fulfill-lines", {"line_entity_ids": [lot]})
    state = await _state(session, auth, lot)
    assert (state["status"], state["status_doc_id"]) == ("memo_out", memo)
    await assert_books_carry_stock(session, auth["company_id"])
    return lot, memo


async def _still_on_memo(session, auth, lot: str, memo: str) -> None:
    session.expire_all()
    state = await _state(session, auth, lot)
    assert (state["status"], state.get("status_doc_id")) == ("memo_out", memo)
    await assert_books_carry_stock(session, auth["company_id"])


async def _close(client, auth, memo: str):
    return await client.post(f"/docs/{memo}/close", headers=auth["headers"], json={})


@pytest.mark.parametrize("route", sorted(_ROUTES))
async def test_goods_out_on_memo_cannot_be_archived(session, client, auth, route):
    lot, memo = await _on_memo(session, client, auth)
    r = await _ROUTES[route](client, auth, lot, "archived")
    assert r.status_code == 409 and _RESOLVE in r.text, r.text
    await session.rollback()  # the refused request's work ends with it, as its own session would
    await _still_on_memo(session, auth, lot, memo)
    assert (await _close(client, auth, memo)).status_code == 409


@pytest.mark.parametrize("path", ["bulk", "single"])
async def test_goods_out_on_memo_cannot_be_expired(session, client, auth, path):
    lot, memo = await _on_memo(session, client, auth)
    if path == "bulk":
        r = await client.post("/items/bulk/expire", headers=auth["headers"], json={"entity_ids": [lot]})
    else:
        r = await client.post(f"/items/{lot}/expire", headers=auth["headers"], json={})
    assert r.status_code == 409 and _RESOLVE in r.text, r.text
    await session.rollback()  # the refused request's work ends with it, as its own session would
    await _still_on_memo(session, auth, lot, memo)
    assert (await _close(client, auth, memo)).status_code == 409


async def test_goods_returned_from_memo_can_be_archived_and_restored(session, client, auth):
    lot, memo = await _on_memo(session, client, auth)
    await _ok(client, auth, "POST", f"/docs/{memo}/revert-lines", {"line_entity_ids": [lot]})
    assert (await _state(session, auth, lot))["status"] == "available"
    assert (await _ROUTES["bulk"](client, auth, lot, "archived")).status_code == 200
    assert (await _state(session, auth, lot))["inventory_on_books"] is True
    await assert_books_carry_stock(session, auth["company_id"])
    assert (await _ROUTES["bulk"](client, auth, lot, "available")).status_code == 200
    await assert_books_carry_stock(session, auth["company_id"])
    assert (await _close(client, auth, memo)).status_code == 200


async def test_goods_a_memo_converts_into_a_sale_can_be_archived_off_the_books(session, client, auth):
    lot, memo = await _on_memo(session, client, auth)
    invoice = (await _ok(client, auth, "POST", f"/docs/{memo}/convert"))["target_doc_id"]
    assert (await _state(session, auth, lot))["status"] == "memo_out"
    await _ok(client, auth, "POST", f"/docs/{invoice}/finalize")
    assert (await _state(session, auth, lot))["status"] == "sold"
    await assert_books_carry_stock(session, auth["company_id"])
    assert (await _ROUTES["bulk"](client, auth, lot, "archived")).status_code == 200
    state = await _state(session, auth, lot)
    assert (state["status"], state.get("inventory_on_books")) == ("archived", None)
    await assert_books_carry_stock(session, auth["company_id"])


async def test_a_sold_lot_is_archived_to_tidy_the_catalog(session, client, auth):
    lot = await _available(client, auth, 100.0)
    await sell_item(client, auth["headers"], lot)
    for route in sorted(_ROUTES):
        lot = await _available(client, auth, 100.0)
        await sell_item(client, auth["headers"], lot)
        r = await _ROUTES[route](client, auth, lot, "archived")
        assert r.status_code == 200, (route, r.text)
        assert (await _state(session, auth, lot))["status"] == "archived"
    await assert_books_carry_stock(session, auth["company_id"])


async def test_a_memo_converted_to_an_invoice_bills_what_the_customer_kept(session, client, auth):
    """Red before: the invoice carried no total, so the customer owed nothing while finalize
    booked the cost of the goods."""
    from stock_books import assert_settled
    lot = await _available(client, auth, 100.0)
    memo = (await _ok(client, auth, "POST", "/docs", {"doc_type": "memo", "tax_rate": 10, "line_items": [
        {"entity_id": lot, "name": "Lot", "quantity": 1, "unit_price": 150.0, "sell_by": "piece"}]}))["id"]
    await _ok(client, auth, "POST", f"/docs/{memo}/finalize")
    await _ok(client, auth, "POST", f"/docs/{memo}/fulfill-lines", {"line_entity_ids": [lot]})
    invoice = (await _ok(client, auth, "POST", f"/docs/{memo}/convert"))["target_doc_id"]
    state = await _state(session, auth, invoice)
    assert (state["subtotal"], state["tax"], state["total"], state["amount_outstanding"]) == (150.0, 15.0, 165.0, 165.0)
    await _ok(client, auth, "POST", f"/docs/{invoice}/finalize")
    session.expire_all()
    state = await _state(session, auth, invoice)
    assert (state["status"], state["amount_outstanding"]) == ("final", 165.0)
    await assert_settled(client, session, auth)
