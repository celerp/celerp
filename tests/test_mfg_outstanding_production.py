# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Production still to come counts once on Demand Planning.

What a run has received is already a lot on hand under the product. Only the rest of its
expected output is still in progress, so supply is what is on hand plus, for each open run,
what it still has to receive. Invoice auto-production nets against the same figure.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from celerp_manufacturing.routes import _in_progress_by_item
from mfg_runs import complete, issue, product, receive, reopen, run, set_settings
from test_cost_restatement import _item, _sell, _state, auth, ids  # noqa: F401  (fixtures)
from test_mfg_finalize_supply import _made, _post_invoice, _runs_for_doc

pytestmark = pytest.mark.asyncio


async def _product(client, auth) -> str:
    raw = await _item(client, auth, 1000.0, qty=100)
    return await product(client, auth, [(raw, 1)])


async def _open_run(client, auth, fg: str, qty: float, *received: float) -> tuple[str, list[str]]:
    """A run of ``qty`` with its components issued and ``received`` taken in, one lot each."""
    order = await run(client, auth, fg, qty)
    assert (await issue(client, auth, order)).status_code == 200
    lots = []
    for q in received:
        r = await receive(client, auth, order, q, key=f"r-{uuid.uuid4().hex[:6]}")
        assert r.status_code == 200, r.text
        lots.append(r.json()["lot_item_id"])
    return order, lots


async def _board(client, auth, fg: str) -> dict:
    r = await client.get("/manufacturing/to-make", headers=auth["headers"])
    assert r.status_code == 200, r.text
    return next(i for i in r.json()["items"] if i["item_id"] == fg)


def _supply(row: dict) -> float:
    return row["on_hand"] + row["in_progress"]


async def _demand(client, auth, fg: str, qty: float) -> str:
    """A posted invoice for ``qty`` of ``fg`` (auto work orders off): open demand."""
    return await _post_invoice(client, auth, fg, qty)


async def test_nothing_received_counts_the_whole_run(client, session, auth):
    fg = await _product(client, auth)
    await _open_run(client, auth, fg, 10)
    await _demand(client, auth, fg, 12)

    row = await _board(client, auth, fg)
    assert (row["on_hand"], row["in_progress"], row["to_make"]) == (0, 10, 2)


async def test_partly_received_counts_the_rest_as_in_progress(client, session, auth):
    fg = await _product(client, auth)
    await _open_run(client, auth, fg, 10, 4)
    await _demand(client, auth, fg, 12)

    row = await _board(client, auth, fg)
    assert (row["on_hand"], row["in_progress"]) == (4, 6)
    assert _supply(row) == 10 and row["to_make"] == 2
    assert row["docs"][0]["shortfall"] == 2


async def test_fully_received_open_run_adds_nothing_more(client, session, auth):
    """Received in full, completed and reopened: it is open again but has nothing left to give."""
    fg = await _product(client, auth)
    order, _ = await _open_run(client, auth, fg, 10, 10)
    assert (await _state(session, auth, order))["status"] == "completed"
    assert (await reopen(client, auth, order, key="again")).status_code == 200
    assert (await _state(session, auth, order))["status"] != "completed"
    await _demand(client, auth, fg, 12)

    row = await _board(client, auth, fg)
    assert (row["on_hand"], row["in_progress"], row["to_make"]) == (10, 0, 2)


async def test_several_receipts_never_count_twice(client, session, auth):
    fg = await _product(client, auth)
    await _open_run(client, auth, fg, 10, 3, 2, 1)
    await _demand(client, auth, fg, 12)

    row = await _board(client, auth, fg)
    assert (row["on_hand"], row["in_progress"], row["to_make"]) == (6, 4, 2)


async def test_a_received_lot_sold_leaves_stock_and_the_run_adds_only_the_rest(client, session, auth):
    fg = await _product(client, auth)
    _, (lot,) = await _open_run(client, auth, fg, 10, 5)
    await _sell(client, session, auth, lot)
    assert (await _state(session, auth, lot))["status"] == "sold"
    await _demand(client, auth, fg, 12)

    row = await _board(client, auth, fg)
    assert (row["on_hand"], row["in_progress"], row["to_make"]) == (0, 5, 7)


async def test_a_received_lot_reserved_counts_as_stock_does_and_the_run_adds_only_the_rest(client, session, auth):
    fg = await _product(client, auth)
    _, (lot,) = await _open_run(client, auth, fg, 10, 5)
    await _demand(client, auth, fg, 12)
    before = await _board(client, auth, fg)
    r = await client.post(f"/items/{lot}/reserve", headers=auth["headers"], json={"quantity": 2})
    assert r.status_code == 200, r.text

    row = await _board(client, auth, fg)
    assert (before["on_hand"], row["on_hand"]) == (5, 5)
    assert row["in_progress"] == 5 and row["to_make"] == 2


async def test_several_open_runs_add_only_what_each_still_has_to_give(client, session, auth):
    fg = await _product(client, auth)
    await _open_run(client, auth, fg, 10, 4)
    await _open_run(client, auth, fg, 5, 2)
    await _open_run(client, auth, fg, 3)
    await _demand(client, auth, fg, 20)

    row = await _board(client, auth, fg)
    assert (row["on_hand"], row["in_progress"], row["to_make"]) == (6, 12, 2)


async def test_completed_runs_add_nothing(client, session, auth):
    """Completion takes in the rest of the output; the run then adds nothing on top of it."""
    fg = await _product(client, auth)
    order, _ = await _open_run(client, auth, fg, 10, 4)
    assert (await complete(client, auth, order, key="done")).status_code == 200
    await _demand(client, auth, fg, 12)

    row = await _board(client, auth, fg)
    assert (row["on_hand"], row["in_progress"], row["to_make"]) == (10, 0, 2)


async def test_received_beyond_expected_counts_as_nothing_to_come():
    """Older records can show more received than expected; that never lowers supply."""
    runs = [SimpleNamespace(state={"status": "in_progress", "output_item_id": "fg",
                                   "expected_outputs": [{"quantity": 10}], "received_qty": 12}),
            SimpleNamespace(state={"status": "planned", "output_item_id": "fg",
                                   "expected_outputs": [{"quantity": 3}], "received_qty": 0})]
    assert _in_progress_by_item(runs) == {"fg": 3.0}


async def test_posting_an_invoice_makes_only_the_true_shortfall(client, session, auth):
    """Demand 12, a run of 10 that received 5, those 5 on hand: 2 are short, not none."""
    fg = await _product(client, auth)
    await _open_run(client, auth, fg, 10, 5)
    await set_settings(session, auth, manufacturing={"auto_create_work_orders": True})

    doc = await _post_invoice(client, auth, fg, 12)

    assert _made(await _runs_for_doc(session, auth, doc)) == [2.0]
