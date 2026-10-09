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

import json

import pytest
from fasthtml.common import to_xml

import ui.api_client as api_client
from test_cost_restatement import _set_cost, _state
from test_lot_split_invariants import _set_measures
from test_receipt_accounting import _books
from test_receive_goods_form import _Request, _Routes
from test_receive_selected_lines import _issued, _post, _ReceiveRows, _stock_lines
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
    # Quantities of any size or number of decimals are worked out the same way.
    ([("a", 1e9), ("b", 1.0)], {"b": 1.0}, 5e8, [("a", 5e8)]),
    ([("a", 0.1234567), ("b", 1.0)], {"b": 1.0}, 0.1234567, [("a", 0.1234567)]),
    ([("a", 1.0000001), ("b", 1.0)], {"b": 1.0}, 1.0, [("a", 1.0)]),
    ([("a", 5), ("b", 3)], {"a": 5}, 2.0000001, [("b", 2.0000001)]),
    ([("a", 5), ("b", 3000)], {"a": 5}, 1000.000001, [("a", 5), ("b", 995.000001)]),
])
def test_line_takes(lots, whole_only, qty, takes):
    from celerp_docs.routes import _line_takes
    _assert_takes(_line_takes(lots, whole_only, qty), takes)


def _assert_takes(got, takes) -> None:
    assert got is not None
    assert [lot for lot, _ in got] == [lot for lot, _ in takes]
    assert [q for _, q in got] == pytest.approx([q for _, q in takes], abs=1e-9)


def _brute_takes(lots, whole_only, qty):
    """What a line gives, worked out by trying every set of the lots that may not be split:
    in receipt order, each lot gives the most it can while the rest can still make up what
    is left; receipt order when nothing makes the quantity up."""
    from itertools import combinations

    eps = 1e-9

    def sums(j):
        whole = [whole_only[lot] for lot, q in lots[j:] if lot in whole_only and q >= whole_only[lot] - eps]
        return {round(sum(c), 9) for n in range(len(whole) + 1) for c in combinations(whole, n)}

    def split(j):
        return sum(q for lot, q in lots[j:] if lot not in whole_only)

    def reach(j, x):
        return any(s - eps <= x <= s + split(j) + eps for s in sums(j))

    left, takes = qty, []
    if not reach(0, qty):
        for lot, q in lots:
            if min(q, left) > eps:
                takes.append((lot, min(q, left)))
                left -= min(q, left)
        return takes
    for i, (lot, q) in enumerate(lots):
        if lot in whole_only:
            w = whole_only[lot]
            take = w if q >= w - eps and w <= left + eps and reach(i + 1, left - w) else 0.0
        else:
            cap = min(q, left)
            fits = [min(cap, left - s) for s in sums(i + 1) if s <= left + eps and left - s - split(i + 1) <= cap + eps]
            take = max(fits, default=0.0)
        if take > eps:
            takes.append((lot, take))
            left -= take
    return takes


def test_line_takes_matches_trying_every_way():
    """Over random lines of up to eight lots, some of which may not be split, the lots a
    quantity comes from are the ones trying every way gives."""
    import random

    from celerp_docs.routes import _line_takes

    rnd = random.Random(374)
    for _ in range(3000):
        places = rnd.choice([0, 1, 2, 3])
        lots = [(f"l{i}", round(rnd.uniform(0.1, 9), places) or 1.0) for i in range(rnd.randint(1, 8))]
        whole_only = {lot: q if rnd.random() < 0.8 else q + 1 for lot, q in lots if rnd.random() < 0.6}
        if rnd.random() < 0.4:
            qty = round(sum(q for lot, q in lots if lot in whole_only and rnd.random() < 0.5), places) or 1.0
        else:
            qty = round(rnd.uniform(0.01, sum(q for _, q in lots)), places) or 1.0
        _assert_takes(_line_takes(lots, whole_only, qty), _brute_takes(lots, whole_only, qty))


