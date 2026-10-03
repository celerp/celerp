# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Reconciling a production run whose materials' value its history cannot prove.

A run an older release started can be left needing reconciliation when it starts on this
release (test_mfg_wip_upgrade): its books disagree, it received output before value was
tracked, a component it used has no inventory account, or its books came from elsewhere. It
then refuses every movement. Someone allowed to run production and keep the books records
the value of each component still in the run and the account that value comes off: an
inventory account that holds at least that much beyond its stock on hand, or retained
earnings for value the books never carried. The value moves onto work in progress, and the
run carries on like any other. Nothing is ever guessed: no value, no account, nothing moves.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from celerp.accounting_roles import AccountRole
from celerp.events.engine import emit_event
from celerp.models.accounting import UserCompany
from celerp.models.company import User
from celerp.models.notification import Notification
from celerp.services.auto_je import _emit_auto_posted_je
from mfg_runs import OPENING, WIP, give_back, issue, lines, receive, refusal, role, set_settings, snapshot
from stock_books import assert_settled, older_release_lot
from test_cost_restatement import TZ, _item, _state, auth, ids  # noqa: F401  (auth and ids are fixtures)
from test_mfg_wip_upgrade import _carried, _events, _facts, _job, _older_issue, _upgrade, in_production_slot  # noqa: F401
from test_posting_roles_lots import _lot
from test_posting_roles_older_stock import _choose, _opening_entry, _restored, _without_accounting

pytestmark = pytest.mark.asyncio

RETAINED = AccountRole.RETAINED_EARNINGS.value


async def reconcile(client, auth, order: str, values: list[tuple[str, float]], account: str | None,
                    key: str = "rec", headers=None):
    body = {"components": [{"item_id": i, "value": v} for i, v in values], "idempotency_key": key}
    if account is not None:
        body["account"] = account
    return await client.post(f"/manufacturing/{order}/reconcile", headers=headers or auth["headers"], json=body)


async def _books_disagree(client, session, auth):
    """An older run that took 4 of a 10-unit component (40.00) off opening inventory, whose
    account was then written down by 10.00: it holds 30.00 beyond its stock on hand."""
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
    return raw, order, ob


async def _carry_on(client, session, auth, raw: str, order: str, lot_cost: float, settled=assert_settled):
    """The reconciled run gives a component back, issues what it still needs, receives its
    output and completes, and the books follow every step."""
    assert (await give_back(client, auth, order, [(raw, 1)], key="back")).status_code == 200
    await settled(client, session, auth)
    r = await issue(client, auth, order, key="rest")
    assert r.status_code == 200, r.text
    await settled(client, session, auth)
    r = await receive(client, auth, order, key="out")
    assert r.status_code == 200, r.text
    assert (await _state(session, auth, r.json()["lot_item_id"]))["cost_total"] == lot_cost
    assert (await _state(session, auth, order))["status"] == "completed"
    await settled(client, session, auth)


async def test_a_run_whose_books_disagree_is_reconciled_and_carries_on(client, session, auth):
    raw, order, ob = await _books_disagree(client, session, auth)

    r = await reconcile(client, auth, order, [(raw, 30.0)], ob)

    assert r.status_code == 200, r.text
    wip = await role(session, auth, WIP)
    assert await lines(session, auth, f"je:auto:{order}:reconcile:rec") == [(ob, (OPENING,), 0, 30.0),
                                                                          (wip, (WIP,), 30.0, 0)]
    assert await _facts(session, auth, order) == {"wip_issued": "30.00", "wip_account_code": wip,
                                                  "wip_untracked": None, "wip_unresolved": None}
    await assert_settled(client, session, auth)
    # 30.00, less the 7.50 the one returned took back onto its lot, plus the 67.50 that lot then
    # held for the seven issued after (six at 10.00 and the returned one at 7.50).
    await _carry_on(client, session, auth, raw, order, 90.0)


