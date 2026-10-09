# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Goods a finalized invoice has costed and not shipped stay for that invoice, whatever
way they would leave stock: a write-off, a count that finds less, a return to the
supplier, an undone receipt, count or customer return, production, or a split
that re-weighs the lot. Each is refused naming the invoice, and units no invoice holds
leave as before, so the invoice's cost leaves inventory once."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from celerp.models.projections import Projection
from stock_books import assert_settled
from test_consignment_in_sale import _customer_return
from test_cost_follows_goods import _doc_number, _invoice, _ship
from test_cost_restatement import _item, _state
from test_invoice_unshipped_books import _lot
from test_quantity_cost_invariant import _located_item

pytestmark = pytest.mark.asyncio


async def _refused(session, auth, r, invoice: str, keep: float | None) -> None:
    """Refused naming the invoice: a lot that would hold less says how much it must keep,
    goods that would leave stock altogether say so."""
    assert r.status_code == 409, r.text
    # A refused request's session is never committed, so production discards anything the route
    # wrote before the refusal. The test client shares one session across requests; roll it back
    # the same way.
    await session.rollback()
    detail = r.json()["detail"]
    assert await _doc_number(session, auth, invoice) in detail, detail
    assert (f"cannot go below {keep:g}" if keep is not None else "cannot leave stock") in detail, detail


async def _qty(session, auth, lot: str) -> float:
    session.expire_all()
    return float((await _state(session, auth, lot))["quantity"])


async def _write_off(client, auth, lot: str, qty: float):
    h = auth["headers"]
    wo = (await client.post("/lists/writeoff", headers=h, json={"entity_ids": [lot]})).json()["id"]
    r = await client.post(f"/lists/{wo}/writeoff-line", headers=h, json={"item_id": lot, "qty_out": qty, "account": "6950"})
    assert r.status_code == 200, r.text
    return await client.post(f"/lists/{wo}/write-off", headers=h)


async def test_a_write_off_cannot_take_set_aside_goods(client, session, auth):
    lot = await _lot(client, auth, "SAX-WO", 3, 30.0)
    inv = await _invoice(client, auth, [(lot, "SAX-WO", 2)])
    await _refused(session, auth, await _write_off(client, auth, lot, 2), inv, None)
    assert (r := await _write_off(client, auth, lot, 1)).status_code == 200, r.text
    assert await _qty(session, auth, lot) == 2
    await assert_settled(client, session, auth)


async def _count(client, auth, loc: str, lot: str, counted: float) -> tuple[str, object]:
    h = auth["headers"]
    audit = (await client.post("/lists/audit", headers=h, json={"location_id": loc})).json()["id"]
    assert (r := await client.post(f"/lists/{audit}/finalize", headers=h)).status_code == 200, r.text
    assert (r := await client.patch(f"/lists/{audit}/line/{lot}", headers=h, json={"counted_qty": counted})).status_code == 200, r.text
    return audit, await client.post(f"/lists/{audit}/adjust", headers=h)


async def test_a_count_that_finds_less_cannot_take_set_aside_goods(client, session, auth):
    lot, loc = await _located_item(client, session, auth, 3, 30.0)
    sku = (await _state(session, auth, lot))["sku"]
    inv = await _invoice(client, auth, [(lot, sku, 2)])
    _audit, r = await _count(client, auth, loc, lot, 0)
    await _refused(session, auth, r, inv, 2)
    _audit, r = await _count(client, auth, loc, lot, 2)
    assert r.status_code == 200, r.text
    assert await _qty(session, auth, lot) == 2
    await assert_settled(client, session, auth)


async def test_undoing_a_count_that_found_more_cannot_take_set_aside_goods(client, session, auth):
    lot, loc = await _located_item(client, session, auth, 2, 20.0)
    sku = (await _state(session, auth, lot))["sku"]
    audit, r = await _count(client, auth, loc, lot, 4)
    assert r.status_code == 200, r.text
    inv = await _invoice(client, auth, [(lot, sku, 4)])
    await _refused(session, auth, await client.post(f"/lists/{audit}/undo-adjust", headers=auth["headers"]), inv, 4)
    assert await _qty(session, auth, lot) == 4
    await assert_settled(client, session, auth)


async def _received_on(client, session, auth, qty: float, doc_type: str = "purchase_order") -> tuple[str, str, str]:
    """A lot of 10 at 100 that a purchase order or bill then receives ``qty`` more of at 5."""
    lot = await _item(client, auth, 100.0, qty=10)
    sku = (await _state(session, auth, lot))["sku"]
    r = await client.post("/docs", headers=auth["headers"], json={
        "doc_type": doc_type, "contact_id": "supplier:1",
        "line_items": [{"item_id": lot, "name": "L0", "quantity": qty, "unit_price": 5.0}]})
    assert r.status_code == 200, r.text
    po = r.json()["id"]
    if doc_type == "bill":
        assert (r := await client.post(f"/docs/{po}/finalize", headers=auth["headers"])).status_code == 200, r.text
    r = await client.post(f"/docs/{po}/receive", headers=auth["headers"], json={"location_id": "", "received_items": [
        {"po_line_index": 0, "item_id": lot, "quantity_received": qty, "receive_as": "stock"}]})
    assert r.status_code == 200, r.text
    return po, lot, sku


