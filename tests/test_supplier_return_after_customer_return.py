# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Goods a customer returned go back to the supplier from the bill that bought them.

A customer return brings sold goods back in a lot of their own, linked to the lot they were
sold from (returned_from). They are still the goods the bill bought, so a supplier return
from the bill takes them back to the receipt lot they came in as: against what the bill
still holds of it, at the bill's cost for that line and with that line's own tax. This holds
for an ordinary bill and for a bill a consignment was converted to after the goods came in.
"""
from __future__ import annotations

import uuid

import pytest

from test_consignment_in_sale import (
    AP, COGS, _books, _consign, _customer_return, _posted, _sell, _settled, _state,
)

pytestmark = pytest.mark.asyncio


async def _return(client, auth, doc: str, lot: str, qty: float):
    return await client.post(f"/docs/{doc}/return-items", headers=auth["headers"],
                             json={"items": [{"item_id": lot, "quantity_returned": qty}]})


async def _bought(client, session, auth, kind: str, *, qty: float = 2) -> tuple[str, str]:
    """``qty`` units at 5 received on an ordinary bill, or on a consignment converted to a
    bill once the goods are in. -> (bill, receipt lot)."""
    if kind == "bill":
        return await _consign(client, session, auth, qty=qty, cost_price=5.0, doc_type="bill")
    con, lot = await _consign(client, session, auth, qty=qty, cost_price=4.0)
    r = await client.post(f"/docs/{con}/convert", headers=auth["headers"])
    assert r.status_code == 200, r.text
    return r.json()["target_doc_id"], lot


async def _owed_on_ap(session, auth, bill: str) -> None:
    """Accounts payable carries exactly what the bill still owes."""
    owed = float((await _state(session, auth, bill))["amount_outstanding"])
    assert (await _books(session, auth, AP))[AP] == pytest.approx(-owed)


async def test_customer_return_after_conversion_goes_back_from_the_bill(client, session, auth):
    """Converted, then sold and returned by the customer: the goods go back from the bill,
    the loop closes with no stock, nothing owed and cost of sales left as the sale and the
    customer return made it."""
    bill, lot = await _bought(client, session, auth, "converted")
    doc = await _sell(client, session, auth, lot)
    ret = await _customer_return(client, session, auth, doc, lot, 2)
    cogs = (await _books(session, auth, COGS))[COGS]

    r = await _return(client, auth, bill, ret, 2)
    assert r.status_code == 200, r.text

    assert (await _state(session, auth, ret))["status"] == "disposed"
    assert (await _books(session, auth, COGS))[COGS] == cogs
    await _owed_on_ap(session, auth, bill)
    assert float((await _state(session, auth, bill))["amount_outstanding"]) == 0
    await _settled(client, session, auth)


async def test_customer_return_on_an_ordinary_bill_goes_back_from_the_bill(client, session, auth):
    """Received on a bill, sold and returned by the customer: the goods go back from the bill."""
    bill, lot = await _bought(client, session, auth, "bill")
    doc = await _sell(client, session, auth, lot)
    ret = await _customer_return(client, session, auth, doc, lot, 2)
    cogs = (await _books(session, auth, COGS))[COGS]

    r = await _return(client, auth, bill, ret, 2)
    assert r.status_code == 200, r.text

    assert (await _state(session, auth, ret))["status"] == "disposed"
    assert (await _books(session, auth, COGS))[COGS] == cogs
    await _owed_on_ap(session, auth, bill)
    assert float((await _state(session, auth, bill))["amount_outstanding"]) == 0
    await _settled(client, session, auth)


@pytest.mark.parametrize("kind", ["bill", "converted"])
async def test_a_customer_returned_lot_goes_back_no_more_than_it_holds(client, session, auth, kind):
    """1 of 2 sold and returned (R): asking for 2 of R, at once or in two parts, is refused
    and books nothing; 1 goes back, after which neither R nor its receipt lot can send more
    than the bill still holds."""
    bill, lot = await _bought(client, session, auth, kind)
    doc = await _sell(client, session, auth, lot, 1)
    sold = next(x["entity_id"] for x in (await _state(session, auth, doc))["line_items"])
    ret = await _customer_return(client, session, auth, doc, sold, 1)
    n = len(await _posted(session, auth))
    for items in ([{"item_id": ret, "quantity_returned": 2}],
                  [{"item_id": ret, "quantity_returned": 1}, {"item_id": ret, "quantity_returned": 1}]):
        r = await client.post(f"/docs/{bill}/return-items", headers=auth["headers"], json={"items": items})
        assert r.status_code in (409, 422), r.text
        # A refused request's session is discarded uncommitted; the test shares one session.
        await session.rollback()
        assert len(await _posted(session, auth)) == n
        assert (await _state(session, auth, ret))["status"] == "available"

    r = await _return(client, auth, bill, ret, 1)
    assert r.status_code == 200, r.text
    r = await _return(client, auth, bill, ret, 1)
    assert r.status_code in (409, 422), r.text
    await _owed_on_ap(session, auth, bill)
    await _settled(client, session, auth)


async def _two_lines(client, session, auth, doc_type: str) -> tuple[str, list[str]]:
    """Line 0 taxed at 10%, line 1 untaxed, 2 units at 5 each received. -> (document, lots)."""
    skus, templates = [], []
    for _ in range(2):
        sku = f"CS-{uuid.uuid4().hex[:6]}"
        r = await client.post("/items", headers=auth["headers"], json={
            "sku": sku, "name": "Lot", "quantity": 0, "sell_by": "piece", "status": "available"})
        skus.append(sku)
        templates.append(r.json()["id"])
    lines = [
        {"item_id": templates[0], "sku": skus[0], "name": "Taxed", "quantity": 2, "unit_price": 5.0,
         "line_total": 10.0, "taxes": [{"code": "VAT", "rate": 10, "amount": 1.0}]},
        {"item_id": templates[1], "sku": skus[1], "name": "Free", "quantity": 2, "unit_price": 5.0,
         "line_total": 10.0},
    ]
    r = await client.post("/docs", headers=auth["headers"], json={
        "doc_type": doc_type, "contact_id": "supplier:1", "line_items": lines,
        "subtotal": 20.0, "tax": 1.0, "total": 21.0})
    assert r.status_code == 200, r.text
    doc = r.json()["id"]
    assert (await client.post(f"/docs/{doc}/finalize", headers=auth["headers"])).status_code == 200
    items = [{"item_id": templates[i], "sku": skus[i], "name": "Lot", "quantity_received": 2,
              "po_line_index": i, "receive_as": "stock", "cost_price": 5.0} for i in range(2)]
    r = await client.post(f"/docs/{doc}/receive", headers=auth["headers"],
                          json={"location_id": "", "received_items": items})
    assert r.status_code == 200, r.text
    return doc, (await _state(session, auth, doc))["received_item_ids"]


@pytest.mark.parametrize("kind", ["bill", "converted"])
async def test_a_customer_returned_lot_takes_its_own_lines_tax_back(client, session, auth, kind):
    """Line 0 (10% tax) sold and returned (R): 1 of R back takes 5 and 0.50 tax off the bill,
    as 1 of line 0 still held would; the untaxed line is untouched."""
    doc, lots = await _two_lines(client, session, auth, "bill" if kind == "bill" else "consignment_in")
    if kind == "bill":
        bill = doc
    else:
        r = await client.post(f"/docs/{doc}/convert", headers=auth["headers"])
        assert r.status_code == 200, r.text
        bill = r.json()["target_doc_id"]
    sale = await _sell(client, session, auth, lots[0])
    ret = await _customer_return(client, session, auth, sale, lots[0], 2)

    r = await _return(client, auth, bill, ret, 1)
    assert r.status_code == 200, r.text
    state = await _state(session, auth, bill)
    assert (float(state["returned_credit"]), float(state["amount_outstanding"])) == (5.5, 15.5)
    await _owed_on_ap(session, auth, bill)
    await _settled(client, session, auth)
