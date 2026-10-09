# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""One allocation rule, fixed freight pools, undo as "never happened", transforms that are not
receipts, and restated transforms that book their restatement.

- Every spread of an amount over parts (freight over a mother and her children, a merge, a
  split, a pool share) is largest-remainder whole cents, and every carve that takes value off
  the books posts what the lot recorded less what it records after (value_moved).
- A bill's freight pool is what the bill charged. Only that bill's own receipt, return, void or
  undo changes it; a count up values the found units at the lot's average goods cost.
- Undoing a receipt is refused once any unit has left the lot (or a part split off it) since.
- A transform makes a new product: it records transformed_from, is not a split of the
  received lot, and cannot go back to the supplier at cost.
- A transform at a cost other than the parent's books the change against stock gains or
  shrinkage, so stock and books agree afterwards.
"""
from __future__ import annotations

import ast
import pathlib
import uuid
from decimal import Decimal

import pytest

from stock_books import assert_settled
from test_cost_restatement import _item, _state
from test_landed_cost_pools import _b, _cost, _inv, _po_into, _post, _received, _sell
from test_landed_cost_removals import _audit, _seed, _split, _writeoff
from test_receipt_accounting import _OPENING, _doc, _finalize, _receive, _return

_VAT = {"doc_taxes": [{"code": "VAT", "rate": 7.0, "order": 1, "is_compound": False, "label": "VAT"}]}
_ORDERS = pytest.mark.parametrize("from_order", [False, True], ids=["bill_first", "order_first"])
LEGS = ("1130-OB", "1130-P", "1130-FRT", "1150", "2110", "4300", "5100", "6970")
_ROOT = pathlib.Path(__file__).resolve().parents[1]


def _goods(price: float, qty: float) -> dict:
    return {"sku": f"LCS-{uuid.uuid4().hex[:6]}", "name": "Beads", "quantity": qty, "unit_price": price}


async def _family(session, auth, root: str) -> list[str]:
    """The root and every lot split off it at any depth, while on hand."""
    from sqlalchemy import select

    from celerp.models.projections import Projection

    session.expire_all()
    rows = (await session.execute(select(Projection).where(
        Projection.company_id == auth["company_id"], Projection.entity_type == "item"))).scalars().all()
    by_parent: dict[str, list[str]] = {}
    for r in rows:
        if r.state.get("split_from"):
            by_parent.setdefault(str(r.state["split_from"]), []).append(r.entity_id)
    states = {r.entity_id: r.state for r in rows}
    out, queue = [], [root]
    while queue:
        x = queue.pop(0)
        s = states.get(x) or {}
        if s.get("status") not in ("sold", "disposed", "archived", "merged") and float(s.get("quantity") or 0) > 0:
            out.append(x)
        queue += sorted(by_parent.get(x, []))
    return out


async def _pools(session, auth, lots, doc=None) -> list[float]:
    out = []
    for x in lots:
        for k, v in ((await _state(session, auth, x)).get("landed_costs") or {}).items():
            if doc is None or k.startswith(f"{doc}::"):
                out.append(float(v))
    return out


def _in_cents(values) -> bool:
    return all(round(v, 2) == v for v in values)


async def _undo(client, auth, doc):
    return await client.delete(f"/docs/{doc}/receive", headers=auth["headers"])


async def _take_back(client, auth, doc):
    r = await _post(client, auth, doc, "void", {"reason": "entered in error"})
    if r.status_code != 200:
        r = await _post(client, auth, doc, "revert-to-draft")
    assert r.status_code == 200, r.text


def _refused_units_sold(r) -> None:
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["message_key"] == "docs.undo_receipt_units_sold", r.text


async def _po_received(client, session, auth, qty, price, ship, **extra):
    doc = await _doc(client, auth, "purchase_order", [_goods(price, qty)], shipping=ship, **extra)
    li = (await _state(session, auth, doc))["line_items"][0]
    r = await _receive(client, auth, doc, {"po_line_index": 0, "sku": li["sku"], "name": li["name"],
                                           "quantity_received": qty})
    assert r.status_code == 200, r.text
    [lot] = (await _state(session, auth, doc))["received_item_ids"]
    return doc, lot


async def _counted_lot(client, session, auth):
    """A lot of 3 units at a location: goods 1.00 (opening 0.50 x 1, then 2 at 0.25) and the
    order's freight pool of 1.00."""
    await _seed(session, auth)
    r = await client.post("/companies/me/locations", headers=auth["headers"],
                          json={"name": f"W-{uuid.uuid4().hex[:4]}", "type": "warehouse"})
    loc = r.json()["id"]
    r = await client.post("/items", headers=auth["headers"], json={
        "sku": f"LCS-{uuid.uuid4().hex[:6]}", "name": "Counted", "quantity": 1,
        "sell_by": "piece", "status": "available", "location_id": loc, "cost_total": 0.5})
    assert r.status_code == 200, r.text
    lot = r.json()["id"]
    doc = await _po_into(client, auth, lot, 2, 0.25, shipping=1.0)
    await assert_settled(client, session, auth)
    return lot, doc, loc


