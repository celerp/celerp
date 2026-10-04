# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A production run carries the value of the materials in it.

Issuing components moves the stock value that left each one onto the run's work in
progress account, in an entry of its own. Receiving output moves the run's share of
that value onto the new lot. Completing trues the lots up to the final cost, sends
waste to cost of goods sold, and leaves the run holding nothing. Each successful step
ends with the books carrying exactly the stock and the work in progress; each refused
step leaves everything exactly as it was.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from celerp.accounting_roles import LOT_ACCOUNT_FIELD, ROLES_KEY
from celerp.events.engine import emit_event
from celerp.services.company_lock import locked_company
from mfg_runs import (
    COGS, OPENING, PURCHASED, WIP, complete, issue, lines, product, receive, refusal, role, run,
    set_settings, snapshot,
)
from stock_books import assert_settled
from test_cost_restatement import _sell
from test_cost_restatement import TZ, _item, _merge, _state, auth, ids  # noqa: F401  (auth and ids are fixtures)
from test_money_stock_and_contact_invariants import _account_net
from test_receipt_accounting import _doc
from test_receipt_accounting import _receive as _po_receive


async def _job(client, session, auth, *, cost=100.0, stock=10.0, per=5.0, qty=2.0):
    """One component lot (``stock`` units costing ``cost``) and a run making ``qty`` of a
    product that takes ``per`` of it each."""
    raw = await _item(client, auth, cost, qty=stock)
    item = await product(client, auth, [(raw, per)])
    order = await run(client, auth, item, qty)
    return raw, item, order


async def _run_facts(session, auth, order):
    s = await _state(session, auth, order)
    return {k: s.get(k) for k in ("status", "wip_issued", "wip_transferred", "wip_wasted", "wip_account_code")}


# ---------------------------------------------------------------------------
# Issue
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_issue_moves_the_value_that_left_the_component_onto_the_run(client, session, auth):
    raw, _, order = await _job(client, session, auth)
    ob, wip = await role(session, auth, OPENING), await role(session, auth, WIP)
    assert (await _state(session, auth, raw))[LOT_ACCOUNT_FIELD] == ob

    r = await issue(client, auth, order, [(raw, 4)], key="k1")
    assert r.status_code == 200, r.text
    assert await lines(session, auth, f"je:auto:{order}:issue:k1") == sorted([
        (ob, (OPENING,), 0.0, 40.0), (wip, (WIP,), 40.0, 0.0)])
    assert await _run_facts(session, auth, order) == {
        "status": "in_progress", "wip_issued": "40.00", "wip_transferred": None, "wip_wasted": None,
        "wip_account_code": wip}
    assert (await _state(session, auth, raw))["quantity"] == 6
    await assert_settled(client, session, auth)

    r = await issue(client, auth, order, [(raw, 6)], key="k2")
    assert r.status_code == 200, r.text
    assert await lines(session, auth, f"je:auto:{order}:issue:k2") == sorted([
        (ob, (OPENING,), 0.0, 60.0), (wip, (WIP,), 60.0, 0.0)])
    assert (await _run_facts(session, auth, order))["wip_issued"] == "100.00"
    await assert_settled(client, session, auth)


@pytest.mark.asyncio
async def test_issue_credits_each_component_its_own_inventory_account(client, session, auth):
    opening = await _item(client, auth, 30.0, qty=3)
    po = await _doc(client, auth, "purchase_order", [
        {"sku": "BEAD-MFG", "name": "Beads", "quantity": 4, "unit_price": 5.0}])
    r = await _po_receive(client, auth, po, {"po_line_index": 0, "sku": "BEAD-MFG", "name": "Beads",
                                             "quantity_received": 4})
    assert r.status_code == 200, r.text
    bought = (await _state(session, auth, po))["received_item_ids"][0]
    ob, p, wip = (await role(session, auth, OPENING), await role(session, auth, PURCHASED),
                  await role(session, auth, WIP))
    assert (await _state(session, auth, bought))[LOT_ACCOUNT_FIELD] == p
    item = await product(client, auth, [(opening, 3), (bought, 4)])
    order = await run(client, auth, item, 1)

    # The same component twice in one request is one issue of the total.
    r = await issue(client, auth, order, [(opening, 1), (bought, 4), (opening, 2)], key="k")
    assert r.status_code == 200, r.text
    assert await lines(session, auth, f"je:auto:{order}:issue:k") == sorted([
        (ob, (OPENING,), 0.0, 30.0), (p, (PURCHASED,), 0.0, 20.0), (wip, (WIP,), 50.0, 0.0)])
    await assert_settled(client, session, auth)


