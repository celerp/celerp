# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Orders that tie on due date are served oldest first, every time.

Demand Planning gives free stock to the order due soonest, undated orders last. Orders that
tie (both undated, or due the same day) are served in the order they were created, then by
document id, whatever order the database happens to return them in. The board, Make selected
and the work orders made when an invoice is posted all settle the same way.
"""
from __future__ import annotations

import pytest
from test_mfg_finalize_supply import _made, _runs_for_doc
from test_mfg_outstanding_demand import _auto, _row, _stocked
from test_mfg_reserved_demand import _make, _order, _pegged

pytestmark = pytest.mark.asyncio


@pytest.fixture(params=["as read", "reversed"])
def read_order(request, monkeypatch):
    """Demand comes back from the database in either order."""
    if request.param == "reversed":
        from celerp_manufacturing import routes

        real = routes._open_demand

        async def backwards(*args, **kwargs):
            return list(reversed(await real(*args, **kwargs)))
        monkeypatch.setattr(routes, "_open_demand", backwards)
    return request.param


def test_peg_serves_tied_documents_oldest_first_then_by_id():
    from celerp_manufacturing.routes import _peg

    def doc(doc_id, created_at, due=None):
        return {"doc_id": doc_id, "created_at": created_at, "due": due, "quantity": 5.0, "reserved": 0.0}

    for docs in ([doc("doc:b", "2026-01-02"), doc("doc:a", "2026-01-01")],
                 [doc("doc:a", "2026-01-01"), doc("doc:b", "2026-01-02")],
                 [doc("doc:z", "2026-01-01", "2026-05-01"), doc("doc:y", "2026-01-01", "2026-05-01")]):
        row = {"demand": 10.0, "on_hand": 5.0, "in_progress": 0.0, "docs": docs}
        _peg(row)
        first = min(docs, key=lambda d: (d["created_at"], d["doc_id"]))
        assert {d["doc_id"]: d["covered"] for d in docs} == {
            d["doc_id"]: (5.0 if d is first else 0.0) for d in docs}


async def _undated(client, auth, fg: str, qty: float) -> str:
    return await _order(client, auth, fg, qty, None)


async def test_two_undated_orders_older_is_served(client, session, auth, read_order):
    fg, _ = await _stocked(client, auth, 5)
    older = await _undated(client, auth, fg, 5)
    newer = await _undated(client, auth, fg, 5)

    assert _pegged(await _row(client, auth, fg)) == {older: (5, 0), newer: (0, 5)}


async def test_two_orders_due_the_same_day_older_is_served(client, session, auth, read_order):
    fg, _ = await _stocked(client, auth, 5)
    older = await _order(client, auth, fg, 5, "2026-11-01")
    newer = await _order(client, auth, fg, 5, "2026-11-01")

    assert _pegged(await _row(client, auth, fg)) == {older: (5, 0), newer: (0, 5)}


async def test_repeated_board_reads_settle_the_same_way(client, session, auth, monkeypatch):
    from celerp_manufacturing import routes

    fg, _ = await _stocked(client, auth, 5)
    older = await _undated(client, auth, fg, 5)
    newer = await _undated(client, auth, fg, 5)
    real = routes._open_demand
    flip = {"n": 0}

    async def alternating(*args, **kwargs):
        flip["n"] += 1
        out = await real(*args, **kwargs)
        return list(reversed(out)) if flip["n"] % 2 else out
    monkeypatch.setattr(routes, "_open_demand", alternating)

    seen = [_pegged(await _row(client, auth, fg)) for _ in range(4)]
    assert seen == [{older: (5, 0), newer: (0, 5)}] * 4


async def test_make_selected_after_a_board_read_makes_for_the_newer_order(client, session, auth, read_order):
    fg, _ = await _stocked(client, auth, 5)
    older = await _undated(client, auth, fg, 5)
    newer = await _undated(client, auth, fg, 5)

    assert _pegged(await _row(client, auth, fg)) == {older: (5, 0), newer: (0, 5)}
    assert await _make(client, auth, [(fg, older), (fg, newer)]) == [(newer, 5.0)]


async def test_posting_the_newer_order_makes_its_shortfall(client, session, auth, read_order):
    """An older undated order already holds the five on hand; posting a newer one makes five."""
    fg, _ = await _stocked(client, auth, 5)
    older = await _undated(client, auth, fg, 5)
    await _auto(session, auth)

    newer = await _undated(client, auth, fg, 5)

    assert _made(await _runs_for_doc(session, auth, newer)) == [5.0]
    assert await _runs_for_doc(session, auth, older) == []
