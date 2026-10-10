# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""What is owed to each consignor.

Every consigned lot records the consignor it came from, and every posting to the
consignor payable names that consignor, whichever document caused it: a sale, a void or
unvoid, a customer return, the conversion to a vendor bill. So the payable filtered to one
consignor is what is owed to that consignor, and it clears to zero once their consignment
is settled, whatever happened to another consignor's goods on the same invoices.
"""
from __future__ import annotations

import uuid

import pytest

from test_consignment_in_sale import (
    AP,
    COGS,
    PAYABLE,
    PURCHASED,
    _books,
    _consign,
    _customer_return,
    _sell,
    _settled,
    _state,
)

pytestmark = pytest.mark.asyncio

CONSIGNOR_FIELD = "consignor_id"


async def _consignor(client, auth, name: str) -> str:
    r = await client.post("/crm/contacts", headers=auth["headers"], json={"name": name, "contact_type": "vendor"})
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _owed(client, auth, contact: str) -> float:
    """The consignor payable filtered to ``contact`` (the empty string: lines naming no
    party), as the amount owed: credits less debits."""
    r = await client.get(f"/accounting/ledger/{PAYABLE}", headers=auth["headers"], params={"contact_id": contact})
    assert r.status_code == 200, r.text
    return round(sum(float(li["credit"]) - float(li["debit"]) for li in r.json()["lines"]), 2)


async def _owed_each(client, auth, *contacts: str) -> tuple[float, ...]:
    return tuple([await _owed(client, auth, c) for c in (*contacts, "")])


async def test_each_consignor_is_owed_for_their_own_goods_until_settled(client, session, auth):
    """A received 2 and B 4, each recorded at 4 a unit and billed at 5. One of A's and three
    of B's sell; the customer brings back two of B's, one of which goes back to B. Each
    consignor's payable carries only their own goods, and converting both clears each."""
    a, b = await _consignor(client, auth, "Consignor A"), await _consignor(client, auth, "Consignor B")
    con_a, lot_a = await _consign(client, session, auth, qty=2, cost_price=4.0, contact_id=a)
    con_b, lot_b = await _consign(client, session, auth, qty=4, cost_price=4.0, contact_id=b)

    await _sell(client, session, auth, lot_a, 1)
    doc_b = await _sell(client, session, auth, lot_b, 3)
    assert await _owed_each(client, auth, a, b) == (4.0, 12.0, 0.0)
    assert ((await _state(session, auth, lot_a))[CONSIGNOR_FIELD],
            (await _state(session, auth, lot_b))[CONSIGNOR_FIELD]) == (a, b)
    await _settled(client, session, auth)

    returned = await _customer_return(client, session, auth, doc_b, lot_b, 2)
    assert (await _state(session, auth, returned))[CONSIGNOR_FIELD] == b
    assert await _owed_each(client, auth, a, b) == (4.0, 4.0, 0.0)
    await _settled(client, session, auth)

    r = await client.post(f"/docs/{con_b}/return-items", headers=auth["headers"],
                          json={"items": [{"item_id": returned, "quantity_returned": 1}]})
    assert r.status_code == 200, r.text
    assert await _owed_each(client, auth, a, b) == (4.0, 4.0, 0.0)
    await _settled(client, session, auth)

    r = await client.post(f"/docs/{con_b}/convert", headers=auth["headers"])
    assert r.status_code == 200, r.text
    assert await _owed_each(client, auth, a, b) == (4.0, 0.0, 0.0)
    await _settled(client, session, auth)

    r = await client.post(f"/docs/{con_a}/convert", headers=auth["headers"])
    assert r.status_code == 200, r.text
    assert await _owed_each(client, auth, a, b) == (0.0, 0.0, 0.0)
    await _settled(client, session, auth)


