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
    COGS, OPENING, WIP, balances, cancel, complete, give_back, issue, lines, product, receive, refusal, reopen,
    role, run, set_settings, snapshot, undo_receipt,
)
from stock_books import assert_settled
from test_cost_restatement import TZ, _invoice, _item, _sell, _state
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


async def _refused(client, session, auth, entities, response, status, key, *, settled=True):
    before = await snapshot(session, auth, *entities)
    detail = refusal(await response(), status, key)
    assert await snapshot(session, auth, *entities) == before
    if settled:
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


async def _locked(session, auth) -> None:
    today = datetime.now(ZoneInfo(TZ)).date()
    await set_settings(session, auth, lock_date=(today + timedelta(days=1)).isoformat())


# ---------------------------------------------------------------------------
# Undo a receipt
# ---------------------------------------------------------------------------

async def _received(client, session, auth, *, qty=3.0, per=2.0, take=(1.0,)):
    """A run making ``qty`` with every component issued and one lot received per ``take``."""
    raw, item, order = await _job(client, auth, per=per, qty=qty)
    assert (await issue(client, auth, order, key="i")).status_code == 200
    lots = []
    for n, q in enumerate(take):
        r = await receive(client, auth, order, q, key=f"r{n}")
        assert r.status_code == 200, r.text
        lots.append(r.json()["lot_item_id"])
    return raw, item, order, lots


async def test_undo_receipt_removes_the_lot_and_gives_its_value_back_to_the_run(client, session, auth):
    raw, item, order = await _job(client, auth, per=2.0, qty=3.0)
    assert (await issue(client, auth, order, key="i")).status_code == 200
    issued = await _books_and_stock(session, auth, raw, order)
    lot = (await receive(client, auth, order, 1, key="r")).json()["lot_item_id"]
    value = (await _state(session, auth, order))["receipts"][0]["value"]
    received = await lines(session, auth, f"je:auto:{order}:receive:r")

    r = await undo_receipt(client, auth, order, lot, key="u")

    assert r.status_code == 200, r.text
    assert r.json() == {"lot_item_id": lot, "quantity": 1.0, "value": value}
    # The exact reverse of the receipt's entry.
    assert await lines(session, auth, f"je:auto:{order}:unreceive:u") == sorted(
        (a, roles, c, d) for a, roles, d, c in received)
    s = await _state(session, auth, lot)
    assert (s["status"], s["quantity"]) == ("archived", 0)
    facts = await _state(session, auth, order)
    assert (facts["received_qty"], facts["received_lots"], facts["receipts"]) == (0, [], [])
    assert await _books_and_stock(session, auth, raw, order) == issued
    await assert_settled(client, session, auth)


async def test_receive_undo_receive_leaves_the_books_as_the_receipt_alone(client, session, auth):
    raw, _, order, (lot,) = await _received(client, session, auth)
    once = await _books_and_stock(session, auth, raw, order)
    assert (await undo_receipt(client, auth, order, lot, key="u")).status_code == 200
    again = (await receive(client, auth, order, 1, key="r-again")).json()["lot_item_id"]
    assert await _books_and_stock(session, auth, raw, order) == once
    assert round((await _state(session, auth, again))["cost_total"], 2) == float(
        (await _state(session, auth, order))["receipts"][0]["value"])
    await assert_settled(client, session, auth)


async def _move(client, session, auth, lot):
    r = await client.post("/companies/me/locations", headers=auth["headers"], json={"name": "Shelf", "type": "warehouse"})
    assert r.status_code in (200, 201), r.text
    r = await client.post(f"/items/{lot}/transfer", headers=auth["headers"], json={"to_location_id": r.json()["id"]})
    assert r.status_code == 200, r.text


async def _split(client, session, auth, lot):
    r = await client.post(f"/items/{lot}/split", headers=auth["headers"], json={"children": [{"quantity": 1}]})
    assert r.status_code == 200, r.text


async def _adjust(client, session, auth, lot):
    r = await client.post(f"/items/{lot}/adjust", headers=auth["headers"], json={"new_qty": 1})
    assert r.status_code == 200, r.text


async def _reserve(client, session, auth, lot):
    doc = await _invoice(client, session, auth, lot)
    r = await client.post(f"/docs/{doc}/reserve-lines", headers=auth["headers"],
                          json={"line_entity_ids": [lot], "new_status": "reserved"})
    assert r.status_code == 200, r.text


