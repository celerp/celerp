# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Goods sent back to the supplier reduce what the bill owes by what the return took off
accounts payable, so the bill, the AP aging and the ledger show the same balance."""
from __future__ import annotations

import uuid

import pytest

from stock_books import assert_settled
from test_cost_restatement import _set_cost, _state
from test_lot_value_boundary import GAIN, SHRINKAGE, _role
from test_money_stock_and_contact_invariants import _account_net
from test_receipt_accounting import _doc, _finalize, _receive, _return
from test_receive_selected_lines import _stamp_line_ids


async def _aged(client, auth) -> float:
    r = await client.get("/reports/ap-aging", headers=auth["headers"])
    assert r.status_code == 200, r.text
    return sum(line["total"] for line in r.json()["lines"])


async def _received_bill(client, session, auth, *, finalize_first: bool) -> tuple[str, str]:
    doc_type = "bill" if finalize_first else "purchase_order"
    doc = await _doc(client, auth, doc_type, [
        {"sku": f"RTN-{uuid.uuid4().hex[:6]}", "name": "Beads", "quantity": 10, "unit_price": 14.0}])
    if finalize_first:
        await _finalize(client, auth, doc)
    sku = (await _state(session, auth, doc))["line_items"][0]["sku"]
    r = await _receive(client, auth, doc, {"po_line_index": 0, "sku": sku, "name": "Beads", "quantity_received": 10})
    assert r.status_code == 200, r.text
    [parcel_id] = (await _state(session, auth, doc))["received_item_ids"]
    return doc, parcel_id


@pytest.mark.parametrize("finalize_first", [True, False])
@pytest.mark.asyncio
async def test_a_return_reduces_what_the_bill_owes_by_the_payable_it_clears(client, session, auth, finalize_first):
    doc, parcel_id = await _received_bill(client, session, auth, finalize_first=finalize_first)
    r = await _return(client, auth, doc, parcel_id, 4)
    assert r.status_code == 200, r.text
    if not finalize_first:
        await _finalize(client, auth, doc)

    assert await _account_net(session, auth["company_id"], "2110") == -84.0
    bill = await _state(session, auth, doc)
    assert bill["total"] == 140.0
    assert bill["amount_outstanding"] == 84.0
    assert await _aged(client, auth) == 84.0

    # Paying what the bill now shows clears it, and the ledger with it.
    r = await client.post(f"/docs/{doc}/payment", headers=auth["headers"],
                          json={"amount": 84.0, "payment_date": "2026-03-02", "bank_account": "1111"})
    assert r.status_code == 200, r.text
    bill = await _state(session, auth, doc)
    assert (bill["amount_outstanding"], bill["status"]) == (0.0, "paid")
    assert await _account_net(session, auth["company_id"], "2110") == 0.0
    assert await _aged(client, auth) == 0.0


@pytest.mark.asyncio
async def test_a_payment_cannot_exceed_what_the_bill_owes_after_a_return(client, session, auth):
    doc, parcel_id = await _received_bill(client, session, auth, finalize_first=True)
    r = await _return(client, auth, doc, parcel_id, 4)
    assert r.status_code == 200, r.text
    r = await client.post(f"/docs/{doc}/payment", headers=auth["headers"],
                          json={"amount": 140.0, "payment_date": "2026-03-02", "bank_account": "1111"})
    assert r.status_code == 409, r.text
    assert "exceeds amount outstanding 84.0" in r.json()["detail"]
    assert await _account_net(session, auth["company_id"], "2110") == -84.0


@pytest.mark.asyncio
async def test_returning_everything_leaves_nothing_owed(client, session, auth):
    doc, parcel_id = await _received_bill(client, session, auth, finalize_first=True)
    r = await _return(client, auth, doc, parcel_id, 10)
    assert r.status_code == 200, r.text
    bill = await _state(session, auth, doc)
    assert (bill["status"], bill["amount_outstanding"]) == ("returned", 0.0)
    assert await _account_net(session, auth["company_id"], "2110") == 0.0
    assert await _aged(client, auth) == 0.0


async def _restated_bill(client, session, auth, *, from_order: bool, cost: float) -> tuple[str, str, str]:
    """A bill for 10 at 12 whose goods were received into one parcel, the parcel's cost
    then corrected to ``cost``. ``from_order``: received on a purchase order that then
    became the bill. -> (doc id, line id, parcel id)."""
    doc = await _doc(client, auth, "purchase_order" if from_order else "bill", [
        {"sku": f"RST-{uuid.uuid4().hex[:6]}", "name": "Beads", "quantity": 10, "unit_price": 12.0}])
    if not from_order:
        await _finalize(client, auth, doc)
    [line_id] = await _stamp_line_ids(session, auth, doc)
    sku = (await _state(session, auth, doc))["line_items"][0]["sku"]
    r = await _receive(client, auth, doc, {"po_line_index": 0, "sku": sku, "name": "Beads", "quantity_received": 10})
    assert r.status_code == 200, r.text
    if from_order:
        await _finalize(client, auth, doc)
    [parcel] = (await _state(session, auth, doc))["received_item_ids"]
    r = await _set_cost(client, auth, parcel, cost)
    assert r.status_code == 200, r.text
    return doc, line_id, parcel


async def _send_back(client, auth, doc: str, line_id: str, parcel: str, qty: float, by: str):
    if by == "line":
        return await client.post(f"/docs/{doc}/return-items", headers=auth["headers"],
                                 json={"lines": [{"line_id": line_id, "quantity_returned": qty}]})
    return await _return(client, auth, doc, parcel, qty)


async def _owes(client, session, auth, doc: str, amount: float) -> None:
    """The bill, the AP aging and accounts payable all show ``amount`` owed."""
    assert (await _state(session, auth, doc))["amount_outstanding"] == amount
    assert await _account_net(session, auth["company_id"], "2110") == -amount
    assert await _aged(client, auth) == amount


@pytest.mark.parametrize("by", ["lot", "line"])
@pytest.mark.parametrize("from_order", [False, True], ids=["bill", "order"])
@pytest.mark.parametrize("cost, role", [(300.0, GAIN), (50.0, SHRINKAGE)], ids=["raised", "lowered"])
async def test_returning_restated_goods_clears_the_bill_at_its_price(
        client, session, auth, from_order, by, cost, role):
    """Goods whose cost was corrected after they came in go back off accounts payable at what
    the bill charged for them. The correction leaves with them through the account it was
    booked to, so nothing stays owed, the stock accounts carry the stock, and the stock gain
    or shrinkage the correction booked is undone."""
    doc, line_id, parcel = await _restated_bill(client, session, auth, from_order=from_order, cost=cost)
    restated = await _account_net(session, auth["company_id"], await _role(session, auth, role))
    assert restated != 0.0

    r = await _send_back(client, auth, doc, line_id, parcel, 10, by)

    assert r.status_code == 200, r.text
    assert (await _state(session, auth, doc))["status"] == "returned"
    await _owes(client, session, auth, doc, 0.0)
    assert await _account_net(session, auth["company_id"], await _role(session, auth, role)) == 0.0
    await assert_settled(client, session, auth)


@pytest.mark.parametrize("from_order", [False, True], ids=["bill", "order"])
async def test_part_of_restated_goods_goes_back_at_the_bill_price(client, session, auth, from_order):
    """Each part sent back takes what the bill charged for it off what the bill owes, and
    undoes its own part of the correction: the goods still held keep the rest of it."""
    doc, line_id, parcel = await _restated_bill(client, session, auth, from_order=from_order, cost=300.0)
    gain = await _role(session, auth, GAIN)

    for qty, owed, kept in ((4, 72.0, -108.0), (3, 36.0, -54.0), (3, 0.0, 0.0)):
        r = await _return(client, auth, doc, parcel, qty)
        assert r.status_code == 200, r.text
        await _owes(client, session, auth, doc, owed)
        assert await _account_net(session, auth["company_id"], gain) == kept
        await assert_settled(client, session, auth)
