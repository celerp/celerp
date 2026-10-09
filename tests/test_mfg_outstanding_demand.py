# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""An order asks production only for what it has not yet received.

Demand on a product is, per order, what its physical lines ordered less what has been sent
and not taken back: a shipment reversed asks for it again, and service or freight lines ask
for nothing. Lines naming a lot of the product are demand for that product. Supply is the
stock on hand, which no longer includes what was sold, plus what open runs still have to
give. Demand Planning and the work orders made when an invoice is posted read the same
figures.
"""
from __future__ import annotations

import uuid

import pytest
from celerp.models.projections import Projection
from mfg_runs import issue, receive, run, set_settings
from test_cost_restatement import _item, _state
from test_mfg_finalize_supply import _made, _runs_for_doc

pytestmark = pytest.mark.asyncio


async def _stocked(client, auth, on_parent: float, *lots: float) -> tuple[str, list[str]]:
    """A manufacturable product holding ``on_parent`` itself plus one produced lot per ``lots``."""
    raw = await _item(client, auth, 1000.0, qty=100)
    fg = await _item(client, auth, 10.0 * on_parent, qty=on_parent, sku=f"FG-{uuid.uuid4().hex[:6]}")
    r = await client.put(f"/manufacturing/items/{fg}/recipe", headers=auth["headers"], json={
        "output_qty": 1, "components": [{"item_id": raw, "quantity": 1}], "labor": [], "overhead": []})
    assert r.status_code == 200, r.text
    return fg, await _lots(client, auth, fg, *lots)


async def _lots(client, auth, fg: str, *lots: float) -> list[str]:
    """``lots`` made by one run that receives them in turn (and so completes)."""
    if not lots:
        return []
    order = await run(client, auth, fg, sum(lots))
    assert (await issue(client, auth, order)).status_code == 200
    out = []
    for q in lots:
        r = await receive(client, auth, order, q, key=f"r-{uuid.uuid4().hex[:6]}")
        assert r.status_code == 200, r.text
        out.append(r.json()["lot_item_id"])
    return out


async def _non_stock(client, auth, kind: str) -> str:
    """A service or freight charge."""
    r = await client.post("/items", headers=auth["headers"], json={
        "sku": f"{kind[:2].upper()}-{uuid.uuid4().hex[:6]}", "name": kind, "quantity": 0,
        "sell_by": "piece", "status": "available", "inventory_type": kind})
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _invoice(client, auth, *lines: tuple[str, float], post: bool = True) -> str:
    items = []
    for item_id, qty in lines:
        sku = (await client.get(f"/items/{item_id}", headers=auth["headers"])).json().get("sku")
        items.append({"item_id": item_id, "sku": sku, "name": "Made", "quantity": qty, "unit_price": 100})
    r = await client.post("/docs", headers=auth["headers"], json={"doc_type": "invoice", "total": 0, "line_items": items})
    assert r.status_code in (200, 201), r.text
    doc = r.json()["id"]
    if post:
        assert (await client.post(f"/docs/{doc}/finalize", headers=auth["headers"])).status_code == 200
    return doc


async def _ship(client, auth, doc: str, *items: str) -> None:
    r = await client.post(f"/docs/{doc}/fulfill-lines", headers=auth["headers"], json={"line_entity_ids": list(items)})
    assert r.status_code == 200, r.text


async def _send_out(client, auth, item_id: str, qty: float) -> None:
    """Send ``qty`` of a lot out on a memo, so it leaves stock without a second invoice booking its cost."""
    sku = (await client.get(f"/items/{item_id}", headers=auth["headers"])).json().get("sku")
    r = await client.post("/docs", headers=auth["headers"], json={"doc_type": "memo", "total": 0, "line_items": [
        {"item_id": item_id, "sku": sku, "name": "Made", "quantity": qty, "unit_price": 100}]})
    assert r.status_code in (200, 201), r.text
    memo = r.json()["id"]
    assert (await client.post(f"/docs/{memo}/finalize", headers=auth["headers"])).status_code == 200
    await _ship(client, auth, memo, item_id)


async def _take_back(client, auth, doc: str, *items: str) -> None:
    r = await client.post(f"/docs/{doc}/revert-lines", headers=auth["headers"], json={"line_entity_ids": list(items)})
    assert r.status_code == 200, r.text


async def _board(client, auth) -> list[dict]:
    r = await client.get("/manufacturing/to-make", headers=auth["headers"])
    assert r.status_code == 200, r.text
    return r.json()["items"]


async def _row(client, auth, fg: str) -> dict:
    """The product's row; the product is on no other row (its lots never stand alone)."""
    sku = (await client.get(f"/items/{fg}", headers=auth["headers"])).json()["sku"]
    rows = await _board(client, auth)
    assert [r["item_id"] for r in rows if r["sku"] == sku and r["item_id"] != fg] == []
    return next((r for r in rows if r["item_id"] == fg),
                {"demand": 0, "on_hand": 0, "in_progress": 0, "to_make": 0, "docs": []})