async def test_a_run_that_received_before_tracking_is_reconciled_and_carries_on(client, session, auth):
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
    ob = await role(session, auth, OPENING)

    assert (await reconcile(client, auth, order, [(raw, 100.0)], ob)).status_code == 200

    assert (await _facts(session, auth, order))["wip_issued"] == "100.00"
    await assert_settled(client, session, auth)
    refusal(await give_back(client, auth, order, [(raw, 1)], key="back"), 409, "return_after_receipt")
    r = await receive(client, auth, order, key="out")
    assert r.status_code == 200, r.text
    assert (await _state(session, auth, r.json()["lot_item_id"]))["cost_total"] == 100.0
    assert (await _state(session, auth, order))["status"] == "completed"
    await assert_settled(client, session, auth)


async def test_a_run_with_a_component_without_an_account_is_reconciled_and_carries_on(client, session, auth):
    """The component an older run used up entirely left no lot on hand for Accounting to place,
    so it has no inventory account and nothing says where its value is."""
    used = await older_release_lot(session, auth["company_id"], auth["user_id"], 40.0, qty=4)
    raw = await _item(client, auth, 100.0, qty=10)
    item, order = await _job(client, auth, raw)
    await _older_issue(session, auth, order, used, 4)
    await _upgrade(session)
    assert (await _facts(session, auth, order))["wip_unresolved"] == "component without an inventory account"
    re = await role(session, auth, RETAINED)
    # The run names what it still holds, though the used-up lot is no input of it.
    needs = (await client.get(f"/manufacturing/{order}/reconcile", headers=auth["headers"])).json()
    assert [c["item_id"] for c in needs["components"]] == [used]

    assert (await reconcile(client, auth, order, [(used, 40.0)], re)).status_code == 200

    wip = await role(session, auth, WIP)
    assert await lines(session, auth, f"je:auto:{order}:reconcile:rec") == [(wip, (WIP,), 40.0, 0),
                                                                          (re, (RETAINED,), 0, 40.0)]
    await assert_settled(client, session, auth)
    r = await issue(client, auth, order, key="rest")
    assert r.status_code == 200, r.text
    r = await receive(client, auth, order, key="out")
    assert r.status_code == 200, r.text
    assert (await _state(session, auth, r.json()["lot_item_id"]))["cost_total"] == 140.0
    await assert_settled(client, session, auth)


async def test_a_run_on_books_from_elsewhere_is_reconciled_and_carries_on(client, session, auth):
    await _without_accounting(session, auth)
    await _restored(session, client, auth)
    raw = await _lot(client, auth, 100.0, qty=10)
    _, order = await _job(client, auth, raw)
    assert (await issue(client, auth, order, [(raw, 4)], key="off")).status_code == 200
    await _upgrade(session)
    assert (await _facts(session, auth, order))["wip_unresolved"] == "books from elsewhere"
    re = await role(session, auth, RETAINED)
    # Books restored from a release without work in progress name no account for it yet: the
    # reconciliation waits for one rather than picking it.
    refusal(await reconcile(client, auth, order, [(raw, 40.0)], re, key="early"), 409, "wip_account_missing")
    r = await client.put(f"/accounting/posting-accounts/{WIP}", headers=auth["headers"], json={"code": "1130-WIP"})
    assert r.status_code == 200, r.text

    r = await reconcile(client, auth, order, [(raw, 40.0)], re)
    assert r.status_code == 200, r.text

    wip = await role(session, auth, WIP)
    assert await lines(session, auth, f"je:auto:{order}:reconcile:rec") == [(wip, (WIP,), 40.0, 0),
                                                                          (re, (RETAINED,), 0, 40.0)]
    # The six still on the shelf are placed the way any restored stock is: on the account the
    # restored books carry them on.
    await _opening_entry(session, auth, 60.0)
    assert (await _choose(client, auth, raw, "1130-OB")).status_code == 200
    await _carried(client, session, auth)
    await _carry_on(client, session, auth, raw, order, 100.0, _carried)


