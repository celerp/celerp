# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Every way stock leaves a lot that carries landed cost takes what the lot carried, to the cent.

A lot holds its goods and its landed cost pools rounded to the cent. Whatever removes part of
it (a sale, a write-off, an audit count, a split, a return to the supplier, an undone receipt)
must take exactly what the lot held before less what it holds after, so the books always carry
the stock. Undoing a receipt means it never happened, so it is refused once any unit has left
the lot since; a return to the supplier takes the goods back at cost instead.
"""
from __future__ import annotations

import uuid

import pytest

from stock_books import assert_settled
from test_cost_restatement import _item, _state
from test_landed_cost_pools import _b, _cost, _inv, _on_hand, _po_into, _received, _sell
from test_receipt_accounting import _OPENING, _doc, _finalize, _receive, _return

_VAT = {"doc_taxes": [{"code": "VAT", "rate": 7.0, "order": 1, "is_compound": False, "label": "VAT"}]}
_ORDERS = pytest.mark.parametrize("from_order", [False, True], ids=["bill_first", "order_first"])


def _goods(price: float, qty: float) -> dict:
    return {"sku": f"LCR-{uuid.uuid4().hex[:6]}", "name": "Beads", "quantity": qty, "unit_price": price}


async def _held(session, auth, lot) -> str:
    """The lot still on hand in a sale family (the mother keeps the rest)."""
    held = await _on_hand(session, auth, lot)
    assert held, "nothing on hand"
    return held[0]


async def _seed(session, auth):
    from celerp_accounting.routes import seed_chart_of_accounts
    await seed_chart_of_accounts(session, auth["company_id"])
    await session.commit()


async def _split(client, auth, lot: str, *qtys: float) -> list[str]:
    r = await client.post(f"/items/{lot}/split", headers=auth["headers"],
                          json={"children": [{"quantity": q} for q in qtys]})
    assert r.status_code == 200, r.text
    return [c["id"] for c in r.json()["children"]]


async def _writeoff(client, auth, lot: str, qty_out: float, account: str = "6950") -> dict:
    h = auth["headers"]
    r = await client.post("/lists/writeoff", headers=h, json={"entity_ids": [lot]})
    assert r.status_code == 200, r.text
    wo = r.json()["id"]
    lines = (await client.get(f"/lists/{wo}", headers=h)).json()["line_items"]
    lid = next(line["line_id"] for line in lines if line["item_id"] == lot)
    r = await client.post(f"/lists/{wo}/writeoff-line", headers=h,
                          json={"line_id": lid, "qty_out": qty_out, "account": account})
    assert r.status_code == 200, r.text
    r = await client.post(f"/lists/{wo}/write-off", headers=h)
    assert r.status_code == 200, r.text
    return r.json()


async def _pooled_lot(client, session, auth) -> str:
    """A lot of 3 units carrying goods 1.00 and a freight pool 1.00 (opening 0.50 x 1, then
    2 units at 0.25 bought with 1.00 shipping)."""
    lot = await _item(client, auth, 0.5, qty=1)
    await _po_into(client, auth, lot, 2, 0.25, shipping=1.0)
    st = await _state(session, auth, lot)
    assert round(float(st["cost_total"]), 2) == 2.0, st
    assert sum((st.get("landed_costs") or {}).values()) == pytest.approx(1.0), st
    await assert_settled(client, session, auth)
    return lot


# ── Write-off of part of a pooled lot ────────────────────────────────────────

# Goods 1.00 + freight 1.00 over 3 units; write off 1. The lot keeps 0.67 + 0.67 = 1.34, so
# the write-off takes 0.66: what the lot held before less what it holds after.
@pytest.mark.parametrize("pooled", [True, False], ids=["goods_and_freight", "goods_only"])
async def test_writeoff_part_of_pooled_lot_books_carry_stock(client, session, auth, pooled):
    await _seed(session, auth)
    lot = await _pooled_lot(client, session, auth) if pooled else await _item(client, auth, 2.0, qty=3)
    out = await _writeoff(client, auth, lot, 1)
    left = await _cost(session, auth, lot)
    assert round(out["value"] + left, 2) == 2.0, ("write-off value + lot left != lot before", out["value"], left)
    await assert_settled(client, session, auth)


# The same lot sold one unit at a time: the three sales relieve 2.00 in total.
async def test_sell_pooled_lot_one_by_one(client, session, auth):
    lot = await _pooled_lot(client, session, auth)
    cogs0 = (await _b(session, auth, "5100"))["5100"]
    for _ in range(3):
        await _sell(client, auth, await _held(session, auth, lot), 1)
        await assert_settled(client, session, auth)
    assert round((await _b(session, auth, "5100"))["5100"] - cogs0, 2) == 2.0


# ── Audit count down of a pooled lot ─────────────────────────────────────────

async def _audit(client, auth, item_id: str, loc: str, counted: float) -> dict:
    h = auth["headers"]
    r = await client.post("/lists/audit", headers=h, json={"location_id": loc})
    assert r.status_code == 200, r.text
    audit = r.json()["id"]
    assert (await client.post(f"/lists/{audit}/finalize", headers=h)).status_code == 200
    r = await client.patch(f"/lists/{audit}/line/{item_id}", headers=h, json={"counted_qty": counted})
    assert r.status_code == 200, r.text
    r = await client.post(f"/lists/{audit}/adjust", headers=h)
    assert r.status_code == 200, r.text
    return r.json()


# Counting the lot down one unit at a time books what the lot held before less what it holds
# after, and the adjustment records that value.
@pytest.mark.parametrize("pooled", [True, False], ids=["goods_and_freight", "goods_only"])
async def test_audit_counts_down_by_one(client, session, auth, pooled):
    from sqlalchemy import select

    from celerp.models.ledger import LedgerEntry

    await _seed(session, auth)
    r = await client.post("/companies/me/locations", headers=auth["headers"],
                          json={"name": f"W-{uuid.uuid4().hex[:4]}", "type": "warehouse"})
    loc = r.json()["id"]
    r = await client.post("/items", headers=auth["headers"], json={
        "sku": f"LCR-{uuid.uuid4().hex[:6]}", "name": "Counted", "quantity": 1 if pooled else 3,
        "sell_by": "piece", "status": "available", "location_id": loc,
        "cost_total": 0.5 if pooled else 2.0})
    assert r.status_code == 200, r.text
    lot = r.json()["id"]
    if pooled:
        await _po_into(client, auth, lot, 2, 0.25, shipping=1.0)
    await assert_settled(client, session, auth)
    for counted in (2, 1):
        before = await _cost(session, auth, lot)
        out = await _audit(client, auth, lot, loc, counted)
        after = await _cost(session, auth, lot)
        assert round(out["shrinkage_value"], 2) == round(before - after, 2), (out, before, after)
        await assert_settled(client, session, auth)
    values = (await session.execute(select(LedgerEntry.data).where(
        LedgerEntry.company_id == auth["company_id"], LedgerEntry.entity_id == lot,
        LedgerEntry.event_type == "item.quantity.adjusted").order_by(LedgerEntry.id))).scalars().all()
    audited = [d["value"] for d in values if d.get("reason") == "audit"]
    # The lot keeps the cent share of its whole cost, goods and freight together, the share an
    # invoice costs the same units at: 2.00 -> 1.33 -> 0.67 whether or not it carries a pool,
    # so the first count takes 0.67 and the second 0.66.
    assert audited == [0.67, 0.66], audited


# ── Undo a receipt once any of its units left ────────────────────────────────

_BOOKS = ("1130-OB", "1130-P", "1130-FRT", "2110", "5100", "6970")


async def _sold_from_received_lot(client, session, auth, sold: float = 3):
    """Opening 10 units (100.00) + an order of 5 at 14.00 with 7.00 shipping into the same
    lot, then ``sold`` units sold at the lot's average cost."""
    lot = await _item(client, auth, _OPENING, qty=10)
    doc = await _po_into(client, auth, lot, 5, 14.0, shipping=7.0)
    await _sell(client, auth, lot, sold)
    await assert_settled(client, session, auth)
    return lot, doc


