# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Goods sent back to the supplier take their share of everything the bill charged for them.

A return credits the bill for the goods at what the bill charged after its discount, plus
the tax the bill booked on them, so once every good has gone back the bill owes nothing for
them and accounts payable and input tax hold nothing for it. Freight the goods carried, the
bill's shipping among it, is a cost of goods that are gone: it is expensed, and the bill still
owes it.
"""
from __future__ import annotations

import uuid

import pytest

from stock_books import assert_settled
from test_cost_restatement import _state
from test_money_stock_and_contact_invariants import _account_net
from test_receipt_accounting import _doc, _finalize, _receive, _return

_VAT = {"doc_taxes": [{"code": "VAT", "rate": 7.0, "order": 1, "is_compound": False, "label": "VAT"}]}


async def _books(session, auth, *codes: str) -> dict[str, float]:
    return {c: round(await _account_net(session, auth["company_id"], c), 2) for c in codes}


async def _bill(client, session, auth, lines: list[dict], *, from_order: bool = False, **extra):
    doc = await _doc(client, auth, "purchase_order" if from_order else "bill", lines, **extra)
    if not from_order:
        await _finalize(client, auth, doc)
    st = await _state(session, auth, doc)
    goods = [{"po_line_index": i, "sku": li["sku"], "name": li["name"], "quantity_received": li["quantity"]}
             for i, (li, asked) in enumerate(zip(st["line_items"], lines)) if not asked.get("entity_id")]
    r = await _receive(client, auth, doc, *goods)
    assert r.status_code == 200, r.text
    if from_order:
        await _finalize(client, auth, doc)
    return doc, (await _state(session, auth, doc))["received_item_ids"]


def _goods(price: float = 12.0, qty: float = 10, **line) -> dict:
    return {"sku": f"RC-{uuid.uuid4().hex[:6]}", "name": "Beads", "quantity": qty, "unit_price": price, **line}


async def _outstanding(session, auth, doc) -> float:
    return (await _state(session, auth, doc))["amount_outstanding"]


@pytest.mark.parametrize("from_order", [False, True], ids=["bill", "order"])
async def test_a_full_return_takes_back_the_tax_the_bill_booked(client, session, auth, from_order):
    doc, [parcel] = await _bill(client, session, auth, [_goods()], from_order=from_order, **_VAT)
    assert await _books(session, auth, "1150", "2110") == {"1150": 8.4, "2110": -128.4}
    for q in (3, 3, 4):
        assert (await _return(client, auth, doc, parcel, q)).status_code == 200
    st = await _state(session, auth, doc)
    assert (st["status"], st["amount_outstanding"]) == ("returned", 0.0)
    assert await _books(session, auth, "1150", "2110", "1130-P") == {"1150": 0.0, "2110": 0.0, "1130-P": 0.0}
    await assert_settled(client, session, auth)


async def test_a_part_return_takes_back_its_share_of_the_tax(client, session, auth):
    doc, [parcel] = await _bill(client, session, auth, [_goods()], **_VAT)
    assert (await _return(client, auth, doc, parcel, 3)).status_code == 200
    # 3 of 10 at 12.00 is 36.00, and 7% of it is 2.52 of the 8.40 booked.
    assert await _outstanding(session, auth, doc) == 89.88
    assert await _books(session, auth, "1150", "2110") == {"1150": 5.88, "2110": -89.88}


async def test_each_line_takes_back_the_tax_on_that_line(client, session, auth):
    taxed = _goods(10.0, 10, taxes=[{"code": "VAT", "rate": 10.0, "order": 1, "is_compound": False, "label": "VAT"}])
    doc, parcels = await _bill(client, session, auth, [taxed, _goods(10.0, 10)])
    assert (await _state(session, auth, doc))["total"] == 210.0
    assert (await _return(client, auth, doc, parcels[0], 10)).status_code == 200
    assert await _outstanding(session, auth, doc) == 100.0
    assert await _books(session, auth, "1150", "2110") == {"1150": 0.0, "2110": -100.0}
    assert (await _return(client, auth, doc, parcels[1], 10)).status_code == 200
    assert await _outstanding(session, auth, doc) == 0.0
    assert await _books(session, auth, "1150", "2110") == {"1150": 0.0, "2110": 0.0}


async def test_voiding_a_returned_bill_with_tax_leaves_nothing_booked(client, session, auth):
    doc, [parcel] = await _bill(client, session, auth, [_goods()], **_VAT)
    assert (await _return(client, auth, doc, parcel, 10)).status_code == 200
    for action in ("void", "unvoid", "void"):
        r = await client.post(f"/docs/{doc}/{action}", headers=auth["headers"], json={})
        assert r.status_code == 200, (action, r.text)
        assert await _books(session, auth, "1150", "2110", "1130-P") == {
            "1150": 0.0, "2110": 0.0, "1130-P": 0.0}, action
        await assert_settled(client, session, auth)


@pytest.mark.parametrize("from_order", [False, True], ids=["bill", "order"])
async def test_a_return_credits_what_the_bill_charged_after_its_discount(client, session, auth, from_order):
    doc, [parcel] = await _bill(client, session, auth, [_goods()], from_order=from_order,
                                discount=10.0, discount_type="percentage")
    assert (await _return(client, auth, doc, parcel, 5)).status_code == 200
    assert await _outstanding(session, auth, doc) == 54.0
    assert await _books(session, auth, "2110") == {"2110": -54.0}
    assert (await _return(client, auth, doc, parcel, 5)).status_code == 200
    st = await _state(session, auth, doc)
    assert (st["status"], st["amount_outstanding"]) == ("returned", 0.0)
    # The discount never reaches profit and loss: stock gains and shrinkage stay untouched.
    assert await _books(session, auth, "2110", "1130-P", "4300", "6970") == {
        "2110": 0.0, "1130-P": 0.0, "4300": 0.0, "6970": 0.0}


async def _freight_item(client, auth) -> str:
    r = await client.post("/items", headers=auth["headers"], json={
        "status": "available", "sku": f"FRT-{uuid.uuid4().hex[:6]}", "name": "Freight", "quantity": 0,
        "sell_by": "piece", "inventory_type": "freight", "landed_cost_kind": "freight"})
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def test_freight_on_goods_sent_back_is_expensed_and_still_owed(client, session, auth):
    freight = await _freight_item(client, auth)
    doc, [parcel] = await _bill(client, session, auth, [
        _goods(15.0, 2), {"entity_id": freight, "sku": "FRT", "name": "Freight", "quantity": 1, "unit_price": 5.0}])
    assert await _books(session, auth, "1130-FRT") == {"1130-FRT": 0.0}
    assert (await _return(client, auth, doc, parcel, 2)).status_code == 200
    st = await _state(session, auth, doc)
    assert (st["status"], st["amount_outstanding"]) == ("returned", 5.0)
    assert await _books(session, auth, "1130-FRT", "1130-P", "2110", "6970") == {
        "1130-FRT": 0.0, "1130-P": 0.0, "2110": -5.0, "6970": 5.0}
    await assert_settled(client, session, auth)
    for action in ("void", "unvoid", "void"):
        r = await client.post(f"/docs/{doc}/{action}", headers=auth["headers"], json={})
        assert r.status_code == 200, (action, r.text)
        want = ({"1130-FRT": 0.0, "1130-P": 0.0, "2110": 0.0, "6970": 0.0} if action == "void"
                else {"1130-FRT": 0.0, "1130-P": 0.0, "2110": -5.0, "6970": 5.0})
        assert await _books(session, auth, *want) == want, action
        await assert_settled(client, session, auth)


async def test_shipping_on_a_bill_whose_goods_all_went_back_is_expensed_and_still_owed(client, session, auth):
    doc, [parcel] = await _bill(client, session, auth, [_goods()], shipping=5.0)
    # The goods carry the shipping, so nothing waits on the freight clearing account.
    assert await _books(session, auth, "1130-FRT") == {"1130-FRT": 0.0}
    for q in (4, 6):
        assert (await _return(client, auth, doc, parcel, q)).status_code == 200
    st = await _state(session, auth, doc)
    assert (st["status"], st["amount_outstanding"]) == ("returned", 5.0)
    assert await _books(session, auth, "1130-FRT", "2110", "6970") == {"1130-FRT": 0.0, "2110": -5.0, "6970": 5.0}
    for action in ("void", "unvoid"):
        r = await client.post(f"/docs/{doc}/{action}", headers=auth["headers"], json={})
        assert r.status_code == 200, (action, r.text)
        want = ({"1130-FRT": 0.0, "2110": 0.0, "6970": 0.0} if action == "void"
                else {"1130-FRT": 0.0, "2110": -5.0, "6970": 5.0})
        assert await _books(session, auth, *want) == want, action