async def _transformed(client, session, auth, child_qty, child_cost=None):
    """A bill of 5 at 10.00 with 1.00 shipping, received; 2 split off and transformed."""
    doc, [lot] = await _received(client, session, auth, [_goods(10.0, 5)], from_order=False, shipping=1.0)
    [kid] = await _split(client, auth, lot, 2)
    body = {"child_sku": f"LCS-{uuid.uuid4().hex[:6]}", "child_category": "Processed",
            "child_sell_by": "piece", "child_quantity": child_qty}
    if child_cost is not None:
        body["child_cost_total"] = child_cost
    r = await client.post(f"/items/{kid}/transform", headers=auth["headers"], json=body)
    assert r.status_code == 200, r.text
    return doc, lot, kid, r.json()["child_id"]


# ── A. One allocation rule ───────────────────────────────────────────────────

@pytest.mark.parametrize("amount, weights, currency, shares", [
    ("1.00", [1, 1, 1], "USD", ["0.34", "0.33", "0.33"]),
    ("2.00", [1, 1, 1], "USD", ["0.67", "0.67", "0.66"]),
    ("-1.00", [1, 1, 1], "USD", ["-0.34", "-0.33", "-0.33"]),
    ("1.01", [13, 0, 7], "USD", ["0.66", "0.00", "0.35"]),
    ("100", [1, 1, 1], "JPY", ["34", "33", "33"]),
], ids=["thirds", "two_thirds", "negative", "zero_weight", "JPY"])
def test_allocate_pro_rata_is_largest_remainder(amount, weights, currency, shares):
    from celerp.services.money import allocate_pro_rata

    out = allocate_pro_rata(Decimal(amount), [Decimal(w) for w in weights], currency)
    assert out == [Decimal(s) for s in shares]
    assert sum(out) == Decimal(amount)


def _calls(fn) -> set[str]:
    names = set()
    for n in ast.walk(fn):
        if isinstance(n, ast.Call):
            f = n.func
            names.add(f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else "")
    return names


def _callers(*targets: str) -> dict[tuple[str, str], set[str]]:
    """Every production function calling one of ``targets``: (module path, function) -> the
    names it calls."""
    out = {}
    for path in [*_ROOT.glob("celerp/**/*.py"), *_ROOT.glob("default_modules/*/celerp_*/**/*.py")]:
        tree = ast.parse(path.read_text())
        for fn in ast.walk(tree):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                called = _calls(fn)
                if called & set(targets):
                    out[(str(path.relative_to(_ROOT)), fn.name)] = called
    return out


_INV = "default_modules/celerp-inventory/celerp_inventory"
_DOCS = "default_modules/celerp-docs/celerp_docs/routes.py"

