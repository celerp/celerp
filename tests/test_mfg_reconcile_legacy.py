# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Reconciling a run an older release received output from before value was tracked.

That release put each receipt into a new lot of the product at a provisional cost (the
standard cost of what was issued, spread over what was received so far) and booked nothing:
the component's value stayed on its inventory account, the lot recording it. On this release
Accounting places the lot like any other stock, and the run waits for reconciling. Reconciling
it records what was issued; each lot it received is restated to the share a receipt takes
today, the run holds the rest, and the books carry the stock and the work in progress exactly
as after a live receipt, before and after the run completes.
"""
from __future__ import annotations

import re
import uuid

import pytest

from celerp.accounting_roles import AccountRole
from celerp.events.engine import emit_event
from celerp.services.auto_je import _emit_auto_posted_je
from mfg_runs import PURCHASED, complete, receive, refusal, role, snapshot
from stock_books import assert_settled
from test_cost_restatement import _fulfil, _invoice, _merge, _state, auth, ids  # noqa: F401  (fixtures)
from test_mfg_reconcile import reconcile
from test_mfg_wip_upgrade import _facts, _older_issue, _older_receive, _upgrade, in_production_slot  # noqa: F401
from mfg_runs import product, run
from test_posting_roles_older_stock import _older_release

pytestmark = pytest.mark.asyncio

RETAINED = AccountRole.RETAINED_EARNINGS.value


async def _older_stock(session, auth, cost: float, qty: float) -> str:
    """A component an older release bought and booked onto its purchased-inventory account."""
    cid, uid = auth["company_id"], auth["user_id"]
    po, lot = f"doc:{uuid.uuid4()}", f"item:{uuid.uuid4()}"
    await emit_event(session, company_id=cid, entity_id=lot, entity_type="item", event_type="item.created",
                     data={"sku": f"RAW-{uuid.uuid4().hex[:6]}", "name": "Raw", "quantity": qty,
                           "sell_by": "piece", "status": "available", "cost_total": cost},
                     actor_id=uid, location_id=None, source="api", idempotency_key=f"{po}:line:0",
                     metadata_={"source_doc": po})
    await _emit_auto_posted_je(
        session, company_id=cid, user_id=uid, je_id=f"je:auto:{po}:rcv:1",
        idem_create=f"{po}:rcv:c", idem_posted=f"{po}:rcv:p", memo=f"Auto JE for {po} received",
        entries=[{"account": "1130-P", "debit": cost, "credit": 0.0},
                 {"account": "2110", "debit": 0.0, "credit": cost}], metadata_={})
    await session.commit()
    return lot


async def _older_run(client, session, auth, *receipts: float):
    """An older run of 2 that took all ten of a 100.00 component and received ``receipts``."""
    await _older_release(session, auth)
    raw = await _older_stock(session, auth, 100.0, 10)
    order = await run(client, auth, await product(client, auth, [(raw, 5)]), 2)
    await _older_issue(session, auth, order, raw, 10)
    lots = [await _older_receive(session, auth, order, q) for q in receipts]
    await _upgrade(session)
    assert (await _facts(session, auth, order))["wip_unresolved"] == "received before tracking"
    return raw, order, lots


async def _cost(session, auth, item: str) -> float:
    return (await _state(session, auth, item))["cost_total"]


async def _finish(client, session, auth, order: str) -> None:
    if (await _state(session, auth, order))["status"] != "completed":
        r = await complete(client, auth, order, key="done")
        assert r.status_code == 200, r.text
    assert (await _state(session, auth, order))["status"] == "completed"
    await assert_settled(client, session, auth)


async def test_a_partly_received_run_restates_its_lot_and_holds_the_rest(client, session, auth):
    raw, order, [lot] = await _older_run(client, session, auth, 1)
    assert await _cost(session, auth, lot) == 100.0
    await assert_settled(client, session, auth)
    p = await role(session, auth, PURCHASED)
    needs = (await client.get(f"/manufacturing/{order}/reconcile", headers=auth["headers"])).json()
    assert [c["item_id"] for c in needs["components"]] == [raw]
    assert [(r["lot_item_id"], r["quantity"], float(r["value"]), r["sku"]) for r in needs["received"]] == [
        (lot, 1.0, 100.0, (await _state(session, auth, lot))["sku"])]

    r = await reconcile(client, auth, order, [(raw, 100.0)], p)

    assert r.status_code == 200, r.text
    assert await _cost(session, auth, lot) == 50.0
    assert (await _state(session, auth, order))["wip_issued"] == "100.00"
    await assert_settled(client, session, auth)
    r = await receive(client, auth, order, 1, key="out")
    assert r.status_code == 200, r.text
    await _finish(client, session, auth, order)
    assert await _cost(session, auth, lot) == 50.0
    assert await _cost(session, auth, r.json()["lot_item_id"]) == 50.0


async def test_a_fully_received_run_keeps_its_lot_and_completes(client, session, auth):
    raw, order, [lot] = await _older_run(client, session, auth, 2)
    p = await role(session, auth, PURCHASED)

    r = await reconcile(client, auth, order, [(raw, 100.0)], p)

    assert r.status_code == 200, r.text
    assert await _cost(session, auth, lot) == 100.0
    await assert_settled(client, session, auth)
    await _finish(client, session, auth, order)
    assert await _cost(session, auth, lot) == 100.0


async def test_several_older_receipts_are_brought_back_to_what_was_issued(client, session, auth):
    """The older release spread the full cost over each receipt as it came: 100.00 then 50.00,
    150.00 for 100.00 issued, while the books carried 100.00: the lots overstate the stock
    until the run is reconciled, and retained earnings cannot absorb the difference."""
    raw, order, lots = await _older_run(client, session, auth, 1, 1)
    assert [await _cost(session, auth, lot) for lot in lots] == [100.0, 50.0]
    p, re = await role(session, auth, PURCHASED), await role(session, auth, RETAINED)
    before = await snapshot(session, auth, raw, order, *lots)
    refusal(await reconcile(client, auth, order, [(raw, 100.0)], re, key="re"), 422, "reconcile_excess")
    assert await snapshot(session, auth, raw, order, *lots) == before

    r = await reconcile(client, auth, order, [(raw, 100.0)], p)

    assert r.status_code == 200, r.text
    assert [await _cost(session, auth, lot) for lot in lots] == [50.0, 50.0]
    await assert_settled(client, session, auth)
    await _finish(client, session, auth, order)
    assert [await _cost(session, auth, lot) for lot in lots] == [50.0, 50.0]


async def test_lots_whose_excess_the_books_already_carry_are_restated_off_retained_earnings(
        client, session, auth):
    """An older release's opening balance put the lots' 50.00 beyond the 100.00 issued on the
    books, against equity: the account holds exactly its stock, so no inventory account holds
    the excess beyond its stock and restating the lots to what was issued takes it back off
    retained earnings. A value below what left the shelf is still refused."""
    raw, order, lots = await _older_run(client, session, auth, 1, 1)
    p, re = await role(session, auth, PURCHASED), await role(session, auth, RETAINED)
    await _emit_auto_posted_je(
        session, company_id=auth["company_id"], user_id=auth["user_id"], je_id=f"je:auto:ob{order}",
        idem_create=f"ob{order}:c", idem_posted=f"ob{order}:p", memo="Opening inventory",
        entries=[{"account": p, "debit": 50.0, "credit": 0.0}, {"account": re, "debit": 0.0, "credit": 50.0}],
        metadata_={})
    await session.commit()
    await assert_settled(client, session, auth)
    refusal(await reconcile(client, auth, order, [(raw, 60.0)], re, key="under"), 422, "reconcile_excess")
    refusal(await reconcile(client, auth, order, [(raw, 100.0)], p, key="p"), 422, "reconcile_left")

    r = await reconcile(client, auth, order, [(raw, 100.0)], re)

    assert r.status_code == 200, r.text
    assert [await _cost(session, auth, lot) for lot in lots] == [50.0, 50.0]
    await assert_settled(client, session, auth)
    await _finish(client, session, auth, order)


async def test_a_lot_already_sold_is_restated_through_its_sale(client, session, auth):
    raw, order, [lot] = await _older_run(client, session, auth, 1)
    await _fulfil(client, await _invoice(client, session, auth, lot), auth, lot)
    await assert_settled(client, session, auth)
    p = await role(session, auth, PURCHASED)

    r = await reconcile(client, auth, order, [(raw, 100.0)], p)

    assert r.status_code == 200, r.text
    await assert_settled(client, session, auth)
    r = await receive(client, auth, order, 1, key="out")
    assert r.status_code == 200, r.text
    await _finish(client, session, auth, order)


async def test_lots_merged_since_are_restated_through_the_merge(client, session, auth):
    raw, order, lots = await _older_run(client, session, auth, 1, 1)
    merged = await _merge(client, auth, lots)
    p = await role(session, auth, PURCHASED)

    r = await reconcile(client, auth, order, [(raw, 100.0)], p)

    assert r.status_code == 200, r.text
    assert await _cost(session, auth, merged) == 100.0
    await assert_settled(client, session, auth)
    await _finish(client, session, auth, order)
    assert await _cost(session, auth, merged) == 100.0


async def test_a_reconciliation_states_the_value_that_was_issued_not_the_one_the_lot_was_given(
        client, session, auth):
    """A smaller value than the books carry would leave the account holding value no stock
    explains, and a larger one is more than left the shelf; refused, nothing changes."""
    raw, order, [lot] = await _older_run(client, session, auth, 1)
    p, re = await role(session, auth, PURCHASED), await role(session, auth, RETAINED)
    before = await snapshot(session, auth, raw, order, lot)

    for value, account, key in ((60.0, p, "reconcile_left"), (0.0, p, "reconcile_left"), (140.0, p, "reconcile_over_history"),
                                (0.0, re, "reconcile_excess"), (60.0, re, "reconcile_excess")):
        refusal(await reconcile(client, auth, order, [(raw, value)], account, key=str(uuid.uuid4())), 422, key)

    assert await snapshot(session, auth, raw, order, lot) == before
    assert (await _facts(session, auth, order))["wip_unresolved"] == "received before tracking"


async def test_a_refusal_names_components_by_sku_and_states_no_negative_amount(client, session, auth):
    """Reconcile refusals are read by the user: a component is named by its SKU, never by its
    record id, and a value the run would put back is not shown as a negative amount taken off."""
    await _older_release(session, auth)
    runs = []
    for _ in range(2):
        raw = await _older_stock(session, auth, 100.0, 10)
        order = await run(client, auth, await product(client, auth, [(raw, 5)]), 2)
        await _older_issue(session, auth, order, raw, 10)
        runs.append((raw, order, await _older_receive(session, auth, order, 1)))
    await _upgrade(session)
    p = await role(session, auth, PURCHASED)
    (raw, order, _), _ = runs
    sku = (await _state(session, auth, raw))["sku"]

    missing = refusal(await reconcile(client, auth, order, [], p), 422, "reconcile_missing")
    assert sku in missing["message"] and "item:" not in missing["message"], missing
    left = refusal(await reconcile(client, auth, order, [(raw, 0.0)], p, key="zero"), 422, "reconcile_left")
    assert not re.search(r"-\d", left["message"]), left


async def test_a_lot_whose_history_is_missing_is_refused(client, session, auth):
    raw, order, [lot] = await _older_run(client, session, auth, 1)
    await emit_event(session, company_id=auth["company_id"], entity_id=order, entity_type="mfg_order",
                     event_type="mfg.order.received", data={"quantity": 1, "lot_item_id": f"item:{uuid.uuid4()}"},
                     actor_id=auth["user_id"], location_id=None, source="api", idempotency_key=str(uuid.uuid4()),
                     metadata_={})
    await session.commit()
    p = await role(session, auth, PURCHASED)
    before = await snapshot(session, auth, raw, order, lot)

    refusal(await reconcile(client, auth, order, [(raw, 100.0)], p), 409, "output_unknown")

    assert await snapshot(session, auth, raw, order, lot) == before


async def test_a_run_is_not_re_costed_below_its_lots_while_another_run_waits(client, session, auth):
    """Two older runs each took ten of a 100.00 component and received one lot of 100.00.
    Stating 0.00 for the first would re-cost its lot to nothing and put 100.00 back on an
    account no stock explains; another run waiting does not change that. Refused, nothing
    changes, and both runs then reconcile at the value that was issued."""
    await _older_release(session, auth)
    runs = []
    for _ in range(2):
        raw = await _older_stock(session, auth, 100.0, 10)
        order = await run(client, auth, await product(client, auth, [(raw, 5)]), 2)
        await _older_issue(session, auth, order, raw, 10)
        runs.append((raw, order, await _older_receive(session, auth, order, 1)))
    await _upgrade(session)
    p = await role(session, auth, PURCHASED)
    (raw, order, lot), other = runs
    before = await snapshot(session, auth, raw, order, lot)

    refusal(await reconcile(client, auth, order, [(raw, 0.0)], p, key="zero"), 422, "reconcile_left")

    assert await snapshot(session, auth, raw, order, lot) == before
    for raw, order, lot in runs:
        r = await reconcile(client, auth, order, [(raw, 100.0)], p, key=f"honest-{order}")
        assert r.status_code == 200, r.text
        assert await _cost(session, auth, lot) == 50.0
    await assert_settled(client, session, auth)


async def test_a_value_too_large_to_record_is_refused(client, session, auth):
    raw, order, [lot] = await _older_run(client, session, auth, 1)
    p = await role(session, auth, PURCHASED)
    before = await snapshot(session, auth, raw, order, lot)

    refusal(await reconcile(client, auth, order, [(raw, 1e300)], p, key="huge"), 422, "reconcile_value_too_large")

    assert await snapshot(session, auth, raw, order, lot) == before


async def test_a_run_its_lots_fully_carry_reconciles_onto_an_account_holding_another_runs_value(
        client, session, auth):
    """The first run's lot carries everything it was issued, so reconciling it takes nothing off
    the account the user names; that account still holds the 60.00 the second run was issued
    after its receipt, which waits to be reconciled. Nothing moves for the first run, and the
    second then takes its value off."""
    await _older_release(session, auth)
    raw = await _older_stock(session, auth, 100.0, 10)
    order = await run(client, auth, await product(client, auth, [(raw, 5)]), 2)
    await _older_issue(session, auth, order, raw, 10)
    lot = await _older_receive(session, auth, order, 2)
    other_raw = await _older_stock(session, auth, 100.0, 10)
    other = await run(client, auth, await product(client, auth, [(other_raw, 5)]), 2)
    await _older_issue(session, auth, other, other_raw, 4)
    await _older_receive(session, auth, other, 1)  # carries the 40.00 issued so far
    await _older_issue(session, auth, other, other_raw, 6)  # 60.00 still on the account
    await _upgrade(session)
    p = await role(session, auth, PURCHASED)

    r = await reconcile(client, auth, order, [(raw, 100.0)], p, key="carried")

    assert r.status_code == 200, r.text
    assert await _cost(session, auth, lot) == 100.0
    r = await reconcile(client, auth, other, [(other_raw, 100.0)], p, key="other")
    assert r.status_code == 200, r.text
    await assert_settled(client, session, auth)


async def test_reconciling_shows_what_each_inventory_account_holds(client, session, auth):
    raw, order, _ = await _older_run(client, session, auth, 1)
    p = await role(session, auth, PURCHASED)
    await _emit_auto_posted_je(
        session, company_id=auth["company_id"], user_id=auth["user_id"], je_id=f"je:auto:x{order}",
        idem_create=f"x{order}:c", idem_posted=f"x{order}:p", memo="Purchase",
        entries=[{"account": p, "debit": 25.0, "credit": 0.0}, {"account": "2110", "debit": 0.0, "credit": 25.0}],
        metadata_={})
    await session.commit()

    rooms = (await client.get(f"/manufacturing/{order}/reconcile", headers=auth["headers"])).json()["rooms"]

    assert rooms[p] == "25.00"
