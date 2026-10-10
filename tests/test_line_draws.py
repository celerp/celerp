# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The one line draw calculation reserve, fulfil and finalize costing share.

A line draws its own holds first, then its free bound lot, then free lots of the same
product in pick order. It never draws a lot another line of the same document holds
or binds, and lines planned together never take the same free quantity twice."""
from __future__ import annotations

L1 = "11111111-1111-4111-8111-111111111111"
L2 = "22222222-2222-4222-8222-222222222222"
OWNER = "doc:INV-1"


def _lot(eid, qty, *, created="2026-01-01", status="available", owner=None, line=None, sku="S"):
    st = {"sku": sku, "quantity": qty, "status": status}
    if owner:
        st["status_doc_id"] = owner
    if line:
        st["status_line_entity_id"] = line
    claim = "free" if status == "available" else ("reserved" if owner == OWNER and status == "reserved" else None)
    return {"entity_id": eid, "quantity": float(qty), "created_at": created, "expires_at": None,
            "state": st, "claim": claim}


def _line(eid, qty, line_id):
    return {"item_id": eid, "quantity": qty, "line_id": line_id, "sku": "S"}


def _plan(lines, index, lots, remaining=None, span=True, method="fifo"):
    from celerp.services.pick import attribute_holds, line_draw_sources, plan_line_draws
    by_id = {lot["entity_id"]: lot for lot in lots}
    held = {e: lot["state"] for e, lot in by_id.items() if lot["claim"] == "reserved"}
    attributed, _orphans, _ambiguous = attribute_holds(lines, held)
    own, primary, free = line_draw_sources(lines, index, by_id, attributed, method)
    return plan_line_draws(float(lines[index]["quantity"]), own=own, primary=primary, free=free,
                           method=method, remaining={} if remaining is None else remaining, span=span)


def _takes(draws):
    return [(lot["entity_id"], take) for lot, take, _full in draws]


def test_bound_lot_then_siblings_leaves_the_rest():
    """10 + 15 in stock, the line asks for 20: the bound 10 then 10 of the 15."""
    lots = [_lot("item:a", 10), _lot("item:b", 15, created="2026-02-01")]
    draws, short = _plan([_line("item:a", 20, L1)], 0, lots)
    assert short == 0
    assert _takes(draws) == [("item:a", 10), ("item:b", 10)]
    assert [full for _l, _t, full in draws] == [True, False]


def test_short_stock_reports_the_shortfall():
    lots = [_lot("item:a", 10), _lot("item:b", 5, created="2026-02-01")]
    _draws, short = _plan([_line("item:a", 20, L1)], 0, lots)
    assert short == 5


def test_own_holds_come_first():
    lots = [_lot("item:a", 10), _lot("item:old", 4, created="2025-01-01"),
            _lot("item:h", 3, created="2026-03-01", status="reserved", owner=OWNER, line=L1)]
    draws, short = _plan([_line("item:a", 5, L1)], 0, lots)
    assert short == 0
    assert _takes(draws) == [("item:h", 3), ("item:a", 2)]


def test_other_lines_hold_is_never_drawn():
    """The document holds item:h for line 2; line 1 of the same document cannot take it,
    even though document-level eligibility would accept it."""
    lines = [_line("item:a", 12, L1), _line("item:h", 3, L2)]
    lots = [_lot("item:a", 10), _lot("item:h", 3, status="reserved", owner=OWNER, line=L2),
            _lot("item:c", 5, created="2026-04-01")]
    draws, short = _plan(lines, 0, lots)
    assert short == 0
    assert _takes(draws) == [("item:a", 10), ("item:c", 2)]


def test_another_lines_bound_lot_is_not_a_sibling():
    lines = [_line("item:a", 12, L1), _line("item:b", 1, L2)]
    lots = [_lot("item:a", 10), _lot("item:b", 9, created="2025-01-01")]
    _draws, short = _plan(lines, 0, lots)
    assert short == 2


def test_lines_planned_together_share_free_stock_once():
    """Two lines of one SKU, each wanting 8 from 10 + 5 of free stock: the first takes
    its bound 10's eight, the second gets the two left of it and the 5, then is short."""
    lines = [_line("item:a", 8, L1), _line("item:a", 8, L2)]
    lots = [_lot("item:a", 10), _lot("item:b", 5, created="2026-02-01")]
    remaining: dict = {}
    first, short1 = _plan(lines, 0, lots, remaining)
    second, short2 = _plan(lines, 1, lots, remaining)
    assert short1 == 0 and _takes(first) == [("item:a", 8)]
    assert _takes(second) == [("item:a", 2), ("item:b", 5)]
    assert short2 == 1


