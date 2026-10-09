# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Return by line takes goods only from lots that can give them.

A line received in more than one delivery has one lot per delivery. A lot whose Allow
Splitting is off goes back to the supplier whole or not at all, so the server picks the
lots a line's quantity comes from with that in mind, keeping to receipt order among the
ways the quantity can be made up. When no way exists the return is refused once, by the
no-split guard every carve goes through, and nothing moves. The books take off what the
lots actually sent back cost.
"""
from __future__ import annotations

import pytest

from test_cost_restatement import _set_cost, _state
from test_lot_split_invariants import _set_measures
from test_receipt_accounting import _books
from test_receive_selected_lines import _issued, _post, _stock_lines
from test_return_selected_lines import _qty, _return_lines

pytestmark = pytest.mark.asyncio


async def _two_deliveries(client, session, auth):
    """A bill line received twice at 2.0 a unit: a lot of 5 that may not be split, then a
    lot of 3 that may, restated to cost 3.0 a unit. -> (bill, line id, first lot, second lot)."""
    bill, [line_id] = await _issued(client, session, auth, "bill", _stock_lines(1, qty=8))
    for qty in (5, 3):
        r = await _post(client, auth, bill, {"source_line_id": line_id, "quantity_received": qty})
        assert r.status_code == 200, r.text
    first, second = (await _state(session, auth, bill))["received_item_ids"]
    await _set_measures(client, auth, first, allow_splitting=False)
    r = await _set_cost(client, auth, second, 9.0)
    assert r.status_code == 200, r.text
    return bill, line_id, first, second


async def _snapshot(session, auth, *lots: str):
    return [((s := await _state(session, auth, lot))["quantity"], s["status"], s.get("cost_total"))
            for lot in lots]


async def test_part_return_comes_from_the_lot_that_may_be_split(client, session, auth):
    bill, line_id, first, second = await _two_deliveries(client, session, auth)
    books = await _books(session, auth, "1130-P", "2110")

    r = await _return_lines(client, auth, bill, {"line_id": line_id, "quantity_returned": 2})
    assert r.status_code == 200, r.text
    assert await _snapshot(session, auth, first, second) == [(5, "available", 10.0), (1, "available", 3.0)]
    [entry] = (await _state(session, auth, bill))["returned_items"]
    assert (entry["item_id"], entry["quantity_returned"], entry["source_line_id"]) == (second, 2, line_id)
    # Two units of the second lot, at its 3.0 a unit, leave the books.
    after = await _books(session, auth, "1130-P", "2110")
    assert after["1130-P"] == pytest.approx(books["1130-P"] - 6.0)
    assert after["2110"] == pytest.approx(books["2110"] + 6.0)


async def test_whole_lot_that_may_not_be_split_goes_back_first(client, session, auth):
    bill, line_id, first, second = await _two_deliveries(client, session, auth)
    books = await _books(session, auth, "1130-P", "2110")

    r = await _return_lines(client, auth, bill, {"line_id": line_id, "quantity_returned": 5})
    assert r.status_code == 200, r.text
    assert await _snapshot(session, auth, first, second) == [(5, "disposed", 10.0), (3, "available", 9.0)]
    [entry] = (await _state(session, auth, bill))["returned_items"]
    assert (entry["item_id"], entry["quantity_returned"]) == (first, 5)
    after = await _books(session, auth, "1130-P", "2110")
    assert after["1130-P"] == pytest.approx(books["1130-P"] - 10.0)


async def test_return_spanning_both_lots_takes_the_first_whole(client, session, auth):
    bill, line_id, first, second = await _two_deliveries(client, session, auth)
    books = await _books(session, auth, "1130-P", "2110")

    r = await _return_lines(client, auth, bill, {"line_id": line_id, "quantity_returned": 7})
    assert r.status_code == 200, r.text
    returned = (await _state(session, auth, bill))["returned_items"]
    assert [(x["item_id"], x["quantity_returned"]) for x in returned] == [(first, 5), (second, 2)]
    assert await _qty(session, auth, second) == 1
    after = await _books(session, auth, "1130-P", "2110")
    assert after["1130-P"] == pytest.approx(books["1130-P"] - 16.0)
    assert after["2110"] == pytest.approx(books["2110"] + 16.0)


async def test_quantity_no_lots_can_make_up_is_refused_once(client, session, auth):
    bill, line_id, first, second = await _two_deliveries(client, session, auth)
    before = await _snapshot(session, auth, first, second)
    books = await _books(session, auth, "1130-P", "2110")

    # Four is more than the lot that may be split holds, and less than taking the other whole.
    r = await _return_lines(client, auth, bill, {"line_id": line_id, "quantity_returned": 4})
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["message_key"] == "lots.splitting_off"
    r = await _return_lines(client, auth, bill, {"line_id": line_id, "quantity_returned": 9})
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["message_key"] == "docs.return_line_not_on_hand"
    assert await _snapshot(session, auth, first, second) == before
    assert not (await _state(session, auth, bill)).get("returned_items")
    assert await _books(session, auth, "1130-P", "2110") == books


async def test_lots_that_may_all_be_split_still_go_in_receipt_order(client, session, auth):
    bill, [line_id] = await _issued(client, session, auth, "bill", _stock_lines(1, qty=8))
    for qty in (5, 3):
        r = await _post(client, auth, bill, {"source_line_id": line_id, "quantity_received": qty})
        assert r.status_code == 200, r.text
    first, second = (await _state(session, auth, bill))["received_item_ids"]
    r = await _return_lines(client, auth, bill, {"line_id": line_id, "quantity_returned": 2})
    assert r.status_code == 200, r.text
    assert [await _qty(session, auth, lot) for lot in (first, second)] == [3, 3]


@pytest.mark.parametrize(("lots", "whole_only", "qty", "takes"), [
    # A lot that may not be split is passed over for a part, taken whole when that fits.
    ([("a", 5), ("b", 3)], {"a": 5}, 2, [("b", 2)]),
    ([("a", 5), ("b", 3)], {"a": 5}, 6, [("a", 5), ("b", 1)]),
    # Receipt order holds among the ways that work: the earlier splittable lot gives first.
    ([("a", 2), ("b", 4), ("c", 3)], {"b": 4}, 5, [("a", 2), ("c", 3)]),
    ([("a", 2), ("b", 4), ("c", 3)], {"b": 4}, 6, [("a", 2), ("b", 4)]),
    # A lot that may not be split but cannot go back whole (part of it held) gives nothing.
    ([("a", 3), ("b", 4)], {"a": 5}, 2, [("b", 2)]),
    # No way exists: receipt order, so the carve guard refuses the part.
    ([("a", 5), ("b", 3)], {"a": 5}, 4, [("a", 4)]),
])
def test_line_takes(lots, whole_only, qty, takes):
    from celerp_docs.routes import _line_takes
    assert _line_takes(lots, whole_only, qty) == takes