async def test_a_return_to_the_supplier_cannot_take_set_aside_goods(client, session, auth):
    po, lot, sku = await _received_on(client, session, auth, 4)
    inv = await _invoice(client, auth, [(lot, sku, 12)])
    r = await client.post(f"/docs/{po}/return-items", headers=auth["headers"],
                          json={"items": [{"item_id": lot, "quantity_returned": 4}]})
    await _refused(session, auth, r, inv, 12)
    r = await client.post(f"/docs/{po}/return-items", headers=auth["headers"],
                          json={"items": [{"item_id": lot, "quantity_returned": 2}]})
    assert r.status_code == 200, r.text
    assert await _qty(session, auth, lot) == 12
    await assert_settled(client, session, auth)


async def test_undoing_a_receipt_cannot_take_set_aside_goods(client, session, auth):
    bill, _lot10, _sku = await _received_on(client, session, auth, 4, "bill")
    session.expire_all()
    got = (await _state(session, auth, bill))["received_item_ids"][-1]
    inv = await _invoice(client, auth, [(got, (await _state(session, auth, got))["sku"], 3)])
    await _refused(session, auth, await client.delete(f"/docs/{bill}/receive", headers=auth["headers"]), inv, None)
    assert await _qty(session, auth, got) == 4
    await assert_settled(client, session, auth)


async def test_undoing_a_customer_return_cannot_take_set_aside_goods(client, session, auth):
    lot = await _lot(client, auth, "SAX-CN", 2, 20.0)
    sold = await _invoice(client, auth, [(lot, "SAX-CN", 2)])
    await _ship(client, auth, sold, lot)
    back = await _customer_return(client, session, auth, sold, lot, 2)
    inv = await _invoice(client, auth, [(back, "SAX-CN", 2)])
    cn = await _credit_note_of(session, auth, sold)
    await _refused(session, auth, await client.delete(f"/docs/{cn}/receive-return", headers=auth["headers"]), inv, None)
    assert await _qty(session, auth, back) == 2
    await assert_settled(client, session, auth)


async def _credit_note_of(session, auth, invoice: str) -> str:
    session.expire_all()
    rows = (await session.execute(select(Projection).where(
        Projection.company_id == auth["company_id"], Projection.entity_type == "doc"))).scalars().all()
    return next(r.entity_id for r in rows
                if (r.state or {}).get("doc_type") == "credit_note" and (r.state or {}).get("original_doc_id") == invoice)


async def test_a_split_that_re_weighs_the_lot_cannot_take_set_aside_goods(client, session, auth):
    lot = await _lot(client, auth, "SAX-SPL", 4, 40.0)
    inv = await _invoice(client, auth, [(lot, "SAX-SPL", 4)])
    r = await client.post(f"/items/{lot}/split", headers=auth["headers"],
                          json={"children": [{"quantity": 1}], "mother_qty": 2})
    await _refused(session, auth, r, inv, 3)  # the mother keeps 3, the part 1
    assert await _qty(session, auth, lot) == 4
    await assert_settled(client, session, auth)


async def test_a_split_that_re_weighs_the_lot_leaves_goods_no_invoice_holds(client, session, auth):
    lot = await _lot(client, auth, "SAX-SPL2", 4, 40.0)
    await _invoice(client, auth, [(lot, "SAX-SPL2", 3)])
    r = await client.post(f"/items/{lot}/split", headers=auth["headers"],
                          json={"children": [{"quantity": 1}], "mother_qty": 2})
    assert r.status_code == 200, r.text
    await assert_settled(client, session, auth)


async def _run(client, auth, component: str, qty: float) -> str:
    h = auth["headers"]
    r = await client.post("/items", headers=h, json={"sku": f"RING-{uuid.uuid4().hex[:6]}", "name": "Ring",
                                                     "quantity": 0, "sell_by": "piece", "status": "available"})
    assert r.status_code == 200, r.text
    ring = r.json()["id"]
    r = await client.put(f"/manufacturing/items/{ring}/recipe", headers=h, json={
        "output_qty": 1, "components": [{"item_id": component, "quantity": 1}], "labor": [], "overhead": []})
    assert r.status_code == 200, r.text
    r = await client.post(f"/manufacturing/items/{ring}/build", headers=h, json={"quantity": qty, "complete": False})
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def test_production_cannot_use_set_aside_goods(client, session, auth):
    gold = await _lot(client, auth, "SAX-GOLD", 3, 30.0)
    inv = await _invoice(client, auth, [(gold, "SAX-GOLD", 2)])
    run = await _run(client, auth, gold, 2)
    r = await client.post(f"/manufacturing/{run}/issue", headers=auth["headers"],
                          json={"items": [{"item_id": gold, "quantity": 2}]})
    await _refused(session, auth, r, inv, 2)
    r = await client.post(f"/manufacturing/{run}/issue", headers=auth["headers"],
                          json={"items": [{"item_id": gold, "quantity": 1}]})
    assert r.status_code == 200, r.text
    assert await _qty(session, auth, gold) == 2
    await assert_settled(client, session, auth)
