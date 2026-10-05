# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Output received from a run that is still open keeps a cost completion can change.

Each lot a run receives takes a provisional share of the work in progress; completion sets
its final cost (waste entered then, for one, changes it). Until then the lot may only go
where that final cost can still reach it: sold whole or merged. Splitting it (directly or by
fulfilling part of it), transforming it, using it in another run, writing it off or counting
it down is refused with a message saying why, and the run then completes with every lot at
its final cost and the books settled.
"""
from __future__ import annotations

import uuid

import pytest

from mfg_runs import complete, issue, product, receive, refusal, run, snapshot
from stock_books import assert_settled
from test_cost_restatement import _item, _merge, _sell, _state

pytestmark = pytest.mark.asyncio


async def _cost(session, auth, item: str) -> float:
    return float((await _state(session, auth, item))["cost_total"])


async def open_output(client, session, auth, received: float = 2) -> tuple[str, str, str]:
    """A run of 4 that took ten of a 100.00 component and received ``received`` of its
    output into one lot, at the provisional 25.00 a unit."""
    raw = await _item(client, auth, 100.0, qty=10)
    made = await product(client, auth, [(raw, 2.5)])
    order = await run(client, auth, made, 4)
    assert (await issue(client, auth, order)).status_code == 200
    r = await receive(client, auth, order, received, key=f"lot-{uuid.uuid4().hex[:6]}")
    assert r.status_code == 200, r.text
    lot = r.json()["lot_item_id"]
    assert await _cost(session, auth, lot) == 25.0 * received
    return made, order, lot


async def completes_recosted(client, session, auth, order: str, settled: bool = True) -> None:
    """Completed with two of the ten components wasted: what was finished is 80.00, 20.00 a
    unit, so every lot received at 25.00 a unit is re-costed down."""
    r = await complete(client, auth, order, key="done", waste_quantity=2, waste_reason="scrap")
    assert r.status_code == 200, r.text
    assert (await _state(session, auth, order))["status"] == "completed"
    if settled:
        await assert_settled(client, session, auth)


async def refused_unchanged(session, auth, act, *watch: str) -> None:
    before = await snapshot(session, auth, *watch)
    detail = refusal(await act(), 409, "output_in_production")
    # The refusal comes from the event that would move the cost, after the request may have
    # written other things (a split's new lots, a write-off's finalizing): its session closes
    # without committing, which the test session shared with the app stands in for here.
    await session.rollback()
    assert "still open" in detail["message"], detail
    assert await snapshot(session, auth, *watch) == before


async def test_split_waits_for_completion(client, session, auth):
    _, order, lot = await open_output(client, session, auth)

    await refused_unchanged(session, auth, lambda: client.post(
        f"/items/{lot}/split", headers=auth["headers"], json={"children": [{"quantity": 1}]}), order, lot)

    await completes_recosted(client, session, auth, order)
    assert await _cost(session, auth, lot) == 40.0
    r = await client.post(f"/items/{lot}/split", headers=auth["headers"], json={"children": [{"quantity": 1}]})
    assert r.status_code == 200, r.text


async def test_partial_fulfillment_waits_for_completion(client, session, auth):
    """Fulfilling one of the lot's two units would carve that unit off it."""
    _, order, lot = await open_output(client, session, auth)
    state = await _state(session, auth, lot)
    r = await client.post("/docs", headers=auth["headers"], json={
        "doc_type": "invoice", "ref_id": f"INV-{uuid.uuid4().hex[:6]}", "total": 500.0,
        "line_items": [{"sku": state["sku"], "name": "Lot", "quantity": 1, "unit_price": 500.0, "entity_id": lot}]})
    assert r.status_code == 200, r.text
    doc = r.json()["id"]
    assert (await client.post(f"/docs/{doc}/finalize", headers=auth["headers"])).status_code == 200

    await refused_unchanged(session, auth, lambda: client.post(
        f"/docs/{doc}/fulfill-lines", headers=auth["headers"], json={"line_entity_ids": [lot]}), order, lot, doc)

    # The finalized invoice has recognized the cost of the unit it has not shipped yet, so the
    # books carry the stock again once it ships.
    await completes_recosted(client, session, auth, order, settled=False)
    assert await _cost(session, auth, lot) == 40.0
    r = await client.post(f"/docs/{doc}/fulfill-lines", headers=auth["headers"], json={"line_entity_ids": [lot]})
    assert r.status_code == 200, r.text
    await assert_settled(client, session, auth)