# Every function that carves a lot (carve_cost, or pools_kept for units leaving), and how the
# value that leaves reaches the books. "value_moved": it posts value_moved itself.
# "value_boundary": the quantity event is booked by lot_origin.value_boundary, which posts
# value_moved. "lot_to_lot": the carve moves cost between lots on one account and posts
# nothing; what each part carries is checked by its own callers below.
_CARVERS = {
    (f"{_INV}/services.py", "pools_kept"): "helper",
    (f"{_INV}/services.py", "adjust_item_quantity"): "value_boundary",
    (f"{_INV}/routes.py", "split_item"): "lot_to_lot",
    (f"{_INV}/routes.py", "split_off_child"): "lot_to_lot",
    (_DOCS, "adjust_audit"): "value_moved",
    ("default_modules/celerp-manufacturing/celerp_manufacturing/movements.py", "_issue"): "value_moved",
}

# Every caller of split_off_child. A part carved off to leave the books posts value_moved; a
# part carved off to be sold whole or put back leaves at its own recorded cost.
_SPLIT_CALLERS = {
    (_DOCS, "return_consignment_items"): "value_moved",
    (_DOCS, "write_off_stock"): "value_moved",
    (_DOCS, "_apply_split_plan"): "lot_to_lot",
    (_DOCS, "_revert_lines_impl"): "lot_to_lot",
}


# A new way of carving a lot has to say how its value reaches the books, and one that takes
# value off the books has to post value_moved: the set of carvers is closed.
def test_every_carve_posts_through_value_moved():
    carvers = _callers("carve_cost", "pools_kept")
    assert set(carvers) == set(_CARVERS), sorted(set(carvers) ^ set(_CARVERS))
    for key, how in _CARVERS.items():
        if how == "value_moved":
            assert "value_moved" in carvers[key], key
    boundary = _callers("value_moved").get(("celerp/services/lot_origin.py", "value_boundary"))
    assert boundary is not None, "value_boundary no longer posts value_moved"

    splitters = _callers("split_off_child")
    assert set(splitters) == set(_SPLIT_CALLERS), sorted(set(splitters) ^ set(_SPLIT_CALLERS))
    for key, how in _SPLIT_CALLERS.items():
        if how == "value_moved":
            assert "value_moved" in splitters[key], key


# Thirds: 3 units with 1.00 shipping split into three, one part sold, then the bill. Every pool
# is whole cents, and what the held parts carry plus what the sale expensed is the 1.00.
async def test_bill_after_split_spreads_freight_in_whole_cents(client, session, auth):
    doc, lot = await _po_received(client, session, auth, 3, 1.0, 1.0)
    [a] = await _split(client, auth, lot, 1)
    await _split(client, auth, lot, 1)
    cogs0 = (await _b(session, auth, "5100"))["5100"]
    await _sell(client, auth, a, 1)
    await _finalize(client, auth, doc)
    await _inv(client, session, auth, doc, step="billed")
    pools = await _pools(session, auth, await _family(session, auth, lot), doc)
    assert _in_cents(pools), pools
    books = await _b(session, auth, "1130-FRT", "5100")
    assert books["1130-FRT"] == 0.0, books
    assert round(sum(pools) + books["5100"] - cogs0 - 1.0, 2) == 1.0, (pools, books)


# Split of a split, a grandchild sold whole, a mother unit sold, then the bill; then everything
# held goes back on it.
async def test_split_of_split_sold_then_bill_then_return_all(client, session, auth):
    doc, lot = await _po_received(client, session, auth, 10, 1.0, 10.0)
    [kid] = await _split(client, auth, lot, 4)
    [gkid] = await _split(client, auth, kid, 2)
    cogs0 = (await _b(session, auth, "5100"))["5100"]
    await _sell(client, auth, gkid, 2)
    await _sell(client, auth, lot, 1)
    await _finalize(client, auth, doc)
    await _inv(client, session, auth, doc, step="billed")
    pools = await _pools(session, auth, await _family(session, auth, lot), doc)
    books = await _b(session, auth, "1130-FRT", "5100")
    assert books["1130-FRT"] == 0.0 and _in_cents(pools), (books, pools)
    assert (round(sum(pools), 2), round(books["5100"] - cogs0, 2)) == (7.0, 6.0), (pools, books)
    for x in await _family(session, auth, lot):
        r = await _return(client, auth, doc, x, float((await _state(session, auth, x))["quantity"]))
        assert r.status_code == 200, r.text
        await _inv(client, session, auth, doc, step=f"returned {x}")
    assert await _b(session, auth, "1130-FRT", "1130-P") == {"1130-FRT": 0.0, "1130-P": 0.0}