def _refused_units_sold(r, gone: float, needed: float) -> None:
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert detail["message_key"] == "docs.undo_receipt_units_sold", detail
    assert (detail["params"]["gone"], detail["params"]["needed"]) == (f"{gone:g}", f"{needed:g}"), detail
    assert "Return to supplier" in detail["message"], detail


# Undoing a receipt means it never happened. Once a unit has left the lot since, its cost went
# out at the lot's average, so the undo is refused and nothing moves; a return to the
# supplier takes the goods back at cost instead.
@pytest.mark.parametrize("sold", [1, 3, 14])
async def test_undo_receipt_refused_once_any_unit_left(client, session, auth, sold):
    lot, doc = await _sold_from_received_lot(client, session, auth, sold)
    before = await _b(session, auth, *_BOOKS)
    _refused_units_sold(await client.delete(f"/docs/{doc}/receive", headers=auth["headers"]), sold, 5)
    assert await _b(session, auth, *_BOOKS) == before
    assert float((await _state(session, auth, await _held(session, auth, lot)))["quantity"]) == 15 - sold
    await assert_settled(client, session, auth)


# A write-off takes units out as surely as a sale does.
async def test_undo_receipt_refused_after_writeoff(client, session, auth):
    await _seed(session, auth)
    lot = await _item(client, auth, _OPENING, qty=10)
    doc = await _po_into(client, auth, lot, 5, 14.0, shipping=7.0)
    await _writeoff(client, auth, lot, 2)
    before = await _b(session, auth, *_BOOKS)
    _refused_units_sold(await client.delete(f"/docs/{doc}/receive", headers=auth["headers"]), 2, 5)
    assert await _b(session, auth, *_BOOKS) == before
    await assert_settled(client, session, auth)


