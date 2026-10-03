# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Every step of a production run can be undone.

Returning issued materials puts them back on the lots they came from at the value the run
recorded when they were issued. Undoing a receipt removes an untouched output lot and gives
its value back to the run. Reopening a completed run reverses completion's own entries from
the values completion recorded. A run that no longer holds anything can be cancelled. Each
successful step leaves the books carrying exactly the stock and the work in progress; each
refused step leaves everything exactly as it was; and undoing a step, then taking it again,
leaves the books and the stock exactly as the step alone did.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from mfg_runs import (
    OPENING, WIP, balances, cancel, complete, give_back, issue, lines, product, receive, refusal, role, run,
    set_settings, snapshot,
)
from stock_books import assert_settled
from test_cost_restatement import TZ, _item, _state
from test_cost_restatement import auth, ids  # noqa: F401  (auth and ids are fixtures)

pytestmark = pytest.mark.asyncio


async def _job(client, auth, *, cost=100.0, stock=10.0, per=5.0, qty=2.0):
    """One component lot (``stock`` units costing ``cost``) and a run making ``qty`` of a
    product that takes ``per`` of it each."""
    raw = await _item(client, auth, cost, qty=stock)
    item = await product(client, auth, [(raw, per)])
    return raw, item, await run(client, auth, item, qty)


async def _books_and_stock(session, auth, *entity_ids: str) -> dict:
    """What a round trip must give back exactly: every account's balance, each named lot's
    quantity and held value, and each named run's progress and work in progress."""
    out: dict = {"balances": await balances(session, auth)}
    for e in entity_ids:
        s = await _state(session, auth, e)
        if s.get("entity_type") == "mfg_order":
            out[e] = {"status": s.get("status"), "received_qty": float(s.get("received_qty") or 0),
                      "issued": {i["item_id"]: float(i.get("issued_qty") or 0) for i in s.get("inputs", [])},
                      "wip": round(float(s.get("wip_issued") or 0) - float(s.get("wip_transferred") or 0)
                                   - float(s.get("wip_wasted") or 0), 2)}
        else:
            out[e] = {"status": s.get("status"), "quantity": float(s.get("quantity") or 0),
                      "held": round(float(s.get("cost_total") or 0), 2)}
    return out


# ---------------------------------------------------------------------------
# Return issued materials
# ---------------------------------------------------------------------------

async def test_return_puts_materials_back_at_the_value_they_were_issued_at(client, session, auth):
    raw, _, order = await _job(client, auth)
    ob, wip = await role(session, auth, OPENING), await role(session, auth, WIP)
    before = await _books_and_stock(session, auth, raw, order)
    assert (await issue(client, auth, order, [(raw, 4)], key="i")).status_code == 200

    r = await give_back(client, auth, order, [(raw, 4)], key="g")

    assert r.status_code == 200, r.text
    assert r.json()["value"] == "40.00"
    assert await lines(session, auth, f"je:auto:{order}:return:g") == sorted([
        (ob, (OPENING,), 40.0, 0.0), (wip, (WIP,), 0.0, 40.0)])
    s = await _state(session, auth, raw)
    assert (s["quantity"], round(s["cost_total"], 2)) == (10, 100.0)
    facts = await _state(session, auth, order)
    assert facts["inputs"][0]["issued_qty"] == 0 and float(facts["wip_issued"]) == 0
    await assert_settled(client, session, auth)
    # The run is back to holding nothing; it stays started, as issuing left it.
    after = await _books_and_stock(session, auth, order)
    assert {**after[order], "status": "planned"} == before[order]


async def test_a_partial_return_takes_its_share_and_the_last_takes_what_is_left(client, session, auth):
    raw, _, order = await _job(client, auth, cost=100.0, stock=3, per=1.5)
    assert (await issue(client, auth, order, key="i")).status_code == 200
    assert (await _state(session, auth, order))["wip_issued"] == "100.00"

    first = await give_back(client, auth, order, [(raw, 1)], key="g1")
    assert first.status_code == 200, first.text
    assert first.json()["value"] == "33.33"
    await assert_settled(client, session, auth)
    second = await give_back(client, auth, order, [(raw, 2)], key="g2")
    assert second.status_code == 200, second.text
    assert second.json()["value"] == "66.67"
    s = await _state(session, auth, raw)
    assert (s["quantity"], round(s["cost_total"], 2)) == (3, 100.0)
    assert float((await _state(session, auth, order))["wip_issued"]) == 0
    await assert_settled(client, session, auth)