@pytest.mark.parametrize("currency, rate", [("EUR", 36.123457), ("JPY", 0.0067)], ids=["EUR", "JPY"])
async def test_fx_bill_after_split_and_sale_then_return_all(client, session, auth, currency, rate):
    doc, lot = await _po_received(client, session, auth, 7, 3.33, 11.11, currency=currency, conversion_rate=rate)
    [kid] = await _split(client, auth, lot, 3)
    await _sell(client, auth, kid, 2)
    await _finalize(client, auth, doc)
    await _inv(client, session, auth, doc, rate=rate, step="billed")
    assert (await _b(session, auth, "1130-FRT"))["1130-FRT"] == 0.0
    for x in await _family(session, auth, lot):
        r = await _return(client, auth, doc, x, float((await _state(session, auth, x))["quantity"]))
        assert r.status_code == 200, r.text
    await assert_settled(client, session, auth)
    assert await _b(session, auth, "1130-FRT", "1130-P") == {"1130-FRT": 0.0, "1130-P": 0.0}


# Many awkward parts, a sale, a write-off and a sale of a grandchild, then everything held
# goes back: the cents never drift.
@_ORDERS
async def test_many_carves_then_return_all_keep_cents(client, session, auth, from_order):
    doc, [lot] = await _received(client, session, auth, [_goods(0.37, 13)], from_order=from_order,
                                 shipping=1.01, **_VAT)
    kids = await _split(client, auth, lot, 1, 1, 1, 2, 3)
    await _inv(client, session, auth, doc, step="split")
    assert _in_cents(await _pools(session, auth, [lot, *kids], doc))
    await _sell(client, auth, kids[0], 1)
    await _writeoff(client, auth, kids[3], 1)
    await _inv(client, session, auth, doc, step="written off")
    [gk] = await _split(client, auth, kids[4], 1)
    await _sell(client, auth, gk, 1)
    await _inv(client, session, auth, doc, step="grandchild sold")
    for x in await _family(session, auth, lot):
        r = await _return(client, auth, doc, x, float((await _state(session, auth, x))["quantity"]))
        assert r.status_code == 200, r.text
        await _inv(client, session, auth, doc, step=f"returned {x}")
    books = await _b(session, auth, "1130-P", "1130-FRT")
    assert books == {"1130-P": 0.0, "1130-FRT": 0.0}, books


# A part split off and returned with VAT: the input VAT follows the bill's tax on what went back.
@_ORDERS
async def test_split_child_return_reverses_its_vat(client, session, auth, from_order):
    doc, [lot] = await _received(client, session, auth, [_goods(10.0, 3)], from_order=from_order,
                                 shipping=1.0, **_VAT)
    [kid] = await _split(client, auth, lot, 1)
    vat0 = (await _b(session, auth, "1150"))["1150"]
    assert (await _return(client, auth, doc, kid, 1)).status_code == 200
    await _inv(client, session, auth, doc, step="kid")
    assert round(vat0 - (await _b(session, auth, "1150"))["1150"], 2) == 0.70
    assert (await _return(client, auth, doc, lot, 2)).status_code == 200
    await _inv(client, session, auth, doc, step="all")
    books = await _b(session, auth, "1130-P", "1130-FRT")
    assert books == {"1130-P": 0.0, "1130-FRT": 0.0}, books