async def test_one_invoice_of_two_consignors_goods_owes_each_their_own(client, session, auth):
    """One unshipped invoice sells a unit of A's and a unit of B's. Voiding, unvoiding and
    shipping it keep each consignor's share apart; converting A's consignment clears A only."""
    a, b = await _consignor(client, auth, "Consignor A"), await _consignor(client, auth, "Consignor B")
    con_a, lot_a = await _consign(client, session, auth, qty=1, cost_price=4.0, contact_id=a)
    _, lot_b = await _consign(client, session, auth, qty=1, cost_price=6.0, contact_id=b)
    lines = []
    for lot in (lot_a, lot_b):
        state = await _state(session, auth, lot)
        lines.append({"entity_id": lot, "sku": state["sku"], "name": "Lot", "quantity": 1,
                      "unit_price": 40.0, "line_total": 40.0})
    r = await client.post("/docs", headers=auth["headers"], json={
        "doc_type": "invoice", "ref_id": f"INV-{uuid.uuid4().hex[:6]}", "line_items": lines, "total": 80.0})
    assert r.status_code == 200, r.text
    doc = r.json()["id"]
    assert (await client.post(f"/docs/{doc}/finalize", headers=auth["headers"])).status_code == 200
    assert await _owed_each(client, auth, a, b) == (4.0, 6.0, 0.0)
    await _settled(client, session, auth)

    for step, owed in (("void", (0.0, 0.0, 0.0)), ("unvoid", (4.0, 6.0, 0.0))):
        r = await client.post(f"/docs/{doc}/{step}", headers=auth["headers"], json={})
        assert r.status_code == 200, r.text
        assert await _owed_each(client, auth, a, b) == owed
        await _settled(client, session, auth)

    r = await client.post(f"/docs/{doc}/fulfill-lines", headers=auth["headers"],
                          json={"line_entity_ids": [lot_a, lot_b]})
    assert r.status_code == 200, r.text
    assert await _owed_each(client, auth, a, b) == (4.0, 6.0, 0.0)
    await _settled(client, session, auth)

    r = await client.post(f"/docs/{con_a}/convert", headers=auth["headers"])
    assert r.status_code == 200, r.text
    assert await _owed_each(client, auth, a, b) == (0.0, 6.0, 0.0)
    await _settled(client, session, auth)


async def test_a_lot_and_a_part_split_off_it_are_owed_to_its_consignment(client, session, auth):
    """A lot not yet sold is owed to the contact of the consignment that received it, and
    so is a lot split from it on a sale."""
    a, b = await _consignor(client, auth, "Consignor A"), await _consignor(client, auth, "Consignor B")
    con_a, lot_a = await _consign(client, session, auth, qty=2, cost_price=4.0, contact_id=a)

    await _sell(client, session, auth, lot_a, 1)
    assert await _owed_each(client, auth, a, b) == (4.0, 0.0, 0.0)
    await _settled(client, session, auth)

    r = await client.post(f"/docs/{con_a}/convert", headers=auth["headers"])
    assert r.status_code == 200, r.text
    assert await _owed_each(client, auth, a, b) == (0.0, 0.0, 0.0)
    await _settled(client, session, auth)


@pytest.mark.parametrize("sold", [1, 2])
async def test_goods_a_customer_returned_and_sent_back_to_the_consignor_are_not_owed(client, session, auth, sold):
    """A consigns 2 recorded at 4 a unit, billed at 5. ``sold`` ship, the customer brings one
    back and it goes back to A. The bill buys what was kept; A is owed nothing more, the one
    unit still sold costs 5 and the unit still held (if any) is inventory at 5."""
    a = await _consignor(client, auth, "Consignor A")
    con_a, lot_a = await _consign(client, session, auth, qty=2, cost_price=4.0, contact_id=a)
    doc = await _sell(client, session, auth, lot_a, sold)
    returned = await _customer_return(client, session, auth, doc, lot_a, 1)
    r = await client.post(f"/docs/{con_a}/return-items", headers=auth["headers"],
                          json={"items": [{"item_id": returned, "quantity_returned": 1}]})
    assert r.status_code == 200, r.text
    await _settled(client, session, auth)

    r = await client.post(f"/docs/{con_a}/convert", headers=auth["headers"])
    assert r.status_code == 200, r.text
    assert await _owed_each(client, auth, a) == (0.0, 0.0)
    assert await _books(session, auth, COGS, PURCHASED, AP) == {
        COGS: 5.0 * (sold - 1), PURCHASED: 5.0 * (2 - sold), AP: -5.0}
    await _settled(client, session, auth)