# The return to the supplier the refusal points to takes the 5 units back at cost and the
# books still carry the stock, with nothing left on clearing.
async def test_return_to_supplier_after_sale_settles(client, session, auth):
    lot, doc = await _sold_from_received_lot(client, session, auth)
    r = await _return(client, auth, doc, await _held(session, auth, lot), 5)
    assert r.status_code == 200, r.text
    await assert_settled(client, session, auth)
    assert (await _b(session, auth, "1130-FRT"))["1130-FRT"] == 0.0


# Bill A puts freight on the lot, order B adds goods with none, part is sold, then B's
# receipt cannot be undone: units left the lot after B came in.
async def test_sale_then_undo_other_bills_receipt(client, session, auth):
    lot = await _item(client, auth, _OPENING, qty=10)
    await _po_into(client, auth, lot, 5, 14.0, shipping=7.0)
    second = await _po_into(client, auth, lot, 5, 16.0)
    await _sell(client, auth, lot, 4)
    await assert_settled(client, session, auth)
    before = await _b(session, auth, *_BOOKS)
    _refused_units_sold(await client.delete(f"/docs/{second}/receive", headers=auth["headers"]), 4, 5)
    assert await _b(session, auth, *_BOOKS) == before
    await assert_settled(client, session, auth)


# Units sold before the receipt do not count: undoing a receipt nothing has left since
# takes its 5 units and its 7.00 of freight back off the lot and the books carry the rest.
async def test_undo_receipt_after_earlier_sale(client, session, auth):
    lot = await _item(client, auth, _OPENING, qty=10)
    await _sell(client, auth, lot, 3)
    held = await _held(session, auth, lot)
    doc = await _po_into(client, auth, held, 5, 14.0, shipping=7.0)
    r = await client.delete(f"/docs/{doc}/receive", headers=auth["headers"])
    assert r.status_code == 200, r.text
    st = await _state(session, auth, held)
    assert float(st["quantity"]) == 7.0 and not st.get("landed_costs"), st
    # The finalized bill waits for its goods: 70.00 on 1130-P and 7.00 on clearing.
    books = await _b(session, auth, "1130-OB", "1130-P", "1130-FRT")
    assert books == {"1130-OB": 70.0, "1130-P": 70.0, "1130-FRT": 7.0}, books
    await _inv(client, session, auth, doc, step="undone", settle=False)

# ── Split then sell both halves, split then return ───────────────────────────

@_ORDERS
async def test_split_then_sell_both_halves(client, session, auth, from_order):
    doc, [lot] = await _received(client, session, auth, [_goods(1.0, 7)], from_order=from_order, shipping=1.0)
    kids = await _split(client, auth, lot, 3, 1)
    await assert_settled(client, session, auth)
    assert round(sum([await _cost(session, auth, x) for x in [lot, *kids]]), 2) == 8.0
    cogs0 = (await _b(session, auth, "5100"))["5100"]
    for x in [*kids, lot]:
        await _sell(client, auth, x, float((await _state(session, auth, x))["quantity"]))
        await assert_settled(client, session, auth)
    books = await _b(session, auth, "5100", "1130-P", "1130-FRT")
    assert round(books["5100"] - cogs0, 2) == 8.0, books
    assert books["1130-P"] == 0.0 and books["1130-FRT"] == 0.0, books


# Split a lot, sell the child partly, return the mother's remainder to the supplier.
@_ORDERS
async def test_split_sell_child_return_mother(client, session, auth, from_order):
    doc, [lot] = await _received(client, session, auth, [_goods(1.0, 7)], from_order=from_order, shipping=1.0)
    [kid] = await _split(client, auth, lot, 3)
    await _sell(client, auth, kid, 2)
    await assert_settled(client, session, auth)
    r = await _return(client, auth, doc, lot, float((await _state(session, auth, lot))["quantity"]))
    assert r.status_code == 200, r.text
    await assert_settled(client, session, auth)
    assert await _b(session, auth, "1130-FRT") == {"1130-FRT": 0.0}