@pytest.mark.asyncio
async def test_issue_retried_with_its_key_moves_nothing_more(client, session, auth):
    raw, _, order = await _job(client, session, auth)
    first = await issue(client, auth, order, [(raw, 4)], key="k")
    assert first.status_code == 200, first.text
    before = await snapshot(session, auth, raw, order)
    again = await issue(client, auth, order, [(raw, 4)], key="k")
    assert again.status_code == 200, again.text
    assert again.json() == first.json()
    assert await snapshot(session, auth, raw, order) == before

    reused = await issue(client, auth, order, [(raw, 5)], key="k")
    refusal(reused, 409, "key_reused")
    assert await snapshot(session, auth, raw, order) == before
    await assert_settled(client, session, auth)


async def _refused_issue(client, session, auth, raw, order, status, key, items=None):
    before = await snapshot(session, auth, raw, order)
    r = await issue(client, auth, order, items if items is not None else [(raw, 4)], key="x")
    detail = refusal(r, status, key)
    assert await snapshot(session, auth, raw, order) == before
    await assert_settled(client, session, auth)
    return detail


@pytest.mark.asyncio
async def test_issue_refuses_more_than_is_free_in_stock(client, session, auth):
    raw, _, order = await _job(client, session, auth, stock=3)
    detail = await _refused_issue(client, session, auth, raw, order, 409, "insufficient_stock")
    assert detail["params"]["available"] == 3


@pytest.mark.asyncio
async def test_issue_refuses_stock_reserved_for_something_else(client, session, auth):
    raw, _, order = await _job(client, session, auth)
    r = await client.post(f"/items/{raw}/reserve", headers=auth["headers"], json={"quantity": 8})
    assert r.status_code == 200, r.text
    detail = await _refused_issue(client, session, auth, raw, order, 409, "insufficient_stock")
    assert detail["params"]["available"] == 2


@pytest.mark.asyncio
async def test_issue_refuses_more_than_the_run_needs(client, session, auth):
    raw, _, order = await _job(client, session, auth)
    await _refused_issue(client, session, auth, raw, order, 409, "over_issue", [(raw, 11)])


@pytest.mark.asyncio
async def test_issue_refuses_something_that_is_not_a_component(client, session, auth):
    raw, _, order = await _job(client, session, auth)
    other = await _item(client, auth, 5.0, qty=5)
    await _refused_issue(client, session, auth, raw, order, 422, "not_an_input", [(other, 1)])


@pytest.mark.asyncio
async def test_issue_refuses_a_draft_component(client, session, auth):
    raw, _, order = await _job(client, session, auth)
    r = await client.post("/items/bulk/revert-to-draft", headers=auth["headers"], json={"entity_ids": [raw]})
    assert r.status_code == 200, r.text
    assert (await _state(session, auth, raw))["status"] == "draft"
    await _refused_issue(client, session, auth, raw, order, 422, "item_draft")


@pytest.mark.asyncio
async def test_issue_refuses_consigned_in_stock(client, session, auth):
    # Goods held on consignment for a supplier, as a consignment-in receipt records them.
    raw = f"item:{uuid.uuid4()}"
    await emit_event(session, company_id=auth["company_id"], entity_id=raw, entity_type="item",
                     event_type="item.created", data={
                         "sku": f"CIN-{uuid.uuid4().hex[:6]}", "name": "Held stone", "quantity": 10,
                         "sell_by": "piece", "status": "available", "cost_total": 100.0, "consignment_flag": "in"},
                     actor_id=auth["user_id"], location_id=None, source="api",
                     idempotency_key=str(uuid.uuid4()), metadata_={})
    await session.commit()
    order = await run(client, auth, await product(client, auth, [(raw, 5)]), 2)
    await _refused_issue(client, session, auth, raw, order, 409, "item_unavailable")


