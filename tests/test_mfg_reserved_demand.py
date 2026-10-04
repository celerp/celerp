# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Stock reserved to a document serves only that document.

Fulfillment ships a reserved lot only from the document that reserved it, so Demand Planning
counts it the same way: an order's own reserved stock covers that order first, no other order
may take it, and stock reserved to a memo or any document that is not demand covers nothing.
Only free stock and what open runs still have to give are shared out, soonest due first.
Releasing a reservation returns the stock to the shared pool. Make selected and the work
orders made when an invoice is posted read the same figures.
"""
from __future__ import annotations

import pytest
from test_cost_restatement import auth, ids  # noqa: F401  (fixtures)
from test_mfg_finalize_supply import _made, _runs_for_doc
from test_mfg_outstanding_demand import _auto, _figures, _row, _stocked

pytestmark = pytest.mark.asyncio


async def _order(client, auth, fg: str, qty: float, due: str, *, post: bool = True) -> str:
    r = await client.post("/docs", headers=auth["headers"], json={
        "doc_type": "invoice", "total": 0, "due_date": due,
        "line_items": [{"item_id": fg, "sku": "FG", "name": "Made", "quantity": qty, "unit_price": 100}]})
    assert r.status_code in (200, 201), r.text
    doc = r.json()["id"]
    if post:
        assert (await client.post(f"/docs/{doc}/finalize", headers=auth["headers"])).status_code == 200
    return doc


async def _memo(client, auth, fg: str, qty: float) -> str:
    r = await client.post("/docs", headers=auth["headers"], json={"doc_type": "memo", "line_items": [
        {"entity_id": fg, "item_id": fg, "sku": "FG", "name": "Made", "quantity": qty,
         "unit_price": 100, "sell_by": "piece"}]})
    assert r.status_code in (200, 201), r.text
    memo = r.json()["id"]
    assert (await client.post(f"/docs/{memo}/finalize", headers=auth["headers"])).status_code == 200
    return memo


async def _hold(client, auth, doc: str, item: str, status: str = "reserved") -> list[str]:
    """Set the document's line for ``item`` reserved (or back to available): the lots it holds."""
    r = await client.post(f"/docs/{doc}/reserve-lines", headers=auth["headers"],
                          json={"line_entity_ids": [item], "new_status": status})
    assert r.status_code == 200, r.text
    return r.json()["reserved"]


def _pegged(row: dict) -> dict[str, tuple[float, float]]:
    """Each order's (covered, shortfall)."""
    return {d["doc_id"]: (d["covered"], d["shortfall"]) for d in row["docs"]}


async def _make(client, auth, lines: list[tuple[str, str]]) -> list[tuple[str, float]]:
    r = await client.post("/manufacturing/to-make/make", headers=auth["headers"],
                          json={"lines": [{"item_id": i, "doc_id": d} for i, d in lines], "complete": False})
    assert r.status_code == 200, r.text
    return [(c["doc_id"], c["quantity"]) for c in r.json()["created"]]


async def test_stock_reserved_to_an_order_covers_that_order(client, session, auth):
    fg, _ = await _stocked(client, auth, 5)
    mine = await _order(client, auth, fg, 5, "2026-12-01")
    await _hold(client, auth, mine, fg)

    row = await _row(client, auth, fg)
    assert _figures(row) == (5, 5, 0, 0)
    assert _pegged(row) == {mine: (5, 0)}


async def test_stock_reserved_to_another_order_does_not_cover_this_one(client, session, auth):
    """Eight on hand, five of them reserved to the later order: the sooner one gets only the free three."""
    fg, _ = await _stocked(client, auth, 8)
    later = await _order(client, auth, fg, 5, "2026-12-01")
    sooner = await _order(client, auth, fg, 6, "2026-11-01")
    await _hold(client, auth, later, fg)

    row = await _row(client, auth, fg)
    assert _figures(row) == (11, 8, 0, 3)
    assert _pegged(row) == {sooner: (3, 3), later: (5, 0)}


async def test_the_later_order_keeps_the_stock_it_reserved(client, session, auth):
    """Supply is not swapped to the order due first when the later one owns the stock."""
    fg, _ = await _stocked(client, auth, 5)
    sooner = await _order(client, auth, fg, 5, "2026-11-01")
    later = await _order(client, auth, fg, 5, "2026-12-01")
    await _hold(client, auth, later, fg)

    row = await _row(client, auth, fg)
    assert _figures(row) == (10, 5, 0, 5)
    assert _pegged(row) == {sooner: (0, 5), later: (5, 0)}


