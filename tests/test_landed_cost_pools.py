# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Landed cost travels with the stock that bore it, to the cent.

Freight and other landed costs are fixed amounts capitalised against the stock they were
paid for. Each lot carries them as absolute pools: receiving more goods into a lot leaves the
pools untouched, removing units (return, sale, split) takes a quantity share of each pool, and
undoing the receipt that brought a pool removes what is left of it. The books must always hold
the stock value to the cent, nothing may be stranded on freight clearing, and a foreign
currency rounding unit must land on goods, never on input VAT or freight.
"""
from __future__ import annotations

import uuid

import pytest

from stock_books import assert_settled
from test_cost_restatement import _item, _state
from test_money_stock_and_contact_invariants import _account_net
from test_receipt_accounting import _OPENING, _doc, _finalize, _receive, _return

_VAT = {"doc_taxes": [{"code": "VAT", "rate": 7.0, "order": 1, "is_compound": False, "label": "VAT"}]}
_ORDERS = pytest.mark.parametrize("from_order", [False, True], ids=["bill_first", "order_first"])


def _goods(price: float, qty: float) -> dict:
    return {"sku": f"LCP-{uuid.uuid4().hex[:6]}", "name": "Beads", "quantity": qty, "unit_price": price}


async def _b(session, auth, *codes: str) -> dict[str, float]:
    return {c: round(await _account_net(session, auth["company_id"], c), 2) for c in codes}


async def _post(client, auth, doc, action, body=None):
    return await client.post(f"/docs/{doc}/{action}", headers=auth["headers"], json=body or {})


async def _inv(client, session, auth, doc, rate: float = 1.0, step: str = "", others: float = 0.0,
               settle: bool = True) -> dict:
    st = await _state(session, auth, doc)
    out = float(st.get("amount_outstanding") or 0) if st.get("status") not in ("draft", "void") else 0.0
    if st.get("doc_type") == "purchase_order":
        out = 0.0
    ap = await _account_net(session, auth["company_id"], "2110")
    assert round(ap, 2) == round(-out * rate - others, 2), (step, "2110", ap, "outstanding", out)
    if settle:  # a finalized bill parks unreceived goods on 1130-P by design
        await assert_settled(client, session, auth)
    return st


async def _cost(session, auth, lot) -> float:
    return round(float((await _state(session, auth, lot)).get("cost_total") or 0), 2)


async def _received(client, session, auth, lines, *, from_order: bool, **extra):
    doc = await _doc(client, auth, "purchase_order" if from_order else "bill", lines, **extra)
    if not from_order:
        await _finalize(client, auth, doc)
    st = await _state(session, auth, doc)
    r = await _receive(client, auth, doc, *(
        {"po_line_index": i, "sku": li["sku"], "name": li["name"], "quantity_received": li["quantity"]}
        for i, li in enumerate(st["line_items"])))
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


async def _po_into(client, auth, lot, qty, price, **extra) -> str:
    doc = await _doc(client, auth, "purchase_order", [{"item_id": lot, "name": "Lot", "quantity": qty,
                                                       "unit_price": price}], **extra)
    r = await _receive(client, auth, doc, {"po_line_index": 0, "item_id": lot, "name": "Lot",
                                           "quantity_received": qty})
    assert r.status_code == 200, r.text
    await _finalize(client, auth, doc)
    return doc


async def _on_hand(session, auth, lot) -> list[str]:
    st = await _state(session, auth, lot)
    out = []
    for x in [lot, *(st.get("children") or [])]:
        s = await _state(session, auth, x)
        if s.get("status") not in ("sold", "disposed", "archived") and float(s.get("quantity") or 0) > 0:
            out.append(x)
    return out


# ── FX rounding lands on goods ──────────────────────────────────────────────

# Shipping is the largest debit and takes the FX rounding unit; the receipts must still draw
# every cent the bill parked on freight clearing.
@pytest.mark.parametrize("currency, rate, price, qty, ship", [
    ("EUR", 36.123457, 1.0, 10, 50.0),
    ("JPY", 0.0067, 111.1, 10, 4999.0),
    ("KWD", 3.2571, 0.1, 10, 5.0),
], ids=["EUR", "JPY", "KWD"])
@_ORDERS
async def test_fx_plug_on_freight_leaves_nothing_on_clearing(client, session, auth, from_order, currency, rate,
                                                            price, qty, ship):
    doc, [lot] = await _received(client, session, auth, [_goods(price, qty)], from_order=from_order,
                                 shipping=ship, currency=currency, conversion_rate=rate)
    await _inv(client, session, auth, doc, rate=rate, step="recv")
    assert await _b(session, auth, "1130-FRT") == {"1130-FRT": 0.0}
    assert (await _return(client, auth, doc, lot, qty)).status_code == 200
    books = await _b(session, auth, "1130-P", "1130-FRT")
    assert books == {"1130-P": 0.0, "1130-FRT": 0.0}, books


# Input VAT is the largest debit (many small lines). The FX rounding unit must not be parked
# on recoverable VAT, and a full return clears VAT to zero.
@_ORDERS
async def test_fx_plug_never_lands_on_input_vat(client, session, auth, from_order):
    rate = 36.123457
    lines = [_goods(1.0, 1) for _ in range(20)]
    doc, lots = await _received(client, session, auth, lines, from_order=from_order,
                                currency="EUR", conversion_rate=rate, **_VAT)
    await _inv(client, session, auth, doc, rate=rate, step="recv")
    vat = await _b(session, auth, "1150")
    assert vat == {"1150": round(round(1.40 * rate, 2), 2)}, ("VAT carries FX rounding of goods", vat)
    for lot in lots:
        assert (await _return(client, auth, doc, lot, 1)).status_code == 200
    books = await _b(session, auth, "1130-P", "1130-FRT", "1150", "2110")
    assert books == {"1130-P": 0.0, "1130-FRT": 0.0, "1150": 0.0, "2110": 0.0}, books


# Zero-priced line beside a priced one, under a document discount, in FX.
@_ORDERS
async def test_fx_zero_priced_line_and_discount(client, session, auth, from_order):
    rate = 36.123457
    doc, [a, b] = await _received(client, session, auth, [_goods(13.37, 7), _goods(0.0, 5)], from_order=from_order,
                                  discount=1.11, shipping=2.22, currency="EUR", conversion_rate=rate, **_VAT)
    await _inv(client, session, auth, doc, rate=rate, step="recv")
    assert await _cost(session, auth, b) == 0.0
    for lot, q in ((a, 3), (b, 5), (a, 4)):
        assert (await _return(client, auth, doc, lot, q)).status_code == 200
    books = await _b(session, auth, "1130-P", "1130-FRT", "1150")
    assert books == {"1130-P": 0.0, "1130-FRT": 0.0, "1150": 0.0}, books


# ── Cumulative share ──────────────────────────────────────────────────────────

# Three lines, receipts in an odd order, two parcels of one line in a single receipt.
@_ORDERS
async def test_odd_order_receipts_carry_each_line_to_the_cent(client, session, auth, from_order):
    lines = [_goods(1.0, 3), _goods(1.0, 3), _goods(1.0, 3)]
    doc = await _doc(client, auth, "purchase_order" if from_order else "bill", lines,
                     discount=0.10, shipping=0.10)
    if not from_order:
        await _finalize(client, auth, doc)
    li = (await _state(session, auth, doc))["line_items"]

    def rec(i, q):
        return {"po_line_index": i, "sku": li[i]["sku"], "name": li[i]["name"], "quantity_received": q}

    for batch in ([rec(2, 1)], [rec(0, 1), rec(0, 1)], [rec(1, 3)], [rec(2, 2), rec(0, 1)]):
        r = await _receive(client, auth, doc, *batch)
        assert r.status_code == 200, r.text
    if from_order:
        await _finalize(client, auth, doc)
    st = await _inv(client, session, auth, doc, step="recv")
    lots = st["received_item_ids"]
    total = round(sum([await _cost(session, auth, x) for x in lots]), 2)
    assert total == 9.0, total
    assert await _b(session, auth, "1130-P", "1130-FRT") == {"1130-P": 9.0, "1130-FRT": 0.0}
    for x in lots:
        q = float((await _state(session, auth, x))["quantity"])
        r = await _return(client, auth, doc, x, q)
        assert r.status_code == 200, r.text
    books = await _b(session, auth, "1130-P", "1130-FRT", "2110", "6970")
    assert books == {"1130-P": 0.0, "1130-FRT": 0.0, "2110": -0.1, "6970": 0.1}, books


# Receipt, undo, receive again in thirds: the cumulative share starts over.
async def test_receipt_undo_rereceive(client, session, auth):
    doc = await _doc(client, auth, "bill", [_goods(1.0, 3)], discount=0.10, shipping=0.10)
    await _finalize(client, auth, doc)
    li = (await _state(session, auth, doc))["line_items"][0]
    rec = {"po_line_index": 0, "sku": li["sku"], "name": li["name"], "quantity_received": 1}
    for _ in range(2):
        assert (await _receive(client, auth, doc, rec)).status_code == 200
    r = await client.delete(f"/docs/{doc}/receive", headers=auth["headers"])
    assert r.status_code == 200, r.text
    await _inv(client, session, auth, doc, step="undone", settle=False)
    for _ in range(3):
        assert (await _receive(client, auth, doc, rec)).status_code == 200
    st = await _inv(client, session, auth, doc, step="re-received")
    assert await _b(session, auth, "1130-P", "1130-FRT") == {"1130-P": 3.0, "1130-FRT": 0.0}
    for x in st["received_item_ids"]:
        if (await _state(session, auth, x)).get("status") == "archived":
            continue
        assert (await _return(client, auth, doc, x, 1)).status_code == 200
    books = await _b(session, auth, "1130-P", "1130-FRT", "6970")
    assert books == {"1130-P": 0.0, "1130-FRT": 0.0, "6970": 0.1}, books


# Partial returns between receipts.
@_ORDERS
async def test_returns_between_receipts(client, session, auth, from_order):
    doc = await _doc(client, auth, "purchase_order" if from_order else "bill", [_goods(1.0, 3)],
                     discount=0.10, shipping=0.10)
    if not from_order:
        await _finalize(client, auth, doc)
    li = (await _state(session, auth, doc))["line_items"][0]
    rec = {"po_line_index": 0, "sku": li["sku"], "name": li["name"], "quantity_received": 1}
    assert (await _receive(client, auth, doc, rec)).status_code == 200
    if from_order:
        await _finalize(client, auth, doc)
    first = (await _state(session, auth, doc))["received_item_ids"][0]
    assert (await _return(client, auth, doc, first, 1)).status_code == 200
    await _inv(client, session, auth, doc, step="ret1", settle=False)
    for _ in range(2):
        r = await _receive(client, auth, doc, rec)
        assert r.status_code == 200, r.text
    await _inv(client, session, auth, doc, step="recv all")
    assert await _b(session, auth, "1130-FRT") == {"1130-FRT": 0.0}
    for x in (await _state(session, auth, doc))["received_item_ids"][1:]:
        assert (await _return(client, auth, doc, x, 1)).status_code == 200
    books = await _b(session, auth, "1130-P", "1130-FRT", "6970")
    assert books == {"1130-P": 0.0, "1130-FRT": 0.0, "6970": 0.1}, books


# ── Return freight: difference of rounded totals ──────────────────────────────

# Seven single-unit returns of a lot whose freight does not divide.
@_ORDERS
async def test_seven_returns_release_freight_exactly(client, session, auth, from_order):
    doc, [lot] = await _received(client, session, auth, [_goods(1.0, 7)], from_order=from_order, shipping=1.0)
    for i in range(7):
        assert (await _return(client, auth, doc, lot, 1)).status_code == 200
        await _inv(client, session, auth, doc, step=f"ret {i}")
    books = await _b(session, auth, "1130-P", "1130-FRT", "6970", "2110")
    assert books == {"1130-P": 0.0, "1130-FRT": 0.0, "6970": 1.0, "2110": -1.0}, books


# A lot split by the user carries its freight into the parts to the cent, once: the parts add
# back to what the lot cost, and the books still hold every part. What is left of the lot the
# bill received then goes back with its own share. (A split part is a lot of its own, which the
# bill did not receive, so it is not returned on the bill.)
@_ORDERS
async def test_split_then_return_the_rest(client, session, auth, from_order):
    doc, [lot] = await _received(client, session, auth, [_goods(1.0, 3)], from_order=from_order, shipping=1.0)
    parts = [lot]
    for _ in range(2):
        r = await client.post(f"/items/{lot}/split", headers=auth["headers"], json={"children": [{"quantity": 1}]})
        assert r.status_code == 200, r.text
        parts += [c["id"] for c in r.json()["children"]]
    await _inv(client, session, auth, doc, step="split")
    assert round(sum([await _cost(session, auth, x) for x in parts]), 2) == 4.0
    r = await _return(client, auth, doc, lot, 1)
    assert r.status_code == 200, r.text
    await _inv(client, session, auth, doc, step="returned", settle=False)
    await assert_settled(client, session, auth)
    held = round(sum([await _cost(session, auth, x) for x in parts[1:]]), 2)
    assert await _b(session, auth, "1130-P", "1130-FRT") == {"1130-P": held, "1130-FRT": 0.0}


# ── Lots holding more than one purchase ─────────────────────────────────────

# A purchase that merged into a lot carrying another bill's freight is undone: the lot must
# get its original freight back (the other bill's freight is still on the books).
async def test_merge_then_undo_restores_other_bills_freight(client, session, auth):
    lot = await _item(client, auth, _OPENING, qty=10)
    await _po_into(client, auth, lot, 5, 14.0, shipping=7.0)
    assert await _cost(session, auth, lot) == 177.0
    second = await _po_into(client, auth, lot, 5, 16.0)
    assert await _cost(session, auth, lot) == 257.0
    r = await client.delete(f"/docs/{second}/receive", headers=auth["headers"])
    assert r.status_code == 200, r.text
    assert await _cost(session, auth, lot) == 177.0, "undo kept the other bill's freight diluted"
    # The second bill stays finalized, so its 80.00 of goods park on 1130-P until received again.
    books = await _b(session, auth, "1130-OB", "1130-P", "1130-FRT")
    assert books == {"1130-OB": 177.0, "1130-P": 80.0, "1130-FRT": 0.0}, books


# Both purchases carry freight; part sold between them; returns against each.
async def test_merge_both_freight_sold_between_then_returned(client, session, auth):
    lot = await _item(client, auth, _OPENING, qty=10)
    first = await _po_into(client, auth, lot, 5, 14.0, shipping=7.0)
    await assert_settled(client, session, auth)
    await _sell(client, auth, lot, 3)
    await assert_settled(client, session, auth)
    held = (await _on_hand(session, auth, lot))[0]
    second = await _po_into(client, auth, held, 5, 16.0, shipping=3.0)
    await assert_settled(client, session, auth)
    third = await _po_into(client, auth, held, 4, 11.0, shipping=1.01)
    await assert_settled(client, session, auth)
    for doc, q in ((first, 2), (second, 5), (third, 3), (first, 3), (third, 1)):
        h = (await _on_hand(session, auth, held))[0]
        r = await _return(client, auth, doc, h, q)
        assert r.status_code == 200, r.text
        await assert_settled(client, session, auth)
    assert await _b(session, auth, "1130-FRT") == {"1130-FRT": 0.0}


# Sold to zero, then more joins it.
async def test_merge_into_lot_sold_to_zero(client, session, auth):
    lot = await _item(client, auth, _OPENING, qty=10)
    await _po_into(client, auth, lot, 5, 14.0, shipping=7.0)
    await _sell(client, auth, lot, 15)
    await assert_settled(client, session, auth)
    st = await _state(session, auth, lot)
    if float(st.get("quantity") or 0) == 0 and st.get("status") not in ("sold",):
        doc = await _po_into(client, auth, lot, 4, 10.0, shipping=2.0)
        await assert_settled(client, session, auth)
        assert await _cost(session, auth, lot) == 42.0
        assert (await _return(client, auth, doc, lot, 4)).status_code == 200
        await assert_settled(client, session, auth)


# Two lines of one order go into the same lot in one receipt, the lot carrying freight.
async def test_two_lines_into_one_lot_one_receipt(client, session, auth):
    lot = await _item(client, auth, _OPENING, qty=10)
    await _po_into(client, auth, lot, 5, 14.0, shipping=7.0)
    doc = await _doc(client, auth, "purchase_order", [
        {"item_id": lot, "name": "Lot", "quantity": 3, "unit_price": 16.0},
        {"item_id": lot, "name": "Lot", "quantity": 2, "unit_price": 12.0}], shipping=1.0)
    r = await _receive(client, auth, doc,
                       {"po_line_index": 0, "item_id": lot, "name": "Lot", "quantity_received": 3},
                       {"po_line_index": 1, "item_id": lot, "name": "Lot", "quantity_received": 2})
    assert r.status_code == 200, r.text
    # 177 + 48 + 24 = 249 before this order's shipping
    assert await _cost(session, auth, lot) == 249.0
    await _finalize(client, auth, doc)
    assert await _cost(session, auth, lot) == 250.0
    await assert_settled(client, session, auth)
    assert await _b(session, auth, "1130-FRT") == {"1130-FRT": 0.0}


# Split after the merge, then return part of one purchase from what the lot kept.
async def test_merge_split_then_return(client, session, auth):
    lot = await _item(client, auth, _OPENING, qty=10)
    first = await _po_into(client, auth, lot, 5, 14.0, shipping=7.0)
    await _po_into(client, auth, lot, 6, 13.0, shipping=2.0)
    before = await _cost(session, auth, lot)
    r = await client.post(f"/items/{lot}/split", headers=auth["headers"], json={"children": [{"quantity": 7}]})
    assert r.status_code == 200, r.text
    await assert_settled(client, session, auth)
    child = [x for x in await _on_hand(session, auth, lot) if x != lot][0]
    assert round(await _cost(session, auth, lot) + await _cost(session, auth, child), 2) == before
    r = await _return(client, auth, first, lot, 5)
    assert r.status_code == 200, r.text
    await assert_settled(client, session, auth)
    assert await _b(session, auth, "1130-FRT") == {"1130-FRT": 0.0}


# ── Other ways cost leaves a lot ─────────────────────────────────────────────

# A lot transformed at its own cost carries its freight once: as the child's landed pool beside
# the goods, never as goods and pool both.
async def test_transform_carries_freight_once(client, session, auth):
    doc, [lot] = await _received(client, session, auth, [_goods(1.0, 10)], from_order=False, shipping=5.0)
    r = await client.post(f"/items/{lot}/transform", headers=auth["headers"], json={
        "child_sku": f"LCP-{uuid.uuid4().hex[:6]}", "child_category": "Processed", "child_sell_by": "piece",
        "child_quantity": 10})
    assert r.status_code == 200, r.text
    child = (await _state(session, auth, lot))["transformed_into"]
    child = child[0] if isinstance(child, list) else child
    assert await _cost(session, auth, child) == 15.0
    await assert_settled(client, session, auth)


# A bill converted from a purchase order nets what the order's receipts posted, so the order's
# money must not change between its receipt and the bill: once any of it is received the order
# refuses edits and cannot go back to draft, whether part or all of it came in.
@pytest.mark.parametrize("field, new", [("shipping", 9.0), ("discount", 1.0)])
@pytest.mark.parametrize("received", [3, 5], ids=["part_received", "received"])
async def test_order_money_is_fixed_once_received(client, session, auth, field, new, received):
    doc = await _doc(client, auth, "purchase_order", [_goods(14.0, 5)], shipping=7.0)
    st = await _state(session, auth, doc)
    li = st["line_items"][0]
    r = await _receive(client, auth, doc, {"po_line_index": 0, "sku": li["sku"], "name": li["name"],
                                           "quantity_received": received})
    assert r.status_code == 200, r.text
    r = await client.patch(f"/docs/{doc}", headers=auth["headers"], json={
        "fields_changed": {field: {"old": st.get(field), "new": new}}})
    assert r.status_code == 409 and r.json()["detail"]["message_key"] == "docs.edit_locked", r.text
    assert (await _post(client, auth, doc, "revert-to-draft")).status_code == 409
    if received == 5:
        await _finalize(client, auth, doc)
        await assert_settled(client, session, auth)
        assert await _b(session, auth, "1130-P", "1130-FRT") == {"1130-P": 77.0, "1130-FRT": 0.0}