# Undo a receipt after the parcel was split: refused (the parcel moved on), the books untouched.
@_ORDERS
async def test_undo_receipt_after_split(client, session, auth, from_order):
    doc, [lot] = await _received(client, session, auth, [_goods(1.0, 7)], from_order=from_order, shipping=1.0)
    await _split(client, auth, lot, 3)
    r = await client.delete(f"/docs/{doc}/receive", headers=auth["headers"])
    assert r.status_code == 409, r.text
    await assert_settled(client, session, auth)
    assert await _b(session, auth, "1130-FRT") == {"1130-FRT": 0.0}


# A lot split by the user, then each part returned on the bill that received it: the freight
# on the lot leaves to the cent.
@_ORDERS
async def test_split_then_return_each_child(client, session, auth, from_order):
    doc, [lot] = await _received(client, session, auth, [_goods(1.0, 3)], from_order=from_order, shipping=1.0)
    parts = [lot]
    for _ in range(2):
        parts += await _split(client, auth, lot, 1)
    await _inv(client, session, auth, doc, step="split")
    for x in parts:
        r = await _return(client, auth, doc, x, float((await _state(session, auth, x))["quantity"]))
        assert r.status_code == 200, r.text
        await _inv(client, session, auth, doc, step=f"returned {x}", settle=False)
        await assert_settled(client, session, auth)
    books = await _b(session, auth, "1130-P", "1130-FRT", "6970", "2110")
    assert books == {"1130-P": 0.0, "1130-FRT": 0.0, "6970": 1.0, "2110": -1.0}, books


# A part split off goes back on the bill no more than the bill brought in: what the mother
# and her parts send back together is capped at the receipt.
async def test_split_parts_return_no_more_than_received(client, session, auth):
    lot = await _item(client, auth, _OPENING, qty=10)
    doc = await _po_into(client, auth, lot, 5, 14.0, shipping=7.0)
    [kid] = await _split(client, auth, lot, 8)
    assert (await _return(client, auth, doc, kid, 3)).status_code == 200
    r = await _return(client, auth, doc, lot, 3)
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["message_key"] == "docs.return_lot_more_than_received"
    assert (await _return(client, auth, doc, lot, 2)).status_code == 200
    await assert_settled(client, session, auth)
    assert await _b(session, auth, "1130-FRT") == {"1130-FRT": 0.0}


# Split after a merge, then return part of one purchase from the part split off. The supplier
# takes the goods back at what it charged for them.
async def test_merge_split_return_child(client, session, auth):
    lot = await _item(client, auth, _OPENING, qty=10)
    first = await _po_into(client, auth, lot, 5, 14.0, shipping=7.0)
    await _po_into(client, auth, lot, 6, 13.0, shipping=2.0)
    [child] = await _split(client, auth, lot, 7)
    await assert_settled(client, session, auth)
    ap = (await _b(session, auth, "2110"))["2110"]
    r = await _return(client, auth, first, child, 5)
    assert r.status_code == 200, r.text
    await assert_settled(client, session, auth)
    books = await _b(session, auth, "1130-FRT", "2110")
    assert books["1130-FRT"] == 0.0, books
    assert round(books["2110"] - ap, 2) == 70.0, books


# ── Freight capitalised after the goods moved on (order received, bill later) ─

# Ten units received on an order with 10.00 shipping, split 4 off, 2 sold from the mother and 1
# from the part: the bill then puts 7.00 of freight on the 7 units held, across the mother
# and every part split off her, and 3.00 on cost of sales.
async def test_bill_after_split_and_partial_sale_capitalises_held_share(client, session, auth):
    doc = await _doc(client, auth, "purchase_order", [_goods(1.0, 10)], shipping=10.0)
    li = (await _state(session, auth, doc))["line_items"][0]
    r = await _receive(client, auth, doc, {"po_line_index": 0, "sku": li["sku"], "name": li["name"],
                                           "quantity_received": 10})
    assert r.status_code == 200, r.text
    [lot] = (await _state(session, auth, doc))["received_item_ids"]
    [kid] = await _split(client, auth, lot, 4)
    cogs0 = (await _b(session, auth, "5100"))["5100"]
    await _sell(client, auth, lot, 2)
    await _sell(client, auth, kid, 1)
    await _finalize(client, auth, doc)
    await assert_settled(client, session, auth)
    held = 0.0
    for x in (lot, kid):
        held += sum(((await _state(session, auth, await _held(session, auth, x))).get("landed_costs") or {}).values())
    books = await _b(session, auth, "1130-FRT", "5100")
    assert books["1130-FRT"] == 0.0, books
    assert (round(held, 2), round(books["5100"] - cogs0, 2)) == (7.0, 6.0), (held, books["5100"] - cogs0)


