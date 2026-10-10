# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Buying a consignment tells its goods apart by the movement that moved them.

The bill covers the units still on hand and the units sold but not yet settled with the
consignor. Units a return sent back to the consignor are not bought: the lot keeps the
quantity that went back as the record of the return, and its status (disposed) says it
left, so the quantity alone never decides. Consigned goods cannot be written off or
archived while held, so a disposed or retired consigned lot is always one that went back.
"""
from __future__ import annotations

import pytest

from test_consignment_in_sale import AP, PAYABLE, PURCHASED, _books, _consign, _sell, _settled, _state
from test_set_aside_goods_every_exit import _write_off
from test_set_aside_goods_follow_the_lot import _split

pytestmark = pytest.mark.asyncio


async def _back(client, auth, con: str, lot: str, qty: float):
    return await client.post(f"/docs/{con}/return-items", headers=auth["headers"],
                             json={"items": [{"item_id": lot, "quantity_returned": qty}]})


async def _convert(client, session, auth, con: str) -> dict:
    r = await client.post(f"/docs/{con}/convert", headers=auth["headers"])
    assert r.status_code == 200, r.text
    return await _state(session, auth, r.json()["target_doc_id"])


async def test_goods_that_went_back_are_not_bought_though_the_lot_keeps_their_quantity(client, session, auth):
    """3 received at 4.00, 1 sold and shipped, 2 sent back. The lot that went back is
    disposed holding 2; the bill buys only the 1 sold, and the payable clears."""
    con, lot = await _consign(client, session, auth, qty=3, cost_price=4.0)
    await _sell(client, session, auth, lot, 1)
    assert (r := await _back(client, auth, con, lot, 2)).status_code == 200, r.text
    left = await _state(session, auth, lot)
    assert (left["status"], float(left["quantity"])) == ("disposed", 2.0)
    bill = await _convert(client, session, auth, con)
    assert [li["quantity"] for li in bill["line_items"]] == [1]
    assert await _books(session, auth, PAYABLE, PURCHASED) == {PAYABLE: 0.0, PURCHASED: 0.0}
    await _settled(client, session, auth)


async def test_goods_on_hand_and_goods_sold_are_both_bought(client, session, auth):
    """Neighbour: 3 received, 1 sold and shipped, 2 still held, nothing sent back: the bill
    buys all 3 (2 into stock, 1 against the sale)."""
    con, lot = await _consign(client, session, auth, qty=3, cost_price=4.0)
    await _sell(client, session, auth, lot, 1)
    bill = await _convert(client, session, auth, con)
    assert [li["quantity"] for li in bill["line_items"]] == [3]
    books = await _books(session, auth, PAYABLE, PURCHASED, AP)
    assert books[PAYABLE] == 0.0 and books[PURCHASED] > 0 and books[AP] < 0, books
    await _settled(client, session, auth)


async def test_consigned_goods_held_cannot_be_written_off_or_archived(client, session, auth):
    """Neighbour: the only way a held consigned lot becomes disposed or retired is going
    back to the consignor. Write-off and archive are refused, and the bill still buys all."""
    con, lot = await _consign(client, session, auth, qty=3, cost_price=4.0)
    r = await _write_off(client, auth, lot, 1)
    assert r.status_code == 409 and r.json()["detail"]["message_key"] == "consignment.not_owned", r.text
    await session.rollback()  # production never commits a refused request; the test client shares the session
    r = await client.patch(f"/items/{lot}", headers=auth["headers"],
                           json={"fields_changed": {"status": {"new": "archived"}}})
    assert r.status_code == 409, r.text
    await session.rollback()
    assert (await _state(session, auth, lot))["status"] == "available"
    bill = await _convert(client, session, auth, con)
    assert [li["quantity"] for li in bill["line_items"]] == [3]
    await _settled(client, session, auth)


async def test_the_lots_bought_on_a_line_share_its_debit_to_the_cent(client, session, auth):
    """A line of 3 at 10.00 in all (3.333 each), split into three lots of 1: the bill's
    10.00 is spread by the allocator, 3.34 / 3.33 / 3.33, and the lots hold exactly it."""
    con, lot = await _consign(client, session, auth, qty=3, unit_price=10 / 3, cost_price=10 / 3)
    parts = [lot, await _split(client, auth, lot, 1), await _split(client, auth, lot, 1)]
    await _convert(client, session, auth, con)
    costs = sorted([round(float((await _state(session, auth, x))["cost_total"]), 2) for x in parts], reverse=True)
    assert costs == [3.34, 3.33, 3.33], costs
    await _settled(client, session, auth)