# A grandchild and a child of the receipt lot together send back no more than came in, across
# requests and inside one request.
async def test_split_parts_cannot_return_more_than_received(client, session, auth):
    lot = await _item(client, auth, _OPENING, qty=10)
    doc = await _po_into(client, auth, lot, 5, 14.0, shipping=7.0)
    [kid] = await _split(client, auth, lot, 8)
    [gkid] = await _split(client, auth, kid, 4)
    assert (await _return(client, auth, doc, gkid, 3)).status_code == 200
    assert (await _return(client, auth, doc, kid, 3)).status_code == 422
    r = await client.post(f"/docs/{doc}/return-items", headers=auth["headers"], json={"items": [
        {"item_id": kid, "quantity_returned": 2}, {"item_id": gkid, "quantity_returned": 1}]})
    assert r.status_code == 422, r.text
    assert (await _return(client, auth, doc, kid, 2)).status_code == 200
    await _inv(client, session, auth, doc, step="returned")
    assert await _b(session, auth, "1130-FRT") == {"1130-FRT": 0.0}


async def test_split_parcel_one_request_over_return_refused(client, session, auth):
    doc, [lot] = await _received(client, session, auth, [_goods(10.0, 5)], from_order=False, shipping=1.0)
    [kid] = await _split(client, auth, lot, 2)
    before = await _b(session, auth, *LEGS)
    r = await client.post(f"/docs/{doc}/return-items", headers=auth["headers"], json={"items": [
        {"item_id": lot, "quantity_returned": 3}, {"item_id": kid, "quantity_returned": 2},
        {"item_id": kid, "quantity_returned": 1}]})
    assert r.status_code == 422, r.text
    assert await _b(session, auth, *LEGS) == before
    await _inv(client, session, auth, doc, step="refused")


# ── B. A bill's freight pool is what the bill charged ────────────────────────

# Counting the lot up from 3 to 4 leaves the freight pool at 1.00 and values the found unit at
# the lot's average goods cost (1.00 / 3), booked as a stock gain.
async def test_count_up_keeps_freight_pool_and_values_found_units_at_goods_cost(client, session, auth):
    lot, doc, loc = await _counted_lot(client, session, auth)
    gain0 = (await _b(session, auth, "4300"))["4300"]
    await _audit(client, auth, lot, loc, 4)
    st = await _state(session, auth, lot)
    assert round(sum((st.get("landed_costs") or {}).values()), 2) == 1.0, st
    assert round(float(st["cost_total"]), 2) == 2.33, st
    assert round(gain0 - (await _b(session, auth, "4300"))["4300"], 2) == 0.33
    await assert_settled(client, session, auth)


async def test_count_up_then_return_all_received(client, session, auth):
    lot, doc, loc = await _counted_lot(client, session, auth)
    await _audit(client, auth, lot, loc, 4)
    r = await _return(client, auth, doc, lot, 2)
    assert r.status_code == 200, r.text
    await assert_settled(client, session, auth)
    assert (await _b(session, auth, "1130-FRT"))["1130-FRT"] == 0.0


# A count up takes nothing out, so the receipt can still be undone: the order's goods and its
# whole freight pool leave the lot, the found unit stays, and the books carry the lot.
async def test_count_up_then_undo_receipt_then_take_bill_back(client, session, auth):
    lot, doc, loc = await _counted_lot(client, session, auth)
    await _audit(client, auth, lot, loc, 4)
    r = await _undo(client, auth, doc)
    assert r.status_code == 200, r.text
    st = await _state(session, auth, lot)
    assert float(st["quantity"]) == 2.0 and not st.get("landed_costs"), st
    assert round(float(st["cost_total"]), 2) == 0.83, st
    # The finalized bill waits for its goods: 0.50 on 1130-P and 1.00 on clearing.
    books = await _b(session, auth, "1130-OB", "1130-P", "1130-FRT")
    assert books == {"1130-OB": 0.83, "1130-P": 0.5, "1130-FRT": 1.0}, books
    await _take_back(client, auth, doc)
    await assert_settled(client, session, auth)
    books = await _b(session, auth, "1130-FRT", "1130-P")
    assert books == {"1130-FRT": 0.0, "1130-P": 0.0}, books