@pytest.mark.parametrize("case", ["unmapped", "deactivated"])
@pytest.mark.asyncio
async def test_issue_refuses_when_work_in_progress_has_no_usable_account(client, session, auth, case):
    from celerp_accounting.models import Account
    from sqlalchemy import update

    raw, _, order = await _job(client, session, auth)
    if case == "unmapped":
        company = await locked_company(session, auth["company_id"])
        roles = {k: v for k, v in company.settings[ROLES_KEY].items() if k != WIP}
        company.settings = {**company.settings, ROLES_KEY: roles}
    else:
        await session.execute(update(Account).where(
            Account.company_id == auth["company_id"], Account.code == await role(session, auth, WIP))
            .values(is_active=False))
    await session.commit()
    before = await snapshot(session, auth, raw, order)
    r = await issue(client, auth, order, [(raw, 4)], key="x")
    refusal(r, 409, "wip_account_missing")
    assert r.headers["X-Celerp-Fix"] == "/settings/accounting?tab=posting-accounts"
    assert await snapshot(session, auth, raw, order) == before


@pytest.mark.asyncio
async def test_issue_refuses_a_day_inside_a_locked_period(client, session, auth):
    raw, _, order = await _job(client, session, auth)
    today = datetime.now(ZoneInfo(TZ)).date()
    await set_settings(session, auth, lock_date=(today + timedelta(days=1)).isoformat())
    before = await snapshot(session, auth, raw, order)
    r = await issue(client, auth, order, [(raw, 4)], key="x")
    assert r.status_code == 422 and "locked" in r.text, r.text
    assert await snapshot(session, auth, raw, order) == before


# ---------------------------------------------------------------------------
# Receive
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_receiving_three_then_two_moves_what_receiving_five_would(client, session, auth):
    raw, item, order = await _job(client, session, auth, cost=100.0, stock=10, per=2, qty=5)
    p, wip = await role(session, auth, PURCHASED), await role(session, auth, WIP)
    assert (await issue(client, auth, order, key="i")).status_code == 200

    r = await receive(client, auth, order, 3, key="r1")
    assert r.status_code == 200, r.text
    lot1 = r.json()["lot_item_id"]
    assert await lines(session, auth, f"je:auto:{order}:receive:r1") == sorted([
        (p, (PURCHASED,), 60.0, 0.0), (wip, (WIP,), 0.0, 60.0)])
    s = await _state(session, auth, lot1)
    assert (s["quantity"], s["cost_total"], s[LOT_ACCOUNT_FIELD], s["parent_item_id"]) == (3, 60.0, p, item)
    await assert_settled(client, session, auth)

    r = await receive(client, auth, order, 2, key="r2")
    assert r.status_code == 200, r.text
    lot2 = r.json()["lot_item_id"]
    assert await lines(session, auth, f"je:auto:{order}:receive:r2") == sorted([
        (p, (PURCHASED,), 40.0, 0.0), (wip, (WIP,), 0.0, 40.0)])
    assert (await _state(session, auth, lot2))["cost_total"] == 40.0
    facts = await _run_facts(session, auth, order)
    assert facts["status"] == "completed" and facts["wip_transferred"] == "100.00"
    await assert_settled(client, session, auth)


@pytest.mark.asyncio
async def test_receiving_a_third_shares_the_value_and_the_last_receipt_takes_the_rest(client, session, auth):
    raw, item, order = await _job(client, session, auth, cost=100.0, stock=3, per=1, qty=3)
    assert (await issue(client, auth, order, key="i")).status_code == 200
    costs = []
    for k in ("a", "b", "c"):
        r = await receive(client, auth, order, 1, key=k)
        assert r.status_code == 200, r.text
        costs.append((await _state(session, auth, r.json()["lot_item_id"]))["cost_total"])
        await assert_settled(client, session, auth)
    # A third of what is left each time: 33.33, then half of 66.67, then the rest.
    assert costs == [33.33, 33.34, 33.33]


@pytest.mark.asyncio
async def test_receive_retried_with_its_key_returns_the_same_lot(client, session, auth):
    raw, item, order = await _job(client, session, auth, stock=10, per=2, qty=5)
    assert (await issue(client, auth, order, key="i")).status_code == 200
    first = await receive(client, auth, order, 2, key="r")
    assert first.status_code == 200, first.text
    before = await snapshot(session, auth, raw, order, first.json()["lot_item_id"])
    again = await receive(client, auth, order, 2, key="r")
    assert again.status_code == 200 and again.json() == first.json(), again.text
    assert await snapshot(session, auth, raw, order, first.json()["lot_item_id"]) == before
    await assert_settled(client, session, auth)


