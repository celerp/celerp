# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The books carry exactly the stock on hand after every step of the journeys that
move a lot onto or off them: Make Available and Revert to Draft, receiving goods and
undoing the receipt, taking goods back on a return and undoing it, and merging lots
and undoing the merge. The other journeys check the same oracle in their own files.
"""
from __future__ import annotations

import pytest

from stock_books import assert_books_carry_stock
from test_cost_restatement import _state, auth, ids  # noqa: F401  (auth and ids are fixtures)
from test_helpers import sell_item
from test_posting_roles_kept_stock import _available, _ok
from test_posting_roles_merge import _merged
from test_posting_roles_older_stock import _net
from test_receipt_accounting import _doc, _finalize, _receive

pytestmark = pytest.mark.asyncio


async def _carried(session, auth) -> dict:
    return await assert_books_carry_stock(session, auth["company_id"])


async def test_make_available_and_revert_to_draft(session, client, auth):
    lot = (await _ok(client, auth, "POST", "/items", {
        "sku": "ORC-DRAFT", "name": "Lot", "quantity": 2, "sell_by": "piece", "cost_total": 40.0}))["id"]
    assert sum((await _carried(session, auth)).values()) == 0
    for move, total in (("make-available", 40), ("revert-to-draft", 0), ("make-available", 40)):
        await _ok(client, auth, "POST", f"/items/bulk/{move}", {"entity_ids": [lot]})
        assert sum((await _carried(session, auth)).values()) == total, move


async def test_receiving_goods_and_undoing_the_receipt(session, client, auth):
    """An order books goods as they are received. A bill books its goods when it is
    finalized, arrived or not, so the books match the stock once a finalized bill's goods
    are in; undoing that receipt leaves the bill's own entry as it was."""
    po = await _doc(client, auth, "purchase_order",
                    [{"sku": "ORC-ORDER", "name": "Goods", "quantity": 2, "unit_price": 15.0}])
    r = await _receive(client, auth, po, {"po_line_index": 0, "sku": "ORC-ORDER", "name": "Goods",
                                          "quantity_received": 2})
    assert r.status_code == 200, r.text
    assert sum((await _carried(session, auth)).values()) == 30

    bill = await _doc(client, auth, "bill", [{"sku": "ORC-BILL", "name": "Goods", "quantity": 1, "unit_price": 20.0}])
    await _finalize(client, auth, bill)
    r = await _receive(client, auth, bill, {"po_line_index": 0, "sku": "ORC-BILL", "name": "Goods",
                                            "quantity_received": 1})
    assert r.status_code == 200, r.text
    assert sum((await _carried(session, auth)).values()) == 50
    await _ok(client, auth, "DELETE", f"/docs/{bill}/receive")
    assert sum((await _net(session, auth, "1130-P", "1130-OB"))) == 50


async def test_taking_goods_back_on_a_return_and_undoing_it(session, client, auth):
    lot = await _available(client, auth, 100.0)
    inv = await sell_item(client, auth["headers"], lot)
    assert sum((await _carried(session, auth)).values()) == 0
    cn = (await _ok(client, auth, "POST", "/docs", {
        "doc_type": "credit_note", "original_doc_id": inv, "total": 150.0, "line_items": [
            {"entity_id": lot, "name": "Lot", "quantity": 1, "unit_price": 150.0, "sell_by": "piece"}]}))["id"]
    await _ok(client, auth, "POST", f"/docs/{cn}/finalize")
    sku = (await _state(session, auth, lot))["sku"]
    await _ok(client, auth, "POST", f"/docs/{cn}/receive-return", {"items": [{"sku": sku, "quantity": 1}]})
    assert sum((await _carried(session, auth)).values()) == 100
    await _ok(client, auth, "DELETE", f"/docs/{cn}/receive-return")
    assert sum((await _carried(session, auth)).values()) == 0


async def test_merging_lots_and_undoing_the_merge(session, client, auth):
    a, b = await _available(client, auth, 30.0), await _available(client, auth, 70.0)
    assert sum((await _carried(session, auth)).values()) == 100
    merged = (await _merged(client, auth, [a, b]))["id"]
    assert sum((await _carried(session, auth)).values()) == 100
    await _ok(client, auth, "POST", f"/items/{merged}/undo-merge")
    assert sum((await _carried(session, auth)).values()) == 100
