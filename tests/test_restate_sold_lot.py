# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Correcting the cost of a sold lot moves cost of sales, never stock that is gone.

A sold lot is no longer on any inventory account, so the change in its cost is booked to
cost of sales against stock gains (an increase) or stock shrinkage (a decrease), and every
inventory account still carries exactly the stock on hand.
"""
from __future__ import annotations

import uuid

import pytest

from stock_books import assert_settled
from test_cost_restatement import _item, _merge, _sell, _set_cost, auth, ids  # noqa: F401  (auth and ids are fixtures)
from test_money_stock_and_contact_invariants import _account_net

pytestmark = pytest.mark.asyncio


async def _restate(client, session, auth, lot: str, cost: float) -> dict[str, float]:
    cid = auth["company_id"]
    codes = ("1130-OB", "5100", "4300", "6970")
    before = {c: await _account_net(session, cid, c) for c in codes}
    r = await _set_cost(client, auth, lot, cost, key=f"k-{uuid.uuid4()}")
    assert r.status_code == 200, r.text
    return {c: round(await _account_net(session, cid, c) - before[c], 2) for c in codes}


@pytest.mark.parametrize(("cost", "moved"), [
    (55.55, {"1130-OB": 0.0, "5100": 18.05, "4300": -18.05, "6970": 0.0}),
    (30.0, {"1130-OB": 0.0, "5100": -7.5, "4300": 0.0, "6970": 7.5}),
])
async def test_a_sold_lots_corrected_cost_moves_cost_of_sales_against_its_source(client, session, auth, cost, moved):
    lot = await _item(client, auth, 37.5, qty=3)
    await _sell(client, session, auth, lot)
    assert await _restate(client, session, auth, lot, cost) == moved
    await assert_settled(client, session, auth)


async def test_a_sold_merge_results_corrected_source_moves_cost_of_sales(client, session, auth):
    a = await _item(client, auth, 10.0)
    b = await _item(client, auth, 20.0)
    result = await _merge(client, auth, [a, b])
    await _sell(client, session, auth, result)
    assert await _restate(client, session, auth, a, 15.0) == {"1130-OB": 0.0, "5100": 5.0, "4300": -5.0, "6970": 0.0}
    await assert_settled(client, session, auth)
