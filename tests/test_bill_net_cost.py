# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Goods are carried at what the bill charged for them.

A lot received against a bill carries its line's share of the bill's discount, shared
over the lines as the bill's entry books it, and its share of the shipping the bill
charged, shared over the goods received by value as freight lines are. Goods an order
adds to a lot already on hand bring their share into that lot. Receiving before or after
finalizing ends in the same lot costs and the same books, the freight clearing account
holds nothing once every good is in, and the cost carries through a sale, a return and
a void.
"""
from __future__ import annotations

import uuid

import pytest

from stock_books import assert_settled
from test_cost_restatement import _item, _state
from test_helpers import sell_item
from test_landed_cost_by_line import _freight_line
from test_money_stock_and_contact_invariants import _account_net
from test_receipt_accounting import _OPENING, _doc, _finalize, _receive, _return

_ORDERS = pytest.mark.parametrize("from_order", [False, True], ids=["finalized_first", "received_first"])


async def _books(session, auth, *codes: str) -> dict[str, float]:
    return {c: round(await _account_net(session, auth["company_id"], c), 2) for c in codes}


def _goods(price: float, qty: float) -> dict:
    return {"sku": f"NC-{uuid.uuid4().hex[:6]}", "name": "Beads", "quantity": qty, "unit_price": price}


async def _received(client, session, auth, lines: list[dict], *, from_order: bool, **extra) -> tuple[str, list[str]]:
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


async def _cost(session, auth, lot: str) -> float:
    return round(float((await _state(session, auth, lot))["cost_total"]), 2)


@_ORDERS
async def test_a_lot_carries_its_share_of_the_bill_discount(client, session, auth, from_order):
    doc, lots = await _received(client, session, auth, [_goods(12.0, 10), _goods(4.0, 5)], from_order=from_order,
                                discount=10.0, discount_type="percentage")
    # 120 and 20 less 10%: 108 and 18.
    assert [await _cost(session, auth, lot) for lot in lots] == [108.0, 18.0]
    assert await _books(session, auth, "1130-P", "2110") == {"1130-P": 126.0, "2110": -126.0}
    await assert_settled(client, session, auth)


@_ORDERS
async def test_goods_bought_at_a_discount_are_sold_returned_and_voided_at_their_net_cost(
        client, session, auth, from_order):
    doc, [sold, kept] = await _received(client, session, auth, [_goods(12.0, 10), _goods(4.0, 5)],
                                        from_order=from_order, discount=10.0, discount_type="percentage")
    await sell_item(client, auth["headers"], sold)
    assert await _books(session, auth, "5100", "1130-P") == {"5100": 108.0, "1130-P": 18.0}
    await assert_settled(client, session, auth)
    assert (await _return(client, auth, doc, kept, 5)).status_code == 200
    assert await _books(session, auth, "1130-P", "2110", "4300", "6970") == {
        "1130-P": 0.0, "2110": -108.0, "4300": 0.0, "6970": 0.0}
    await assert_settled(client, session, auth)
    for action in ("void", "unvoid"):
        r = await client.post(f"/docs/{doc}/{action}", headers=auth["headers"], json={})
        if r.status_code == 200:
            await assert_settled(client, session, auth)


@_ORDERS
async def test_bill_shipping_is_carried_by_the_goods_received(client, session, auth, from_order):
    doc, lots = await _received(client, session, auth, [_goods(12.0, 10), _goods(4.0, 10)],
                                from_order=from_order, shipping=8.0)
    # 8.00 shared by value over 120 and 40: 6.00 and 2.00.
    assert [await _cost(session, auth, lot) for lot in lots] == [126.0, 42.0]
    assert await _books(session, auth, "1130-P", "1130-FRT", "2110") == {
        "1130-P": 168.0, "1130-FRT": 0.0, "2110": -168.0}
    await assert_settled(client, session, auth)
    for action in ("void", "unvoid"):
        r = await client.post(f"/docs/{doc}/{action}", headers=auth["headers"], json={})
        if r.status_code == 200:
            await assert_settled(client, session, auth)


@_ORDERS
async def test_discount_and_shipping_together(client, session, auth, from_order):
    doc, [lot] = await _received(client, session, auth, [_goods(10.0, 10)], from_order=from_order,
                                 discount=10.0, discount_type="percentage", shipping=5.0)
    assert await _cost(session, auth, lot) == 95.0
    assert await _books(session, auth, "1130-P", "1130-FRT", "2110") == {
        "1130-P": 95.0, "1130-FRT": 0.0, "2110": -95.0}
    await assert_settled(client, session, auth)


async def _order_received(client, session, auth, lines: list[dict], **extra) -> tuple[str, list[str]]:
    doc = await _doc(client, auth, "purchase_order", lines, **extra)
    st = await _state(session, auth, doc)
    r = await _receive(client, auth, doc, *(
        {"po_line_index": i, "sku": li["sku"], "name": li["name"], "quantity_received": li["quantity"]}
        for i, li in enumerate(st["line_items"]) if li["sku"] != "FRT"))
    assert r.status_code == 200, r.text
    return doc, (await _state(session, auth, doc))["received_item_ids"]


async def test_goods_sold_before_the_order_is_billed_take_their_shipping_to_cost_of_goods_sold(
        client, session, auth):
    doc, [sold, kept] = await _order_received(client, session, auth, [_goods(12.0, 10), _goods(4.0, 10)],
                                              shipping=8.0)
    await sell_item(client, auth["headers"], sold)
    await _finalize(client, auth, doc)
    assert await _cost(session, auth, kept) == 42.0
    assert await _books(session, auth, "5100", "1130-P", "1130-FRT", "2110") == {
        "5100": 126.0, "1130-P": 42.0, "1130-FRT": 0.0, "2110": -168.0}
    await assert_settled(client, session, auth)


async def test_goods_sent_back_before_the_order_is_billed_take_their_shipping_to_shrinkage(
        client, session, auth):
    doc, [back, kept] = await _order_received(client, session, auth, [_goods(12.0, 10), _goods(4.0, 10)],
                                              shipping=8.0)
    assert (await _return(client, auth, doc, back, 10)).status_code == 200
    await _finalize(client, auth, doc)
    assert await _cost(session, auth, kept) == 42.0
    assert await _books(session, auth, "6970", "1130-P", "1130-FRT") == {
        "6970": 6.0, "1130-P": 42.0, "1130-FRT": 0.0}
    await assert_settled(client, session, auth)


async def test_a_freight_line_on_goods_received_before_the_order_is_billed_is_carried_by_them(
        client, session, auth):
    doc, [lot] = await _order_received(client, session, auth, [
        _goods(15.0, 2), await _freight_line(client, auth, 10.0)])
    await _finalize(client, auth, doc)
    assert await _cost(session, auth, lot) == 40.0
    assert await _books(session, auth, "1130-P", "1130-FRT", "2110") == {
        "1130-P": 40.0, "1130-FRT": 0.0, "2110": -40.0}
    await assert_settled(client, session, auth)


async def _into_lot(client, session, auth, lines: list[dict], *receipts: list[dict], **extra) -> tuple[str, str]:
    """A purchase order whose first line is goods of a lot already on hand (opening stock of
    ten at 100.00), received as ``receipts`` before it is billed. A receipt entry names its
    line, and the first line's goods go into that lot."""
    lot = await _item(client, auth, _OPENING, qty=10)
    doc = await _doc(client, auth, "purchase_order", [{**lines[0], "item_id": lot, "name": "Lot"}, *lines[1:]], **extra)
    st = await _state(session, auth, doc)
    for entries in receipts:
        r = await _receive(client, auth, doc, *(
            {"po_line_index": i, "quantity_received": qty, "name": st["line_items"][i]["name"],
             **({"item_id": lot} if i == 0 else {"sku": st["line_items"][i]["sku"]})}
            for i, qty in entries))
        assert r.status_code == 200, r.text
    return doc, lot