async def test_reconciling_again_with_the_same_key_changes_nothing(client, session, auth):
    raw, order, ob = await _books_disagree(client, session, auth)
    first = await reconcile(client, auth, order, [(raw, 30.0)], ob)
    assert first.status_code == 200, first.text
    events = await _events(session, auth)

    again = await reconcile(client, auth, order, [(raw, 30.0)], ob)

    assert again.status_code == 200 and again.json() == first.json(), again.text
    assert await _events(session, auth) == events
    refusal(await reconcile(client, auth, order, [(raw, 20.0)], ob), 409, "key_reused")
    refusal(await reconcile(client, auth, order, [(raw, 30.0)], ob, key="other"), 409, "not_unresolved")
    assert await _events(session, auth) == events
    await assert_settled(client, session, auth)


async def test_reconciling_in_a_locked_period_is_refused(client, session, auth):
    raw, order, ob = await _books_disagree(client, session, auth)
    today = datetime.now(ZoneInfo(TZ)).date()
    await set_settings(session, auth, lock_date=(today + timedelta(days=1)).isoformat())
    before = await snapshot(session, auth, raw, order)

    r = await reconcile(client, auth, order, [(raw, 30.0)], ob)

    assert r.status_code == 422 and "locked" in r.text, r.text
    assert await snapshot(session, auth, raw, order) == before
    assert (await _facts(session, auth, order))["wip_unresolved"] == "books disagree"


async def test_reconciling_needs_manufacturing_and_accounting_permission(client, session, auth):
    from test_helpers import make_authed_token

    raw, order, ob = await _books_disagree(client, session, auth)
    uid = uuid.uuid4()
    session.add(User(id=uid, email=f"op-{uid.hex[:8]}@test.co", name="Operator", auth_hash="x", is_active=True))
    await session.flush()
    session.add(UserCompany(id=uuid.uuid4(), user_id=uid, company_id=auth["company_id"], role="operator",
                            is_active=True))
    await session.commit()
    operator = {"Authorization": f"Bearer {await make_authed_token(session, str(uid), str(auth['company_id']), 'operator')}"}
    before = await snapshot(session, auth, raw, order)

    r = await reconcile(client, auth, order, [(raw, 30.0)], ob, headers=operator)

    assert r.status_code == 403, r.text
    assert await snapshot(session, auth, raw, order) == before


async def test_reconciling_refuses_values_or_accounts_it_cannot_book(client, session, auth):
    raw, order, ob = await _books_disagree(client, session, auth)
    other = await _item(client, auth, 10.0, qty=1)
    before = await snapshot(session, auth, raw, order)

    for values, account, status, key in (
            ([], ob, 422, "reconcile_missing"),                      # the component's value is missing
            ([(raw, -1.0)], ob, 422, "reconcile_values"),            # a negative value
            ([(raw, 30.0), (other, 1.0)], ob, 422, "reconcile_values"),  # not a component of the run
            ([(raw, 30.0)], None, 422, "reconcile_account"),         # no account for a value
            ([(raw, 30.0)], "5100", 422, "reconcile_account"),       # not inventory or retained earnings
            ([(raw, 40.0)], ob, 422, "reconcile_account")):          # more than the account holds
        refusal(await reconcile(client, auth, order, values, account, key=str(uuid.uuid4())), status, key)

    assert await snapshot(session, auth, raw, order) == before
    assert (await _facts(session, auth, order))["wip_unresolved"] == "books disagree"


async def test_the_notification_links_to_the_run_that_needs_reconciling(client, session, auth):
    raw, order, ob = await _books_disagree(client, session, auth)
    notice = (await session.execute(select(Notification).where(
        Notification.company_id == auth["company_id"],
        Notification.title == "Production run needs reconciling"))).scalar_one()
    assert notice.action_url == f"/manufacturing/runs/{order}/reconcile"
    assert "books disagree" in notice.body
    needs = (await client.get(f"/manufacturing/{order}/reconcile", headers=auth["headers"])).json()
    assert needs["reason"] == "books disagree"
    assert [(c["item_id"], c["sku"]) for c in needs["components"]] == [(raw, (await _state(session, auth, raw))["sku"])]