# An invoice books its goods' cost when it is finalized, while the lot stays on hand until it
# is fulfilled; the books oracle does not model that window, so those cases prove absence only.
_INVOICED = {"on_a_document", "reserved"}
_TOUCHES = {"sold": _sell, "moved": _move, "split": _split, "adjusted": _adjust,
            "on_a_document": lambda c, s, a, lot: _invoice(c, s, a, lot), "reserved": _reserve}


@pytest.mark.parametrize("touch", list(_TOUCHES))
async def test_undo_receipt_is_refused_once_the_lot_has_changed(client, session, auth, touch):
    raw, _, order, (lot,) = await _received(client, session, auth, take=(2.0,))
    await _TOUCHES[touch](client, session, auth, lot)
    await _refused(client, session, auth, (raw, order, lot),
                   lambda: undo_receipt(client, auth, order, lot, key="u"), 409, "output_changed",
                   settled=touch not in _INVOICED)


async def test_undo_receipt_refuses_a_lot_the_run_did_not_receive(client, session, auth):
    raw, _, order, _ = await _received(client, session, auth)
    await _refused(client, session, auth, (raw, order),
                   lambda: undo_receipt(client, auth, order, raw, key="u"), 422, "not_a_receipt")


async def test_undo_receipt_on_a_completed_run_asks_to_reopen_it_first(client, session, auth):
    raw, _, order, (lot,) = await _received(client, session, auth)
    assert (await complete(client, auth, order, key="c")).status_code == 200
    await _refused(client, session, auth, (raw, order, lot),
                   lambda: undo_receipt(client, auth, order, lot, key="u"), 409, "reopen_first")


async def test_undo_receipt_retried_with_its_key_moves_nothing_more(client, session, auth):
    raw, _, order, (first, second) = await _received(client, session, auth, take=(1.0, 1.0))
    done = await undo_receipt(client, auth, order, first, key="u")
    assert done.status_code == 200, done.text
    before = await snapshot(session, auth, raw, order, first, second)
    again = await undo_receipt(client, auth, order, first, key="u")
    assert again.status_code == 200 and again.json() == done.json(), again.text
    assert await snapshot(session, auth, raw, order, first, second) == before
    refusal(await undo_receipt(client, auth, order, second, key="u"), 409, "key_reused")
    assert await snapshot(session, auth, raw, order, first, second) == before
    # Undone once, the lot is no longer a receipt of the run.
    refusal(await undo_receipt(client, auth, order, first, key="u2"), 422, "not_a_receipt")
    await assert_settled(client, session, auth)


async def test_undo_receipt_on_a_locked_day_is_refused_and_changes_nothing(client, session, auth):
    raw, _, order, (lot,) = await _received(client, session, auth)
    await _locked(session, auth)
    before = await snapshot(session, auth, raw, order, lot)
    r = await undo_receipt(client, auth, order, lot, key="u")
    assert r.status_code == 422 and "locked" in r.text, r.text
    assert await snapshot(session, auth, raw, order, lot) == before


# ---------------------------------------------------------------------------
# Reopen a completed run
# ---------------------------------------------------------------------------

async def _completed(client, session, auth):
    """A run making 2: everything issued (100), one received, then completed with 2 of the 10
    components wasted, so completion receives the other, trues both lots up and books waste."""
    raw, item, order, (first,) = await _received(client, session, auth, per=5.0, qty=2.0)
    received = await _books_and_stock(session, auth, raw, order, first)
    assert (await complete(client, auth, order, key="c", waste_quantity=2)).status_code == 200
    second = next(lot for lot in (await _state(session, auth, order))["received_lots"] if lot != first)
    return raw, order, first, second, received


async def test_reopen_reverses_completion_exactly(client, session, auth):
    raw, order, first, second, _ = await _completed(client, session, auth)
    closed = await lines(session, auth, f"je:auto:{order}:complete:c")
    assert any(roles == (COGS,) for _, roles, _, _ in closed)
    facts = await _state(session, auth, order)

    r = await reopen(client, auth, order, key="o")

    assert r.status_code == 200, r.text
    assert await lines(session, auth, f"je:auto:{order}:reopen:o") == sorted(
        (a, roles, c, d) for a, roles, d, c in closed)
    after = await _state(session, auth, order)
    assert after["status"] == "in_progress" and after["is_in_production"] is True
    assert "closing" not in after and "wip_wasted" not in after and after.get("waste") is None
    assert float(after["wip_transferred"]) == sum(float(x["value"]) for x in facts["receipts"])
    for lot, receipt in zip((first, second), facts["receipts"]):
        assert round((await _state(session, auth, lot))["cost_total"], 2) == float(receipt["value"])
    await assert_settled(client, session, auth)