async def test_shipping_on_goods_added_to_a_lot_on_hand_joins_that_lot(client, session, auth):
    doc, lot = await _into_lot(client, session, auth, [{"quantity": 5, "unit_price": 14.0}],
                               [(0, 5)], shipping=7.0)
    await _finalize(client, auth, doc)
    assert await _cost(session, auth, lot) == 177.0
    assert await _books(session, auth, "1130-OB", "1130-FRT", "2110") == {
        "1130-OB": 177.0, "1130-FRT": 0.0, "2110": -77.0}
    await assert_settled(client, session, auth)


async def test_shipping_on_goods_received_in_parts_into_a_lot_on_hand_and_as_a_new_lot(client, session, auth):
    doc, lot = await _into_lot(client, session, auth, [{"quantity": 10, "unit_price": 10.0}, _goods(20.0, 5)],
                               [(0, 4), (1, 5)], [(0, 6)], shipping=20.0)
    await _finalize(client, auth, doc)
    [made] = (await _state(session, auth, doc))["received_item_ids"]
    # 20.00 shared by value over 100 and 100: 10.00 each.
    assert (await _cost(session, auth, lot), await _cost(session, auth, made)) == (210.0, 110.0)
    assert await _books(session, auth, "1130-OB", "1130-P", "1130-FRT", "2110") == {
        "1130-OB": 210.0, "1130-P": 110.0, "1130-FRT": 0.0, "2110": -220.0}
    await assert_settled(client, session, auth)


