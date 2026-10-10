# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A bill's landed cost belongs to the units it bought, wherever they go.

Per bill and kind of charge: what the bill charged = what its lots still hold + cost of goods
sold for its units sold + stock shrinkage for its units sent back, to the cent. Its units sent
back take its charge per unit of its own goods, never more than its lots still hold of it, and
the last of them take whatever is left. Its units sold take their share of what the family held
right after the receipt. The books say the same whether the bill was finalized before the goods
came in or after.

Every test also checks the books hold: stock equals the inventory accounts, nothing is left on
the freight clearing account, and accounts payable equals what the bills still owe.
"""
from __future__ import annotations

import uuid

import pytest

from stock_books import assert_settled
from test_cost_restatement import _merge, _state
from test_landed_cost_pools import _b, _po_into, _post, _sell
from test_landed_cost_removals import _seed, _split
from test_landed_cost_structural import _undo
from test_money_stock_and_contact_invariants import _account_net
from test_receipt_accounting import _doc, _finalize, _receive, _return
from test_receive_selected_lines import _stamp_line_ids

LEGS = ("1130-OB", "1130-P", "1130-FRT", "1150", "2110", "4300", "5100", "6960", "6970")


async def _opening(client, auth, qty, cost):
    r = await client.post("/items", headers=auth["headers"], json={
        "sku": f"FL-{uuid.uuid4().hex[:6]}", "name": "Opening", "quantity": qty, "sell_by": "piece",
        "status": "available", "cost_total": cost})
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _order(client, auth, lot, qty, price, **extra):
    """A purchase order received into a lot on hand, not yet a bill."""
    doc = await _doc(client, auth, "purchase_order", [{"item_id": lot, "name": "Lot", "quantity": qty,
                                                       "unit_price": price}], **extra)
    r = await _receive(client, auth, doc, {"po_line_index": 0, "item_id": lot, "name": "Lot",
                                           "quantity_received": qty})
    assert r.status_code == 200, r.text
    return doc


async def _made(client, session, auth, qty, price, bill_first, **extra):
    """A document whose receipt makes a new lot; finalized before the receipt when ``bill_first``."""
    sku = f"FM-{uuid.uuid4().hex[:6]}"
    doc = await _doc(client, auth, "bill" if bill_first else "purchase_order",
                     [{"sku": sku, "name": "Made", "quantity": qty, "unit_price": price}], **extra)
    if bill_first:
        await _finalize(client, auth, doc)
    r = await _receive(client, auth, doc, {"po_line_index": 0, "sku": sku, "name": "Made",
                                           "quantity_received": qty})
    assert r.status_code == 200, r.text
    return doc, (await _state(session, auth, doc))["received_item_ids"][0]


def _pool(st: dict, doc: str, kind: str = "freight") -> float:
    return round(float((st.get("landed_costs") or {}).get(f"{doc}::{kind}") or 0), 2)


def _delta(start: dict, end: dict) -> dict:
    return {k: round(end[k] - start[k], 2) for k in LEGS}


async def _books_hold(client, session, auth, docs, start) -> dict:
    """Stock equals the books, the freight clearing account is clear and accounts payable is
    what the bills still owe; the legs moved since ``start``."""
    await assert_settled(client, session, auth)
    owed = 0.0
    for d in docs:
        st = await _state(session, auth, d)
        if st.get("doc_type") != "purchase_order" and st.get("status") not in ("draft", "void"):
            owed += float(st.get("amount_outstanding") or 0)
    assert round(await _account_net(session, auth["company_id"], "2110"), 2) == round(-owed, 2)
    moved = _delta(start, await _b(session, auth, *LEGS))
    assert moved["1130-FRT"] == 0.0, moved
    return moved


# ── The worked example: 10 on hand at 10.00, a bill adds 5 at 14.00 with 7.00 freight and 2
# go back. They take 1.40 each of the freight: 2.80 to shrinkage, 4.20 stays on the 3 kept,
# and the lot carries 100.00 + 3 x 14.00 + 4.20 = 146.20. ──────────────────────────────────
@pytest.mark.parametrize("order", ["bill_first", "order_first"])
async def test_units_sent_back_take_the_bills_freight_per_unit(client, session, auth, order):
    await _seed(session, auth)
    lot = await _opening(client, auth, 10, 100.0)
    start = await _b(session, auth, *LEGS)
    bill = await (_po_into if order == "bill_first" else _order)(client, auth, lot, 5, 14.0, shipping=7.0)
    r = await _return(client, auth, bill, lot, 2)
    assert r.status_code == 200, r.text
    if order == "order_first":
        await _finalize(client, auth, bill)
    moved = await _books_hold(client, session, auth, [bill], start)
    assert round(float((await _state(session, auth, lot))["cost_total"]), 2) == 146.20
    assert moved == {**dict.fromkeys(LEGS, 0.0), "1130-OB": 46.20, "2110": -49.0, "6970": 2.80}, moved


# The worked example sold out: cost of goods sold takes the lot's 146.20 once, unit for unit,
# and the books hold none of its stock.
@pytest.mark.parametrize("order", ["bill_first", "order_first"])
async def test_the_worked_example_sells_out_at_its_cost_once(client, session, auth, order):
    await _seed(session, auth)
    lot = await _opening(client, auth, 10, 100.0)
    start = await _b(session, auth, *LEGS)
    bill = await (_po_into if order == "bill_first" else _order)(client, auth, lot, 5, 14.0, shipping=7.0)
    r = await _return(client, auth, bill, lot, 2)
    assert r.status_code == 200, r.text
    if order == "order_first":
        await _finalize(client, auth, bill)
    await _sell(client, auth, lot, 13)
    moved = await _books_hold(client, session, auth, [bill], start)
    assert moved == {**dict.fromkeys(LEGS, 0.0), "1130-OB": -100.0, "2110": -49.0, "5100": 146.20,
                     "6970": 2.80}, moved


# The bill's goods all going back sends all its freight to shrinkage; taking the bill back to
# draft then leaves every leg where it started.
@pytest.mark.parametrize("order", ["bill_first", "order_first"])
async def test_the_worked_example_fully_returned_clears_every_leg(client, session, auth, order):
    await _seed(session, auth)
    lot = await _opening(client, auth, 10, 100.0)
    start = await _b(session, auth, *LEGS)
    bill = await (_po_into if order == "bill_first" else _order)(client, auth, lot, 5, 14.0, shipping=7.0)
    for qty in (2, 3):
        r = await _return(client, auth, bill, lot, qty)
        assert r.status_code == 200, r.text
    if order == "order_first":
        await _finalize(client, auth, bill)
    moved = await _books_hold(client, session, auth, [bill], start)
    assert moved == {**dict.fromkeys(LEGS, 0.0), "2110": -7.0, "6970": 7.0}, moved
    assert round(float((await _state(session, auth, lot))["cost_total"]), 2) == 100.0
    r = await _post(client, auth, bill, "revert-to-draft")
    assert r.status_code == 200, r.text
    assert await _books_hold(client, session, auth, [bill], start) == dict.fromkeys(LEGS, 0.0)


# ── F1. A lot the bill made, topped up by another bill: the first bill's units going back
# take all of its freight, the other bill's units none. Bill B makes 5 at 10.00 with 5.00
# freight, bill A adds 5 at 10.00 and B returns its 5: 5.00 to shrinkage, the lot is A's
# 50.00. A returning its own 5 then empties the lot and every stock account. ───────────────
@pytest.mark.parametrize("order", ["bill_first", "order_first"])
async def test_a_made_lot_sends_back_its_bills_freight_with_its_units(client, session, auth, order):
    await _seed(session, auth)
    start = await _b(session, auth, *LEGS)
    bill, lot = await _made(client, session, auth, 5, 10.0, order == "bill_first", shipping=5.0)
    other = await _po_into(client, auth, lot, 5, 10.0)
    r = await _return(client, auth, bill, lot, 5)
    assert r.status_code == 200, r.text
    if order == "order_first":
        await _finalize(client, auth, bill)
    moved = await _books_hold(client, session, auth, [bill, other], start)
    st = await _state(session, auth, lot)
    assert round(float(st["cost_total"]), 2) == 50.00 and _pool(st, bill) == 0.0, st
    assert moved == {**dict.fromkeys(LEGS, 0.0), "1130-P": 50.0, "2110": -55.0, "6970": 5.0}, moved
    # Everything back: no stock and no cost of goods sold; the bill still owes its freight.
    r = await _return(client, auth, other, lot, 5)
    assert r.status_code == 200, r.text
    moved = await _books_hold(client, session, auth, [bill, other], start)
    assert moved == {**dict.fromkeys(LEGS, 0.0), "2110": -5.0, "6970": 5.0}, moved


# ── F2. Units sold before another bill tops the lot up take their share of what the lot held
# right after the receipt: 10 of the 15 then on hand were sold, so 7.00 x 10/15 = 4.67 of the
# freight is cost of goods sold (with 100.00 + 70.00 x 10/15 of goods: 118.00 in all) and 2.33
# stays with the 5 kept, never diluted by the 10 added later. ───────────────────────────────
@pytest.mark.parametrize("order", ["bill_first", "order_first"])
async def test_units_sold_take_the_freight_of_the_family_at_the_receipt(client, session, auth, order):
    await _seed(session, auth)
    lot = await _opening(client, auth, 10, 100.0)
    start = await _b(session, auth, *LEGS)
    bill = await (_po_into if order == "bill_first" else _order)(client, auth, lot, 5, 14.0, shipping=7.0)
    await _sell(client, auth, lot, 10)
    other = await _po_into(client, auth, lot, 10, 10.0)
    if order == "order_first":
        await _finalize(client, auth, bill)
    moved = await _books_hold(client, session, auth, [bill, other], start)
    assert _pool(await _state(session, auth, lot), bill) == 2.33
    # Cost of goods sold is booked once, for the 10 units sold.
    assert moved == {**dict.fromkeys(LEGS, 0.0), "1130-OB": 59.0, "2110": -177.0, "5100": 118.0}, moved


# ── F3. The bill's 5 units go back from a part split off the lot and from the lot itself: all
# 7.00 of its freight goes to shrinkage, whichever goes first, and none stays on the opening
# units. Taking the bill back to draft clears every leg; finalizing it again books exactly
# what the returns booked. ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("order", ["bill_first", "order_first"])
@pytest.mark.parametrize("first", ["part", "lot"])
async def test_a_split_lineage_sends_back_all_the_bills_freight(client, session, auth, first, order):
    await _seed(session, auth)
    lot = await _opening(client, auth, 10, 100.0)
    start = await _b(session, auth, *LEGS)
    bill = await (_po_into if order == "bill_first" else _order)(client, auth, lot, 5, 14.0, shipping=7.0)
    [part] = await _split(client, auth, lot, 5)
    for item, qty in ([(part, 2), (lot, 3)] if first == "part" else [(lot, 3), (part, 2)]):
        r = await _return(client, auth, bill, item, qty)
        assert r.status_code == 200, r.text
    if order == "order_first":
        await _finalize(client, auth, bill)
    returned = await _books_hold(client, session, auth, [bill], start)
    assert returned == {**dict.fromkeys(LEGS, 0.0), "2110": -7.0, "6970": 7.0}, returned
    assert [_pool(await _state(session, auth, x), bill) for x in (lot, part)] == [0.0, 0.0]
    r = await _post(client, auth, bill, "revert-to-draft")
    assert r.status_code == 200, r.text
    assert await _books_hold(client, session, auth, [bill], start) == dict.fromkeys(LEGS, 0.0)
    await _finalize(client, auth, bill)
    assert await _books_hold(client, session, auth, [bill], start) == returned


# ── F4. Two lines of one bill into one lot at very different prices: 1 at 1000.00 and 9 at
# 1.00, with 10.00 freight spread by value (9.91 and 0.09). The cheap line goes back by line:
# the bill owes 9.00 less, the lot gives up 9.00 of goods and the line's 0.09 of freight. The
# dear line then empties the bill's goods: the lot is the opening 100.00 again and all 10.00
# of freight is shrinkage. By lot the two prices cannot be told apart, so it is refused. ────
async def _two_lines_into(client, session, auth, lot, prices, qtys, shipping, bill_first=True):
    """A purchase order whose two lines are received into one lot; finalized when ``bill_first``."""
    doc = await _doc(client, auth, "purchase_order", [
        {"item_id": lot, "name": f"Line {i}", "quantity": q, "unit_price": p}
        for i, (p, q) in enumerate(zip(prices, qtys))], shipping=shipping)
    ids = await _stamp_line_ids(session, auth, doc)
    r = await _receive(client, auth, doc, *({"source_line_id": i, "item_id": lot, "name": f"Line {n}",
                                             "quantity_received": q} for n, (i, q) in enumerate(zip(ids, qtys))))
    assert r.status_code == 200, r.text
    if bill_first:
        await _finalize(client, auth, doc)
    return doc, ids


async def _return_line(client, auth, doc, line_id, qty):
    return await client.post(f"/docs/{doc}/return-items", headers=auth["headers"],
                             json={"lines": [{"line_id": line_id, "quantity_returned": qty}]})


@pytest.mark.parametrize("order", ["bill_first", "order_first"])
async def test_lines_sharing_a_lot_go_back_at_their_own_price_and_freight(client, session, auth, order):
    await _seed(session, auth)
    lot = await _opening(client, auth, 10, 100.0)
    start = await _b(session, auth, *LEGS)
    bill, (dear, cheap) = await _two_lines_into(client, session, auth, lot, (1000.0, 1.0), (1, 9), 10.0,
                                                order == "bill_first")
    if order == "bill_first":
        assert round(float((await _state(session, auth, bill))["amount_outstanding"]), 2) == 1019.0
    held = await _b(session, auth, *LEGS)
    r = await _return(client, auth, bill, lot, 9)
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["message_key"] == "docs.return_lot_mixed_prices", r.text
    assert await _b(session, auth, *LEGS) == held
    r = await _return_line(client, auth, bill, dear, 2)
    assert r.status_code == 422 and "docs.return_line_not_on_hand" in r.text, r.text

    r = await _return_line(client, auth, bill, cheap, 9)
    assert r.status_code == 200, r.text
    if order == "order_first":
        await _finalize(client, auth, bill)
    moved = await _books_hold(client, session, auth, [bill], start)
    assert round(float((await _state(session, auth, bill))["amount_outstanding"]), 2) == 1010.0
    assert moved == {**dict.fromkeys(LEGS, 0.0), "1130-OB": 1009.91, "2110": -1010.0, "6970": 0.09}, moved
    st = await _state(session, auth, lot)
    assert (float(st["quantity"]), round(float(st["cost_total"]), 2), _pool(st, bill)) == (11.0, 1109.91, 9.91)

    r = await _return_line(client, auth, bill, dear, 1)
    assert r.status_code == 200, r.text
    moved = await _books_hold(client, session, auth, [bill], start)
    assert moved == {**dict.fromkeys(LEGS, 0.0), "2110": -10.0, "6970": 10.0}, moved
    st = await _state(session, auth, lot)
    assert (float(st["quantity"]), round(float(st["cost_total"]), 2)) == (10.0, 100.0)


# Lines at the same price into one lot go back by lot as before, and by line too.
@pytest.mark.parametrize("order", ["bill_first", "order_first"])
async def test_lines_at_one_price_sharing_a_lot_go_back_by_lot_or_line(client, session, auth, order):
    await _seed(session, auth)
    lot = await _opening(client, auth, 10, 100.0)
    start = await _b(session, auth, *LEGS)
    bill, (first, second) = await _two_lines_into(client, session, auth, lot, (14.0, 14.0), (5, 5), 7.0,
                                                  order == "bill_first")
    r = await _return(client, auth, bill, lot, 3)
    assert r.status_code == 200, r.text
    r = await _return_line(client, auth, bill, second, 5)
    assert r.status_code == 200, r.text
    r = await _return_line(client, auth, bill, first, 2)
    assert r.status_code == 200, r.text
    if order == "order_first":
        await _finalize(client, auth, bill)
    moved = await _books_hold(client, session, auth, [bill], start)
    assert moved == {**dict.fromkeys(LEGS, 0.0), "2110": -7.0, "6970": 7.0}, moved
    assert round(float((await _state(session, auth, lot))["cost_total"]), 2) == 100.0


# ── F5. A lot merged into another holds none of the goods any more: undoing the receipt and
# returning from it both say where they went, and nothing moves. ───────────────────────────
@pytest.mark.parametrize("order", ["bill_first", "order_first"])
async def test_a_merged_lot_says_where_its_goods_went(client, session, auth, order):
    await _seed(session, auth)
    lot = await _opening(client, auth, 10, 100.0)
    bill = await (_po_into if order == "bill_first" else _order)(client, auth, lot, 5, 14.0, shipping=7.0)
    other = await _opening(client, auth, 3, 30.0)
    into = await _merge(client, auth, [lot, other])
    into_sku = (await _state(session, auth, into))["sku"]
    held = await _b(session, auth, *LEGS)
    r = await _return(client, auth, bill, lot, 1)
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert detail["message_key"] == "docs.return_lot_merged" and detail["params"]["into"] == into_sku, detail
    if order == "order_first":
        await _finalize(client, auth, bill)
        held = await _b(session, auth, *LEGS)
    r = await _undo(client, auth, bill)
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert detail["message_key"] == "docs.undo_receipt_merged" and detail["params"]["into"] == into_sku, detail
    assert await _b(session, auth, *LEGS) == held
    await _books_hold(client, session, auth, [bill], held)