async def test_complete_reopen_complete_leaves_the_books_as_completing_once(client, session, auth):
    raw, order, first, second, _ = await _completed(client, session, auth)
    once = await _books_and_stock(session, auth, raw, order, first, second)
    assert (await reopen(client, auth, order, key="o")).status_code == 200
    assert (await complete(client, auth, order, key="c2", waste_quantity=2)).status_code == 200
    assert await _books_and_stock(session, auth, raw, order, first, second) == once
    await assert_settled(client, session, auth)


@pytest.mark.parametrize("touch", ["sold", "moved", "adjusted", "on_a_document"])
async def test_reopen_is_refused_once_an_output_lot_has_changed(client, session, auth, touch):
    raw, order, first, second, _ = await _completed(client, session, auth)
    await _TOUCHES[touch](client, session, auth, second)
    await _refused(client, session, auth, (raw, order, first, second),
                   lambda: reopen(client, auth, order, key="o"), 409, "output_changed",
                   settled=touch not in _INVOICED)


@pytest.mark.parametrize("state", ["planned", "in_progress", "cancelled"])
async def test_only_a_completed_run_can_be_reopened(client, session, auth, state):
    raw, _, order = await _job(client, auth)
    if state == "in_progress":
        assert (await issue(client, auth, order, [(raw, 1)], key="i")).status_code == 200
    if state == "cancelled":
        assert (await cancel(client, auth, order, key="x")).status_code == 200
    await _refused(client, session, auth, (raw, order), lambda: reopen(client, auth, order, key="o"),
                   409, "not_completed")


async def test_reopen_retried_with_its_key_changes_nothing_more(client, session, auth):
    raw, order, first, second, _ = await _completed(client, session, auth)
    assert (await reopen(client, auth, order, key="o")).status_code == 200
    before = await snapshot(session, auth, raw, order, first, second)
    again = await reopen(client, auth, order, key="o")
    assert again.status_code == 200, again.text
    assert await snapshot(session, auth, raw, order, first, second) == before
    await assert_settled(client, session, auth)


async def test_reopen_on_a_locked_day_is_refused_and_changes_nothing(client, session, auth):
    raw, order, first, second, _ = await _completed(client, session, auth)
    await _locked(session, auth)
    before = await snapshot(session, auth, raw, order, first, second)
    r = await reopen(client, auth, order, key="o")
    assert r.status_code == 422 and "locked" in r.text, r.text
    assert await snapshot(session, auth, raw, order, first, second) == before


# ---------------------------------------------------------------------------
# A whole run undone, step by step
# ---------------------------------------------------------------------------

async def test_undoing_every_step_gives_back_the_books_and_stock_exactly(client, session, auth):
    raw, item, order = await _job(client, auth, per=5.0, qty=2.0)
    start = await _books_and_stock(session, auth, raw, order)
    assert (await issue(client, auth, order, key="i")).status_code == 200
    issued = await _books_and_stock(session, auth, raw, order)
    first = (await receive(client, auth, order, 1, key="r")).json()["lot_item_id"]
    received = await _books_and_stock(session, auth, raw, order, first)
    assert (await complete(client, auth, order, key="c", waste_quantity=2)).status_code == 200
    second = next(lot for lot in (await _state(session, auth, order))["received_lots"] if lot != first)

    assert (await reopen(client, auth, order, key="o")).status_code == 200
    assert (await undo_receipt(client, auth, order, second, key="u2")).status_code == 200
    assert await _books_and_stock(session, auth, raw, order, first) == received
    assert (await undo_receipt(client, auth, order, first, key="u1")).status_code == 200
    assert await _books_and_stock(session, auth, raw, order) == issued
    assert (await give_back(client, auth, order, key="g")).status_code == 200
    assert (await cancel(client, auth, order, key="x")).status_code == 200

    end = await _books_and_stock(session, auth, raw, order)
    assert end["balances"] == start["balances"] and end[raw] == start[raw]
    assert end[order] == {**start[order], "status": "cancelled"}
    for lot in (first, second):
        s = await _state(session, auth, lot)
        assert (s["status"], s["quantity"]) == ("archived", 0)
    await assert_settled(client, session, auth)


