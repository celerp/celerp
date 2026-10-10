# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Converting a consignment after part of it went back to the consignor, as one operation.

Initial conditions per test: a fresh company with the seeded chart and a consignment of 4
units billed at 5.00 a unit, received at a recorded cost of 4.00 (_consign). One unit is
sold on an invoice and 2 go back to the consignor, in either order; 1 is still held. The
consignment is then converted to a vendor bill.

The conversion succeeds for the unit held and the unit sold. Every bill line's ordered
and received quantities agree (2 and 2), and so do its receipts: the 2 units that went
back to the consignor are not returns on the bill. Ownership moves once: each kept lot is bought
once and a second conversion is refused and books nothing. The consignor payable clears,
accounts payable owes the bill (10.00), the held unit is inventory at the bill's cost
(5.00) and the sold unit's cost of sales is the bill's cost (5.00). The held unit goes
back from the bill at that cost, and the bill then owes for the unit sold.
"""
from __future__ import annotations

import pytest
from sqlalchemy import func, select

from celerp.accounting_roles import LOT_ACCOUNT_FIELD
from celerp.models.ledger import LedgerEntry
from test_consignment_in_sale import AP, COGS, PAYABLE, PURCHASED, _books, _consign, _posted, _sell, _settled, _state

pytestmark = pytest.mark.asyncio


async def _back(client, auth, con: str, lot: str, qty: float):
    r = await client.post(f"/docs/{con}/return-items", headers=auth["headers"],
                          json={"items": [{"item_id": lot, "quantity_returned": qty}]})
    assert r.status_code == 200, r.text


async def _fulfil(client, auth, doc: str, lot: str) -> None:
    r = await client.post(f"/docs/{doc}/fulfill-lines", headers=auth["headers"], json={"line_entity_ids": [lot]})
    assert r.status_code == 200, r.text


async def _bought(session, auth) -> dict[str, int]:
    session.expire_all()
    rows = (await session.execute(select(LedgerEntry.entity_id, func.count()).where(
        LedgerEntry.company_id == auth["company_id"], LedgerEntry.event_type == "item.consignment.bought",
    ).group_by(LedgerEntry.entity_id))).all()
    return {entity_id: n for entity_id, n in rows}


async def _converted_after_return(client, session, auth, order: str) -> tuple[str, str]:
    """The consignment above, 1 unit sold and 2 sent back in ``order``, converted. -> (bill, lot)."""
    con, lot = await _consign(client, session, auth, qty=4, cost_price=4.0)
    if order == "sold_then_back":
        await _sell(client, session, auth, lot, 1)
        await _back(client, auth, con, lot, 2)
    else:
        doc = await _sell(client, session, auth, lot, 1, ship=False)
        await _back(client, auth, con, lot, 2)
        await _fulfil(client, auth, doc, lot)
    assert await _books(session, auth, PAYABLE, COGS) == {PAYABLE: -4.0, COGS: 4.0}
    r = await client.post(f"/docs/{con}/convert", headers=auth["headers"])
    assert r.status_code == 200, r.text
    return r.json()["target_doc_id"], lot


@pytest.mark.parametrize("order", ["sold_then_back", "back_then_sold"])
async def test_converting_after_a_partial_return_buys_what_was_kept_once(client, session, auth, order):
    bill_id, lot = await _converted_after_return(client, session, auth, order)
    bill = await _state(session, auth, bill_id)
    con = bill["source_consignment_id"]
    lines = [(float(li["quantity"]), float(li.get("quantity_received") or 0)) for li in bill["line_items"]]
    assert lines == [(2.0, 2.0)], bill["line_items"]
    assert float(bill["total"]) == 10.0

    held = await _state(session, auth, lot)
    assert (held.get("consignment_flag"), held[LOT_ACCOUNT_FIELD], float(held["cost_total"]),
            float(held["quantity"])) == (None, PURCHASED, 5.0, 1.0)
    bought = await _bought(session, auth)
    assert bought and set(bought.values()) == {1}, bought
    assert await _books(session, auth, PAYABLE, COGS, PURCHASED, AP) == {
        PAYABLE: 0.0, COGS: 5.0, PURCHASED: 5.0, AP: -10.0}
    await _settled(client, session, auth)

    entries = len(await _posted(session, auth))
    again = await client.post(f"/docs/{con}/convert", headers=auth["headers"])
    assert again.status_code >= 400, again.text
    assert len(await _posted(session, auth)) == entries
    assert await _bought(session, auth) == bought


@pytest.mark.parametrize("order", ["sold_then_back", "back_then_sold"])
async def test_the_bill_receives_and_sends_back_only_what_it_took_over(client, session, auth, order):
    """B03: the bill's receipts are the 2 units it bought, not the 4 the consignment received,
    and the 2 that went back to the consignor are not returns on the bill. The unit still
    held goes back from the bill at the bill's 5.00, so the bill owes 5.00 for the unit sold;
    a second unit is refused and books nothing."""
    bill_id, lot = await _converted_after_return(client, session, auth, order)
    bill = await _state(session, auth, bill_id)
    assert [float(x["quantity_received"]) for x in bill["received_items"]] == [2.0]
    assert sum(float(x["quantity_received"]) for x in bill["received_items"]) == sum(
        float(li["quantity_received"]) for li in bill["line_items"])
    assert (bill.get("returned_items") or [], bill.get("returned_credit")) == ([], None)
    doc = (await client.get(f"/docs/{bill_id}", headers=auth["headers"])).json()
    assert [li.get("returnable_quantity") for li in doc["line_items"]] == [1.0]

    r = await client.post(f"/docs/{bill_id}/return-items", headers=auth["headers"],
                          json={"items": [{"item_id": lot, "quantity_returned": 1}]})
    assert r.status_code == 200, r.text
    bill = await _state(session, auth, bill_id)
    assert (bill["status"], float(bill["amount_outstanding"]), float(bill["returned_credit"])) == (
        "partial_returned", 5.0, 5.0)
    assert await _books(session, auth, PAYABLE, COGS, PURCHASED, AP) == {
        PAYABLE: 0.0, COGS: 5.0, PURCHASED: 0.0, AP: -5.0}
    entries = len(await _posted(session, auth))
    r = await client.post(f"/docs/{bill_id}/return-items", headers=auth["headers"],
                          json={"items": [{"item_id": lot, "quantity_returned": 1}]})
    assert r.status_code in (409, 422), r.text
    assert len(await _posted(session, auth)) == entries
    await _settled(client, session, auth)


# Neighbouring rule: a consignment kept whole still converts with its lines and receipts as
# received, and its goods go back from the bill at the bill's cost.


async def test_a_consignment_kept_whole_converts_with_its_lines_as_received(client, session, auth):
    con, lot = await _consign(client, session, auth, qty=4, cost_price=4.0)
    r = await client.post(f"/docs/{con}/convert", headers=auth["headers"])
    assert r.status_code == 200, r.text
    bill = await _state(session, auth, r.json()["target_doc_id"])
    assert [(float(li["quantity"]), float(li["quantity_received"])) for li in bill["line_items"]] == [(4.0, 4.0)]
    assert await _books(session, auth, PAYABLE, PURCHASED, AP) == {PAYABLE: 0.0, PURCHASED: 20.0, AP: -20.0}
    await _settled(client, session, auth)
    bill_id = r.json()["target_doc_id"]
    bill = await _state(session, auth, bill_id)
    assert [float(x["quantity_received"]) for x in bill["received_items"]] == [4.0]
    r = await client.post(f"/docs/{bill_id}/return-items", headers=auth["headers"],
                          json={"items": [{"item_id": lot, "quantity_returned": 1}]})
    assert r.status_code == 200, r.text
    assert await _books(session, auth, PAYABLE, PURCHASED, AP) == {PAYABLE: 0.0, PURCHASED: 15.0, AP: -15.0}
    assert float((await _state(session, auth, bill_id))["amount_outstanding"]) == 15.0
    await _settled(client, session, auth)


# Goods a customer returned before the conversion were bought by the bill like the rest
# (item.consignment.bought): they go back to the supplier from the bill at the bill's cost,
# counted against the lot they were sold from, never past what the bill bought of it.


async def test_goods_a_customer_returned_go_back_from_the_bill_at_its_cost(client, session, auth):
    """2 received at a recorded 4 and billed at 5, both sold and both returned by the customer,
    then converted: the bill bought the 2 back on the shelf. 1 goes back from the bill at 5,
    so the bill owes 5; a third unit is refused and books nothing."""
    from test_consignment_in_sale import _customer_return, _sell as _sold

    con, lot = await _consign(client, session, auth, qty=2, cost_price=4.0)
    doc = await _sold(client, session, auth, lot)
    returned = await _customer_return(client, session, auth, doc, lot, 2)
    r = await client.post(f"/docs/{con}/convert", headers=auth["headers"])
    assert r.status_code == 200, r.text
    bill_id = r.json()["target_doc_id"]
    assert await _books(session, auth, PAYABLE, COGS, PURCHASED, AP) == {
        PAYABLE: 0.0, COGS: 0.0, PURCHASED: 10.0, AP: -10.0}

    r = await client.post(f"/docs/{bill_id}/return-items", headers=auth["headers"],
                          json={"items": [{"item_id": returned, "quantity_returned": 1}]})
    assert r.status_code == 200, r.text
    bill = await _state(session, auth, bill_id)
    assert (float(bill["amount_outstanding"]), float(bill["returned_credit"])) == (5.0, 5.0)
    assert await _books(session, auth, PAYABLE, COGS, PURCHASED, AP) == {
        PAYABLE: 0.0, COGS: 0.0, PURCHASED: 5.0, AP: -5.0}
    await _settled(client, session, auth)

    r = await client.post(f"/docs/{bill_id}/return-items", headers=auth["headers"],
                          json={"items": [{"item_id": returned, "quantity_returned": 1}]})
    assert r.status_code == 200, r.text
    assert await _books(session, auth, PAYABLE, COGS, PURCHASED, AP) == {
        PAYABLE: 0.0, COGS: 0.0, PURCHASED: 0.0, AP: 0.0}
    entries = len(await _posted(session, auth))
    r = await client.post(f"/docs/{bill_id}/return-items", headers=auth["headers"],
                          json={"items": [{"item_id": lot, "quantity_returned": 1}]})
    assert r.status_code in (409, 422), r.text
    assert len(await _posted(session, auth)) == entries
    await _settled(client, session, auth)


async def test_held_and_customer_returned_goods_go_back_from_the_bill_to_what_it_bought(client, session, auth):
    """4 received at a recorded 4 and billed at 5: 1 sold and returned by the customer, 3 held.
    The bill bought 4 for 20. The 3 held and the 1 returned all go back at 5 a unit, the bill
    then owes nothing, and nothing more goes back."""
    from test_consignment_in_sale import _customer_return, _sell as _sold

    con, lot = await _consign(client, session, auth, qty=4, cost_price=4.0)
    doc = await _sold(client, session, auth, lot, 1)
    sold = next(x["entity_id"] for x in (await _state(session, auth, doc))["line_items"])
    returned = await _customer_return(client, session, auth, doc, sold, 1)
    r = await client.post(f"/docs/{con}/convert", headers=auth["headers"])
    assert r.status_code == 200, r.text
    bill_id = r.json()["target_doc_id"]
    assert await _books(session, auth, PAYABLE, PURCHASED, AP) == {PAYABLE: 0.0, PURCHASED: 20.0, AP: -20.0}

    r = await client.post(f"/docs/{bill_id}/return-items", headers=auth["headers"], json={"items": [
        {"item_id": lot, "quantity_returned": 3}, {"item_id": returned, "quantity_returned": 1}]})
    assert r.status_code == 200, r.text
    bill = await _state(session, auth, bill_id)
    assert (float(bill["amount_outstanding"]), float(bill["returned_credit"])) == (0.0, 20.0)
    assert await _books(session, auth, PAYABLE, PURCHASED, AP) == {PAYABLE: 0.0, PURCHASED: 0.0, AP: 0.0}
    await _settled(client, session, auth)


async def test_customer_returned_goods_partly_back_with_the_consignor_go_back_from_the_bill_only_as_bought(
        client, session, auth):
    """2 sold and returned by the customer, 1 of them sent back to the consignor: the bill
    bought the 1 left at 5. It goes back from the bill at 5; the unit already with the
    consignor is refused and books nothing."""
    from test_consignment_in_sale import _customer_return, _sell as _sold

    con, lot = await _consign(client, session, auth, qty=2, cost_price=4.0)
    doc = await _sold(client, session, auth, lot)
    returned = await _customer_return(client, session, auth, doc, lot, 2)
    await _back(client, auth, con, returned, 1)
    r = await client.post(f"/docs/{con}/convert", headers=auth["headers"])
    assert r.status_code == 200, r.text
    bill_id = r.json()["target_doc_id"]
    r = await client.post(f"/docs/{bill_id}/return-items", headers=auth["headers"],
                          json={"items": [{"item_id": returned, "quantity_returned": 1}]})
    assert r.status_code == 200, r.text
    assert await _books(session, auth, PAYABLE, PURCHASED, AP) == {PAYABLE: 0.0, PURCHASED: 0.0, AP: 0.0}
    entries = len(await _posted(session, auth))
    gone = next(x["returned_lot_id"] for x in (await _state(session, auth, con))["returned_items"]
                if x.get("returned_lot_id"))
    r = await client.post(f"/docs/{bill_id}/return-items", headers=auth["headers"],
                          json={"items": [{"item_id": gone, "quantity_returned": 1}]})
    assert r.status_code in (409, 422), r.text
    assert len(await _posted(session, auth)) == entries
    await _settled(client, session, auth)
