# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Who a receipt's freight belongs to, what counts as its units leaving, and what a supplier
return and a revert take off the books.

- A bill made from an order and taken back to the order after all its goods went back
  reverses the freight it capitalised: every leg ends where it started, as for a bill
  received after it was finalized.
- Every spread of an amount over parts goes through the largest-remainder allocator, so no
  part is ever negative. The set of spreaders is closed.
- A receipt's lineage is the lot it came into and the lots split off that lot after it.
  A lot split off before the receipt gets none of its freight, and its sales are not the
  receipt's units leaving.
- A removal undone by its own reversal (a write-off and its undo, a sale and its reversal,
  a count and its undo) did not take any units away. A count up never hides a sale.
- A bill's goods going back to its supplier come off accounts payable and off stock at that
  bill's price, as an undo of its receipt would take them, never more than the lot carries;
  what the price comes to beyond that goes to stock shrinkage, and the other bills' freight
  stays on the lot.
- In a foreign currency, the partial returns of a bill take off accounts payable its share of
  the bill in the company's currency, so accounts payable follows what is still owed.
- A lot made by a transform is a different product, however it was recorded: a transform's
  product from before transforms stopped inheriting split_from cannot go back on the bill,
  and is not a part of the receipt still on hand.
- Another bill's goods going back leave at that bill's price, so they never take this
  receipt's cost with them: its receipt can still be undone while the lot holds its units.
- A bill taken back to its order reverses the tax and payable its returns took off with the
  bill, and finalizing it again puts them back.
- Goods in a lot split off before the receipt never came in on it, so they cannot go back on it.
- Finalizing a bill again after taking it back to its order puts its freight where it was:
  the units still held carry it, the units sold expensed it and the units sent back
  shrank it.