async def test_stock_reserved_to_a_memo_covers_nothing(client, session, auth):
    fg, _ = await _stocked(client, auth, 5)
    memo = await _memo(client, auth, fg, 5)
    order = await _order(client, auth, fg, 5, "2026-11-01")
    await _hold(client, auth, memo, fg)

    row = await _row(client, auth, fg)
    assert _figures(row) == (5, 0, 0, 5)
    assert _pegged(row) == {order: (0, 5)}


async def test_released_from_a_memo_the_stock_is_free_again(client, session, auth):
    fg, _ = await _stocked(client, auth, 5)
    memo = await _memo(client, auth, fg, 5)
    order = await _order(client, auth, fg, 5, "2026-11-01")
    await _hold(client, auth, memo, fg)
    await _hold(client, auth, memo, fg, "available")

    row = await _row(client, auth, fg)
    assert _figures(row) == (5, 5, 0, 0)
    assert _pegged(row) == {order: (5, 0)}


async def test_released_from_an_order_the_stock_goes_to_the_order_due_first(client, session, auth):
    fg, _ = await _stocked(client, auth, 5)
    sooner = await _order(client, auth, fg, 5, "2026-11-01")
    later = await _order(client, auth, fg, 5, "2026-12-01")
    await _hold(client, auth, later, fg)
    await _hold(client, auth, later, fg, "available")

    row = await _row(client, auth, fg)
    assert _figures(row) == (10, 5, 0, 5)
    assert _pegged(row) == {sooner: (5, 0), later: (0, 5)}


async def test_make_selected_makes_only_what_reserved_stock_leaves_short(client, session, auth):
    fg, _ = await _stocked(client, auth, 8)
    later = await _order(client, auth, fg, 5, "2026-12-01")
    sooner = await _order(client, auth, fg, 6, "2026-11-01")
    await _hold(client, auth, later, fg)

    assert await _make(client, auth, [(fg, sooner), (fg, later)]) == [(sooner, 3.0)]
    row = await _row(client, auth, fg)
    assert _figures(row) == (11, 8, 3, 0)
    assert _pegged(row) == {sooner: (6, 0), later: (5, 0)}


async def test_make_selected_for_stock_reserved_to_a_memo_makes_the_order_in_full(client, session, auth):
    fg, _ = await _stocked(client, auth, 5)
    memo = await _memo(client, auth, fg, 5)
    order = await _order(client, auth, fg, 5, "2026-11-01")
    await _hold(client, auth, memo, fg)

    assert await _make(client, auth, [(fg, order)]) == [(order, 5.0)]


async def test_posting_beside_another_orders_reserved_stock_makes_the_shortfall(client, session, auth):
    """Eight on hand, five reserved to a later order: an order due sooner for six makes three."""
    fg, _ = await _stocked(client, auth, 8)
    later = await _order(client, auth, fg, 5, "2026-12-01")
    await _hold(client, auth, later, fg)
    await _auto(session, auth)

    sooner = await _order(client, auth, fg, 6, "2026-11-01")

    assert _made(await _runs_for_doc(session, auth, sooner)) == [3.0]
    assert await _runs_for_doc(session, auth, later) == []


async def test_posting_beside_stock_reserved_to_a_memo_makes_what_free_stock_leaves_short(client, session, auth):
    fg, _ = await _stocked(client, auth, 8)
    memo = await _memo(client, auth, fg, 5)
    await _hold(client, auth, memo, fg)
    await _auto(session, auth)

    order = await _order(client, auth, fg, 5, "2026-11-01")

    assert _made(await _runs_for_doc(session, auth, order)) == [2.0]


_RECIPE = {"components": [{"item_id": "item:raw"}]}


async def test_stock_is_split_as_fulfillment_draws_it():
    """Reserved stock is keyed by the document holding it; stock reserved to no document can be
    drawn by none and counts nowhere; a quantity put aside with item.reserved does not stop
    fulfillment drawing the lot, so it stays free."""
    from celerp_manufacturing.routes import _stock_by_product

    states = {
        "item:prod": {"quantity": 1.0, "status": "available", "recipe": _RECIPE},
        "item:lot-1": {"parent_item_id": "item:prod", "quantity": 4.0, "status": "available", "reserved_quantity": 3.0},
        "item:lot-2": {"parent_item_id": "item:prod", "quantity": 2.0, "status": "reserved", "status_doc_id": "doc:A"},
        "item:lot-3": {"parent_item_id": "item:prod", "quantity": 5.0, "status": "reserved"},
        "item:lot-4": {"parent_item_id": "item:prod", "quantity": 7.0, "status": "memo_out", "status_doc_id": "doc:M"},
    }
    free, held = _stock_by_product(states)
    assert free == {"item:prod": 5.0}
    assert held == {("item:prod", "doc:A"): 2.0}