def test_same_bound_lot_on_two_lines():
    """4 + 7 on one bound lot of 11 both fit; the second line takes the rest whole."""
    lines = [_line("item:a", 4, L1), _line("item:a", 7, L2)]
    lots = [_lot("item:a", 11)]
    remaining: dict = {}
    first, _ = _plan(lines, 0, lots, remaining)
    second, short = _plan(lines, 1, lots, remaining)
    assert _takes(first) == [("item:a", 4)] and _takes(second) == [("item:a", 7)]
    assert short == 0 and second[0][2] is True


def test_no_span_when_splitting_is_off():
    lots = [_lot("item:a", 1), _lot("item:b", 1, created="2026-02-01")]
    _draws, short = _plan([_line("item:a", 2, L1)], 0, lots, span=False)
    assert short == 1


def test_own_holds_bound_first_then_pick_order():
    lots = [_lot("item:new", 2, created="2026-05-01", status="reserved", owner=OWNER, line=L1),
            _lot("item:old", 2, created="2025-01-01", status="reserved", owner=OWNER, line=L1),
            _lot("item:a", 2, created="2026-09-01", status="reserved", owner=OWNER, line=L1)]
    draws, _ = _plan([_line("item:a", 6, L1)], 0, lots)
    assert _takes(draws) == [("item:a", 2), ("item:old", 2), ("item:new", 2)]


def test_pick_order_is_respected():
    lots = [_lot("item:a", 1), _lot("item:old", 5, created="2025-01-01"),
            _lot("item:new", 5, created="2026-05-01")]
    fifo, _ = _plan([_line("item:a", 3, L1)], 0, lots)
    lifo, _ = _plan([_line("item:a", 3, L1)], 0, lots, method="lifo")
    assert _takes(fifo) == [("item:a", 1), ("item:old", 2)]
    assert _takes(lifo) == [("item:a", 1), ("item:new", 2)]


def test_legacy_hold_attributed_only_when_one_line_binds_it():
    from celerp.services.pick import attribute_holds
    held = {"item:a": {"status": "reserved", "status_doc_id": OWNER}}
    one, orphans, ambiguous = attribute_holds([_line("item:a", 1, L1), _line("item:b", 1, L2)], held)
    assert one == {0: ["item:a"]} and not orphans and not ambiguous
    two, orphans, ambiguous = attribute_holds([_line("item:a", 1, L1), _line("item:a", 1, L2)], held)
    assert two == {} and ambiguous == {"item:a": [0, 1]}
    none, orphans, _ = attribute_holds([_line("item:b", 1, L1)], held)
    assert none == {} and orphans == ["item:a"]


def test_stamped_hold_follows_its_line_not_its_binding():
    from celerp.services.pick import attribute_holds
    held = {"item:a": {"status": "reserved", "status_doc_id": OWNER, "status_line_entity_id": L2}}
    by_line, orphans, ambiguous = attribute_holds([_line("item:a", 1, L1), _line("item:a", 1, L2)], held)
    assert by_line == {1: ["item:a"]} and not orphans and not ambiguous
    gone, orphans, _ = attribute_holds([_line("item:a", 1, L1)], held)
    assert gone == {} and orphans == ["item:a"]


def test_legacy_sibling_hold_belongs_to_the_one_line_of_its_product():
    from celerp.services.pick import attribute_holds
    held = {"item:sib": {"sku": "S", "status": "reserved", "status_doc_id": OWNER}}
    one, orphans, ambiguous = attribute_holds([_line("item:a", 4, L1)], held)
    assert one == {0: ["item:sib"]} and not orphans and not ambiguous
    two, orphans, ambiguous = attribute_holds([_line("item:a", 1, L1), _line("item:b", 1, L2)], held)
    assert two == {} and ambiguous == {"item:sib": [0, 1]}
    other = {"item:sib": {"sku": "T", "status": "reserved", "status_doc_id": OWNER}}
    none, orphans, _ = attribute_holds([_line("item:a", 1, L1)], other)
    assert none == {} and orphans == ["item:sib"]
