# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Demand Planning's Make selected makes each short demand line once.

Each selected line makes at most one run per action: the same action sent again (its answer
lost, say) returns the runs it made, even when the board would still show the line short,
which FIFO pegging does once a later order's run covers an earlier order. A line selected
twice is made once, and each run made counts as supply for the lines after it, so one
action never makes more than the demand. Another action judges the shortfall afresh.
Concurrent actions are covered in test_mfg_make_selected_race_pg.
"""
from __future__ import annotations

import pytest

from mfg_runs import COGS, OPENING, PURCHASED, WIP, lines, product
from stock_books import assert_settled
from test_cost_restatement import _item
from test_mfg_creation_contract import _count

pytestmark = pytest.mark.asyncio


async def _demand(client, auth, made: str, qty: float, due: str) -> str:
    r = await client.post("/docs", headers=auth["headers"], json={
        "doc_type": "invoice", "total": qty, "due_date": due,
        "line_items": [{"item_id": made, "sku": "FG", "name": "Made", "quantity": qty, "unit_price": 1}]})
    assert r.status_code == 200, r.text
    assert (await client.post(f"/docs/{r.json()['id']}/finalize", headers=auth["headers"])).status_code == 200
    return r.json()["id"]


async def _make(client, auth, lines: list[tuple[str, str]], key: str | None, complete: bool = False) -> dict:
    body = {"lines": [{"item_id": i, "doc_id": d} for i, d in lines], "complete": complete}
    if key:
        body["idempotency_key"] = key
    r = await client.post("/manufacturing/to-make/make", headers=auth["headers"], json=body)
    assert r.status_code == 200, r.text
    return r.json()


async def _board(client, auth, made: str) -> dict:
    rows = (await client.get("/manufacturing/to-make", headers=auth["headers"])).json()["items"]
    return next(r for r in rows if r["item_id"] == made)


async def _setup(client, auth) -> tuple[str, str, str]:
    """A product with nothing on hand, and two orders for 5 of it: one due first."""
    made = await product(client, auth, [(await _item(client, auth, 100.0, qty=100), 1)])
    return made, await _demand(client, auth, made, 5, "2026-05-01"), await _demand(client, auth, made, 5, "2026-06-01")


async def test_the_same_action_sent_again_returns_the_runs_it_made(client, session, auth):
    made, early, _ = await _setup(client, auth)
    first = await _make(client, auth, [(made, early)], "op")

    again = await _make(client, auth, [(made, early)], "op")

    assert again == first and len(first["created"]) == 1
    assert await _count(session, auth, entity_type="mfg_order") == 1


async def test_a_lost_answer_sent_again_does_not_make_a_line_pegging_still_shows_short(client, session, auth):
    """The run made for the later order is pegged to the earlier one, so the later order
    still shows short; sending the same action again makes nothing more."""
    made, early, late = await _setup(client, auth)
    first = await _make(client, auth, [(made, late)], "op")
    docs = {d["doc_id"]: d for d in (await _board(client, auth, made))["docs"]}
    assert docs[late]["shortfall"] == 5.0 and docs[early]["shortfall"] == 0.0

    again = await _make(client, auth, [(made, late)], "op")

    assert again == first
    assert (await _board(client, auth, made))["in_progress"] == 5.0
    assert await _count(session, auth, entity_type="mfg_order") == 1
    fresh = await _make(client, auth, [(made, late)], "next")
    assert [c["quantity"] for c in fresh["created"]] == [5.0]
    assert (await _board(client, auth, made))["to_make"] == 0.0


async def test_a_line_selected_twice_is_made_once(client, session, auth):
    made, early, _ = await _setup(client, auth)

    out = await _make(client, auth, [(made, early), (made, early)], "op")

    assert [c["quantity"] for c in out["created"]] == [5.0] and out["skipped"] == []
    assert await _count(session, auth, entity_type="mfg_order") == 1


async def test_each_run_made_counts_for_the_lines_after_it(client, session, auth):
    made, early, late = await _setup(client, auth)

    out = await _make(client, auth, [(made, late), (made, early), (made, "")], "op")

    assert [(c["doc_id"], c["quantity"]) for c in out["created"]] == [(early, 5.0), (late, 5.0)]
    assert out["skipped"] == [{"item_id": made, "doc_id": "", "reason": "nothing to make"}]
    row = await _board(client, auth, made)
    assert (row["in_progress"], row["to_make"]) == (10.0, 0.0)


async def test_a_later_action_judges_the_shortfall_afresh(client, session, auth):
    made, early, late = await _setup(client, auth)
    await _make(client, auth, [(made, early), (made, late)], "a")

    out = await _make(client, auth, [(made, early), (made, late), (made, "")], "b")

    assert out["created"] == [] and [s["reason"] for s in out["skipped"]] == ["nothing to make"] * 3
    assert await _count(session, auth, entity_type="mfg_order") == 2


async def test_make_and_complete_sent_again_completes_once(client, session, auth):
    made, early, _ = await _setup(client, auth)
    first = await _make(client, auth, [(made, early)], "op", complete=True)
    events = await _count(session, auth, event_type="mfg.order.completed")

    again = await _make(client, auth, [(made, early)], "op", complete=True)

    assert again == first and events == 1
    assert await _count(session, auth, event_type="mfg.order.completed") == 1
    assert await _count(session, auth, entity_type="mfg_order") == 1
    assert await _run_entries(session, auth, first["created"][0], "op") == _MADE_FIVE  # booked once


_MADE_FIVE = {"issue": [("1130-OB", (OPENING,), 0.0, 5.0), ("1130-WIP", (WIP,), 5.0, 0.0)],
              "receive": [("1130-P", (PURCHASED,), 5.0, 0.0), ("1130-WIP", (WIP,), 0.0, 5.0)]}
"""Five made at 1.00 each: the components leave opening inventory through work in progress
onto the account for made goods."""


async def _run_entries(session, auth, made: dict, key: str) -> dict[str, list[tuple]]:
    """The lines of the entries a run Make selected made and completed under ``key`` posted."""
    ref = f"je:auto:{made['run_id']}:{{}}:mfg-from-doc:{made['doc_id']}:{made['item_id']}:{key}:{{}}"
    return {step: await lines(session, auth, ref.format(step, step)) for step in ("issue", "receive")}


async def test_orders_made_and_shipped_leave_every_account_carrying_its_stock(client, session, auth):
    """Neither order is costed when posted: nothing is on hand yet. Making them puts the
    rings on the account for made goods, and shipping each costs its sale from there."""
    made, early, late = await _setup(client, auth)
    out = await _make(client, auth, [(made, early), (made, late)], "op", complete=True)
    assert [c["doc_id"] for c in out["created"]] == [early, late]

    for run, doc in zip(out["created"], (early, late)):
        assert await _run_entries(session, auth, run, "op") == _MADE_FIVE
        r = await client.post(f"/docs/{doc}/fulfill-lines", headers=auth["headers"], json={"line_entity_ids": [made]})
        assert r.status_code == 200, r.text
        assert r.json()["fulfillment_status"] == "fulfilled"
        assert await lines(session, auth, f"je:auto:{doc}:cogs-adj:fulfill-0:l0") == [
            ("1130-P", (PURCHASED,), 0.0, 5.0), ("5100", (COGS,), 5.0, 0.0)]
    await assert_settled(client, session, auth)