def _figures(row: dict) -> tuple[float, float, float, float]:
    """(demand, on hand, in progress, to make), holding the two equations every row must:
    supply = on hand + still to come, and to make = what demand supply leaves uncovered."""
    assert row["to_make"] == max(0.0, row["demand"] - row["on_hand"] - row["in_progress"])
    assert row["demand"] == sum(d["quantity"] for d in row["docs"])
    return row["demand"], row["on_hand"], row["in_progress"], row["to_make"]


async def _outstanding(session, auth, doc: str) -> list[tuple[int, float, float, float]]:
    from celerp.services.fulfill import outstanding_physical_lines

    session.expire_all()
    row = await session.get(Projection, {"company_id": auth["company_id"], "entity_id": doc})
    lines = (await outstanding_physical_lines(session, auth["company_id"], [row]))[doc]
    return [(ln.index, ln.ordered, ln.fulfilled, ln.outstanding) for ln in lines]


async def test_nothing_shipped_asks_for_the_whole_order(client, session, auth):
    fg, _ = await _stocked(client, auth, 0)
    doc = await _invoice(client, auth, (fg, 5))

    assert _figures(await _row(client, auth, fg)) == (5, 0, 0, 5)
    assert await _outstanding(session, auth, doc) == [(0, 5, 0, 5)]


async def test_fully_shipped_asks_for_nothing(client, session, auth):
    """Shipped from the product's own unit and four made lots: the sale is complete."""
    fg, _ = await _stocked(client, auth, 1, 4)
    doc = await _invoice(client, auth, (fg, 5))
    await _ship(client, auth, doc, fg)

    assert _figures(await _row(client, auth, fg)) == (0, 0, 0, 0)
    assert await _outstanding(session, auth, doc) == [(0, 5, 5, 0)]


async def test_fully_shipped_from_part_of_the_stock_asks_for_nothing(client, session, auth):
    """Shipping part of the product's stock carves the sold units off as their own lot."""
    fg, _ = await _stocked(client, auth, 8)
    doc = await _invoice(client, auth, (fg, 5))
    await _ship(client, auth, doc, fg)

    assert _figures(await _row(client, auth, fg)) == (0, 0, 0, 0)
    assert await _outstanding(session, auth, doc) == [(0, 5, 5, 0)]


async def test_ten_ordered_four_shipped_asks_for_six(client, session, auth):
    fg, (lot,) = await _stocked(client, auth, 4, 6)
    doc = await _invoice(client, auth, (fg, 4), (lot, 6))
    await _ship(client, auth, doc, fg)

    assert _figures(await _row(client, auth, fg)) == (6, 6, 0, 0)
    assert await _outstanding(session, auth, doc) == [(0, 4, 4, 0), (1, 6, 0, 6)]


async def test_four_shipped_two_taken_back_asks_for_eight(client, session, auth):
    fg, (back, out) = await _stocked(client, auth, 2, 2, 6)
    doc = await _invoice(client, auth, (fg, 2), (back, 2), (out, 6))
    await _ship(client, auth, doc, fg, back)
    await _take_back(client, auth, doc, back)

    assert _figures(await _row(client, auth, fg)) == (8, 8, 0, 0)
    assert await _outstanding(session, auth, doc) == [(0, 2, 2, 0), (1, 2, 0, 2), (2, 6, 0, 6)]


async def test_all_shipped_all_taken_back_asks_for_all_again(client, session, auth):
    fg, _ = await _stocked(client, auth, 4, 6)
    doc = await _invoice(client, auth, (fg, 10))
    await _ship(client, auth, doc, fg)
    await _take_back(client, auth, doc, fg)

    assert _figures(await _row(client, auth, fg)) == (10, 10, 0, 0)
    assert await _outstanding(session, auth, doc) == [(0, 10, 0, 10)]