@pytest.mark.parametrize("case", ["before_issue", "over_receipt", "zero"])
@pytest.mark.asyncio
async def test_receive_refusals_leave_everything_as_it_was(client, session, auth, case):
    raw, item, order = await _job(client, session, auth, stock=10, per=2, qty=5)
    if case != "before_issue":
        assert (await issue(client, auth, order, key="i")).status_code == 200
    before = await snapshot(session, auth, raw, order, item)
    qty, status, key = {"before_issue": (2, 409, "issue_first"), "over_receipt": (6, 409, "over_receipt"),
                        "zero": (0, 422, "receive_quantity")}[case]
    refusal(await receive(client, auth, order, qty, key="r"), status, key)
    assert await snapshot(session, auth, raw, order, item) == before
    await assert_settled(client, session, auth)


# ---------------------------------------------------------------------------
# Complete
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_complete_sends_waste_to_cost_of_goods_sold_and_leaves_the_run_empty(client, session, auth):
    raw, item, order = await _job(client, session, auth, cost=100.0, stock=10, per=5, qty=2)
    p, cogs = await role(session, auth, PURCHASED), await role(session, auth, COGS)
    assert (await issue(client, auth, order, key="i")).status_code == 200
    r = await receive(client, auth, order, 1, key="r")
    lot = r.json()["lot_item_id"]
    assert (await _state(session, auth, lot))["cost_total"] == 50.0

    r = await complete(client, auth, order, key="c", waste_quantity=2, waste_reason="spilt")
    assert r.status_code == 200, r.text
    # Completing received the other unit at 50, emptying the run. 2 of the 10 units issued were
    # wasted: 20 to cost of goods sold, and each lot gives back 10 of what it took.
    lot2 = next(x for x in (await _state(session, auth, order))["received_lots"] if x != lot)
    assert await lines(session, auth, f"je:auto:{order}:complete:c") == sorted([
        (p, (PURCHASED,), 0.0, 20.0), (cogs, (COGS,), 20.0, 0.0)])
    assert [(await _state(session, auth, x))["cost_total"] for x in (lot, lot2)] == [40.0, 40.0]
    facts = await _run_facts(session, auth, order)
    assert (facts["status"], facts["wip_transferred"], facts["wip_wasted"]) == ("completed", "80.00", "20.00")
    await assert_settled(client, session, auth)


@pytest.mark.asyncio
async def test_an_older_run_with_no_output_does_not_go_ahead(client, session, auth):
    """A run an older release created without naming a product has nothing to receive into,
    so it issues, receives and completes nothing until its product is chosen."""
    raw = await _item(client, auth, 100.0, qty=10)
    order = f"mfg:{uuid.uuid4()}"
    await emit_event(session, company_id=auth["company_id"], entity_id=order, entity_type="mfg_order",
                     event_type="mfg.order.created",
                     data={"description": "Trial batch", "inputs": [{"item_id": raw, "quantity": 10}],
                           "expected_outputs": [{"sku": "TRIAL", "name": "Trial", "quantity": 1.0}]},
                     actor_id=auth["user_id"], location_id=None, source="api", idempotency_key=str(uuid.uuid4()),
                     metadata_={})
    await session.commit()

    before = await snapshot(session, auth, raw, order)
    refusal(await issue(client, auth, order, key="i"), 409, "no_output")
    refusal(await receive(client, auth, order, 1, key="r"), 409, "no_output")
    refusal(await complete(client, auth, order, key="c", waste_quantity=10, waste_reason="trial"), 409, "no_output")
    assert await snapshot(session, auth, raw, order) == before
    await assert_settled(client, session, auth)


@pytest.mark.asyncio
async def test_complete_trues_up_a_lot_sold_before_the_run_finished(client, session, auth):
    raw, item, order = await _job(client, session, auth, cost=100.0, stock=10, per=5, qty=2)
    cogs = await role(session, auth, COGS)
    assert (await issue(client, auth, order, key="i")).status_code == 200
    lot = (await receive(client, auth, order, 1, key="r")).json()["lot_item_id"]
    await _sell(client, session, auth, lot)
    assert (await _state(session, auth, lot))["status"] == "sold"

    sold_at = await _account_net(session, auth["company_id"], cogs)
    r = await complete(client, auth, order, key="c", waste_quantity=2)
    assert r.status_code == 200, r.text
    # The lot was sold at 50 and should have cost 40: its cost of goods sold gives back 10, and
    # the waste adds 20.
    assert (await _state(session, auth, lot))["cost_total"] == 40.0
    assert await _account_net(session, auth["company_id"], cogs) == sold_at + 10.0
    await assert_settled(client, session, auth)


