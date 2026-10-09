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


def _lots_of(n: int, places: int) -> tuple[list[tuple[str, float]], dict[str, float]]:
    """``n`` lots that may not be split, in receipt order, each a different quantity between
    0.5 and 9.5 to ``places`` decimals, every one an even number of the smallest unit."""
    import random

    rnd, unit = random.Random(n * 10 + places), 10 ** places
    lots = [(f"l{i}", 2 * rnd.randint(unit // 4, 19 * unit // 4) / unit) for i in range(n)]
    return lots, dict(lots)


def _timed_takes(lots, whole_only, *qtys: float, seconds: float = 1.0) -> list:
    """``_line_takes`` for each of ``qtys``, run in a process of its own held to 2 GB, each
    call within ``seconds``."""
    import json
    import os
    import subprocess
    import sys

    code = ("import json, resource, sys, time\n"
            "resource.setrlimit(resource.RLIMIT_AS, (2 << 30, 2 << 30))\n"
            "from celerp_docs.routes import _line_takes\n"
            "lots, whole_only, qtys = json.load(sys.stdin)\n"
            "for qty in qtys:\n"
            "    t = time.perf_counter()\n"
            "    takes = _line_takes([tuple(x) for x in lots], whole_only, qty)\n"
            "    print(json.dumps([time.perf_counter() - t, takes]), flush=True)\n")
    try:
        done = subprocess.run([sys.executable, "-c", code], input=json.dumps([lots, whole_only, qtys]),
                              capture_output=True, text=True, timeout=30 + 5 * len(qtys),
                              env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)})
    except subprocess.TimeoutExpired:
        pytest.fail(f"_line_takes over {len(lots)} lots did not finish")
    assert done.returncode == 0, done.stderr[-2000:]
    out = [json.loads(line) for line in done.stdout.splitlines()]
    assert [round(took, 3) for took, _ in out if took > seconds] == [], len(lots)
    return [None if takes is None else [tuple(t) for t in takes] for _, takes in out]


@pytest.mark.parametrize("n", [30, 45, 60])
def test_line_takes_over_many_lots_that_may_not_be_split(n):
    """Many lots that may not be split, each a different quantity, are worked out within a
    bound of time and memory, whether the quantity can be made up from them or not."""
    lots, whole_only = _lots_of(n, 3)
    # Made up of every third lot: each lot that gives goes back whole. Every lot is an even
    # number of thousandths, so one more thousandth cannot be made up: receipt order, and
    # the carve guard refuses the part.
    qty = round(sum(q for _, q in lots[::3]), 3)
    made, odd = _timed_takes(lots, whole_only, qty, round(qty + 0.001, 3))
    assert all(whole_only[lot] == q for lot, q in made)
    assert round(sum(q for _, q in made), 3) == qty
    _gives_in_receipt_order(lots, qty + 0.001, odd)

    lots, whole_only = _lots_of(n, 6)
    smallest = min(lots, key=lambda lot: lot[1])
    under = round(smallest[1] - 0.000002, 6)
    alone, short, past = _timed_takes(lots, whole_only, smallest[1], under,
                                      round(sum(q for _, q in lots[::3]), 6))
    # A quantity only the smallest lot makes up comes from it alone; just under it, nothing
    # makes it up; past the bounds of the search, nothing is worked out.
    assert alone == [smallest]
    _gives_in_receipt_order(lots, under, short)
    assert past is None


def _gives_in_receipt_order(lots, qty, takes) -> None:
    assert takes[:-1] == lots[:len(takes) - 1]
    assert takes[-1][0] == lots[len(takes) - 1][0]
    assert sum(q for _, q in takes) == pytest.approx(qty)


@pytest.mark.parametrize(("lots", "whole_only", "qty"), [
    # Finer than the places the search works to.
    ([("a", 5), ("b", 3)], {"a": 5}, 2.0000001),
    # More units than the search may cover.
    ([("a", 5), ("b", 3000)], {"a": 5}, 1000.000001),
])
def test_line_takes_past_its_bounds_works_nothing_out(lots, whole_only, qty):
    from celerp_docs.routes import _line_takes
    assert _line_takes(lots, whole_only, qty) is None


async def test_return_by_line_past_what_can_be_worked_out_names_return_by_lot(client, session, auth):
    """When which lots make up a line's quantity cannot be worked out, the return is refused
    with the way that works, and nothing moves."""
    bill, line_id, first, second = await _two_deliveries(client, session, auth)
    before = await _snapshot(session, auth, first, second)
    books = await _books(session, auth, "1130-P", "2110")
    r = await _return_lines(client, auth, bill, {"line_id": line_id, "quantity_returned": 2.0000001})
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert detail["message_key"] == "docs.return_line_by_lot"
    [line] = (await _state(session, auth, bill))["line_items"]
    sku = line["sku"]
    assert detail["message"] == (f"{sku}: which of this line's lots make up the quantity cannot be worked "
                                 "out, as some of them may not be split. Return the goods by lot instead.")
    assert await _snapshot(session, auth, first, second) == before
    assert await _books(session, auth, "1130-P", "2110") == books