# ── C. Undo receipt means the receipt never happened ─────────────────────────

# The received parcel itself was partly sold: the same refusal as for a lot topped up, not a
# "moved on" conflict.
@_ORDERS
async def test_undo_refused_when_parcel_partly_sold(client, session, auth, from_order):
    doc, [lot] = await _received(client, session, auth, [_goods(10.0, 5)], from_order=from_order, shipping=1.0)
    await _sell(client, auth, lot, 2)
    before = await _b(session, auth, *LEGS)
    _refused_units_sold(await _undo(client, auth, doc))
    assert await _b(session, auth, *LEGS) == before
    await assert_settled(client, session, auth)


# Units sold from a part split off the parcel count as gone too.
async def test_undo_refused_when_split_part_sold(client, session, auth):
    doc, [lot] = await _received(client, session, auth, [_goods(10.0, 5)], from_order=False, shipping=1.0)
    [kid] = await _split(client, auth, lot, 2)
    await _sell(client, auth, kid, 1)
    before = await _b(session, auth, *LEGS)
    _refused_units_sold(await _undo(client, auth, doc))
    assert await _b(session, auth, *LEGS) == before


# Receive again after an undo, sell part of the new parcel, undo again: refused, nothing moves.
async def test_undo_refused_on_second_receipt_cycle(client, session, auth):
    doc, [lot] = await _received(client, session, auth, [_goods(10.0, 5)], from_order=False, shipping=1.0)
    assert (await _undo(client, auth, doc)).status_code == 200
    li = (await _state(session, auth, doc))["line_items"][0]
    r = await _receive(client, auth, doc, {"po_line_index": 0, "sku": li["sku"], "name": li["name"],
                                           "quantity_received": 5})
    assert r.status_code == 200, r.text
    [parcel] = (await _state(session, auth, doc))["received_item_ids"]
    await _sell(client, auth, parcel, 3)
    before = await _b(session, auth, *LEGS)
    _refused_units_sold(await _undo(client, auth, doc))
    assert await _b(session, auth, *LEGS) == before
    await _inv(client, session, auth, doc, step="refused")


# A cheap receipt into an expensive lot with most units sold since: refused, so no unit is left
# carrying more than its share.
async def test_undo_refused_cheap_receipt_most_sold(client, session, auth):
    lot = await _item(client, auth, 200.0, qty=10)
    doc = await _po_into(client, auth, lot, 5, 1.0)
    await _sell(client, auth, lot, 9)
    before = await _b(session, auth, *LEGS)
    _refused_units_sold(await _undo(client, auth, doc))
    assert await _b(session, auth, *LEGS) == before
    await assert_settled(client, session, auth)


async def test_undo_refused_after_merge(client, session, auth):
    from test_cost_restatement import _merge

    lot = await _item(client, auth, _OPENING, qty=10)
    doc = await _po_into(client, auth, lot, 5, 14.0, shipping=7.0)
    await _merge(client, auth, [lot, await _item(client, auth, 50.0, qty=5)])
    before = await _b(session, auth, *LEGS)
    r = await _undo(client, auth, doc)
    assert r.status_code in (409, 422), r.text
    assert await _b(session, auth, *LEGS) == before
    await assert_settled(client, session, auth)


# Another document's own goods going back (its receipt undone, or returned to its supplier)
# leave at that document's cost and take nothing of this receipt's, so this receipt can still
# be undone and the lot ends where it started.
@pytest.mark.parametrize("other_back", ["undone", "returned"])
async def test_undo_allowed_after_other_documents_goods_went_back(client, session, auth, other_back):
    lot = await _item(client, auth, _OPENING, qty=10)
    start = await _state(session, auth, lot)
    first = await _po_into(client, auth, lot, 5, 14.0)
    second = await _po_into(client, auth, lot, 5, 20.0)
    if other_back == "undone":
        r = await _undo(client, auth, first)
    else:
        r = await _return(client, auth, first, lot, 5)
    assert r.status_code == 200, r.text
    r = await _undo(client, auth, second)
    assert r.status_code == 200, r.text
    end = await _state(session, auth, lot)
    assert (float(end["quantity"]), end.get("cost_total")) == (float(start["quantity"]), start.get("cost_total"))