"""
from __future__ import annotations

import ast
import uuid

import pytest

from stock_books import assert_settled
from test_cost_restatement import _state
from test_landed_cost_pools import _b, _inv, _po_into, _post, _received, _sell
from test_landed_cost_removals import _audit, _seed, _split, _writeoff
from test_landed_cost_structural import (LEGS, _ROOT, _callers, _counted_lot, _family, _goods,
                                         _refused_units_sold, _transformed, _undo)
from test_money_stock_and_contact_invariants import _account_net
from test_receipt_accounting import _doc, _finalize, _receive, _return

_ORDERS = pytest.mark.parametrize("from_order", [False, True], ids=["bill_first", "order_first"])
_VAT = {"doc_taxes": [{"code": "VAT", "rate": 7.0, "order": 1, "is_compound": False, "label": "VAT"}]}


async def _location(client, auth) -> str:
    r = await client.post("/companies/me/locations", headers=auth["headers"],
                          json={"name": f"W-{uuid.uuid4().hex[:4]}", "type": "warehouse"})
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _opening(client, auth, qty: float, cost: float) -> str:
    r = await client.post("/items", headers=auth["headers"], json={
        "sku": f"LCL-{uuid.uuid4().hex[:6]}", "name": "Opening", "quantity": qty, "sell_by": "piece",
        "status": "available", "cost_total": cost})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _doc_pools(state: dict, doc: str) -> dict[str, float]:
    return {k: float(v) for k, v in (state.get("landed_costs") or {}).items() if k.startswith(f"{doc}::")}


# ── D1. A revert takes back the freight the bill capitalised ─────────────────

@_ORDERS
async def test_full_return_then_revert_leaves_every_leg_where_it_started(client, session, auth, from_order):
    await _seed(session, auth)
    start = await _b(session, auth, *LEGS)
    doc, [lot] = await _received(client, session, auth, [_goods(10.0, 5)], from_order=from_order, shipping=1.0)
    r = await _return(client, auth, doc, lot, 5)
    assert r.status_code == 200, r.text
    r = await _post(client, auth, doc, "revert-to-draft")
    assert r.status_code == 200, r.text
    assert await _b(session, auth, *LEGS) == start
    assert not _doc_pools(await _state(session, auth, lot), doc)
    await assert_settled(client, session, auth)


# Goods added to a lot on hand: the lot keeps its own unit and cost, and no freight pool of an
# order that has no bill any more.
async def test_into_lot_full_return_then_revert_leaves_the_lot_as_it_was(client, session, auth):
    await _seed(session, auth)
    lot = await _opening(client, auth, 1, 0.5)
    start = await _b(session, auth, *LEGS)
    doc = await _po_into(client, auth, lot, 2, 0.25, shipping=1.0)
    r = await _return(client, auth, doc, lot, 2)
    assert r.status_code == 200, r.text
    r = await _post(client, auth, doc, "revert-to-draft")
    assert r.status_code == 200, r.text
    assert await _b(session, auth, *LEGS) == start
    st = await _state(session, auth, lot)
    assert not _doc_pools(st, doc), st.get("landed_costs")
    assert (float(st["quantity"]), round(float(st["cost_total"]), 2)) == (1.0, 0.5), st
    await assert_settled(client, session, auth)


@pytest.mark.parametrize("ops", ["none", "sell", "count", "ret2", "count_ret2", "count_sell", "sell_ret1",
                                 "count_sell_ret2"])
async def test_revert_after_counts_sales_and_returns_clears_freight(client, session, auth, ops):
    lot, doc, loc = await _counted_lot(client, session, auth)
    if "count" in ops:
        await _audit(client, auth, lot, loc, 4)
    if "sell" in ops:
        await _sell(client, auth, lot, 1)
    if "ret" in ops:
        r = await _return(client, auth, doc, lot, int(ops[-1]))
        assert r.status_code == 200, r.text
    await _post(client, auth, doc, "revert-to-draft")
    await assert_settled(client, session, auth)
    assert (await _b(session, auth, "1130-FRT"))["1130-FRT"] == 0.0


# ── D2. One allocator for every spread ──────────────────────────────────────

def test_allocate_landed_cost_is_largest_remainder():
    from celerp.services.landed_cost import allocate_landed_cost

    goods = [{"key": i, "value": 1.0, "qty": 1.0} for i in range(7)]
    out = allocate_landed_cost(goods, [{"kind": "freight", "amount": 0.05}], "USD")
    shares = sorted(out[i].get("freight", 0.0) for i in range(7))
    assert shares == [0.0, 0.0, 0.01, 0.01, 0.01, 0.01, 0.01], shares


async def test_bill_lines_never_take_a_negative_freight_pool(client, session, auth):
    doc, lots = await _received(client, session, auth, [_goods(1.0, 1) for _ in range(7)],
                                from_order=False, shipping=0.05)
    pools = []
    for x in lots:
        pools += list(_doc_pools(await _state(session, auth, x), doc).values())
    assert all(p >= 0 for p in pools) and round(sum(pools), 2) == 0.05, pools
    await assert_settled(client, session, auth)


_DOCS = "default_modules/celerp-docs/celerp_docs/routes.py"
_INV = "default_modules/celerp-inventory/celerp_inventory/services.py"
_MFG = "default_modules/celerp-manufacturing/celerp_manufacturing/movements.py"
_AJE = "celerp/services/auto_je.py"

# Every function that spreads an amount over parts through the allocator.
_SPREADERS = {
    ("celerp/services/money.py", "allocate_pro_rata"): "the allocator",
    ("celerp/services/landed_cost.py", "allocate_landed_cost"): "a bill's landed cost over its goods lines",
    (_AJE, "_inventory_lines"): "a lot's value over its inventory accounts",
    (_AJE, "_clearing_lines"): "landed cost over its clearing roles",
    (_AJE, "bill_line_charges"): "a bill's discount and tax over its lines",
    (_AJE, "create_for_supplier_return"): "the last return's payable over the goods accounts",
    (_MFG, "_close"): "a production run's cost over its outputs",
    (_MFG, "reconcile"): "a production run's variance over its outputs",
    (_DOCS, "_received_goods_cost"): "a line's cost over its receipts",
    (_DOCS, "_received_landed"): "a line's landed cost over its receipts",
    (_DOCS, "_capitalise_landed_received"): "freight over a receipt's lineage",
    (_INV, "carve_cost"): "a lot's cost over a part and the rest",
    (_INV, "kept"): "carve_cost's share of one amount",
}

# Functions that put a rounding unit on one part, and why that is not a spread over parts.
_RESIDUALS = {
    (_AJE, "create_for_landed_capitalisation"): "one rounding unit on the largest expensed role",
    (_AJE, "_base_debits"): "the conversion's rounding unit on the largest goods line",
    ("default_modules/celerp-accounting/celerp_accounting/routes.py", "_expand_item_lines"): "report display",
    ("default_modules/celerp-accounting/celerp_accounting/routes.py", "_split_cash_movement"): "cash flow report",
}


def _residual_placers() -> set[tuple[str, str]]:
    import re

    out = set()
    for path in [*_ROOT.glob("celerp/**/*.py"), *_ROOT.glob("default_modules/*/celerp_*/**/*.py")]:
        for fn in ast.walk(ast.parse(path.read_text())):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
                    isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)
                    and re.search(r"residual|largest|biggest", n.id) for n in ast.walk(fn)):
                out.add((str(path.relative_to(_ROOT)), fn.name))
    return out


# A new spread has to go through the allocator, and a new rounding residual has to say why it
# is not a spread: both sets are closed.
def test_every_spread_goes_through_the_allocator():
    spreaders = _callers("allocate_pro_rata", "received_share")
    assert set(spreaders) == set(_SPREADERS), sorted(set(spreaders) ^ set(_SPREADERS))
    placers = _residual_placers()
    assert placers == set(_RESIDUALS), sorted(placers ^ set(_RESIDUALS))


# ── D3. A receipt's lineage starts at the receipt ───────────────────────────

async def test_a_lot_split_off_before_the_receipt_takes_none_of_its_freight(client, session, auth):
    await _seed(session, auth)
    lot = await _opening(client, auth, 10, 200.0)
    [kid] = await _split(client, auth, lot, 3)
    doc = await _po_into(client, auth, lot, 5, 1.0, shipping=1.0)
    assert not (await _state(session, auth, kid)).get("landed_costs")
    assert _doc_pools(await _state(session, auth, lot), doc) == {f"{doc}::freight": 1.0}
    await assert_settled(client, session, auth)


async def test_selling_a_lot_split_off_before_the_receipt_leaves_the_undo_open(client, session, auth):
    await _seed(session, auth)
    lot = await _opening(client, auth, 10, 200.0)
    [kid] = await _split(client, auth, lot, 3)
    doc = await _po_into(client, auth, lot, 5, 1.0, shipping=1.0)
    await _sell(client, auth, kid, 3)
    r = await _undo(client, auth, doc)
    assert r.status_code == 200, r.text


# ── D4. A removal and its own reversal cancel ───────────────────────────────

async def test_a_write_off_undone_leaves_the_receipt_undo_open(client, session, auth):
    await _seed(session, auth)
    lot = await _opening(client, auth, 1, 20.0)
    doc = await _po_into(client, auth, lot, 2, 1.0, shipping=1.0)
    h = auth["headers"]
    wo = (await client.post("/lists/writeoff", headers=h, json={"entity_ids": [lot]})).json()["id"]
    lines = (await client.get(f"/lists/{wo}", headers=h)).json()["line_items"]
    lid = next(line["line_id"] for line in lines if line["item_id"] == lot)
    r = await client.post(f"/lists/{wo}/writeoff-line", headers=h, json={"line_id": lid, "qty_out": 3, "account": "6950"})
    assert r.status_code == 200, r.text
    assert (await client.post(f"/lists/{wo}/write-off", headers=h)).status_code == 200
    assert (await client.post(f"/lists/{wo}/undo-write-off", headers=h)).status_code == 200
    r = await _undo(client, auth, doc)
    assert r.status_code == 200, r.text


# A count up on a part split off after the receipt does not bring back the unit it sold.
async def test_a_count_up_does_not_hide_a_sale(client, session, auth):
    lot, doc, loc = await _counted_lot(client, session, auth)
    [kid] = await _split(client, auth, lot, 2)
    await _sell(client, auth, kid, 1)
    await _audit(client, auth, kid, loc, 2)
    _refused_units_sold(await _undo(client, auth, doc))


# A partial write-off undone, then a part of the same lot sold: the sale still counts.
async def test_a_write_off_undone_does_not_hide_a_later_sale(client, session, auth):
    lot, doc, _loc = await _counted_lot(client, session, auth)
    h = auth["headers"]
    wo = (await client.post("/lists/writeoff", headers=h, json={"entity_ids": [lot]})).json()["id"]
    lines = (await client.get(f"/lists/{wo}", headers=h)).json()["line_items"]
    lid = next(line["line_id"] for line in lines if line["item_id"] == lot)
    await client.post(f"/lists/{wo}/writeoff-line", headers=h, json={"line_id": lid, "qty_out": 1, "account": "6950"})
    assert (await client.post(f"/lists/{wo}/write-off", headers=h)).status_code == 200
    assert (await client.post(f"/lists/{wo}/undo-write-off", headers=h)).status_code == 200
    await _sell(client, auth, lot, 1)
    _refused_units_sold(await _undo(client, auth, doc))


# ── L1. Another bill's return comes off at that bill's price ────────────────

async def test_another_bills_return_out_of_a_mixed_lot_is_credited_at_its_price(client, session, auth):
    from celerp_inventory.services import goods_basis

    await _seed(session, auth)
    lot = await _opening(client, auth, 10, 200.0)
    b = await _po_into(client, auth, lot, 5, 100.0)
    await _sell(client, auth, lot, 12)
    a = await _po_into(client, auth, lot, 5, 1.0, shipping=5.0)
    before = await _b(session, auth, "2110", "6970")
    r = await _return(client, auth, b, lot, 3)
    assert r.status_code == 200, r.text
    after = await _b(session, auth, "2110", "6970")
    # Accounts payable falls by 3 x 100.00. The goods leave at that price, but the lot carries
    # only 145.00 of goods, so all of it leaves and the 155.00 it no longer carried is a credit
    # to shrinkage. The units that stay re-average to nothing; A's freight stays with them.
    assert round(after["2110"] - before["2110"], 2) == 300.0, (before, after)
    assert round(after["6970"] - before["6970"], 2) == -155.0, (before, after)
    assert float((await _state(session, auth, b))["amount_outstanding"]) == 200.0
    st = await _state(session, auth, lot)
    assert float(st["quantity"]) == 5.0
    assert round(goods_basis(st), 2) == 0.0, st
    assert _doc_pools(st, a) == {f"{a}::freight": 5.0}, st.get("landed_costs")
    await _inv(client, session, auth, b, others=float((await _state(session, auth, a))["amount_outstanding"]))


# ── L2. Partial returns in a foreign currency follow what is still owed ─────

async def _ap_follows(session, auth, docs, rate, step):
    owed = 0.0
    for d in docs:
        st = await _state(session, auth, d)
        if st.get("doc_type") != "purchase_order" and st.get("status") not in ("draft", "void"):
            owed += round(float(st.get("amount_outstanding") or 0) * rate, 2)
    ap = await _account_net(session, auth["company_id"], "2110")
    assert abs(round(ap, 2) + round(owed, 2)) <= 0.01 * len(docs) + 1e-9, (step, ap, -owed)


@pytest.mark.parametrize("currency, rate", [(None, None), ("EUR", 36.123457), ("JPY", 0.0067)],
                         ids=["base", "EUR", "JPY"])
async def test_accounts_payable_follows_what_is_owed_through_many_partial_returns(
        client, session, auth, currency, rate):
    await _seed(session, auth)
    extra = dict(_VAT)
    if currency:
        extra.update(currency=currency, conversion_rate=rate)
    rate = rate or 1.0
    docs, lots = [], []
    for i, (q, p, s) in enumerate([(13, 0.37, 1.01), (7, 3.33, 11.11), (11, 1.07, 0.97), (9, 2.21, 0.13),
                                   (17, 0.19, 3.07)]):
        doc, [lot] = await _received(client, session, auth, [_goods(p, q)], from_order=bool(i % 2),
                                     shipping=s, **extra)
        docs.append(doc)
        lots.append(lot)
        await _ap_follows(session, auth, docs, rate, f"received {i}")
    n = 0
    for doc, lot in zip(docs, lots):
        kids = await _split(client, auth, lot, 1, 2)
        gk = await _split(client, auth, kids[1], 1)
        await _sell(client, auth, kids[0], 1)
        await _sell(client, auth, gk[0], 1)
        await _writeoff(client, auth, lot, 1)
        await _sell(client, auth, lot, 1)
        n += 6
        for x in (lot, kids[1]):
            r = await _return(client, auth, doc, x, 1)
            assert r.status_code == 200, r.text
            n += 1
            await _ap_follows(session, auth, docs, rate, f"op {n}")
        for x in [lot, *kids, *gk]:
            for _ in range(2):
                s = await _state(session, auth, x)
                if s.get("status") == "available" and float(s.get("quantity") or 0) >= 2:
                    [y] = await _split(client, auth, x, 1)
                    await _sell(client, auth, y, 1)
                    n += 2
        for x in await _family(session, auth, lot):
            r = await _return(client, auth, doc, x, float((await _state(session, auth, x))["quantity"]))
            assert r.status_code == 200, r.text
            n += 1
            await _ap_follows(session, auth, docs, rate, f"op {n}")
            await assert_settled(client, session, auth)
    assert n >= 50
    assert (await _b(session, auth, "1130-FRT"))["1130-FRT"] == 0.0


# One unit at a time until the bill holds none: within a cent at every step, exact at the end.
@pytest.mark.parametrize("currency, rate", [("EUR", 36.123457), ("JPY", 0.0067)], ids=["EUR", "JPY"])
async def test_returning_a_bill_one_unit_at_a_time_ends_exactly(client, session, auth, currency, rate):
    await _seed(session, auth)
    doc, [lot] = await _received(client, session, auth, [_goods(0.37, 13)], from_order=False, shipping=1.01,
                                 currency=currency, conversion_rate=rate, **_VAT)
    for i in range(13):
        r = await _return(client, auth, doc, lot, 1)
        assert r.status_code == 200, r.text
        await _ap_follows(session, auth, [doc], rate, f"return {i}")
    await _inv(client, session, auth, doc, rate=rate, step="all back")


# ── A transform's product from before transforms stopped inheriting split_from ──

async def _transformed_before(client, session, auth, field: str):
    """A bill of 5 at 10.00 received, 2 split off and transformed into 10, the product recorded
    the way transforms recorded it before: carrying the split_from of the lot it was made from.
    ``field`` "kept" also keeps transformed_from; "gone" leaves only the ledger's transform;
    "new" leaves the product as transforms record it now."""
    from celerp.models.projections import Projection

    doc, lot, kid, made = await _transformed(client, session, auth, 10)
    if field == "new":
        return doc, lot, made
    row = await session.get(Projection, {"company_id": auth["company_id"], "entity_id": made})
    state = {**row.state, "split_from": lot}
    if field == "gone":
        state.pop("transformed_from", None)
    row.state = state
    await session.commit()
    return doc, lot, made


@pytest.mark.parametrize("field", ["new", "kept", "gone"])
async def test_an_older_transform_product_cannot_go_back_on_the_bill(client, session, auth, field):
    doc, lot, made = await _transformed_before(client, session, auth, field)
    before = await _b(session, auth, *LEGS)
    r = await _return(client, auth, doc, made, 5)
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["message_key"] == "docs.return_lot_transformed", r.text
    assert await _b(session, auth, *LEGS) == before
    assert (await _return(client, auth, doc, lot, 3)).status_code == 200


@pytest.mark.parametrize("field", ["new", "kept", "gone"])
async def test_an_older_transform_product_is_not_the_receipt_still_on_hand(client, session, auth, field):
    doc, lot, made = await _transformed_before(client, session, auth, field)
    before = await _b(session, auth, *LEGS)
    _refused_units_sold(await _undo(client, auth, doc))
    assert await _b(session, auth, *LEGS) == before


# ── Another bill's return leaves at that bill's price ───────────────────────

# Bill B makes a lot of 5 and 3 are sold; bill A then adds 4 at 20.00 (and bill C 5 at 10.00).
# B sending back 5 at 10.00 leaves the lot 1 unit, too few to undo A's 4. Sending back 2, or 3
# while C's goods keep the lot stocked, leaves A's 80.00 in the lot, and A's receipt undoes
# cleanly. The bill still stands after an undo, so the books are checked settled before it.
@pytest.mark.parametrize("back, third, refused", [(5, False, True), (2, False, False), (3, True, False)],
                         ids=["more_than_held", "own_only", "beyond_own_still_stocked"])
async def test_another_bills_return_leaves_this_receipt_undoable(client, session, auth, back, third, refused):
    await _seed(session, auth)
    b, [lot] = await _received(client, session, auth, [_goods(10.0, 5)], from_order=False)
    await _sell(client, auth, lot, 3)
    a = await _po_into(client, auth, lot, 4, 20.0)
    if third:
        await _po_into(client, auth, lot, 5, 10.0)
    r = await _return(client, auth, b, lot, back)
    assert r.status_code == 200, r.text
    await assert_settled(client, session, auth)
    before = await _b(session, auth, *LEGS)
    from celerp_inventory.services import goods_basis

    st = await _state(session, auth, lot)
    held, cost = float(st["quantity"]), round(goods_basis(st) or 0.0, 2)
    r = await _undo(client, auth, a)
    if refused:
        assert r.status_code == 409, r.text
        assert await _b(session, auth, *LEGS) == before
    else:
        assert r.status_code == 200, r.text
        st = await _state(session, auth, lot)
        assert (float(st["quantity"]), round(goods_basis(st) or 0.0, 2)) == (held - 4, round(cost - 80.0, 2)), st


# ── S1. A bill's return relieves stock at the bill's price ──────────────────

# Opening 10 at 20.00; A adds 5 at 100.00; 5 are sold at the average (or split off and sold);
# B adds 5 at 10.00 and sends them straight back. Stock and accounts payable fall by 50.00 and
# nothing goes to shrinkage: the same as undoing B's receipt.
@pytest.mark.parametrize("sale", ["lot", "split"])
async def test_a_bills_return_relieves_stock_at_its_price(client, session, auth, sale):
    await _seed(session, auth)
    lot = await _opening(client, auth, 10, 200.0)
    await _po_into(client, auth, lot, 5, 100.0)
    if sale == "split":
        [kid] = await _split(client, auth, lot, 5)
        await _sell(client, auth, kid, 5)
    else:
        await _sell(client, auth, lot, 5)
    b = await _po_into(client, auth, lot, 5, 10.0)
    before = await _b(session, auth, *LEGS)
    cost = round(float((await _state(session, auth, lot))["cost_total"]), 2)
    r = await _return(client, auth, b, lot, 5)
    assert r.status_code == 200, r.text
    after = await _b(session, auth, *LEGS)
    delta = {k: round(after[k] - before[k], 2) for k in LEGS if after[k] != before[k]}
    assert delta == {"2110": 50.0, "1130-OB": -50.0}, delta
    assert round(float((await _state(session, auth, lot))["cost_total"]), 2) == round(cost - 50.0, 2)
    await assert_settled(client, session, auth)


# Three bills on one lot, with a sale, a split, a write-off and a count between them: B's 4
# units go back at 50.00 each, 200.00 off stock and off accounts payable, and A's and C's
# freight stays on the lot.
async def test_three_bills_return_relieves_the_returning_bills_price(client, session, auth):
    from celerp_inventory.services import goods_basis

    await _seed(session, auth)
    loc = await _location(client, auth)
    r = await client.post("/items", headers=auth["headers"], json={
        "sku": f"LCL-{uuid.uuid4().hex[:6]}", "name": "Opening", "quantity": 10, "sell_by": "piece",
        "status": "available", "cost_total": 200.0, "location_id": loc})
    assert r.status_code == 200, r.text
    lot = r.json()["id"]
    a = await _po_into(client, auth, lot, 5, 30.0, shipping=5.0)
    await _sell(client, auth, lot, 6)
    b = await _po_into(client, auth, lot, 4, 50.0, shipping=2.0)
    await _split(client, auth, lot, 3)
    await _writeoff(client, auth, lot, 1)
    c = await _po_into(client, auth, lot, 3, 10.0, shipping=0.3)
    await _audit(client, auth, lot, loc, float((await _state(session, auth, lot))["quantity"]) + 1)
    s0 = await _state(session, auth, lot)
    before = await _b(session, auth, "2110")
    r = await _return(client, auth, b, lot, 4)
    assert r.status_code == 200, r.text
    s1 = await _state(session, auth, lot)
    assert round((await _b(session, auth, "2110"))["2110"] - before["2110"], 2) == 200.0
    assert round(goods_basis(s0) - goods_basis(s1), 2) == 200.0, (s0["cost_total"], s1["cost_total"])
    assert (_doc_pools(s1, a), _doc_pools(s1, c)) == (_doc_pools(s0, a), _doc_pools(s0, c))
    await assert_settled(client, session, auth)


# ── F2. A revert to the order takes the returns' tax off with the bill ──────

@pytest.mark.parametrize("steps", [[13], [3, 10]], ids=["full_once", "partial_then_rest"])
@pytest.mark.parametrize("currency, rate", [("THB", 1.0), ("EUR", 36.123457)], ids=["THB", "EUR"])
@pytest.mark.parametrize("vat", [True, False], ids=["vat", "novat"])
async def test_revert_to_order_after_returns_leaves_every_leg_where_it_started(client, session, auth, steps,
                                                                             currency, rate, vat):
    await _seed(session, auth)
    start = await _b(session, auth, *LEGS, "6960")
    extra = dict(_VAT) if vat else {}
    if currency != "THB":
        extra.update(currency=currency, conversion_rate=rate)
    doc, [lot] = await _received(client, session, auth, [_goods(0.37, 13)], from_order=True, shipping=1.01, **extra)
    for qty in steps:
        r = await _return(client, auth, doc, lot, qty)
        assert r.status_code == 200, r.text
    r = await _post(client, auth, doc, "revert-to-draft")
    assert r.status_code == 200, r.text
    assert await _b(session, auth, *LEGS, "6960") == start
    await assert_settled(client, session, auth)


# The goods back in two returns, then the bill taken back to its order and finalized again:
# the returns' tax and payable come back with the bill, so the books are as before the revert.
@pytest.mark.parametrize("currency, rate", [("THB", 1.0), ("EUR", 36.123457)], ids=["THB", "EUR"])
async def test_refinalize_after_revert_restores_the_returns_charges(client, session, auth, currency, rate):
    await _seed(session, auth)
    extra = dict(_VAT)
    if currency != "THB":
        extra.update(currency=currency, conversion_rate=rate)
    doc, [lot] = await _received(client, session, auth, [_goods(0.37, 13)], from_order=True, shipping=1.01, **extra)
    for qty in (3, 10):
        r = await _return(client, auth, doc, lot, qty)
        assert r.status_code == 200, r.text
    returned = await _b(session, auth, *LEGS, "6960")
    assert (await _post(client, auth, doc, "revert-to-draft")).status_code == 200
    assert (await _post(client, auth, doc, "finalize")).status_code == 200
    assert await _b(session, auth, *LEGS, "6960") == returned
    await assert_settled(client, session, auth)


# ── F3. Goods split off before the receipt never came in on it ─────────────

async def test_a_lot_split_off_before_the_receipt_cannot_go_back_on_it(client, session, auth):
    await _seed(session, auth)
    lot = await _opening(client, auth, 6, 60.0)
    [pre] = await _split(client, auth, lot, 2)
    doc = await _doc(client, auth, "purchase_order", [{"item_id": lot, "name": "Lot", "quantity": 6,
                                                       "unit_price": 20.0}], shipping=3.0)
    r = await _receive(client, auth, doc, {"po_line_index": 0, "item_id": lot, "name": "Lot", "quantity_received": 3})
    assert r.status_code == 200, r.text
    await _finalize(client, auth, doc)
    [post] = await _split(client, auth, lot, 2)
    r = await _receive(client, auth, doc, {"po_line_index": 0, "item_id": lot, "name": "Lot", "quantity_received": 3})
    assert r.status_code == 200, r.text
    before = await _b(session, auth, *LEGS)
    r = await _return(client, auth, doc, pre, 1)
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["message_key"] == "docs.return_lot_split_before_receipt", r.text
    assert await _b(session, auth, *LEGS) == before
    r = await _return(client, auth, doc, post, 2)
    assert r.status_code == 200, r.text
    await assert_settled(client, session, auth)


# ── D1. Finalizing again puts the freight back where it was ─────────────────

# Opening 2 at 1.00; an order adds 2 at 0.25 with 1.00 freight and becomes the bill; 1 unit is
# sold and the bill's 2 go back. Taken back to the order, the bill's freight leaves the books
# by where it went: off cost of goods sold for the unit sold, off shrinkage for the units sent
# back. Finalized again, the books and the lot are as they were before the revert.
async def test_refinalize_after_revert_puts_the_freight_back_where_it_was(client, session, auth):
    await _seed(session, auth)
    lot = await _opening(client, auth, 2, 1.0)
    start = await _b(session, auth, *LEGS)
    doc = await _po_into(client, auth, lot, 2, 0.25, shipping=1.0)
    await _sell(client, auth, lot, 1)
    goods_sold = round((await _b(session, auth, "5100"))["5100"] - start["5100"] - 0.25, 2)
    r = await _return(client, auth, doc, lot, 2)
    assert r.status_code == 200, r.text
    returned = await _b(session, auth, *LEGS)
    pools = _doc_pools(await _state(session, auth, lot), doc)
    assert (await _post(client, auth, doc, "revert-to-draft")).status_code == 200
    reverted = await _b(session, auth, *LEGS)
    assert (reverted["6970"], round(reverted["5100"] - start["5100"], 2)) == (start["6970"], goods_sold), reverted
    await assert_settled(client, session, auth)
    assert (await _post(client, auth, doc, "finalize")).status_code == 200
    assert await _b(session, auth, *LEGS) == returned
    assert _doc_pools(await _state(session, auth, lot), doc) == pools == {f"{doc}::freight": 0.25}
    await assert_settled(client, session, auth)
