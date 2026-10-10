# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A bill's money carries to the cent through receipts, sales and returns.

At every step accounts payable equals what the bill still owes at its rate, the AP aging
agrees, the books carry exactly the stock on hand, the freight clearing account holds
nothing once every good is in, and input VAT holds only the VAT on goods not sent back.
Goods received a few at a time carry, together, exactly what the bill booked for their
line, landed cost included. Landed cost a lot carries is a fixed amount: stock joining
the lot later spreads it, never adds to it.
"""
from __future__ import annotations

import uuid

import pytest

from stock_books import assert_settled
from test_cost_restatement import _item, _set_cost, _state
from test_money_stock_and_contact_invariants import _account_net
from test_receipt_accounting import _OPENING, _doc, _finalize, _receive, _return
from test_supplier_return_settles_bill import _aged

_VAT = {"doc_taxes": [{"code": "VAT", "rate": 7.0, "order": 1, "is_compound": False, "label": "VAT"}]}
_ORDERS = pytest.mark.parametrize("from_order", [False, True], ids=["bill_first", "order_first"])


def _goods(price: float, qty: float) -> dict:
    return {"sku": f"BM-{uuid.uuid4().hex[:6]}", "name": "Beads", "quantity": qty, "unit_price": price}


async def _b(session, auth, *codes: str) -> dict[str, float]:
    return {c: round(await _account_net(session, auth["company_id"], c), 2) for c in codes}


async def _post(client, auth, doc, action, body=None):
    return await client.post(f"/docs/{doc}/{action}", headers=auth["headers"], json=body or {})


async def _inv(client, session, auth, doc, rate: float = 1.0, step: str = "", others: float = 0.0,
               settled: bool = True) -> dict:
    st = await _state(session, auth, doc)
    out = float(st.get("amount_outstanding") or 0) if st.get("status") not in ("draft", "void") else 0.0
    if st.get("doc_type") == "purchase_order":
        out = 0.0
    ap = await _account_net(session, auth["company_id"], "2110")
    assert round(ap, 2) == round(-out * rate - others, 2), (step, "2110", ap, "outstanding", out, st.get("status"))
    assert round(await _aged(client, auth), 2) == round(out + others, 2), (step, "aging")
    if settled:
        await assert_settled(client, session, auth)
    return st


async def _received(client, session, auth, lines, *, from_order: bool, **extra):
    doc = await _doc(client, auth, "purchase_order" if from_order else "bill", lines, **extra)
    if not from_order:
        await _finalize(client, auth, doc)
    st = await _state(session, auth, doc)
    r = await _receive(client, auth, doc, *(
        {"po_line_index": i, "sku": li["sku"], "name": li["name"], "quantity_received": li["quantity"]}
        for i, li in enumerate(st["line_items"]) if li.get("sku") != "FRT"))
    assert r.status_code == 200, r.text
    if from_order:
        await _finalize(client, auth, doc)
    return doc, (await _state(session, auth, doc))["received_item_ids"]


async def _sell(client, auth, lot: str, qty: float) -> str:
    r = await client.post("/docs", headers=auth["headers"], json={
        "doc_type": "invoice", "line_items": [{"entity_id": lot, "name": "Lot", "quantity": qty,
                                               "unit_price": 50.0, "sell_by": "piece"}],
        "total": 50.0 * qty})
    assert r.status_code == 200, r.text
    doc = r.json()["id"]
    assert (await _post(client, auth, doc, "finalize")).status_code == 200
    r = await _post(client, auth, doc, "fulfill-lines", {"line_entity_ids": [lot]})
    assert r.status_code == 200, r.text
    return doc


# Discount + shipping + VAT, partial then full return, two lines.
@_ORDERS
async def test_discount_shipping_vat_partial_then_full_return(client, session, auth, from_order):
    doc, [a, b] = await _received(client, session, auth, [_goods(12.0, 10), _goods(4.0, 5)], from_order=from_order,
                                  discount=10.0, discount_type="percentage", shipping=8.0, **_VAT)
    st = await _inv(client, session, auth, doc, step="recv")
    # subtotal 140, -14 = 126, VAT 8.82, shipping 8 -> 142.82
    assert st["total"] == 142.82
    assert await _b(session, auth, "1130-P", "1130-FRT", "1150", "2110") == {
        "1130-P": 134.0, "1130-FRT": 0.0, "1150": 8.82, "2110": -142.82}
    assert (await _return(client, auth, doc, a, 3)).status_code == 200
    await _inv(client, session, auth, doc, step="ret a3")
    # 3 of line a: net 32.40, VAT 2.27 (7% of 32.40 = 2.268)
    assert (await _return(client, auth, doc, b, 2)).status_code == 200
    await _inv(client, session, auth, doc, step="ret b2")
    assert (await _return(client, auth, doc, a, 7)).status_code == 200
    await _inv(client, session, auth, doc, step="ret a7")
    assert (await _return(client, auth, doc, b, 3)).status_code == 200
    st = await _inv(client, session, auth, doc, step="ret b3")
    assert (st["status"], st["amount_outstanding"]) == ("returned", 8.0)
    assert await _b(session, auth, "1130-P", "1130-FRT", "1150", "2110", "6970", "4300", "5100") == {
        "1130-P": 0.0, "1130-FRT": 0.0, "1150": 0.0, "2110": -8.0, "6970": 8.0, "4300": 0.0, "5100": 0.0}


# Partial receipts of a discounted line: lots must sum to what the bill booked.
@pytest.mark.parametrize("discount, shipping", [(0.10, 0.10), (0.10, 0.0), (0.0, 0.10)],
                         ids=["both", "discount_only", "shipping_only"])
@_ORDERS
async def test_discounted_line_received_in_thirds_carries_to_the_cent(client, session, auth, from_order,
                                                                      discount, shipping):
    doc = await _doc(client, auth, "purchase_order" if from_order else "bill", [_goods(1.0, 3)],
                     discount=discount, shipping=shipping)
    if not from_order:
        await _finalize(client, auth, doc)
    st = await _state(session, auth, doc)
    li = st["line_items"][0]
    for _ in range(3):
        r = await _receive(client, auth, doc, {"po_line_index": 0, "sku": li["sku"], "name": li["name"],
                                               "quantity_received": 1})
        assert r.status_code == 200, r.text
    if from_order:
        await _finalize(client, auth, doc)
    st = await _inv(client, session, auth, doc, step="thirds")
    lots = st["received_item_ids"]
    costs = [round(float((await _state(session, auth, x))["cost_total"]), 2) for x in lots]
    want = round(3.00 - discount + shipping, 2)
    assert round(sum(costs), 2) == want, costs
    assert await _b(session, auth, "1130-P", "1130-FRT") == {"1130-P": want, "1130-FRT": 0.0}
    for x in lots:
        assert (await _return(client, auth, doc, x, 1)).status_code == 200
        await _inv(client, session, auth, doc, step=f"ret {x}")
    assert await _b(session, auth, "1130-P", "1130-FRT", "2110", "6970") == {
        "1130-P": 0.0, "1130-FRT": 0.0, "2110": -shipping, "6970": shipping}


# Restatement then partial sale then return of the rest.
async def test_restated_part_sold_rest_returned(client, session, auth):
    doc, [lot] = await _received(client, session, auth, [_goods(12.0, 10)], from_order=False,
                                 shipping=5.0, **_VAT)
    assert round(float((await _state(session, auth, lot))["cost_total"]), 2) == 125.0
    assert (await _set_cost(client, auth, lot, 300.0)).status_code == 200
    await _inv(client, session, auth, doc, step="restated")
    await _sell(client, auth, lot, 5)
    await _inv(client, session, auth, doc, step="sold5")
    st = await _state(session, auth, doc)
    left = [x for x in [lot, *(await _state(session, auth, lot)).get("children", [])]
            if (await _state(session, auth, x)).get("status") not in ("sold",)
            and float((await _state(session, auth, x)).get("quantity") or 0) > 0]
    assert left, "nothing left on hand to return"
    assert (await _return(client, auth, doc, left[0], 5)).status_code == 200, "return rest"
    st = await _inv(client, session, auth, doc, step="ret5")
    # Bill 120 + 8.40 VAT + 5 ship = 133.40; 5 back at 60 + 4.20 VAT -> owes 69.20.
    assert st["amount_outstanding"] == 69.2
    assert await _b(session, auth, "1130-P", "1130-FRT", "1150") == {"1130-P": 0.0, "1130-FRT": 0.0, "1150": 4.2}
    r = await _post(client, auth, doc, "void")
    assert r.status_code == 409, ("void of a bill whose goods were partly sold must refuse", r.text)
    r = await _post(client, auth, doc, "revert-to-draft")
    assert r.status_code == 409, ("revert of a bill whose goods were partly sold must refuse", r.text)


# FX with discount, shipping and VAT.
@pytest.mark.parametrize("currency, rate", [("EUR", 1.37)])
@_ORDERS
async def test_fx_discount_shipping_vat_returns(client, session, auth, from_order, currency, rate):
    doc, [a, b] = await _received(client, session, auth, [_goods(13.37, 7), _goods(4.99, 11)], from_order=from_order,
                                  discount=3.0, discount_type="percentage", shipping=9.99,
                                  currency=currency, conversion_rate=rate, **_VAT)
    await _inv(client, session, auth, doc, rate=rate, step="recv")
    for lot, q in ((a, 2), (b, 5), (a, 5), (b, 6)):
        assert (await _return(client, auth, doc, lot, q)).status_code == 200
        await _inv(client, session, auth, doc, rate=rate, step=f"ret {q}")
    st = await _state(session, auth, doc)
    assert (st["status"], st["amount_outstanding"]) == ("returned", 9.99)
    books = await _b(session, auth, "1130-P", "1130-FRT", "1150")
    assert books == {"1130-P": 0.0, "1130-FRT": 0.0, "1150": 0.0}, books


# Void / unvoid / revert ordering on a fully returned bill with every charge.
async def test_return_void_unvoid_revert_refinalize_cycle(client, session, auth):
    doc, [a] = await _received(client, session, auth, [_goods(12.0, 10)], from_order=False,
                               discount=5.0, shipping=6.0, **_VAT)
    for q in (4, 6):
        assert (await _return(client, auth, doc, a, q)).status_code == 200
        await _inv(client, session, auth, doc, step=f"ret{q}")
    r = await _return(client, auth, doc, a, 1)
    assert r.status_code in (409, 422), r.text
    zero = {"1130-P": 0.0, "1130-FRT": 0.0, "1150": 0.0, "2110": 0.0, "6970": 0.0, "4300": 0.0, "5100": 0.0}
    for action in ("void", "unvoid", "void", "unvoid"):
        r = await _post(client, auth, doc, action)
        assert r.status_code == 200, (action, r.text)
        await _inv(client, session, auth, doc, step=action)
        if action == "void":
            assert await _b(session, auth, *zero) == zero, action
    r = await _post(client, auth, doc, "revert-to-draft")
    assert r.status_code == 200, r.text
    await _inv(client, session, auth, doc, step="revert")
    assert await _b(session, auth, *zero) == zero
    await _finalize(client, auth, doc)
    st = await _inv(client, session, auth, doc, step="refinalize", settled=False)
    assert st["amount_outstanding"] == st["total"]
    li = st["line_items"][0]
    r = await _receive(client, auth, doc, {"po_line_index": 0, "sku": li["sku"], "name": li["name"],
                                           "quantity_received": 10})
    assert r.status_code == 200, r.text
    st = await _inv(client, session, auth, doc, step="re-receive")
    new = [x for x in st["received_item_ids"] if x != a]
    assert new, st["received_item_ids"]
    assert await _b(session, auth, "1130-FRT") == {"1130-FRT": 0.0}
    assert (await _return(client, auth, doc, new[-1], 10)).status_code == 200
    st = await _inv(client, session, auth, doc, step="re-return")
    assert st["amount_outstanding"] == 6.0
    assert (await _post(client, auth, doc, "void")).status_code == 200
    await _inv(client, session, auth, doc, step="final void")
    assert await _b(session, auth, *zero) == zero


# Received-first order with shipping, part returned, reverted to the order, billed again.
async def test_order_with_shipping_returned_reverted_and_billed_again(client, session, auth):
    doc, [lot] = await _received(client, session, auth, [_goods(10.0, 10)], from_order=True, shipping=10.0)
    assert (await _return(client, auth, doc, lot, 4)).status_code == 200
    await _inv(client, session, auth, doc, step="ret4")
    r = await _post(client, auth, doc, "revert-to-draft")
    assert r.status_code in (200, 409), r.text
    if r.status_code == 200:
        await _inv(client, session, auth, doc, step="reverted")
        await _finalize(client, auth, doc)
        await _inv(client, session, auth, doc, step="billed again")
        assert await _b(session, auth, "1130-FRT") == {"1130-FRT": 0.0}
    assert (await _return(client, auth, doc, lot, 6)).status_code == 200
    st = await _inv(client, session, auth, doc, step="ret6")
    assert await _b(session, auth, "1130-P", "1130-FRT", "6970") == {"1130-P": 0.0, "1130-FRT": 0.0, "6970": 10.0}
    assert st["amount_outstanding"] == 10.0


# Quantities in the millions.
@_ORDERS
async def test_millions_of_units(client, session, auth, from_order):
    doc, [lot] = await _received(client, session, auth, [_goods(0.0033, 3_000_000)], from_order=from_order,
                                 discount=10.0, discount_type="percentage", shipping=123.45, **_VAT)
    await _inv(client, session, auth, doc, step="recv")
    for q in (1_234_567, 765_433, 999_999, 1):
        assert (await _return(client, auth, doc, lot, q)).status_code == 200
        await _inv(client, session, auth, doc, step=f"ret {q}")
    st = await _state(session, auth, doc)
    assert (st["status"], st["amount_outstanding"]) == ("returned", 123.45)
    assert await _b(session, auth, "1130-P", "1130-FRT", "1150", "6970") == {
        "1130-P": 0.0, "1130-FRT": 0.0, "1150": 0.0, "6970": 123.45}


# Two orders into one lot on hand, each with shipping, both billed, returns from each.
async def test_two_orders_into_one_lot_with_shipping_then_returned(client, session, auth):
    lot = await _item(client, auth, _OPENING, qty=10)
    docs = []
    owed = 0.0
    for price, ship in ((14.0, 7.0), (16.0, 3.0)):
        doc = await _doc(client, auth, "purchase_order", [{"item_id": lot, "name": "Lot", "quantity": 5,
                                                           "unit_price": price}], shipping=ship, **_VAT)
        r = await _receive(client, auth, doc, {"po_line_index": 0, "item_id": lot, "name": "Lot",
                                               "quantity_received": 5})
        assert r.status_code == 200, r.text
        await _finalize(client, auth, doc)
        docs.append(doc)
        await _inv(client, session, auth, doc, step=f"billed {price}", others=owed)
        owed += (await _state(session, auth, doc))["amount_outstanding"]
    # 100 + 70 + 7 + 80 + 3 = 260 over 20 units
    assert round(float((await _state(session, auth, lot))["cost_total"]), 2) == 260.0
    for doc in docs:
        assert (await _return(client, auth, doc, lot, 5)).status_code == 200
    await _inv(client, session, auth, docs[0], step="returned",
               others=(await _state(session, auth, docs[1]))["amount_outstanding"])
    for doc, ship in zip(docs, (7.0, 3.0)):
        st = await _state(session, auth, doc)
        assert (st["status"], st["amount_outstanding"]) == ("returned", ship), doc
    assert await _b(session, auth, "1130-FRT", "1150", "2110") == {"1130-FRT": 0.0, "1150": 0.0, "2110": -10.0}


# Merge into an existing lot, billed, part sold, rest of the order's goods returned.
async def test_merged_lot_part_sold_then_order_goods_returned(client, session, auth):
    lot = await _item(client, auth, _OPENING, qty=10)
    doc = await _doc(client, auth, "purchase_order", [{"item_id": lot, "name": "Lot", "quantity": 5,
                                                       "unit_price": 14.0}], shipping=7.0, discount=5.0, **_VAT)
    r = await _receive(client, auth, doc, {"po_line_index": 0, "item_id": lot, "name": "Lot", "quantity_received": 5})
    assert r.status_code == 200, r.text
    await _finalize(client, auth, doc)
    await _inv(client, session, auth, doc, step="billed")
    await _sell(client, auth, lot, 8)
    await _inv(client, session, auth, doc, step="sold8")
    held = [x for x in [lot, *(await _state(session, auth, lot)).get("children", [])]
            if (await _state(session, auth, x)).get("status") != "sold"
            and float((await _state(session, auth, x)).get("quantity") or 0) > 0]
    r = await _return(client, auth, doc, held[0], 5)
    assert r.status_code == 200, r.text
    st = await _inv(client, session, auth, doc, step="ret5")
    assert await _b(session, auth, "1130-FRT", "1150") == {"1130-FRT": 0.0, "1150": 0.0}
    assert st["amount_outstanding"] == 7.0


# Freight charge line under a bill discount, everything returned.
@_ORDERS
async def test_discounted_freight_line_and_shipping_all_returned(client, session, auth, from_order):
    r = await client.post("/items", headers=auth["headers"], json={
        "status": "available", "sku": "FRT", "name": "Freight", "quantity": 0, "sell_by": "piece",
        "inventory_type": "freight", "landed_cost_kind": "freight"})
    frt = {"entity_id": r.json()["id"], "sku": "FRT", "name": "Freight", "quantity": 1, "unit_price": 10.0}
    doc, [lot] = await _received(client, session, auth, [_goods(9.0, 10), frt], from_order=from_order,
                                 discount=10.0, discount_type="percentage", shipping=4.0, **_VAT)
    # 90 + 10 = 100, -10 = 90 (goods 81, freight 9), VAT 6.30, shipping 4 -> 100.30
    st = await _inv(client, session, auth, doc, step="recv")
    assert st["total"] == 100.3
    assert round(float((await _state(session, auth, lot))["cost_total"]), 2) == 94.0
    assert (await _return(client, auth, doc, lot, 10)).status_code == 200
    st = await _inv(client, session, auth, doc, step="ret")
    # Freight line 9 + its VAT 0.63 + shipping 4 remain owed.
    assert st["amount_outstanding"] == 13.63
    assert await _b(session, auth, "1130-P", "1130-FRT", "1150", "6970") == {
        "1130-P": 0.0, "1130-FRT": 0.0, "1150": 0.63, "6970": 13.0}


# Freight capitalised per unit onto a merged lot must not grow when later goods join the lot.
# Only a purchase order received against an existing item merges into that lot.
async def test_freight_on_merged_lot_not_inflated_by_later_receipt(client, session, auth):
    lot = await _item(client, auth, _OPENING, qty=10)  # 10 @ 10 = 100
    first = await _doc(client, auth, "purchase_order",
                       [{"item_id": lot, "name": "Lot", "quantity": 5, "unit_price": 14.0}], shipping=7.0)
    r = await _receive(client, auth, first, {"po_line_index": 0, "item_id": lot, "name": "Lot", "quantity_received": 5})
    assert r.status_code == 200, r.text
    await _finalize(client, auth, first)
    assert round(float((await _state(session, auth, lot))["cost_total"]), 2) == 177.0
    await assert_settled(client, session, auth)
    # A second, freight-free purchase of 5 @ 16 joins the same lot: 177 + 80 = 257.
    second = await _doc(client, auth, "purchase_order", [{"item_id": lot, "name": "Lot", "quantity": 5,
                                                          "unit_price": 16.0}])
    r = await _receive(client, auth, second, {"po_line_index": 0, "item_id": lot, "name": "Lot",
                                              "quantity_received": 5})
    assert r.status_code == 200, r.text
    assert round(float((await _state(session, auth, lot))["cost_total"]), 2) == 257.0
    await assert_settled(client, session, auth)


# FX: the lots carry what the bill booked, and a full return leaves every leg clean.
# A partial return at a rate this far from 1 moves AP by the base value of what it takes
# off the bill, which a document-currency balance can only show to its own rounding unit.
@pytest.mark.parametrize("currency, rate", [("JPY", 0.0067), ("EUR", 36.123457)])
@_ORDERS
async def test_fx_full_return_end_state(client, session, auth, from_order, currency, rate):
    doc, [a, b] = await _received(client, session, auth, [_goods(13.37, 7), _goods(4.99, 11)], from_order=from_order,
                                  discount=3.0, discount_type="percentage", shipping=9.99,
                                  currency=currency, conversion_rate=rate, **_VAT)
    await _inv(client, session, auth, doc, rate=rate, step="recv")
    for lot, q in ((a, 2), (b, 5), (a, 5), (b, 6)):
        assert (await _return(client, auth, doc, lot, q)).status_code == 200
    st = await _state(session, auth, doc)
    ship = 10.0 if currency == "JPY" else 9.99  # yen has no minor unit
    assert (st["status"], st["amount_outstanding"]) == ("returned", ship)
    books = await _b(session, auth, "1130-P", "1130-FRT", "1150", "2110", "6970")
    assert books == {"1130-P": 0.0, "1130-FRT": 0.0, "1150": 0.0, "2110": round(-ship * rate, 2),
                     "6970": round(ship * rate, 2)}, books


# Same three single-unit receipts, everything returned: no residue may survive.
@pytest.mark.parametrize("discount, shipping", [(0.10, 0.10), (0.10, 0.0), (0.0, 0.10)],
                         ids=["both", "discount_only", "shipping_only"])
@_ORDERS
async def test_thirds_full_return_end_state(client, session, auth, from_order, discount, shipping):
    doc = await _doc(client, auth, "purchase_order" if from_order else "bill", [_goods(1.0, 3)],
                     discount=discount, shipping=shipping)
    if not from_order:
        await _finalize(client, auth, doc)
    li = (await _state(session, auth, doc))["line_items"][0]
    for _ in range(3):
        r = await _receive(client, auth, doc, {"po_line_index": 0, "sku": li["sku"], "name": li["name"],
                                               "quantity_received": 1})
        assert r.status_code == 200, r.text
    if from_order:
        await _finalize(client, auth, doc)
    for x in (await _state(session, auth, doc))["received_item_ids"]:
        assert (await _return(client, auth, doc, x, 1)).status_code == 200
    books = await _b(session, auth, "1130-P", "1130-FRT", "2110", "6970", "4300", "5100")
    assert books == {"1130-P": 0.0, "1130-FRT": 0.0, "2110": -shipping, "6970": shipping,
                     "4300": 0.0, "5100": 0.0}, books
