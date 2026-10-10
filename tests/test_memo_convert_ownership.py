# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Converting a memo hands its lots to the invoice.

The goods stay out with the customer, now billed on the draft invoice. Finalizing the
invoice sells them and books cost, revenue and tax once; taking them back on the invoice
returns them to stock and reverses the cost. Voiding or deleting the draft gives the goods
back to the memo, which can then be converted again. Voiding a finalized converted
invoice reverses its books and gives the goods back to the memo as well."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from celerp.models.projections import Projection

from gl_support import gl_totals
from line_actions_support import h, item, line, line_ids, lot, state  # noqa: F401
from stock_books import assert_settled
from test_helpers import company_auth

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def auth(session):
    return await company_auth(session, uuid.uuid4(), uuid.uuid4())


async def _ok(r) -> dict:
    assert r.status_code == 200, r.text
    return r.json()


async def _memo_out(client, hd, lots: list[str], sku: str, qty: float) -> str:
    """A finalized memo for ``qty`` of ``sku`` bound to the first lot, every lot out on it."""
    li = line(lots[0], qty, price=300.0, sku=sku, taxes=[{"code": "VAT", "rate": 10}])
    memo = (await _ok(await client.post("/docs", headers=hd, json={
        "doc_type": "memo", "line_items": [li], "total": 330.0 * qty})))["id"]
    await _ok(await client.post(f"/docs/{memo}/finalize", headers=hd))
    await _ok(await client.post(f"/docs/{memo}/fulfill-lines", headers=hd,
                                json={"line_ids": await line_ids(client, hd, memo)}))
    for lot_id in lots:
        assert await _owner(client, hd, lot_id) == ("memo_out", memo)
    return memo


async def _owner(client, hd, lot_id: str) -> tuple:
    s = await item(client, hd, lot_id)
    return s.get("status"), s.get("status_doc_id")


async def _converted(client, auth, *, lots: int = 1):
    hd = auth["headers"]
    sku = f"MC-{uuid.uuid4().hex[:6]}"
    ids = [await lot(client, hd, sku, 1, cost=100) for _ in range(lots)]
    memo = await _memo_out(client, hd, ids, sku, lots)
    memo_status = (await state(client, hd, memo))["status"]
    inv = (await _ok(await client.post(f"/docs/{memo}/convert", headers=hd)))["target_doc_id"]
    return hd, ids, memo, memo_status, inv


def _tag(doc_id: str) -> str:
    return f":{doc_id}:"


async def test_convert_hands_the_lots_to_the_draft_invoice(client, session, auth):
    hd, (lot_id,), memo, _, inv = await _converted(client, auth)
    assert await _owner(client, hd, lot_id) == ("memo_out", inv)
    assert (await state(client, hd, inv))["source_memo_id"] == memo
    assert (await state(client, hd, memo))["status"] == "converted"
    assert await gl_totals(session, auth["company_id"], entry_id_part=_tag(inv)) == {}
    await assert_settled(client, session, auth)


async def test_finalizing_the_converted_invoice_books_once(client, session, auth):
    hd, (lot_id,), _, _, inv = await _converted(client, auth)
    await _ok(await client.post(f"/docs/{inv}/finalize", headers=hd))
    await client.post(f"/docs/{inv}/finalize", headers=hd)
    assert await _owner(client, hd, lot_id) == ("sold", inv)
    gl = await gl_totals(session, auth["company_id"], entry_id_part=_tag(inv))
    assert (gl.get("5100"), gl.get("4100"), gl.get("2120"), gl.get("1120")) == (100.0, -300.0, -30.0, 330.0), gl
    await assert_settled(client, session, auth)


async def test_finalize_sells_every_lot_billed_from_the_memo(client, session, auth):
    hd, ids, _, _, inv = await _converted(client, auth, lots=2)
    for lot_id in ids:
        assert await _owner(client, hd, lot_id) == ("memo_out", inv)
    await _ok(await client.post(f"/docs/{inv}/finalize", headers=hd))
    for lot_id in ids:
        assert await _owner(client, hd, lot_id) == ("sold", inv)
    gl = await gl_totals(session, auth["company_id"], entry_id_part=_tag(inv))
    assert (gl.get("5100"), gl.get("4100")) == (200.0, -600.0), gl
    await assert_settled(client, session, auth)


async def test_goods_out_on_another_memo_are_not_costed_by_an_invoice(client, session, auth):
    """Neighbour (CI-c): lot A (1 at 100.00) is out on a memo that was not converted; an
    invoice for 2 of the SKU bound to lot B (1 at 100.00, in stock) costs only B. The unit
    with no free stock stays provisional, so the memo's goods are never priced by it."""
    hd = auth["headers"]
    sku = f"MC-{uuid.uuid4().hex[:6]}"
    out_lot = await lot(client, hd, sku, 1, cost=100)
    memo = await _memo_out(client, hd, [out_lot], sku, 1)
    in_stock = await lot(client, hd, sku, 1, cost=100)
    inv = (await _ok(await client.post("/docs", headers=hd, json={
        "doc_type": "invoice", "line_items": [line(in_stock, 2, price=300.0, sku=sku)], "total": 600.0})))["id"]
    await _ok(await client.post(f"/docs/{inv}/finalize", headers=hd))
    gl = await gl_totals(session, auth["company_id"], entry_id_part=_tag(inv))
    assert gl.get("5100") == 100.0, gl
    assert await _owner(client, hd, out_lot) == ("memo_out", memo)
    await assert_settled(client, session, auth)