async def test_return_without_items_returns_everything_issued(client, session, auth):
    raw, _, order = await _job(client, auth)
    assert (await issue(client, auth, order, [(raw, 6)], key="i")).status_code == 200
    r = await give_back(client, auth, order, key="g")
    assert r.status_code == 200, r.text
    assert r.json()["returned"] == [{"item_id": raw, "quantity": 6.0, "value": "60.00"}]
    assert (await _state(session, auth, raw))["quantity"] == 10
    await assert_settled(client, session, auth)


async def _refused(client, session, auth, entities, response, status, key):
    before = await snapshot(session, auth, *entities)
    detail = refusal(await response(), status, key)
    assert await snapshot(session, auth, *entities) == before
    await assert_settled(client, session, auth)
    return detail


async def test_return_refuses_more_than_was_issued(client, session, auth):
    raw, _, order = await _job(client, auth)
    assert (await issue(client, auth, order, [(raw, 4)], key="i")).status_code == 200
    detail = await _refused(client, session, auth, (raw, order),
                            lambda: give_back(client, auth, order, [(raw, 5)], key="g"), 409, "over_return")
    assert detail["params"] == {"item": raw, "issued": 4.0}


async def test_return_refuses_a_component_that_is_not_in_the_run(client, session, auth):
    raw, _, order = await _job(client, auth)
    other = await _item(client, auth, 10.0, qty=1)
    assert (await issue(client, auth, order, [(raw, 4)], key="i")).status_code == 200
    await _refused(client, session, auth, (raw, other, order),
                   lambda: give_back(client, auth, order, [(other, 1)], key="g"), 422, "not_an_input")


async def test_return_refuses_a_quantity_that_is_not_positive(client, session, auth):
    raw, _, order = await _job(client, auth)
    assert (await issue(client, auth, order, [(raw, 4)], key="i")).status_code == 200
    await _refused(client, session, auth, (raw, order),
                   lambda: give_back(client, auth, order, [(raw, 0)], key="g"), 422, "return_quantity")


async def test_return_with_nothing_issued_returns_nothing(client, session, auth):
    raw, _, order = await _job(client, auth)
    before = await snapshot(session, auth, raw, order)
    r = await give_back(client, auth, order, key="g")
    assert r.status_code == 200, r.text
    assert r.json() == {"returned": [], "value": "0"}
    assert await snapshot(session, auth, raw, order) == before


async def test_return_is_refused_once_output_was_received(client, session, auth):
    raw, _, order = await _job(client, auth)
    assert (await issue(client, auth, order, key="i")).status_code == 200
    assert (await receive(client, auth, order, 1, key="r")).status_code == 200
    await _refused(client, session, auth, (raw, order),
                   lambda: give_back(client, auth, order, [(raw, 1)], key="g"), 409, "return_after_receipt")


async def test_return_is_refused_on_a_completed_run(client, session, auth):
    raw, _, order = await _job(client, auth)
    assert (await complete(client, auth, order, key="c")).status_code == 200
    await _refused(client, session, auth, (raw, order),
                   lambda: give_back(client, auth, order, key="g"), 409, "run_closed")


async def test_return_to_a_lot_that_is_no_longer_held_is_refused(client, session, auth):
    raw, _, order = await _job(client, auth, stock=10)
    assert (await issue(client, auth, order, [(raw, 4)], key="i")).status_code == 200
    from test_cost_restatement import _sell
    await _sell(client, session, auth, raw)
    await _refused(client, session, auth, (raw, order),
                   lambda: give_back(client, auth, order, [(raw, 4)], key="g"), 409, "return_lot_unavailable")


async def test_return_retried_with_its_key_moves_nothing_more(client, session, auth):
    raw, _, order = await _job(client, auth)
    assert (await issue(client, auth, order, [(raw, 6)], key="i")).status_code == 200
    first = await give_back(client, auth, order, [(raw, 2)], key="g")
    assert first.status_code == 200, first.text
    before = await snapshot(session, auth, raw, order)
    again = await give_back(client, auth, order, [(raw, 2)], key="g")
    assert again.status_code == 200 and again.json() == first.json(), again.text
    assert await snapshot(session, auth, raw, order) == before
    refusal(await give_back(client, auth, order, [(raw, 3)], key="g"), 409, "key_reused")
    assert await snapshot(session, auth, raw, order) == before
    await assert_settled(client, session, auth)