async def test_supply_goes_only_to_the_order_still_open(client, session, auth):
    fg, _ = await _stocked(client, auth, 1, 4)
    shipped = await _invoice(client, auth, (fg, 5))
    await _ship(client, auth, shipped, fg)
    await _lots(client, auth, fg, 3)
    open_doc = await _invoice(client, auth, (fg, 5))

    row = await _row(client, auth, fg)
    assert _figures(row) == (5, 3, 0, 2)
    assert [(d["doc_id"], d["covered"], d["shortfall"]) for d in row["docs"]] == [(open_doc, 3, 2)]


async def test_part_shipped_order_with_production_running_is_short_only_the_rest(client, session, auth):
    fg, _ = await _stocked(client, auth, 4)
    order = await run(client, auth, fg, 3)
    assert (await issue(client, auth, order)).status_code == 200
    fg_lot = (await _lots(client, auth, fg, 6))[0]
    doc = await _invoice(client, auth, (fg, 4), (fg_lot, 6))
    await _ship(client, auth, doc, fg)
    await _send_out(client, auth, fg_lot, 6)

    # The first order still wants the six whose lot went out on a memo; three are on the way.
    assert _figures(await _row(client, auth, fg)) == (6, 0, 3, 3)
    assert await _outstanding(session, auth, doc) == [(0, 4, 4, 0), (1, 6, 0, 6)]


async def test_service_and_freight_lines_ask_for_nothing(client, session, auth):
    fg, _ = await _stocked(client, auth, 0)
    service, freight = [await _non_stock(client, auth, kind) for kind in ("service", "freight")]
    doc = await _invoice(client, auth, (service, 2), (fg, 3), (freight, 1))

    assert _figures(await _row(client, auth, fg)) == (3, 0, 0, 3)
    assert await _outstanding(session, auth, doc) == [(1, 3, 0, 3)]


async def test_goods_billed_from_a_memo_were_already_delivered(client, session, auth):
    """A memo sent five out; converting it bills them. The invoice asks for nothing more."""
    fg, _ = await _stocked(client, auth, 1, 4)
    r = await client.post("/docs", headers=auth["headers"], json={"doc_type": "memo", "total": 0, "line_items": [
        {"item_id": fg, "sku": (await _state(session, auth, fg))["sku"], "name": "Made", "quantity": 5, "unit_price": 100}]})
    assert r.status_code in (200, 201), r.text
    memo = r.json()["id"]
    assert (await client.post(f"/docs/{memo}/finalize", headers=auth["headers"])).status_code == 200
    await _ship(client, auth, memo, fg)
    r = await client.post(f"/docs/{memo}/convert", headers=auth["headers"], json={"target_type": "invoice"})
    assert r.status_code == 200, r.text
    doc = r.json()["target_doc_id"]
    assert (await client.post(f"/docs/{doc}/finalize", headers=auth["headers"])).status_code == 200

    assert _figures(await _row(client, auth, fg)) == (0, 0, 0, 0)
    assert await _outstanding(session, auth, doc) == [(0, 5, 5, 0)]


# -- The same journeys when posting an invoice makes the work orders ------------------------

async def _auto(session, auth) -> None:
    await set_settings(session, auth, manufacturing={"auto_create_work_orders": True})


async def test_posting_after_a_shipped_order_with_enough_stock_makes_nothing(client, session, auth):
    fg, _ = await _stocked(client, auth, 1, 4)
    shipped = await _invoice(client, auth, (fg, 5))
    await _ship(client, auth, shipped, fg)
    await _lots(client, auth, fg, 5)
    await _auto(session, auth)

    doc = await _invoice(client, auth, (fg, 5))

    assert await _runs_for_doc(session, auth, doc) == []


async def test_posting_after_a_shipped_order_makes_only_what_stock_leaves_short(client, session, auth):
    fg, _ = await _stocked(client, auth, 1, 4)
    shipped = await _invoice(client, auth, (fg, 5))
    await _ship(client, auth, shipped, fg)
    await _lots(client, auth, fg, 3)
    await _auto(session, auth)

    doc = await _invoice(client, auth, (fg, 5))

    assert _made(await _runs_for_doc(session, auth, doc)) == [2.0]