# Undo, then receive the same goods again, then send them all back: clearing ends at zero.
async def test_undo_then_receive_again_then_return_all(client, session, auth):
    lot = await _item(client, auth, _OPENING, qty=10)
    doc = await _po_into(client, auth, lot, 5, 14.0, shipping=7.0)
    assert (await _undo(client, auth, doc)).status_code == 200
    r = await _receive(client, auth, doc, {"po_line_index": 0, "item_id": lot, "name": "Lot",
                                           "quantity_received": 5})
    assert r.status_code == 200, r.text
    await _inv(client, session, auth, doc, step="received again")
    [parcel] = (await _state(session, auth, doc))["received_item_ids"]
    assert round(sum(await _pools(session, auth, [parcel], doc)), 2) == 7.0
    r = await _return(client, auth, doc, parcel, 5)
    assert r.status_code == 200, r.text
    await _inv(client, session, auth, doc, step="returned")
    assert (await _b(session, auth, "1130-FRT"))["1130-FRT"] == 0.0


# ── D. Transforms are not receipts ───────────────────────────────────────────

async def test_transform_records_transformed_from_not_split_from(client, session, auth):
    doc, lot, kid, made = await _transformed(client, session, auth, 10)
    st = await _state(session, auth, made)
    assert st.get("transformed_from") == kid and not st.get("split_from"), st


# Two units cut into ten pieces cannot go back on the bill at cost, and the 3 real units
# still on the received lot stay returnable.
async def test_transformed_goods_cannot_return_on_bill(client, session, auth):
    doc, lot, kid, made = await _transformed(client, session, auth, 10)
    before = await _b(session, auth, *LEGS)
    r = await _return(client, auth, doc, made, 5)
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["message_key"] == "docs.return_lot_transformed", r.text
    assert "credit note" in r.json()["detail"]["message"]
    assert await _b(session, auth, *LEGS) == before
    assert (await _return(client, auth, doc, lot, 3)).status_code == 200
    await _inv(client, session, auth, doc, step="returned")


# ── E. A transform at a restated cost books the restatement ──────────────────

# 2 units carrying 20.40 transformed at 40.00: 1130 moves by 19.60 against stock gains, and
# selling the product afterwards leaves stock and books equal.
@pytest.mark.parametrize("child_cost, moved", [(40.0, 19.60), (10.0, -10.40)], ids=["up", "down"])
async def test_transform_at_restated_cost_books_the_change(client, session, auth, child_cost, moved):
    doc, [lot] = await _received(client, session, auth, [_goods(10.0, 5)], from_order=False, shipping=1.0)
    [kid] = await _split(client, auth, lot, 2)
    assert await _cost(session, auth, kid) == 20.40
    p0 = (await _b(session, auth, "1130-P"))["1130-P"]
    r = await client.post(f"/items/{kid}/transform", headers=auth["headers"], json={
        "child_sku": f"LCS-{uuid.uuid4().hex[:6]}", "child_category": "Processed", "child_sell_by": "piece",
        "child_quantity": 2, "child_cost_total": child_cost})
    assert r.status_code == 200, r.text
    made = r.json()["child_id"]
    assert round((await _b(session, auth, "1130-P"))["1130-P"] - p0, 2) == moved
    await _inv(client, session, auth, doc, step="transformed")
    before = await _b(session, auth, *LEGS)
    r = await _return(client, auth, doc, made, 2)
    assert r.status_code == 422, r.text
    assert await _b(session, auth, *LEGS) == before
    await _sell(client, auth, made, 2)
    await assert_settled(client, session, auth)
    assert (await _return(client, auth, doc, lot, 3)).status_code == 200
    await _inv(client, session, auth, doc, step="returned")