async def test_return_on_a_locked_day_is_refused_and_changes_nothing(client, session, auth):
    raw, _, order = await _job(client, auth)
    assert (await issue(client, auth, order, [(raw, 4)], key="i")).status_code == 200
    today = datetime.now(ZoneInfo(TZ)).date()
    await set_settings(session, auth, lock_date=(today + timedelta(days=1)).isoformat())
    before = await snapshot(session, auth, raw, order)
    r = await give_back(client, auth, order, [(raw, 4)], key="g")
    assert r.status_code == 422 and "locked" in r.text, r.text
    assert await snapshot(session, auth, raw, order) == before


async def test_issue_return_issue_leaves_books_and_stock_as_the_issue_alone(client, session, auth):
    raw, _, order = await _job(client, auth, cost=100.0, stock=3, per=1.5)
    assert (await issue(client, auth, order, [(raw, 1)], key="i1")).status_code == 200
    once = await _books_and_stock(session, auth, raw, order)
    assert (await give_back(client, auth, order, [(raw, 1)], key="g")).status_code == 200
    assert (await issue(client, auth, order, [(raw, 1)], key="i2")).status_code == 200
    assert await _books_and_stock(session, auth, raw, order) == once
    await assert_settled(client, session, auth)


async def test_cancel_after_returning_everything(client, session, auth):
    raw, _, order = await _job(client, auth)
    before = await _books_and_stock(session, auth, raw)
    assert (await issue(client, auth, order, [(raw, 4)], key="i")).status_code == 200
    detail = await _refused(client, session, auth, (raw, order), lambda: cancel(client, auth, order, key="c"),
                            409, "cancel_moved")
    assert "return" in detail["message"].lower()
    assert (await give_back(client, auth, order, key="g")).status_code == 200
    r = await cancel(client, auth, order, key="c")
    assert r.status_code == 200, r.text
    assert (await _state(session, auth, order))["status"] == "cancelled"
    assert await _books_and_stock(session, auth, raw) == before
    await assert_settled(client, session, auth)


async def _bulk(client, auth, action: str, *orders: str):
    r = await client.post("/manufacturing/bulk-action", headers=auth["headers"],
                          json={"action": action, "run_ids": list(orders)})
    assert r.status_code == 200, r.text
    return r.json()


async def test_bulk_return_returns_each_run_wholly_or_not_at_all(client, session, auth):
    raw, item, order = await _job(client, auth, stock=20)
    blocked = await run(client, auth, item, 2)
    assert (await issue(client, auth, order, key="i")).status_code == 200
    assert (await issue(client, auth, blocked, key="i")).status_code == 200
    assert (await receive(client, auth, blocked, 1, key="r")).status_code == 200
    kept = await snapshot(session, auth, blocked)

    out = await _bulk(client, auth, "return", order, blocked)

    assert out["done"] == [order]
    assert [(s["id"], s["message_key"]) for s in out["skipped"]] == [(blocked, "mfg.return_after_receipt")]
    assert (await _state(session, auth, raw))["quantity"] == 10
    assert (await snapshot(session, auth, blocked))["states"] == kept["states"]
    await assert_settled(client, session, auth)


async def test_bulk_cancel_cancels_runs_that_hold_nothing_and_skips_the_rest(client, session, auth):
    raw, item, order = await _job(client, auth, stock=20)
    holding = await run(client, auth, item, 2)
    assert (await issue(client, auth, holding, [(raw, 1)], key="i")).status_code == 200

    out = await _bulk(client, auth, "cancel", order, holding)

    assert out["done"] == [order]
    assert [(s["id"], s["message_key"]) for s in out["skipped"]] == [(holding, "mfg.cancel_moved")]
    assert (await _state(session, auth, holding))["status"] == "in_progress"
    assert (await _bulk(client, auth, "return", holding))["done"] == [holding]
    assert (await _bulk(client, auth, "cancel", holding))["done"] == [holding]
    assert (await _state(session, auth, raw))["quantity"] == 20
    await assert_settled(client, session, auth)
