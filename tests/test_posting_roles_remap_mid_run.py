# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Changing the inventory accounts while a production run is open.

Materials issued before the change go back to the account their lots record, never to
the new one; output received after it is booked on the account the change named; and
every step, including undoing a receipt and reopening the run, leaves each inventory
account carrying exactly the stock it holds.
"""
from __future__ import annotations

import pytest

from celerp.services.account_roles import set_role
from mfg_runs import OPENING, PURCHASED, balances, complete, give_back, issue, product, receive, reopen, run, \
    undo_receipt
from stock_books import assert_settled
from test_cost_restatement import _item, _state
from test_posting_roles_lots import _FIELD, _new_inventory_account

pytestmark = pytest.mark.asyncio


async def _remap(session, auth, role: str, code: str) -> None:
    await set_role(session, auth["company_id"], role, code)
    await session.commit()


async def test_remapping_inventory_in_the_middle_of_a_run_keeps_every_account_equal_to_its_stock(
        client, session, auth):
    raw = await _item(client, auth, 100.0, qty=10)  # 10 each, booked on opening inventory
    assert (await _state(session, auth, raw))[_FIELD] == "1130-OB"
    order = await run(client, auth, await product(client, auth, [(raw, 2)]), 3)
    assert (await issue(client, auth, order, key="i")).status_code == 200
    await assert_settled(client, session, auth)

    await _remap(session, auth, OPENING, await _new_inventory_account(client, auth, "1131"))
    await _remap(session, auth, PURCHASED, await _new_inventory_account(client, auth, "1132"))
    await assert_settled(client, session, auth)

    # Part of the materials comes back: to the lot, and so to the account the lot records.
    r = await give_back(client, auth, order, [(raw, 2)], key="g")
    assert r.status_code == 200, r.text
    assert (await _state(session, auth, raw))[_FIELD] == "1130-OB"
    before = await balances(session, auth)
    assert (before.get("1130-OB"), before.get("1131", 0), before.get("1132", 0)) == (60.0, 0, 0)
    await assert_settled(client, session, auth)
    # Issued again after the change: still taken off the account the lot records.
    assert (await issue(client, auth, order, key="i2")).status_code == 200
    assert (await balances(session, auth)).get("1130-OB") == 40.0
    await assert_settled(client, session, auth)

    r = await receive(client, auth, order, 1, key="r1")
    assert r.status_code == 200, r.text
    lot = r.json()["lot_item_id"]
    assert (await _state(session, auth, lot))[_FIELD] == "1132"  # made after the change
    await assert_settled(client, session, auth)

    assert (await undo_receipt(client, auth, order, lot, key="u")).status_code == 200
    assert (await balances(session, auth)).get("1132", 0) == 0
    await assert_settled(client, session, auth)

    r = await complete(client, auth, order, key="c")
    assert r.status_code == 200, r.text
    outputs = (await _state(session, auth, order))["received_lots"]
    assert outputs
    for out in outputs:
        assert (await _state(session, auth, out))[_FIELD] == "1132"
    done = await balances(session, auth)
    assert (done.get("1130-OB"), done.get("1132")) == (40.0, 60.0)  # 6 issued at 10 each became the output
    await assert_settled(client, session, auth)

    r = await reopen(client, auth, order, key="o")
    assert r.status_code == 200, r.text
    await assert_settled(client, session, auth)
    assert (await balances(session, auth)).get("1130-OB") == 40.0  # the issued lot still holds what it held