@pytest.mark.asyncio
async def test_complete_follows_a_lot_merged_before_the_run_finished(client, session, auth):
    raw, item, order = await _job(client, session, auth, cost=100.0, stock=10, per=5, qty=2)
    assert (await issue(client, auth, order, key="i")).status_code == 200
    lot = (await receive(client, auth, order, 1, key="r")).json()["lot_item_id"]
    other = await _item(client, auth, 10.0, qty=1, sku=(await _state(session, auth, lot))["sku"])
    merged = await _merge(client, auth, [lot, other])
    await assert_settled(client, session, auth)

    r = await complete(client, auth, order, key="c", waste_quantity=2)
    assert r.status_code == 200, r.text
    # The lot came in at 50 and should have cost 40; the merged lot takes the difference.
    assert (await _state(session, auth, merged))["cost_total"] == 50.0
    await assert_settled(client, session, auth)


@pytest.mark.asyncio
async def test_complete_keeps_the_work_in_progress_account_the_run_started_on(client, session, auth):
    raw, item, order = await _job(client, session, auth, cost=100.0, stock=10, per=5, qty=2)
    first = await role(session, auth, WIP)
    assert (await issue(client, auth, order, key="i")).status_code == 200
    r = await client.post("/accounting/accounts", headers=auth["headers"], json={
        "code": "1135", "name": "Production in progress", "account_type": "asset"})
    assert r.status_code in (200, 201), r.text
    r = await client.put(f"/accounting/posting-accounts/{WIP}", headers=auth["headers"], json={"code": "1135"})
    assert r.status_code == 200, r.text
    # A run started after the change books to the new account.
    later_raw = await _item(client, auth, 10.0, qty=1)
    later = await run(client, auth, await product(client, auth, [(later_raw, 1)]), 1)
    assert (await issue(client, auth, later, key="i")).status_code == 200
    assert (await _run_facts(session, auth, later))["wip_account_code"] == "1135"

    r = await complete(client, auth, order, key="c")
    assert r.status_code == 200, r.text
    wip_lines = [line for line in await lines(session, auth, f"je:auto:{order}:receive:c:receive")
                 if WIP in line[1]]
    assert [line[0] for line in wip_lines] == [first]
    await assert_settled(client, session, auth)


@pytest.mark.asyncio
async def test_complete_twice_and_cancel_after_movement_change_nothing(client, session, auth):
    raw, item, order = await _job(client, session, auth)
    assert (await issue(client, auth, order, [(raw, 4)], key="i")).status_code == 200

    before = await snapshot(session, auth, raw, order)
    r = await client.post(f"/manufacturing/{order}/cancel", headers=auth["headers"], json={"idempotency_key": "x"})
    refusal(r, 409, "cancel_moved")
    assert await snapshot(session, auth, raw, order) == before

    r = await complete(client, auth, order, key="c")
    assert r.status_code == 200, r.text
    await assert_settled(client, session, auth)
    done = await snapshot(session, auth, raw, order)
    again = await complete(client, auth, order, key="c")
    assert again.status_code == 200, again.text
    assert await snapshot(session, auth, raw, order) == done
    refusal(await complete(client, auth, order, key="other"), 409, "run_closed")
    assert await snapshot(session, auth, raw, order) == done


@pytest.mark.asyncio
async def test_cancel_before_anything_moved_is_allowed(client, session, auth):
    raw, item, order = await _job(client, session, auth)
    r = await client.post(f"/manufacturing/{order}/cancel", headers=auth["headers"],
                          json={"reason": "not needed", "idempotency_key": "x"})
    assert r.status_code == 200, r.text
    assert (await _state(session, auth, order))["status"] == "cancelled"
    await assert_settled(client, session, auth)


@pytest.mark.asyncio
async def test_one_tap_build_books_every_step(client, session, auth):
    raw = await _item(client, auth, 100.0, qty=10)
    item = await product(client, auth, [(raw, 5)])
    r = await client.post(f"/manufacturing/items/{item}/build", headers=auth["headers"],
                          json={"quantity": 2, "complete": True, "idempotency_key": "b"})
    assert r.status_code == 200, r.text
    order = r.json()["id"]
    facts = await _run_facts(session, auth, order)
    assert (facts["status"], facts["wip_issued"], facts["wip_transferred"]) == ("completed", "100.00", "100.00")
    lots = (await _state(session, auth, order))["received_lots"]
    assert [(await _state(session, auth, lot))["cost_total"] for lot in lots] == [100.0]
    await assert_settled(client, session, auth)