@pytest.mark.parametrize("action", ["revert-lines", "set-available"])
@pytest.mark.parametrize("lots", [1, 2])
async def test_taking_the_goods_back_on_the_invoice_reverses_the_cost(client, session, auth, action, lots):
    hd, ids, _, _, inv = await _converted(client, auth, lots=lots)
    await _ok(await client.post(f"/docs/{inv}/finalize", headers=hd))
    await _ok(await client.post(f"/docs/{inv}/{action}", headers=hd,
                                json={"line_ids": await line_ids(client, hd, inv)}))
    for lot_id in ids:
        assert await _owner(client, hd, lot_id) == ("available", None)
    gl = await gl_totals(session, auth["company_id"], entry_id_part=_tag(inv))
    assert (gl.get("5100"), gl.get("4100"), gl.get("2120")) == (None, -300.0 * lots, -30.0 * lots), gl
    await assert_settled(client, session, auth)


async def _assert_back_on_memo(client, session, auth, ids, memo, memo_status):
    hd = auth["headers"]
    for lot_id in ids:
        assert await _owner(client, hd, lot_id) == ("memo_out", memo)
    m = await state(client, hd, memo)
    assert m["status"] == memo_status
    assert not m.get("converted_to")
    await assert_settled(client, session, auth)
    again = (await _ok(await client.post(f"/docs/{memo}/convert", headers=hd)))["target_doc_id"]
    for lot_id in ids:
        assert await _owner(client, hd, lot_id) == ("memo_out", again)


@pytest.mark.parametrize("lots", [1, 2])
async def test_voiding_the_draft_gives_the_goods_back_to_the_memo(client, session, auth, lots):
    hd, ids, memo, memo_status, inv = await _converted(client, auth, lots=lots)
    await _ok(await client.post(f"/docs/{inv}/void", headers=hd, json={"reason": "wrong customer"}))
    await _assert_back_on_memo(client, session, auth, ids, memo, memo_status)


async def test_deleting_the_draft_gives_the_goods_back_to_the_memo(client, session, auth):
    hd, ids, memo, memo_status, inv = await _converted(client, auth)
    await _ok(await client.delete(f"/docs/{inv}", headers=hd))
    await _assert_back_on_memo(client, session, auth, ids, memo, memo_status)


async def test_bulk_deleting_the_draft_gives_the_goods_back_to_the_memo(client, session, auth):
    hd, ids, memo, memo_status, inv = await _converted(client, auth)
    await _ok(await client.delete("/docs/bulk-draft", headers=hd, params={"doc_ids": inv}))
    await _assert_back_on_memo(client, session, auth, ids, memo, memo_status)


async def test_voiding_the_finalized_invoice_reverses_its_books(client, session, auth):
    hd, ids, memo, memo_status, inv = await _converted(client, auth)
    await _ok(await client.post(f"/docs/{inv}/finalize", headers=hd))
    await _ok(await client.post(f"/docs/{inv}/void", headers=hd, json={"reason": "wrong customer"}))
    gl = await gl_totals(session, auth["company_id"], entry_id_part=_tag(inv))
    assert gl == {}, gl
    await _assert_back_on_memo(client, session, auth, ids, memo, memo_status)


async def test_unvoiding_an_invoice_whose_goods_went_back_is_refused(client, session, auth):
    hd, ids, memo, memo_status, inv = await _converted(client, auth)
    await _ok(await client.post(f"/docs/{inv}/void", headers=hd, json={"reason": "wrong customer"}))
    r = await client.post(f"/docs/{inv}/unvoid", headers=hd, json={})
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["message_key"] == "documents.unvoid_conversion_undone", r.text
    assert (await state(client, hd, inv))["status"] == "void"
    for lot_id in ids:
        assert await _owner(client, hd, lot_id) == ("memo_out", memo)
    await assert_settled(client, session, auth)


async def test_a_retried_convert_makes_one_invoice(client, session, auth):
    hd = auth["headers"]
    sku = f"MC-{uuid.uuid4().hex[:6]}"
    lot_id = await lot(client, hd, sku, 1, cost=100)
    memo = await _memo_out(client, hd, [lot_id], sku, 1)
    key = str(uuid.uuid4())
    first = await _ok(await client.post(f"/docs/{memo}/convert", headers=hd, json={"idempotency_key": key}))
    second = await _ok(await client.post(f"/docs/{memo}/convert", headers=hd, json={"idempotency_key": key}))
    assert first == second
    assert await _owner(client, hd, lot_id) == ("memo_out", first["target_doc_id"])
    session.expire_all()
    invoices = (await session.execute(select(Projection.entity_id).where(
        Projection.company_id == auth["company_id"], Projection.entity_type == "doc",
        Projection.state["source_memo_id"].as_string() == memo))).scalars().all()
    assert invoices == [first["target_doc_id"]]
