# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Waste is valued per component, at what that component cost when it was issued.

Completing a run names the components it wasted and how much of each. Each is valued at its
share of the value it carried into the run: all of it when everything issued was wasted,
otherwise in proportion to the quantity. Nothing else feeds the figure, not a later change in
the component's cost and not the other components' units or costs. The shorthand waste
quantity is for a run with one component; with several it is refused rather than guessed.
"""
from __future__ import annotations

import uuid

import pytest

from celerp.models.ledger import LedgerEntry
from celerp.models.projections import Projection
from mfg_runs import COGS, OPENING, PURCHASED, WIP, complete, issue, lines, product, receive, refusal, reopen, role, run, snapshot
from sqlalchemy import select
from stock_books import assert_settled
from test_cost_restatement import _item, _merge, _sell, _state, auth, ids  # noqa: F401  (fixtures)

pytestmark = pytest.mark.asyncio


async def _component(client, auth, cost: float, qty: float, unit: str) -> str:
    r = await client.post("/items", headers=auth["headers"], json={
        "sku": f"C-{uuid.uuid4().hex[:6]}", "name": "Component", "quantity": qty, "sell_by": unit,
        "cost_total": cost, "status": "available"})
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _two_component_run(client, auth, *, gold=(1000.0, 10.0, "gram"), bead=(10.0, 10.0, "piece")):
    """A run making 2, each taking 5 of a gold component and 5 of a bead component."""
    g = await _component(client, auth, *gold)
    b = await _component(client, auth, *bead)
    order = await run(client, auth, await product(client, auth, [(g, 5), (b, 5)]), 2)
    return g, b, order


async def _recost(session, auth, item: str, cost_total: float) -> None:
    """Give what is left of ``item`` a new cost. The app refuses to re-cost a lot already used, so
    this writes the lot directly: the books no longer carry it, and only the waste is checked."""
    session.expire_all()
    row = await session.get(Projection, {"company_id": auth["company_id"], "entity_id": item})
    row.state = {**row.state, "cost_total": cost_total, "cost_base": cost_total}
    await session.commit()


def _ob(debit: float, credit: float) -> tuple:
    return ("1130-OB", (OPENING,), debit, credit)


def _wip(debit: float, credit: float) -> tuple:
    return ("1130-WIP", (WIP,), debit, credit)


def _made(debit: float, credit: float) -> tuple:
    return ("1130-P", (PURCHASED,), debit, credit)


def _cogs(debit: float, credit: float) -> tuple:
    return ("5100", (COGS,), debit, credit)


async def _run_entries(session, auth, order: str, issue: str = "c:issue") -> dict[str, list[tuple]]:
    """The lines of the entries completing a run under key ``c`` posts: what it issued (by
    ``issue``'s key, or by completing), what it received, and what it wasted."""
    return {"issue": await lines(session, auth, f"je:auto:{order}:issue:{issue}"),
            "receive": await lines(session, auth, f"je:auto:{order}:receive:c:receive"),
            "complete": await lines(session, auth, f"je:auto:{order}:complete:c")}


def _waste(*pairs) -> list[dict]:
    return [{"item_id": i, "quantity": q} for i, q in pairs]


async def _identity(session, auth, order: str) -> dict:
    """What the run took in equals what its lots carry plus what it wasted, and it holds nothing."""
    s = await _state(session, auth, order)
    issued, moved, wasted = (float(s.get(k) or 0) for k in ("wip_issued", "wip_transferred", "wip_wasted"))
    assert round(issued - moved - wasted, 2) == 0, s
    return {"issued": issued, "transferred": moved, "wasted": wasted}


async def test_one_component_shorthand_is_valued_as_before(client, session, auth):
    """Two of the ten units issued (100.00) are wasted: 20.00, exactly as the shorthand always did."""
    raw = await _component(client, auth, 100.0, 10, "piece")
    order = await run(client, auth, await product(client, auth, [(raw, 5)]), 2)
    r = await complete(client, auth, order, key="c", waste_quantity=2, waste_unit="piece", waste_reason="spilt")
    assert r.status_code == 200, r.text
    assert await _identity(session, auth, order) == {"issued": 100.0, "transferred": 80.0, "wasted": 20.0}
    s = await _state(session, auth, order)
    assert s["waste"] == {"items": [{"item_id": raw, "quantity": 2.0, "value": "20.00"}],
                          "unit": "piece", "reason": "spilt"}
    await assert_settled(client, session, auth)


async def test_shorthand_unit_must_be_the_components_unit(client, session, auth):
    raw = await _component(client, auth, 100.0, 10, "piece")
    order = await run(client, auth, await product(client, auth, [(raw, 5)]), 2)
    before = await snapshot(session, auth, raw, order)
    refusal(await complete(client, auth, order, key="c", waste_quantity=2, waste_unit="gram"), 422, "waste_unit")
    assert await snapshot(session, auth, raw, order) == before


async def test_grams_and_pieces_are_each_valued_at_their_own_cost(client, session, auth):
    g, b, order = await _two_component_run(client, auth)
    cogs = await role(session, auth, COGS)
    r = await complete(client, auth, order, key="c", waste_items=_waste((g, 2), (b, 3)))
    assert r.status_code == 200, r.text
    # Gold went in at 100.00 a gram, beads at 1.00 each: 200.00 + 3.00 wasted of 1010.00.
    assert await _identity(session, auth, order) == {"issued": 1010.0, "transferred": 807.0, "wasted": 203.0}
    assert [ln for ln in await lines(session, auth, f"je:auto:{order}:complete:c") if ln[0] == cogs] == [
        (cogs, (COGS,), 203.0, 0.0)]
    lots = (await _state(session, auth, order))["received_lots"]
    assert round(sum([(await _state(session, auth, x))["cost_total"] for x in lots]), 2) == 807.0
    await assert_settled(client, session, auth)


@pytest.mark.parametrize("wasted,value", [("cheap", 1.0), ("dear", 1000.0)])
async def test_components_in_the_same_unit_keep_their_own_cost(client, session, auth, wasted, value):
    """One diamond costs a thousand beads. Wasting one of either costs what that one cost."""
    dear, cheap, order = await _two_component_run(
        client, auth, gold=(10000.0, 10.0, "piece"), bead=(10.0, 10.0, "piece"))
    item = {"cheap": cheap, "dear": dear}[wasted]
    r = await complete(client, auth, order, key="c", waste_items=_waste((item, 1)))
    assert r.status_code == 200, r.text
    assert (await _identity(session, auth, order))["wasted"] == value
    await assert_settled(client, session, auth)


async def test_shorthand_on_a_run_with_several_components_is_refused(client, session, auth):
    g, b, order = await _two_component_run(client, auth, gold=(10000.0, 10.0, "piece"))
    before = await snapshot(session, auth, g, b, order)
    for body in ({"waste_quantity": 1}, {"waste_quantity": 1, "waste_unit": "piece"}):
        detail = refusal(await complete(client, auth, order, key="c", **body), 422, "waste_ambiguous")
        assert sorted(detail["params"]["components"]) == sorted(
            [(await _state(session, auth, x))["sku"] for x in (g, b)])
        assert await snapshot(session, auth, g, b, order) == before


@pytest.mark.parametrize("body,key", [
    ({"waste_items": [{"quantity": 11}]}, "over_waste"),
    ({"waste_items": [{"quantity": 6}, {"quantity": 5}]}, "over_waste"),
    ({"waste_items": [{"item_id": "item:not-in-this-run", "quantity": 1}]}, "not_an_input"),
    ({"waste_items": [{"quantity": 1}], "waste_quantity": 1}, "waste_twice"),
])
async def test_waste_beyond_what_was_issued_is_refused_with_no_effect(client, session, auth, body, key):
    """A run that issued 10 grams of gold cannot waste more, however the request splits it."""
    g, b, order = await _two_component_run(client, auth)
    for line in body["waste_items"]:
        line.setdefault("item_id", g)
    assert (await issue(client, auth, order, key="i")).status_code == 200
    before = await snapshot(session, auth, g, b, order)
    refusal(await complete(client, auth, order, key="c", **body), 422, key)
    assert await snapshot(session, auth, g, b, order) == before


async def test_waste_beyond_what_completing_issues_is_refused_with_no_effect(client, session, auth):
    """Nothing issued yet: completing would issue 10 g, so 11 g wasted is refused and nothing is issued."""
    g, b, order = await _two_component_run(client, auth)
    before = await snapshot(session, auth, g, b, order)
    refusal(await complete(client, auth, order, key="c", waste_items=_waste((g, 11))), 422, "over_waste")
    assert await snapshot(session, auth, g, b, order) == before


async def test_a_cost_change_after_issue_does_not_change_the_waste(client, session, auth):
    g, b, order = await _two_component_run(client, auth, gold=(2000.0, 20.0, "gram"))
    assert (await issue(client, auth, order, key="i")).status_code == 200  # 10 g at 100.00
    await _recost(session, auth, g, 10.0)  # the 10 g left now cost 1.00 a gram
    r = await complete(client, auth, order, key="c", waste_items=_waste((g, 2)))
    assert r.status_code == 200, r.text
    assert (await _identity(session, auth, order))["wasted"] == 200.0
    assert await _run_entries(session, auth, order, issue="i") == {
        "issue": [_ob(0.0, 1010.0), _wip(1010.0, 0.0)],
        "receive": [_made(1010.0, 0.0), _wip(0.0, 1010.0)],
        "complete": [_made(0.0, 200.0), _cogs(200.0, 0.0)]}


@pytest.mark.parametrize("grams,value", [(4, 220.0), (10, 550.0)])
async def test_waste_after_two_issues_uses_everything_issued(client, session, auth, grams, value):
    """5 g go in at 100.00 a gram, the gold is re-costed to 10.00 a gram, and completing issues
    the other 5 g at that: 550.00 for 10 g. Four grams wasted are 220.00; all ten are 550.00."""
    g, b, order = await _two_component_run(client, auth, gold=(2000.0, 20.0, "gram"))
    assert (await issue(client, auth, order, [(g, 5)], key="i1")).status_code == 200
    await _recost(session, auth, g, 150.0)
    r = await complete(client, auth, order, key="c", waste_items=_waste((g, grams)))
    assert r.status_code == 200, r.text
    gold = next(i for i in (await _state(session, auth, order))["inputs"] if i["item_id"] == g)
    assert (gold["issued_qty"], gold["issued_value"]) == (10.0, "550.00")
    assert (await _identity(session, auth, order))["wasted"] == value
    assert await lines(session, auth, f"je:auto:{order}:issue:i1") == [_ob(0.0, 500.0), _wip(500.0, 0.0)]
    assert await _run_entries(session, auth, order) == {  # the other 5 g at 10.00 and the beads
        "issue": [_ob(0.0, 60.0), _wip(60.0, 0.0)],
        "receive": [_made(560.0, 0.0), _wip(0.0, 560.0)],
        "complete": [_made(0.0, value), _cogs(value, 0.0)]}


async def test_repeated_lines_are_one_line_in_component_order(client, session, auth):
    g, b, order = await _two_component_run(client, auth)
    r = await complete(client, auth, order, key="c", waste_items=_waste((g, 1), (b, 1), (g, 1)))
    assert r.status_code == 200, r.text
    expected = sorted([{"item_id": g, "quantity": 2.0, "value": "200.00"},
                       {"item_id": b, "quantity": 1.0, "value": "1.00"}], key=lambda x: x["item_id"])
    assert (await _state(session, auth, order))["waste"]["items"] == expected
    assert (await _identity(session, auth, order))["wasted"] == 201.0
    assert await _run_entries(session, auth, order) == {
        "issue": [_ob(0.0, 1010.0), _wip(1010.0, 0.0)],
        "receive": [_made(1010.0, 0.0), _wip(0.0, 1010.0)],
        "complete": [_made(0.0, 201.0), _cogs(201.0, 0.0)]}
    await assert_settled(client, session, auth)


async def test_the_same_waste_in_another_order_is_the_same_request(client, session, auth):
    g, b, order = await _two_component_run(client, auth)
    first = await complete(client, auth, order, key="c", waste_items=_waste((g, 2), (b, 1)))
    assert first.status_code == 200, first.text
    after = await snapshot(session, auth, g, b, order)
    again = await complete(client, auth, order, key="c", waste_items=_waste((b, 1), (g, 1), (g, 1)))
    assert again.status_code == 200, again.text
    assert await snapshot(session, auth, g, b, order) == after
    refusal(await complete(client, auth, order, key="c", waste_items=_waste((g, 3))), 409, "key_reused")
    session.expire_all()
    done = (await session.execute(select(LedgerEntry).where(
        LedgerEntry.entity_id == order, LedgerEntry.event_type == "mfg.order.completed"))).scalars().all()
    assert len(done) == 1
    assert await _run_entries(session, auth, order) == {  # booked once, as the first request
        "issue": [_ob(0.0, 1010.0), _wip(1010.0, 0.0)],
        "receive": [_made(1010.0, 0.0), _wip(0.0, 1010.0)],
        "complete": [_made(0.0, 201.0), _cogs(201.0, 0.0)]}
    await assert_settled(client, session, auth)


async def test_issued_value_is_finished_value_plus_waste_through_the_run_life(client, session, auth):
    """Completing, reopening, completing with other waste, then merging one lot and selling the
    other: at every step the run took in exactly what its lots carry plus what it wasted."""
    g, b, order = await _two_component_run(client, auth)
    assert (await issue(client, auth, order, key="i")).status_code == 200
    first = (await receive(client, auth, order, 1, key="r")).json()["lot_item_id"]

    assert (await complete(client, auth, order, key="c", waste_items=_waste((g, 2), (b, 3)))).status_code == 200
    assert await _identity(session, auth, order) == {"issued": 1010.0, "transferred": 807.0, "wasted": 203.0}
    await assert_settled(client, session, auth)

    assert (await reopen(client, auth, order, key="o")).status_code == 200
    s = await _state(session, auth, order)
    assert float(s["wip_issued"]) - float(s["wip_transferred"]) == 0 and not s.get("wip_wasted")
    await assert_settled(client, session, auth)

    assert (await complete(client, auth, order, key="c2", waste_items=_waste((g, 10)))).status_code == 200
    assert await _identity(session, auth, order) == {"issued": 1010.0, "transferred": 10.0, "wasted": 1000.0}
    lots = (await _state(session, auth, order))["received_lots"]
    assert round(sum([(await _state(session, auth, x))["cost_total"] for x in lots]), 2) == 10.0
    await assert_settled(client, session, auth)

    second = next(x for x in lots if x != first)
    sku = (await _state(session, auth, first))["sku"]
    await _merge(client, auth, [first, await _item(client, auth, 3.0, qty=1, sku=sku)])
    await _sell(client, session, auth, second)
    assert await _identity(session, auth, order) == {"issued": 1010.0, "transferred": 10.0, "wasted": 1000.0}
    await assert_settled(client, session, auth)
