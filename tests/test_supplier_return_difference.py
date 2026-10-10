# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A supplier return relieves stock at what the lot actually carries and classifies the rest.

The supplier credits what the bill charged for the units sent back. Stock is relieved at the
returned units' own cost, capped at the lot's carrying value. Any excess of the credit over
the value relieved is a stock gain; any shortfall, and freight the supplier does not credit,
is stock shrinkage. Cost of sales already recognised never moves, and the last return of a
bill never re-spreads the payable over the stock it relieves.
"""
from __future__ import annotations

from stock_books import assert_settled
from test_cost_restatement import _item, _state
import uuid

from test_landed_cost_pools import _po_into, _sell
from test_money_stock_and_contact_invariants import _account_net
from test_receipt_accounting import _doc, _finalize, _receive, _return

_ACCOUNTS = ("1130-OB", "1130-P", "1130-FRT", "2110", "4300", "5100", "6970")


async def _gl(session, auth) -> dict[str, float]:
    return {c: round(await _account_net(session, auth["company_id"], c), 2) for c in _ACCOUNTS}


def _delta(before: dict, after: dict) -> dict[str, float]:
    return {k: round(after[k] - before[k], 2) for k in after if round(after[k] - before[k], 2)}


def _stock(d: dict) -> float:
    return round(sum(v for k, v in d.items() if k.startswith("1130")), 2)


async def _send_back(client, session, auth, bill: str, lot: str, qty: float) -> dict[str, float]:
    before = await _gl(session, auth)
    r = await _return(client, auth, bill, lot, qty)
    assert r.status_code == 200, r.text
    return _delta(before, await _gl(session, auth))


async def test_excess_on_the_last_return_goes_to_stock_gain(client, session, auth):
    """Lot 10 at 1.00, a bill adds 1 at 1000.00 (11 at 1010.00). Sell 10: the lot keeps 1 at
    91.82. Returning that unit is the bill's last return: Dr payable 1000.00, Cr stock 91.82,
    Cr stock gain 908.18. Cost of sales and shrinkage do not move."""
    lot = await _item(client, auth, 10.0, qty=10)
    bill = await _po_into(client, auth, lot, 1, 1000.0)
    await _sell(client, auth, lot, 10)
    assert round(float((await _state(session, auth, lot))["cost_total"]), 2) == 91.82
    d = await _send_back(client, session, auth, bill, lot, 1)
    assert d.get("2110") == 1000.0, d
    assert _stock(d) == -91.82, d
    assert d.get("4300") == -908.18, d
    assert "6970" not in d and "5100" not in d, d
    assert (await _state(session, auth, lot)).get("status") == "disposed"
    await assert_settled(client, session, auth)


async def test_excess_on_a_return_that_is_not_the_last_goes_to_stock_gain(client, session, auth):
    """Lot 10 at 1.00, a bill adds 2 at 1000.00 (12 at 2010.00). Sell 10: the lot keeps 2 at
    335.00. Return 1 (the bill keeps the other): own cost 1000.00 capped at 335.00. Dr payable
    1000.00, Cr stock 335.00, Cr stock gain 665.00; never a credit to shrinkage."""
    lot = await _item(client, auth, 10.0, qty=10)
    bill = await _po_into(client, auth, lot, 2, 1000.0)
    await _sell(client, auth, lot, 10)
    d = await _send_back(client, session, auth, bill, lot, 1)
    assert d.get("2110") == 1000.0, d
    assert _stock(d) == -335.0, d
    assert d.get("4300") == -665.0, d
    assert "6970" not in d and "5100" not in d, d
    st = await _state(session, auth, lot)
    assert round(float(st.get("cost_total") or 0), 2) == 0.0
    # The second unit goes back too: the supplier credits 1000.00 again, the stock holds nothing.
    d = await _send_back(client, session, auth, bill, lot, 1)
    assert d.get("2110") == 1000.0, d
    assert _stock(d) == 0.0, d
    assert d.get("4300") == -1000.0, d
    assert "6970" not in d and "5100" not in d, d
    await assert_settled(client, session, auth)


async def test_a_whole_lot_returned_while_the_bill_keeps_other_goods_is_credited_at_the_bill_price(
        client, session, auth):
    """Lot 10 at 1.00; one bill adds 1 at 1000.00 to it and 1 at 50.00 of a new item (payable
    1050.00). Sell 10: the lot keeps 1 at 91.82. Returning that unit takes the lot whole while
    the bill keeps the other goods: Dr payable 1000.00, Cr stock 91.82, Cr stock gain 908.18;
    the bill still owes 50.00."""
    lot = await _item(client, auth, 10.0, qty=10)
    sku = f"SRD-{uuid.uuid4().hex[:6]}"
    bill = await _doc(client, auth, "purchase_order", [
        {"item_id": lot, "name": "Lot", "quantity": 1, "unit_price": 1000.0},
        {"sku": sku, "name": "Other", "quantity": 1, "unit_price": 50.0}])
    r = await _receive(client, auth, bill,
                       {"po_line_index": 0, "item_id": lot, "name": "Lot", "quantity_received": 1},
                       {"po_line_index": 1, "sku": sku, "name": "Other", "quantity_received": 1})
    assert r.status_code == 200, r.text
    await _finalize(client, auth, bill)
    await _sell(client, auth, lot, 10)
    d = await _send_back(client, session, auth, bill, lot, 1)
    assert d.get("2110") == 1000.0, d
    assert _stock(d) == -91.82, d
    assert d.get("4300") == -908.18, d
    assert "6970" not in d and "5100" not in d, d
    assert round(await _account_net(session, auth["company_id"], "2110"), 2) == -50.0
    await assert_settled(client, session, auth)


async def test_a_whole_lot_worth_more_than_the_credit_leaves_the_rest_as_shrinkage(client, session, auth):
    """Neighbour: lot 10 at 1000.00, a bill adds 1 at 1.00 (11 at 1001.00). Sell 10: the lot
    keeps 1 at 91.00. Returning it exhausts the lot: Dr payable 1.00, Cr stock 91.00, Dr
    shrinkage 90.00. Stock gain and cost of sales do not move."""
    lot = await _item(client, auth, 1000.0, qty=10)
    bill = await _po_into(client, auth, lot, 1, 1.0)
    await _sell(client, auth, lot, 10)
    d = await _send_back(client, session, auth, bill, lot, 1)
    assert d.get("2110") == 1.0, d
    assert _stock(d) == -91.0, d
    assert d.get("6970") == 90.0, d
    assert "4300" not in d and "5100" not in d, d
    await assert_settled(client, session, auth)


async def test_returning_at_the_bill_price_moves_no_gain_or_shrinkage(client, session, auth):
    """Neighbour: lot 10 at 10.00, a bill adds 5 at 10.00. Nothing sold. Return 2 then 3 (the
    last): each time payable and stock move by the bill's charge and nothing else moves."""
    lot = await _item(client, auth, 100.0, qty=10)
    bill = await _po_into(client, auth, lot, 5, 10.0)
    for qty in (2, 3):
        d = await _send_back(client, session, auth, bill, lot, qty)
        assert d == {"2110": 10.0 * qty, **{k: v for k, v in d.items() if k.startswith("1130")}}, d
        assert _stock(d) == -10.0 * qty, d
    assert round(float((await _state(session, auth, lot))["cost_total"]), 2) == 100.0
    await assert_settled(client, session, auth)


async def test_uncredited_freight_is_shrinkage_and_the_bill_still_owes_it(client, session, auth):
    """Neighbour (R09 clause 5): lot 10 at 10.00, a bill adds 5 at 14.00 with 7.00 shipping
    (15 at 177.00, payable 77.00). Return 2: stock relieved 30.80, Dr payable 28.00, Dr
    shrinkage 2.80; the lot keeps 13 at 146.20 and the bill owes 49.00."""
    lot = await _item(client, auth, 100.0, qty=10)
    bill = await _po_into(client, auth, lot, 5, 14.0, shipping=7.0)
    d = await _send_back(client, session, auth, bill, lot, 2)
    assert d.get("2110") == 28.0, d
    assert d.get("6970") == 2.8, d
    assert _stock(d) == -30.8, d
    assert "4300" not in d and "5100" not in d, d
    st = await _state(session, auth, lot)
    assert (float(st["quantity"]), round(float(st["cost_total"]), 2)) == (13.0, 146.2)
    assert round(await _account_net(session, auth["company_id"], "2110"), 2) == -49.0
    await assert_settled(client, session, auth)
