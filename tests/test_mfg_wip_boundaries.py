# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Where a production run meets the rest of the books.

A locked day refuses receiving and completing as it refuses issuing. A run, its
components and its account belong to one company and are absent to every other. Labor
and overhead stay planning data: a run carries only the value of the materials issued
to it. A component's cost cannot be corrected once part of it went into a run, so the
run keeps the value it was given. The account a run's balance stays on after a
remap cannot be switched off while it carries that balance, and a run whose account can
no longer take postings anyway is refused rather than moved somewhere else.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import update

from celerp_accounting.models import Account
from mfg_runs import WIP, balances, complete, issue, product, receive, role, run, set_settings, snapshot
from stock_books import assert_settled
from test_cost_restatement import _item, _set_cost, _state
from test_helpers import TZ, company_auth

pytestmark = pytest.mark.asyncio


async def _issued(client, auth):
    """A run making 2 of a product taking 5 each of a 10-unit lot costing 100, all issued."""
    raw = await _item(client, auth, 100.0, qty=10)
    item = await product(client, auth, [(raw, 5)])
    order = await run(client, auth, item, 2)
    assert (await issue(client, auth, order, key="i")).status_code == 200
    return raw, item, order


@pytest.mark.parametrize("step", ["receive", "complete"])
async def test_receiving_or_completing_on_a_locked_day_is_refused_and_changes_nothing(client, session, auth, step):
    raw, item, order = await _issued(client, auth)
    today = datetime.now(ZoneInfo(TZ)).date()
    await set_settings(session, auth, lock_date=(today + timedelta(days=1)).isoformat())
    before = await snapshot(session, auth, raw, item, order)

    r = await (receive(client, auth, order, 1, key="r") if step == "receive" else complete(client, auth, order, key="c"))

    assert r.status_code == 422 and "locked" in r.text, r.text
    assert await snapshot(session, auth, raw, item, order) == before


async def test_another_company_finds_no_run_component_or_product(client, session, auth):
    raw, item, order = await _issued(client, auth)
    other = await company_auth(session, uuid.uuid4(), uuid.uuid4())
    before = await snapshot(session, auth, raw, item, order)

    for r in (await issue(client, other, order, key="x"), await receive(client, other, order, 1, key="x"),
              await complete(client, other, order, key="x"),
              await client.post(f"/manufacturing/{order}/cancel", headers=other["headers"],
                                json={"idempotency_key": "x"}),
              await client.post(f"/manufacturing/items/{item}/build", headers=other["headers"],
                                json={"quantity": 1, "complete": True, "idempotency_key": "x"})):
        assert r.status_code == 404, r.text
    theirs = await product(client, other, [])
    r = await client.put(f"/manufacturing/items/{theirs}/recipe", headers=other["headers"], json={
        "output_qty": 1, "components": [{"item_id": raw, "quantity": 1}], "labor": [], "overhead": []})
    assert r.status_code in (404, 422), r.text

    assert await snapshot(session, auth, raw, item, order) == before
    await assert_settled(client, session, auth)
    await assert_settled(client, session, other)


async def test_labor_and_overhead_are_not_added_to_work_in_progress(client, session, auth):
    raw = await _item(client, auth, 100.0, qty=10)
    item = await product(client, auth, [(raw, 5)])
    r = await client.put(f"/manufacturing/items/{item}/recipe", headers=auth["headers"], json={
        "output_qty": 1, "components": [{"item_id": raw, "quantity": 5}],
        "labor": [{"operation": "Assemble", "hours": 2, "rate": 40}],
        "overhead": [{"description": "Packing", "amount": 7}]})
    assert r.status_code == 200, r.text

    r = await client.post(f"/manufacturing/items/{item}/build", headers=auth["headers"],
                          json={"quantity": 2, "complete": True, "idempotency_key": "b"})

    assert r.status_code == 200, r.text
    state = await _state(session, auth, r.json()["id"])
    assert (state["wip_issued"], state["wip_transferred"]) == ("100.00", "100.00")
    assert [(await _state(session, auth, lot))["cost_total"] for lot in state["received_lots"]] == [100.0]
    await assert_settled(client, session, auth)


async def _moved_on(client, session, auth) -> str:
    """Work in progress remapped to a new account; returns the account the run's balance stays on."""
    first = await role(session, auth, WIP)
    r = await client.post("/accounting/accounts", headers=auth["headers"], json={
        "code": "1135", "name": "Production in progress", "account_type": "asset"})
    assert r.status_code in (200, 201), r.text
    r = await client.put(f"/accounting/posting-accounts/{WIP}", headers=auth["headers"], json={"code": "1135"})
    assert r.status_code == 200, r.text
    return first


async def test_an_account_still_carrying_a_runs_balance_cannot_be_switched_off(client, session, auth):
    raw, item, order = await _issued(client, auth)
    first = await _moved_on(client, session, auth)
    carried = (await balances(session, auth))[first]

    off = await client.patch(f"/accounting/accounts/{first}", headers=auth["headers"], json={"is_active": False})
    assert off.status_code == 409, off.text
    detail = off.json()["detail"]
    assert detail["message_key"] == "posting.account_keeps_balance", off.text
    assert (detail["params"]["code"], detail["params"]["role"]) == (first, WIP)
    assert float(detail["params"]["balance"]) == carried

    assert (await complete(client, auth, order, key="c")).status_code == 200
    assert first not in await balances(session, auth)
    off = await client.patch(f"/accounting/accounts/{first}", headers=auth["headers"], json={"is_active": False})
    assert off.status_code == 200, off.text
    await assert_settled(client, session, auth)


async def test_a_run_whose_account_cannot_take_postings_is_refused_and_changes_nothing(client, session, auth):
    """An account switched off before it could be refused (an older release, an import)."""
    raw, item, order = await _issued(client, auth)
    first = await _moved_on(client, session, auth)
    await session.execute(update(Account).where(
        Account.company_id == auth["company_id"], Account.code == first).values(is_active=False))
    await session.commit()
    before = await snapshot(session, auth, raw, item, order)

    for r in (await receive(client, auth, order, 1, key="r"), await complete(client, auth, order, key="c")):
        assert r.status_code == 409, r.text
        assert r.json()["detail"]["message_key"] == "posting.continued_account_unusable", r.text
    assert await snapshot(session, auth, raw, item, order) == before
    await assert_settled(client, session, auth)


@pytest.mark.parametrize("taken", [4, 10], ids=["part", "all"])
async def test_correcting_a_components_cost_after_it_went_into_a_run_is_refused(client, session, auth, taken):
    raw = await _item(client, auth, 100.0, qty=10)
    order = await run(client, auth, await product(client, auth, [(raw, 5)]), 2)
    assert (await issue(client, auth, order, [(raw, taken)], key="i")).status_code == 200
    before = await snapshot(session, auth, raw, order)

    r = await _set_cost(client, auth, raw, 300.0)

    assert r.status_code == 409 and "cannot be carried" in r.text, r.text
    assert await snapshot(session, auth, raw, order) == before
    assert (await _state(session, auth, order))["wip_issued"] == f"{10.0 * taken:.2f}"
    await assert_settled(client, session, auth)