async def test_posting_after_a_shipment_taken_back_counts_that_order_again(client, session, auth):
    """The order taken back is open again and first in line, so the new one is short in full."""
    fg, _ = await _stocked(client, auth, 4, 6)
    first = await _invoice(client, auth, (fg, 10))
    await _ship(client, auth, first, fg)
    await _take_back(client, auth, first, fg)
    await _auto(session, auth)

    doc = await _invoice(client, auth, (fg, 3))

    assert _made(await _runs_for_doc(session, auth, doc)) == [3.0]


async def test_posting_alongside_a_part_shipped_order_and_running_production(client, session, auth):
    """Six still owed on the first order and three on the way: the new order of two is short two."""
    fg, _ = await _stocked(client, auth, 4)
    order = await run(client, auth, fg, 3)
    assert (await issue(client, auth, order)).status_code == 200
    fg_lot = (await _lots(client, auth, fg, 6))[0]
    first = await _invoice(client, auth, (fg, 4), (fg_lot, 6))
    await _ship(client, auth, first, fg)
    await _send_out(client, auth, fg_lot, 6)
    await _auto(session, auth)

    doc = await _invoice(client, auth, (fg, 2))

    assert _made(await _runs_for_doc(session, auth, doc)) == [2.0]


async def test_posting_service_and_freight_lines_makes_work_only_for_the_goods(client, session, auth):
    fg, _ = await _stocked(client, auth, 0)
    service, freight = [await _non_stock(client, auth, kind) for kind in ("service", "freight")]
    await _auto(session, auth)

    doc = await _invoice(client, auth, (service, 2), (fg, 3), (freight, 1))

    assert [(r["output_item_id"], r["expected_outputs"][0]["quantity"]) for r in await _runs_for_doc(session, auth, doc)] == [(fg, 3.0)]


async def _credit(client, auth, invoice: str, *returns: tuple[str, float, str | None]) -> str:
    """A posted credit note on ``invoice`` taking back (sku, quantity, lot) of what it sold."""
    r = await client.post("/docs", headers=auth["headers"], json={
        "doc_type": "credit_note", "original_doc_id": invoice, "total": 0, "line_items": [
            {"name": "Made", "sku": sku, "quantity": q, "unit_price": 0, "sell_by": "piece"}
            for sku, q, _ in returns]})
    assert r.status_code == 200, r.text
    note = r.json()["id"]
    assert (await client.post(f"/docs/{note}/finalize", headers=auth["headers"])).status_code == 200
    r = await client.post(f"/docs/{note}/receive-return", headers=auth["headers"], json={"items": [
        {"sku": sku, "quantity": q, **({"item_id": lot} if lot else {})} for sku, q, lot in returns]})
    assert r.status_code == 200, r.text
    return note


async def test_goods_back_on_a_credit_note_are_asked_for_again(client, session, auth):
    """Five shipped, two of them back on a credit note on the invoice: the order asks for the
    two again, and the two back on the shelf cover them. Undoing the return closes it again."""
    fg, (lot,) = await _stocked(client, auth, 0, 5)
    doc = await _invoice(client, auth, (lot, 5))
    await _ship(client, auth, doc, lot)
    assert await _outstanding(session, auth, doc) == [(0, 5, 5, 0)]
    sku = (await client.get(f"/items/{lot}", headers=auth["headers"])).json()["sku"]

    note = await _credit(client, auth, doc, (sku, 2, lot))

    assert await _outstanding(session, auth, doc) == [(0, 5, 3, 2)]
    assert _figures(await _row(client, auth, fg)) == (2, 2, 0, 0)

    r = await client.delete(f"/docs/{note}/receive-return", headers=auth["headers"])
    assert r.status_code == 200, r.text
    assert await _outstanding(session, auth, doc) == [(0, 5, 5, 0)]
    assert _figures(await _row(client, auth, fg)) == (0, 0, 0, 0)


async def test_goods_back_by_sku_go_to_the_line_that_sold_them(client, session, auth):
    """A return naming only the SKU is the most recently sold lot of it; it reopens that lot's
    line and no other."""
    fg, (a, b) = await _stocked(client, auth, 0, 3, 4)
    doc = await _invoice(client, auth, (a, 3), (b, 4))
    await _ship(client, auth, doc, a, b)
    sku = (await client.get(f"/items/{b}", headers=auth["headers"])).json()["sku"]

    await _credit(client, auth, doc, (sku, 1, None))

    lines = await _outstanding(session, auth, doc)
    assert sorted(ln[3] for ln in lines) == [0, 1] and sum(ln[2] for ln in lines) == 6, lines
    assert _figures(await _row(client, auth, fg)) == (1, 1, 0, 0)