async def test_transform_waits_for_completion(client, session, auth):
    _, order, lot = await open_output(client, session, auth)

    await refused_unchanged(session, auth, lambda: client.post(
        f"/items/{lot}/transform", headers=auth["headers"], json={
            "child_sku": f"TX-{uuid.uuid4().hex[:6]}", "child_category": "Processed", "child_sell_by": "piece",
            "child_quantity": 2}), order, lot)

    await completes_recosted(client, session, auth, order)
    assert await _cost(session, auth, lot) == 40.0


async def test_use_in_another_run_waits_for_completion(client, session, auth):
    _, order, lot = await open_output(client, session, auth)
    other = await _item(client, auth, 0.0, qty=0, sku=f"FG-{uuid.uuid4().hex[:6]}")
    r = await client.post("/manufacturing", headers=auth["headers"], json={
        "description": "Assembly", "output_item_id": other, "quantity": 1, "inputs": [{"item_id": lot, "quantity": 1}]})
    assert r.status_code == 200, r.text
    second = r.json()["id"]

    await refused_unchanged(session, auth, lambda: issue(client, auth, second, key="use"), order, second, lot)

    await completes_recosted(client, session, auth, order)
    assert await _cost(session, auth, lot) == 40.0
    assert (await issue(client, auth, second, key="use-after")).status_code == 200
    await assert_settled(client, session, auth)


async def test_write_off_waits_for_completion(client, session, auth):
    _, order, lot = await open_output(client, session, auth)
    r = await client.post("/lists/writeoff", headers=auth["headers"], json={"entity_ids": [lot]})
    assert r.status_code == 200, r.text
    wo = r.json()["id"]
    r = await client.post(f"/lists/{wo}/writeoff-line", headers=auth["headers"],
                          json={"item_id": lot, "qty_out": 2, "account": "6950"})
    assert r.status_code == 200, r.text

    await refused_unchanged(session, auth, lambda: client.post(
        f"/lists/{wo}/write-off", headers=auth["headers"]), order, lot)

    await completes_recosted(client, session, auth, order)
    assert await _cost(session, auth, lot) == 40.0


async def test_counting_it_down_waits_for_completion(client, session, auth):
    """A count that lowers the lot moves part of its cost out of it, just as a split does."""
    _, order, lot = await open_output(client, session, auth)

    await refused_unchanged(session, auth, lambda: client.post(
        f"/items/{lot}/adjust", headers=auth["headers"], json={"new_qty": 1}), order, lot)

    await completes_recosted(client, session, auth, order)
    assert await _cost(session, auth, lot) == 40.0


async def test_whole_sale_and_merge_stay_allowed(client, session, auth):
    """One lot sold whole and two merged before the run completes: completion re-costs the
    sale and the merge result."""
    _, order, sold = await open_output(client, session, auth, received=1)
    lots = [(await receive(client, auth, order, 1, key=k)).json()["lot_item_id"] for k in ("b", "c")]
    await _sell(client, session, auth, sold)
    merged = await _merge(client, auth, lots)
    assert await _cost(session, auth, merged) == 50.0

    await completes_recosted(client, session, auth, order)

    assert await _cost(session, auth, sold) == 20.0
    assert await _cost(session, auth, merged) == 40.0


async def test_a_merge_result_waits_for_completion(client, session, auth):
    """Stock merged from a lot of an open run carries that lot's cost still to be set."""
    _, order, first = await open_output(client, session, auth, received=1)
    second = (await receive(client, auth, order, 1, key="b")).json()["lot_item_id"]
    merged = await _merge(client, auth, [first, second])

    await refused_unchanged(session, auth, lambda: client.post(
        f"/items/{merged}/split", headers=auth["headers"], json={"children": [{"quantity": 1}]}), order, merged)

    await completes_recosted(client, session, auth, order)
    assert await _cost(session, auth, merged) == 40.0