@pytest.mark.parametrize("places", [3, 6])
@pytest.mark.parametrize("n", [60, 120, 200])
def test_line_takes_over_many_lots_some_of_which_may_be_split(n, places):
    """Many lots, half of which may not be split, each a different quantity: a quantity
    within what they hold is worked out within a bound of time and memory."""
    import random

    rnd, unit = random.Random(n + places), 10 ** places
    lots = [(f"l{i}", rnd.randint(unit // 2, 50 * unit) / unit) for i in range(n)]
    whole_only = {lot: q for i, (lot, q) in enumerate(lots) if i % 2 == 0}
    qtys = [round(sum(q for _, q in lots) * share, places) for share in (0.1, 0.5, 0.9)]
    for qty, takes in zip(qtys, _timed_takes(lots, whole_only, *qtys)):
        assert takes is not None, qty
        held = dict(lots)
        assert all(q <= held[lot] + 1e-9 and (lot not in whole_only or q == whole_only[lot]) for lot, q in takes)
        assert sum(q for _, q in takes) == pytest.approx(qty, abs=1e-6)


def test_line_takes_many_equal_lots_that_may_not_be_split():
    """Fifty lots of 100.125 that may not be split: all of them, or all but one, go back."""
    lots = [(f"l{i}", 100.125) for i in range(50)]
    every, but_one = _timed_takes(lots, dict(lots), 100.125 * 50, 100.125 * 49)
    assert every == lots
    assert but_one == lots[:49]


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
    third = round(sum(q for _, q in lots[::3]), 6)
    alone, short, past = _timed_takes(lots, whole_only, smallest[1], under, third)
    # A quantity only the smallest lot makes up comes from it alone; just under it, nothing
    # makes it up. Thirty lots are still worked out to the millionth; past the bounds of
    # the search, nothing is worked out.
    assert alone == [smallest]
    _gives_in_receipt_order(lots, under, short)
    if n == 30:
        assert all(whole_only[lot] == q for lot, q in past)
        assert round(sum(q for _, q in past), 6) == third
    else:
        assert past is None


def _gives_in_receipt_order(lots, qty, takes) -> None:
    assert takes[:-1] == lots[:len(takes) - 1]
    assert takes[-1][0] == lots[len(takes) - 1][0]
    assert sum(q for _, q in takes) == pytest.approx(qty)


def _no_search(monkeypatch) -> None:
    """Hold the search for the lots a line's quantity comes from to no work at all."""
    from celerp_docs import routes
    monkeypatch.setattr(routes, "_LINE_TAKES_RANGES", 0, raising=False)
    monkeypatch.setattr(routes, "_LINE_TAKES_BITS", 0, raising=False)


def test_line_takes_past_its_bounds_works_nothing_out(monkeypatch):
    from celerp_docs.routes import _line_takes
    lots, whole_only = [("a", 5), ("b", 3)], {"a": 5}
    _no_search(monkeypatch)
    assert _line_takes(lots, whole_only, 2) is None
    # Every lot giving all it can, or every lot that may be split, needs no search.
    assert _line_takes(lots, whole_only, 8) == lots
    assert _line_takes(lots, {}, 6) == [("a", 5), ("b", 1)]


async def test_return_by_line_past_what_can_be_worked_out_names_return_by_lot(client, session, auth, monkeypatch):
    """When which lots make up a line's quantity cannot be worked out, the return is refused
    with the way that works, naming the line, and nothing moves."""
    bill, line_id, first, second = await _two_deliveries(client, session, auth)
    before = await _snapshot(session, auth, first, second)
    books = await _books(session, auth, "1130-P", "2110")
    _no_search(monkeypatch)
    r = await _return_lines(client, auth, bill, {"line_id": line_id, "quantity_returned": 2.0000001})
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert detail["message_key"] == "docs.return_line_by_lot"
    assert (detail["params"]["line"], detail["params"]["line_id"]) == (0, line_id)
    [line] = (await _state(session, auth, bill))["line_items"]
    sku = line["sku"]
    assert detail["message"] == (f"{sku}: which of this line's lots make up the quantity cannot be worked "
                                 "out, as some of them may not be split. Return the goods by lot instead.")
    assert await _snapshot(session, auth, first, second) == before
    assert await _books(session, auth, "1130-P", "2110") == books


# Return Goods by lot: a line opens to the lots its goods went into, each ticked with its own
# quantity, and the form sends those lots instead of the line.

def _return_handler(client, auth, monkeypatch, sent: list):
    from ui.routes import documents

    async def return_goods(_tok, entity_id, data):
        sent.append(data)
        return api_client._raise(await client.post(
            f"/docs/{entity_id}/return-items", headers=auth["headers"], json=data)).json()

    monkeypatch.setattr(documents, "_token", lambda request: "tok")
    monkeypatch.setattr(api_client, "return_goods", return_goods)
    routes = _Routes()
    documents.setup_routes(routes)
    return routes.routes[("post", "/docs/{entity_id}/return-goods")]


def _trigger(resp) -> dict:
    return json.loads(resp.headers["HX-Trigger"])


async def test_return_form_lists_each_lines_lots(client, session, auth):
    from ui.routes import documents

    bill, line_id, first, second = await _two_deliveries(client, session, auth)
    doc = (await client.get(f"/docs/{bill}", headers=auth["headers"])).json()
    [line] = doc["line_items"]
    assert [(x["item_id"], x["quantity"], x["whole_only"]) for x in line["return_lots"]] == [
        (first, 5, True), (second, 3, False)]
    html = to_xml(documents._li_bulk_toolbar(bill, False, show_fulfill=True, is_inbound=True,
                                             inbound_line_items=doc["line_items"], locations=[]))
    assert 'class="return-lots"' in html
    for j, (lot, qty) in enumerate(((first, 5), (second, 3))):
        assert f'name="lot_0_{j}" value="{lot}"' in html
        assert f'name="lot_qty_0_{j}"' in html and f'data-max="{qty}"' in html
    # The lot list is closed, so the form sends the line as before, not its lots.
    form = _ReceiveRows({0}, form_id="li-bulk-revert-btn")
    form.feed(html)
    assert form.qty_inputs == {"qty_0": {"value": "8", "max": "8"}}
    assert not [n for n, _ in form.fields if n.startswith("lot_")]


async def test_return_form_sends_the_lots_ticked(client, session, auth, monkeypatch):
    bill, line_id, first, second = await _two_deliveries(client, session, auth)
    sent: list[dict] = []
    handler = _return_handler(client, auth, monkeypatch, sent)

    # Lots and lines are not sent together; a lot ticked needs a quantity above 0.
    resp = await handler(_Request([("qty_0", "2"), ("line_id_0", line_id), ("lot_0_1", second),
                                   ("lot_qty_0_1", "1")]), bill)
    assert "by line or by lot" in _trigger(resp)["celerpToast"]["message"]
    resp = await handler(_Request([("lot_0_1", second), ("lot_qty_0_1", "0")]), bill)
    assert _trigger(resp)["celerpToast"]["message"] == "Enter a quantity above 0 for each lot ticked."
    assert sent == []

    resp = await handler(_Request([("lot_0_0", first), ("lot_qty_0_0", "5"), ("lot_0_1", second),
                                   ("lot_qty_0_1", "2"), ("weight_lot_0_1", "1.5"),
                                   ("idempotency_key", "k1")]), bill)
    assert resp.status_code == 204, resp.body
    assert _trigger(resp)["celerpToast"]["message"] == "1 line returned to the supplier."
    assert sent[0]["items"] == [{"item_id": first, "quantity_returned": 5.0},
                                {"item_id": second, "quantity_returned": 2.0, "weight": 1.5}]
    assert "lines" not in sent[0] and sent[0]["idempotency_key"] == "k1"
    returned = (await _state(session, auth, bill))["returned_items"]
    assert [(x["item_id"], x["quantity_returned"]) for x in returned] == [(first, 5), (second, 2)]


async def test_return_by_line_refused_opens_the_lines_lots(client, session, auth, monkeypatch):
    bill, line_id, first, second = await _two_deliveries(client, session, auth)
    sent: list[dict] = []
    handler = _return_handler(client, auth, monkeypatch, sent)
    _no_search(monkeypatch)

    resp = await handler(_Request([("qty_0", "2"), ("line_id_0", line_id)]), bill)
    trigger = _trigger(resp)
    assert "Return the goods by lot instead." in trigger["celerpToast"]["message"]
    assert trigger["celerpOpenReturnLots"] == {"line": 0}
    # A weight given for a line whose goods are in more than one lot opens them too.
    resp = await handler(_Request([("qty_0", "7"), ("line_id_0", line_id), ("weight_0", "3")]), bill)
    assert _trigger(resp)["celerpOpenReturnLots"] == {"line": 0}
    # Any other refusal leaves the form as it is.
    resp = await handler(_Request([("qty_0", "9"), ("line_id_0", line_id)]), bill)
    assert "celerpOpenReturnLots" not in _trigger(resp)
    assert not (await _state(session, auth, bill)).get("returned_items")


async def test_return_by_lot_keeps_books_with_stock_and_bill(client, session, auth, monkeypatch):
    """Goods sent back by lot leave the books at what those lots cost: inventory still equals
    what the bill's lots on hand cost, and payables what the bill still owes."""
    bill, line_id, first, second = await _two_deliveries(client, session, auth)
    handler = _return_handler(client, auth, monkeypatch, [])
    resp = await handler(_Request([("lot_0_0", first), ("lot_qty_0_0", "5"), ("lot_0_1", second),
                                   ("lot_qty_0_1", "2")]), bill)
    assert resp.status_code == 204, resp.body

    lots = [await _state(session, auth, lot) for lot in (first, second)]
    assert [(x["quantity"], x["status"]) for x in lots] == [(5, "disposed"), (1, "available")]
    books = await _books(session, auth, "1130-P", "2110")
    assert books["1130-P"] == pytest.approx(sum(float(x.get("cost_total") or 0) for x in lots
                                                if x["status"] != "disposed"))
    owed = float((await _state(session, auth, bill)).get("amount_outstanding") or 0)
    assert books["2110"] == pytest.approx(-owed)
