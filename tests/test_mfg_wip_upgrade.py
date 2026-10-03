# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Production runs already open when a release starts tracking materials in production.

An older release issued components to a run without recording their value or booking
anything: the value stayed on the components' inventory accounts until the run completed.
On the first start of this release, each such run gets the value its components actually
gave up, replayed from their own history (never today's costs), and the books move it onto
work in progress: off the components' accounts when those accounts hold exactly that
value beyond their stock on hand, or against retained earnings when Accounting is being
turned on and the books never carried it. Anything the history or the books cannot prove
leaves the run needing reconciliation, and it refuses every movement until then.

A run issued while Accounting was off keeps its value on the run; turning Accounting on
books it onto work in progress against retained earnings.

Every start after the first changes nothing.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select

from celerp.accounting_roles import INVENTORY_ORIGIN_KEY, LOT_ACCOUNT_FIELD, ROLES_KEY, AccountRole
from celerp.events.engine import emit_event
from celerp.models.ledger import LedgerEntry
from celerp.models.notification import Notification
from celerp.models.projections import Projection
from celerp.modules import slots
from celerp.services.auto_je import _emit_auto_posted_je
from celerp.services.company_lock import locked_company
from mfg_runs import OPENING, PURCHASED, WIP, complete, issue, lines, product, receive, refusal, role, run, snapshot
from mfg_runs import cancel, give_back, set_settings, undo_receipt
from stock_books import assert_books_carry_stock, assert_settled, assert_wip_carried, older_release_lot
from test_cost_restatement import TZ, _item, _state, auth, ids  # noqa: F401  (auth and ids are fixtures)
from test_money_stock_and_contact_invariants import _account_net
from test_posting_roles_lots import _lot
from test_posting_roles_older_stock import _older_release, _opening_entry, _restored, _without_accounting
from test_posting_roles_rollout import _startup

pytestmark = pytest.mark.asyncio

RETAINED = AccountRole.RETAINED_EARNINGS.value


@pytest.fixture(autouse=True)
def in_production_slot():
    """The manufacturing contribution the module loader registers in production."""
    saved = slots.get("inventory_in_production")
    slots._slots["inventory_in_production"] = [{
        "handler": "celerp_manufacturing.movements:legacy_in_production", "_module": "celerp-manufacturing"}]
    yield
    slots._slots["inventory_in_production"] = saved


async def _upgrade(session) -> None:
    """A start of this release: Accounting places the stock, then open runs are settled."""
    from celerp_manufacturing.routes import settle_open_runs_hook

    await _startup(session)
    await settle_open_runs_hook(session=session)
    await session.commit()


async def _older_issue(session, auth, order: str, item: str, qty: float) -> None:
    """``qty`` of ``item`` issued to ``order`` as an older release did it: consumed and
    recorded on the run, with no value and no entry."""
    cid, uid = auth["company_id"], auth["user_id"]
    await emit_event(session, company_id=cid, entity_id=item, entity_type="item", event_type="item.consumed",
                     data={"quantity_consumed": qty}, actor_id=uid, location_id=None, source="api",
                     idempotency_key=str(uuid.uuid4()), metadata_={"manufacturing_order_id": order})
    await emit_event(session, company_id=cid, entity_id=order, entity_type="mfg_order",
                     event_type="mfg.order.issued", data={"items": [{"item_id": item, "quantity": qty}],
                                                          "issued_by": str(uid)},
                     actor_id=uid, location_id=None, source="api", idempotency_key=str(uuid.uuid4()), metadata_={})
    await session.commit()


async def _job(client, auth, raw: str):
    item = await product(client, auth, [(raw, 5)])
    return item, await run(client, auth, item, 2)


async def _facts(session, auth, order: str) -> dict:
    s = await _state(session, auth, order)
    return {k: s.get(k) for k in ("wip_issued", "wip_account_code", "wip_untracked", "wip_unresolved")}


async def _events(session, auth) -> int:
    return await session.scalar(select(func.count()).select_from(LedgerEntry).where(
        LedgerEntry.company_id == auth["company_id"]))


async def _entry(session, auth, je_id: str):
    session.expire_all()
    return await session.get(Projection, {"company_id": auth["company_id"], "entity_id": je_id})


async def _notices(session, auth, title: str) -> int:
    return await session.scalar(select(func.count()).select_from(Notification).where(
        Notification.company_id == auth["company_id"], Notification.title == title))


async def _carried(client, session, auth) -> None:
    """The books carry the stock and the work in progress. Stock the books first recognized
    at an upgrade keeps its opening inventory entry posted: that entry is how it came in."""
    await assert_books_carry_stock(session, auth["company_id"])
    await assert_wip_carried(session, auth["company_id"])


async def _finish(client, session, auth, raw: str, order: str, settled=assert_settled) -> None:
    """The settled run carries on like any other and the books follow every step."""
    assert (await issue(client, auth, order, [(raw, 6)], key="rest")).status_code == 200
    await settled(client, session, auth)
    r = await receive(client, auth, order, 2, key="out")
    assert r.status_code == 200, r.text
    assert (await _state(session, auth, r.json()["lot_item_id"]))["cost_total"] == 100.0
    await settled(client, session, auth)
    assert (await _state(session, auth, order))["status"] == "completed"


async def test_an_older_runs_issued_value_moves_off_its_component_account_onto_work_in_progress(
        client, session, auth):
    raw = await _item(client, auth, 100.0, qty=10)
    _, order = await _job(client, auth, raw)
    await _older_issue(session, auth, order, raw, 4)
    before = await snapshot(session, auth, raw, order)
    refusal(await issue(client, auth, order, [(raw, 6)], key="early"), 409, "reconciliation_required")
    assert await snapshot(session, auth, raw, order) == before

    await _upgrade(session)

    ob, wip = await role(session, auth, OPENING), await role(session, auth, WIP)
    assert await lines(session, auth, f"je:auto:{order}:wip-opened") == [(ob, (OPENING,), 0, 40.0),
                                                                       (wip, (WIP,), 40.0, 0)]
    assert await _facts(session, auth, order) == {"wip_issued": "40.00", "wip_account_code": wip,
                                                  "wip_untracked": None, "wip_unresolved": None}
    assert await _notices(session, auth, "Materials in production recorded") == 1
    await assert_settled(client, session, auth)
    events = await _events(session, auth)
    await _upgrade(session)
    assert await _events(session, auth) == events
    await _finish(client, session, auth, raw, order)


async def test_older_stock_and_an_older_run_are_placed_together(client, session, auth):
    """Stock from before lots recorded their account is normalized with the value an open
    run took still on the books, and the run then takes that value off purchased inventory."""
    await _older_release(session, auth)
    raw = await older_release_lot(session, auth["company_id"], auth["user_id"], 100.0, qty=10)
    await _opening_entry(session, auth, 100.0)
    _, order = await _job(client, auth, raw)
    await _older_issue(session, auth, order, raw, 4)

    await _upgrade(session)

    assert (await _state(session, auth, raw))[LOT_ACCOUNT_FIELD] == "1130-P"
    wip = await role(session, auth, WIP)
    assert await lines(session, auth, f"je:auto:{order}:wip-opened") == [("1130-P", (PURCHASED,), 0, 40.0),
                                                                       (wip, (WIP,), 40.0, 0)]
    await _carried(client, session, auth)
    await _finish(client, session, auth, raw, order, _carried)


async def test_older_stock_is_placed_while_an_older_run_is_still_open(client, session, auth):
    """Accounting alone, before manufacturing settles anything, already places the stock:
    the value an open run took is still on the inventory accounts, and that is expected."""
    await _older_release(session, auth)
    raw = await older_release_lot(session, auth["company_id"], auth["user_id"], 100.0, qty=10)
    await _opening_entry(session, auth, 100.0)
    _, order = await _job(client, auth, raw)
    await _older_issue(session, auth, order, raw, 4)

    await _startup(session)

    assert (await _state(session, auth, raw))[LOT_ACCOUNT_FIELD] == "1130-P"
    assert INVENTORY_ORIGIN_KEY in (await locked_company(session, auth["company_id"])).settings


async def test_an_older_run_open_when_accounting_is_turned_on_opens_against_retained_earnings(
        client, session, auth):
    await _without_accounting(session, auth)
    raw = await _lot(client, auth, 100.0, qty=10)
    _, order = await _job(client, auth, raw)
    await _older_issue(session, auth, order, raw, 4)

    await _upgrade(session)

    assert (await _state(session, auth, raw))[LOT_ACCOUNT_FIELD] == "1130-OB"
    re, wip = await role(session, auth, RETAINED), await role(session, auth, WIP)
    assert await lines(session, auth, f"je:auto:{order}:wip-opened") == [(wip, (WIP,), 40.0, 0),
                                                                       (re, (RETAINED,), 0, 40.0)]
    assert await _account_net(session, auth["company_id"], "1130-OB") == 60.0
    await _carried(client, session, auth)
    await _finish(client, session, auth, raw, order, _carried)


async def test_a_run_issued_while_accounting_was_off_is_booked_when_it_is_turned_on(client, session, auth):
    await _without_accounting(session, auth)
    raw = await _lot(client, auth, 100.0, qty=10)
    _, order = await _job(client, auth, raw)
    r = await issue(client, auth, order, [(raw, 4)], key="off")
    assert r.status_code == 200 and r.json()["value"] == "40.00", r.text
    assert await _entry(session, auth, f"je:auto:{order}:issue:off") is None

    await _upgrade(session)

    re, wip = await role(session, auth, RETAINED), await role(session, auth, WIP)
    assert await lines(session, auth, f"je:auto:{order}:wip-booked") == [(wip, (WIP,), 40.0, 0),
                                                                       (re, (RETAINED,), 0, 40.0)]
    assert (await _facts(session, auth, order))["wip_account_code"] == wip
    await _carried(client, session, auth)
    events = await _events(session, auth)
    await _upgrade(session)
    assert await _events(session, auth) == events
    await _finish(client, session, auth, raw, order, _carried)


async def _refused_everywhere(client, session, auth, raw: str, order: str) -> None:
    before = await snapshot(session, auth, raw, order)
    refusal(await issue(client, auth, order, [(raw, 6)], key="i"), 409, "reconciliation_required")
    refusal(await receive(client, auth, order, 1, key="r"), 409, "reconciliation_required")
    refusal(await complete(client, auth, order, key="c"), 409, "reconciliation_required")
    refusal(await give_back(client, auth, order, key="g"), 409, "reconciliation_required")
    refusal(await undo_receipt(client, auth, order, raw, key="u"), 409, "reconciliation_required")
    refusal(await cancel(client, auth, order, key="x"), 409, "reconciliation_required")
    assert await snapshot(session, auth, raw, order) == before


async def test_an_older_run_on_books_holding_something_else_waits_for_reconciling_then_carries_on(client, session, auth):
    raw = await _item(client, auth, 100.0, qty=10)
    _, order = await _job(client, auth, raw)
    await _older_issue(session, auth, order, raw, 4)
    ob = await role(session, auth, OPENING)
    await _emit_auto_posted_je(
        session, company_id=auth["company_id"], user_id=auth["user_id"], je_id="je:manual:writeoff",
        idem_create="writeoff:c", idem_posted="writeoff:p", memo="Write-off",
        entries=[{"account": "5100", "debit": 10.0, "credit": 0.0}, {"account": ob, "debit": 0.0, "credit": 10.0}],
        metadata_={})
    await session.commit()

    await _upgrade(session)

    assert (await _facts(session, auth, order))["wip_unresolved"] == "books disagree"
    assert await _entry(session, auth, f"je:auto:{order}:wip-opened") is None
    assert await _notices(session, auth, "Production run needs reconciling") == 1
    await _refused_everywhere(client, session, auth, raw, order)
    events = await _events(session, auth)
    await _upgrade(session)
    assert await _events(session, auth) == events
    # Settling never guesses; reconciling the run with the value its account still holds
    # (40.00 less the 10.00 written off) lets it carry on.
    r = await client.post(f"/manufacturing/{order}/reconcile", headers=auth["headers"], json={
        "components": [{"item_id": raw, "value": 30.0}], "account": ob, "idempotency_key": "rec"})
    assert r.status_code == 200, r.text
    assert (await _facts(session, auth, order))["wip_unresolved"] is None
    await assert_settled(client, session, auth)
    assert (await issue(client, auth, order, [(raw, 6)], key="rest")).status_code == 200
    r = await receive(client, auth, order, 2, key="out")
    assert r.status_code == 200, r.text
    assert (await _state(session, auth, r.json()["lot_item_id"]))["cost_total"] == 90.0
    await assert_settled(client, session, auth)


async def test_an_older_run_that_received_output_needs_reconciling(client, session, auth):
    raw = await _item(client, auth, 100.0, qty=10)
    _, order = await _job(client, auth, raw)
    await _older_issue(session, auth, order, raw, 10)
    await emit_event(session, company_id=auth["company_id"], entity_id=order, entity_type="mfg_order",
                     event_type="mfg.order.received", data={"quantity": 1, "lot_item_id": f"item:{uuid.uuid4()}"},
                     actor_id=auth["user_id"], location_id=None, source="api", idempotency_key=str(uuid.uuid4()),
                     metadata_={})
    await session.commit()

    await _upgrade(session)

    assert (await _facts(session, auth, order))["wip_unresolved"] == "received before tracking"
    assert await _entry(session, auth, f"je:auto:{order}:wip-opened") is None


async def test_a_run_issued_while_accounting_was_off_in_restored_books_needs_reconciling(client, session, auth):
    await _without_accounting(session, auth)
    await _restored(session, client, auth)
    raw = await _lot(client, auth, 100.0, qty=10)
    _, order = await _job(client, auth, raw)
    assert (await issue(client, auth, order, [(raw, 4)], key="off")).status_code == 200

    await _upgrade(session)

    assert (await _facts(session, auth, order))["wip_unresolved"] == "books from elsewhere"
    assert await _entry(session, auth, f"je:auto:{order}:wip-booked") is None


async def test_an_older_run_waits_for_its_stock_to_be_placed_and_for_an_open_period(client, session, auth):
    from celerp_manufacturing.movements import settle_open_runs

    raw = await _item(client, auth, 100.0, qty=10)
    _, order = await _job(client, auth, raw)
    await _older_issue(session, auth, order, raw, 4)
    company = await locked_company(session, auth["company_id"])
    unplaced = {k: v for k, v in company.settings.items() if k != INVENTORY_ORIGIN_KEY}
    company.settings = unplaced
    await session.commit()
    await settle_open_runs(session, auth["company_id"])
    await session.commit()
    assert (await _facts(session, auth, order))["wip_untracked"] is True

    today = datetime.now(ZoneInfo(TZ)).date()
    await set_settings(session, auth, lock_date=(today + timedelta(days=1)).isoformat())
    events = await _events(session, auth)
    await _upgrade(session)
    assert (await _facts(session, auth, order))["wip_untracked"] is True
    assert await _entry(session, auth, f"je:auto:{order}:wip-opened") is None
    assert await _events(session, auth) == events

    await set_settings(session, auth, lock_date=None)
    await _upgrade(session)
    assert (await _facts(session, auth, order))["wip_issued"] == "40.00"
    await assert_settled(client, session, auth)


async def test_an_older_run_waits_for_a_work_in_progress_account(client, session, auth):
    raw = await _item(client, auth, 100.0, qty=10)
    _, order = await _job(client, auth, raw)
    await _older_issue(session, auth, order, raw, 4)
    company = await locked_company(session, auth["company_id"])
    roles = dict(company.settings[ROLES_KEY])
    wip = roles.pop(WIP)
    company.settings = {**company.settings, ROLES_KEY: roles}
    await session.commit()

    from celerp_manufacturing.routes import settle_open_runs_hook

    await settle_open_runs_hook(session=session)
    await session.commit()
    assert (await _facts(session, auth, order))["wip_untracked"] is True
    await set_settings(session, auth, **{ROLES_KEY: {**roles, WIP: wip}})
    await settle_open_runs_hook(session=session)
    await session.commit()
    assert (await _facts(session, auth, order))["wip_account_code"] == wip
    await assert_books_carry_stock(session, auth["company_id"])


async def test_accounting_places_stock_before_manufacturing_settles_its_runs():
    from pathlib import Path

    from celerp.modules.loader import _topo_sort

    root = Path(__file__).resolve().parents[1] / "default_modules"
    paths = sorted((p for p in root.iterdir() if (p / "__init__.py").exists()), key=lambda p: p.name)
    order = [p.name for p in _topo_sort(paths, {p.name for p in paths})]
    assert order.index("celerp-accounting") < order.index("celerp-manufacturing")


# ---------------------------------------------------------------------------
# Undoing a step across Accounting being turned on
# ---------------------------------------------------------------------------

async def test_materials_issued_and_returned_while_accounting_was_off_leave_nothing_to_book(
        client, session, auth):
    from mfg_runs import give_back

    await _without_accounting(session, auth)
    raw = await _lot(client, auth, 100.0, qty=10)
    _, order = await _job(client, auth, raw)
    assert (await issue(client, auth, order, [(raw, 4)], key="i")).status_code == 200
    r = await give_back(client, auth, order, key="g")
    assert r.status_code == 200 and r.json()["value"] == "40.00", r.text
    assert await _entry(session, auth, f"je:auto:{order}:return:g") is None

    await _upgrade(session)

    assert await _entry(session, auth, f"je:auto:{order}:wip-booked") is None
    assert round((await _state(session, auth, raw))["cost_total"], 2) == 100.0
    assert await _account_net(session, auth["company_id"], "1130-OB") == 100.0
    await _carried(client, session, auth)


async def test_materials_issued_while_accounting_was_off_return_onto_the_books_once_it_is_on(
        client, session, auth):
    from mfg_runs import give_back

    await _without_accounting(session, auth)
    raw = await _lot(client, auth, 100.0, qty=10)
    _, order = await _job(client, auth, raw)
    assert (await issue(client, auth, order, [(raw, 4)], key="i")).status_code == 200
    await _upgrade(session)

    r = await give_back(client, auth, order, key="g")

    assert r.status_code == 200, r.text
    wip = await role(session, auth, WIP)
    assert await lines(session, auth, f"je:auto:{order}:return:g") == sorted([
        ("1130-OB", (OPENING,), 40.0, 0.0), (wip, (WIP,), 0.0, 40.0)])
    assert await _account_net(session, auth["company_id"], "1130-OB") == 100.0
    await _carried(client, session, auth)


async def test_output_received_while_accounting_was_off_is_taken_back_once_it_is_on(client, session, auth):
    from mfg_runs import undo_receipt

    await _without_accounting(session, auth)
    raw = await _lot(client, auth, 100.0, qty=10)
    _, order = await _job(client, auth, raw)
    assert (await issue(client, auth, order, key="i")).status_code == 200
    lot = (await receive(client, auth, order, 1, key="r")).json()["lot_item_id"]
    await _upgrade(session)
    code = (await _state(session, auth, lot))[LOT_ACCOUNT_FIELD]

    r = await undo_receipt(client, auth, order, lot, key="u")

    assert r.status_code == 200, r.text
    wip = await role(session, auth, WIP)
    assert await lines(session, auth, f"je:auto:{order}:unreceive:u") == sorted([
        (code, (OPENING,), 0.0, 50.0), (wip, (WIP,), 50.0, 0.0)])
    await _carried(client, session, auth)


async def test_a_run_completed_while_accounting_was_off_is_not_reopened_once_it_is_on(client, session, auth):
    from mfg_runs import reopen

    await _without_accounting(session, auth)
    raw = await _lot(client, auth, 100.0, qty=10)
    _, order = await _job(client, auth, raw)
    assert (await complete(client, auth, order, key="c")).status_code == 200
    await _upgrade(session)
    before = await snapshot(session, auth, raw, order)

    refusal(await reopen(client, auth, order, key="o"), 409, "reconciliation_required")

    assert await snapshot(session, auth, raw, order) == before
    await _carried(client, session, auth)