async def test_shipping_on_goods_added_to_a_lot_sold_before_the_order_is_billed_goes_to_cost_of_goods_sold(
        client, session, auth):
    doc, lot = await _into_lot(client, session, auth, [{"quantity": 5, "unit_price": 14.0}],
                               [(0, 5)], shipping=7.0)
    await sell_item(client, auth["headers"], lot)
    await _finalize(client, auth, doc)
    assert await _books(session, auth, "5100", "1130-OB", "1130-FRT", "2110") == {
        "5100": 177.0, "1130-OB": 0.0, "1130-FRT": 0.0, "2110": -77.0}
    await assert_settled(client, session, auth)


async def test_shipping_on_goods_added_to_a_lot_and_sent_back_before_the_order_is_billed_goes_to_shrinkage(
        client, session, auth):
    doc, lot = await _into_lot(client, session, auth, [{"quantity": 5, "unit_price": 14.0}],
                               [(0, 5)], shipping=7.0)
    assert (await _return(client, auth, doc, lot, 2)).status_code == 200
    await _finalize(client, auth, doc)
    # 7.00 over five units: the two sent back take 2.80, the three kept 4.20.
    assert await _cost(session, auth, lot) == 146.2
    assert await _books(session, auth, "6970", "1130-OB", "1130-FRT") == {
        "6970": 2.8, "1130-OB": 146.2, "1130-FRT": 0.0}
    await assert_settled(client, session, auth)


async def test_shipping_on_goods_added_to_a_lot_and_sent_back_after_the_order_is_billed_lands_the_same(
        client, session, auth):
    doc, lot = await _into_lot(client, session, auth, [{"quantity": 5, "unit_price": 14.0}],
                               [(0, 5)], shipping=7.0)
    await _finalize(client, auth, doc)
    assert (await _return(client, auth, doc, lot, 2)).status_code == 200
    # The freight is the five units', 1.40 each: the two sent back take 2.80 to shrinkage
    # while the supplier credits their 28.00, and the lot keeps 4.20 of it.
    assert await _cost(session, auth, lot) == 146.2
    assert await _books(session, auth, "6970", "1130-OB", "1130-FRT") == {
        "6970": 2.8, "1130-OB": 146.2, "1130-FRT": 0.0}
    await assert_settled(client, session, auth)


async def test_a_freight_line_on_goods_added_to_a_lot_on_hand_joins_that_lot(client, session, auth):
    doc, lot = await _into_lot(client, session, auth, [{"quantity": 2, "unit_price": 15.0},
                                                       await _freight_line(client, auth, 10.0)],
                               [(0, 2)])
    await _finalize(client, auth, doc)
    assert await _cost(session, auth, lot) == 140.0
    assert await _books(session, auth, "1130-OB", "1130-FRT", "2110") == {
        "1130-OB": 140.0, "1130-FRT": 0.0, "2110": -40.0}
    await assert_settled(client, session, auth)