# ── Many small removals of a pooled lot with awkward freight ─────────────────

@_ORDERS
async def test_many_small_sales_and_writeoffs_keep_cents(client, session, auth, from_order):
    await _seed(session, auth)
    doc, [lot] = await _received(client, session, auth, [_goods(0.37, 13)], from_order=from_order, shipping=1.01)
    cogs0 = (await _b(session, auth, "5100"))["5100"]
    wo = 0.0
    for i in range(12):
        h = await _held(session, auth, lot)
        if i % 3 == 2:
            wo += (await _writeoff(client, auth, h, 1))["value"]
        else:
            await _sell(client, auth, h, 1)
        await assert_settled(client, session, auth)
    left = await _cost(session, auth, await _held(session, auth, lot))
    books = await _b(session, auth, "5100", "1130-FRT")
    assert books["1130-FRT"] == 0.0
    assert round(books["5100"] - cogs0 + wo + left, 2) == round(13 * 0.37 + 1.01, 2), (books, wo, left)


# ── FX with freight and VAT, two goods lines, partial receipts ───────────────

@pytest.mark.parametrize("currency, rate, p1, p2, ship", [
    ("EUR", 36.123457, 1.11, 2.37, 9.99),
    ("JPY", 0.0067, 333.0, 777.0, 4999.0),
], ids=["EUR", "JPY"])
@_ORDERS
async def test_fx_two_lines_freight_vat_partial_receipts(client, session, auth, from_order, currency, rate,
                                                        p1, p2, ship):
    from celerp.services.money import round_money

    doc = await _doc(client, auth, "purchase_order" if from_order else "bill", [_goods(p1, 7), _goods(p2, 5)],
                     shipping=ship, currency=currency, conversion_rate=rate, **_VAT)
    if not from_order:
        await _finalize(client, auth, doc)
    li = (await _state(session, auth, doc))["line_items"]

    def rec(i, q):
        return {"po_line_index": i, "sku": li[i]["sku"], "name": li[i]["name"], "quantity_received": q}

    for batch in ([rec(1, 2)], [rec(0, 7), rec(1, 3)]):
        r = await _receive(client, auth, doc, *batch)
        assert r.status_code == 200, r.text
    if from_order:
        await _finalize(client, auth, doc)
    st = await _state(session, auth, doc)
    books = await _b(session, auth, "1150", "1130-FRT", "2110")
    assert books["1150"] == float(round_money(float(st.get("tax") or 0) * rate, "USD")), books
    assert books["1130-FRT"] == 0.0, books
    assert books["2110"] == round(-float(st["amount_outstanding"]) * rate, 2), (books, st["amount_outstanding"])
    await assert_settled(client, session, auth)
    for x in st["received_item_ids"]:
        r = await _return(client, auth, doc, x, float((await _state(session, auth, x))["quantity"]))
        assert r.status_code == 200, r.text
    books = await _b(session, auth, "1150", "1130-FRT", "1130-P", "2110")
    st = await _state(session, auth, doc)
    assert books["1150"] == 0.0 and books["1130-FRT"] == 0.0 and books["1130-P"] == 0.0, books
    assert books["2110"] == round(-float(st["amount_outstanding"]) * rate, 2), (books, st["amount_outstanding"])
    await assert_settled(client, session, auth)


# ── Transform a lot that holds two bills' freight, then sell it ──────────────

async def test_transform_merged_pools_then_sell(client, session, auth):
    lot = await _item(client, auth, _OPENING, qty=10)
    await _po_into(client, auth, lot, 5, 14.0, shipping=7.0)
    await _po_into(client, auth, lot, 5, 16.0, shipping=3.0)
    before = await _cost(session, auth, lot)
    r = await client.post(f"/items/{lot}/transform", headers=auth["headers"], json={
        "child_sku": f"LCR-{uuid.uuid4().hex[:6]}", "child_category": "Processed", "child_sell_by": "piece",
        "child_quantity": 6})
    assert r.status_code == 200, r.text
    child = r.json()["child_id"]
    assert await _cost(session, auth, child) == before
    await assert_settled(client, session, auth)
    cogs0 = (await _b(session, auth, "5100"))["5100"]
    await _sell(client, auth, child, 5)
    await assert_settled(client, session, auth)
    await _sell(client, auth, await _held(session, auth, child), 1)
    await assert_settled(client, session, auth)
    assert round((await _b(session, auth, "5100"))["5100"] - cogs0, 2) == before
