# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Goods going back from an invoice to the memo they came from: Back to memo, void, delete and
bulk delete each put every lot back on its own memo line and give the memo's fixed amounts
back, and no unit is ever lost or counted twice on the way."""
from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from celerp.models.ledger import LedgerEntry
from memo_draw_support import (
    auth, auth_full, back_to_memo, draft, draw, drawn, drawn_lines, item, memo_out, ok, owner, revoke,
    service_line, state,
)


async def back_all(client, h, invoice: str, memo: str | None = None):
    return await ok(await back_to_memo(client, h, invoice, [li["line_id"] for li in drawn_lines(await state(client, h, invoice), memo)]))


async def sku_units(client, h, sku: str) -> list[dict]:
    rows = await ok(await client.get("/items", headers=h, params={"q": sku, "limit": 200}))
    return [i for i in (rows.get("items", rows) if isinstance(rows, dict) else rows) if i.get("sku") == sku]


async def released(client, h, memo: str) -> list[bool]:
    return [t["released"] for t in (await state(client, h, memo)).get("invoiced_on") or []]


async def _two_memos_one_draft(client, h, *, new_invoice: bool):
    """A draft holding a two-lot line from memo 1, a line from memo 2 and an ordinary line."""
    m1, l1, lots1 = await memo_out(client, h, [(2, 10.0)], lots_per_line=2, shipping=50.0)
    m2, l2, lots2 = await memo_out(client, h, [(1, 30.0)], shipping=30.0)
    pf = None if new_invoice else await draft(client, h, lines=[service_line()], shipping=20.0)
    pf = await drawn(client, h, m1, {l1[0]: 2}, target=pf)
    await drawn(client, h, m2, {l2[0]: 1}, target=pf)
    if new_invoice:
        doc = await state(client, h, pf)
        await ok(await client.patch(f"/docs/{pf}", headers=h, json={
            "fields_changed": {"line_items": {"new": [*doc["line_items"], service_line()]}},
            "expected_version": doc["version"]}))
    return pf, (m1, l1, lots1[0]), (m2, l2, lots2[0])


# ── Back to memo ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("new_invoice", [True, False])
async def test_back_to_memo_returns_lot_to_its_memo_line(client, session, new_invoice):
    h = await auth(session)
    pf, (m1, l1, lots1), (m2, l2, lots2) = await _two_memos_one_draft(client, h, new_invoice=new_invoice)
    assert len(drawn_lines(await state(client, h, pf), m1)) == 1

    await back_all(client, h, pf)

    for i in lots1:
        assert await owner(client, h, i) == ("memo_out", m1)
    assert await owner(client, h, lots2[0]) == ("memo_out", m2)
    doc = await state(client, h, pf)
    assert [li["description"] for li in doc["line_items"]] == ["Setting fee"]
    labels = {li["line_id"]: li.get("shipped_label") for li in (await state(client, h, m1))["line_items"]}
    assert labels[l1[0]] == "On Memo"


async def test_back_to_memo_refuses_undrawn_line(client, session):
    h = await auth(session)
    memo, lids, lots = await memo_out(client, h, [(2, 10.0)], lots_per_line=2)
    pf = await draft(client, h, lines=[service_line()])
    await drawn(client, h, memo, {lids[0]: 1}, target=pf)
    doc = await state(client, h, pf)
    own = [li["line_id"] for li in doc["line_items"] if not li.get("drawn_from")]

    r = await back_to_memo(client, h, pf, own)
    assert r.status_code == 409, r.text

    await ok(await client.post(f"/docs/{pf}/finalize", headers=h))
    r = await back_to_memo(client, h, pf, [li["line_id"] for li in drawn_lines(doc, memo)])
    assert r.status_code == 409, r.text
    assert len(drawn_lines(await state(client, h, pf), memo)) == 1


@pytest.mark.parametrize("removed", ["edit_documents", "fulfill_documents"])
async def test_back_to_memo_requires_edit_and_fulfill(client, session, removed):
    a = await auth_full(session)
    h = a["headers"]
    memo, lids, lots = await memo_out(client, h, [(2, 10.0)], lots_per_line=2)
    inv = await drawn(client, h, memo, {lids[0]: 1})
    lines = [li["line_id"] for li in drawn_lines(await state(client, h, inv), memo)]
    await revoke(session, a["company_id"], removed)

    r = await back_to_memo(client, h, inv, lines)

    assert r.status_code == 403, r.text
    assert len(drawn_lines(await state(client, h, inv), memo)) == 1


async def test_split_child_return_then_split_again_keeps_memo_line(client, session):
    h = await auth(session)
    memo, lids, lots = await memo_out(client, h, [(10, 10.0), (5, 10.0)])
    inv = await drawn(client, h, memo, {lids[0]: 4})
    await back_all(client, h, inv, memo)

    again = await state(client, h, await drawn(client, h, memo, {lids[0]: 3}))

    line = drawn_lines(again, memo)[0]
    assert line["drawn_from"]["memo_line_id"] == lids[0]
    assert (await item(client, h, line["item_id"]))["sku"] == (await item(client, h, lots[0][0]))["sku"]
    assert sum(i["quantity"] for i in await sku_units(client, h, (await item(client, h, lots[0][0]))["sku"])) == 10


@pytest.mark.parametrize("new_invoice", [True, False])
async def test_unit_conservation_across_draw_back_void_delete(client, session, new_invoice):
    h = await auth(session)
    memo, lids, lots = await memo_out(client, h, [(10, 10.0)])
    sku = (await item(client, h, lots[0][0]))["sku"]

    async def conserved():
        units = await sku_units(client, h, sku)
        assert sum(i["quantity"] for i in units) == 10
        assert all(i["status"] in ("memo_out", "sold") for i in units), [(i["status"], i["quantity"]) for i in units]

    async def target():
        return None if new_invoice else await draft(client, h, lines=[service_line()])

    a = await drawn(client, h, memo, {lids[0]: 3}, target=await target())
    await conserved()
    await back_all(client, h, a, memo)
    await conserved()
    b = await drawn(client, h, memo, {lids[0]: 2}, target=await target())
    await ok(await client.post(f"/docs/{b}/finalize", headers=h))
    await conserved()
    await ok(await client.post(f"/docs/{b}/void", headers=h, json={}))
    await conserved()
    c = await drawn(client, h, memo, {lids[0]: 4}, target=await target())
    await ok(await client.delete(f"/docs/{c}", headers=h))
    await conserved()
    d = await drawn(client, h, memo, {lids[0]: 1}, target=await target())
    e = await drawn(client, h, memo, {lids[0]: 1}, target=await target())
    await ok(await client.delete("/docs/bulk-draft", headers=h, params={"doc_ids": f"{d},{e}"}))
    await conserved()
    assert all(await released(client, h, memo))


# ── Void, unvoid, delete, close ───────────────────────────────────────────────

async def test_void_returns_lots_to_each_memo_and_reopens_closed(client, session):
    h = await auth(session)
    m1, l1, lots1 = await memo_out(client, h, [(2, 10.0)], lots_per_line=2)
    m2, l2, lots2 = await memo_out(client, h, [(1, 30.0)])
    pf = await drawn(client, h, m1, {l1[0]: 1})
    await drawn(client, h, m2, line_ids=l2, target=pf)
    await ok(await client.post(f"/docs/{pf}/finalize", headers=h))
    await ok(await client.post(f"/docs/{m2}/close", headers=h, json={}))
    assert (await state(client, h, m2))["status"] == "closed"

    await ok(await client.post(f"/docs/{pf}/void", headers=h, json={}))

    for i in lots1[0]:
        assert await owner(client, h, i) == ("memo_out", m1)
    assert await owner(client, h, lots2[0][0]) == ("memo_out", m2)
    assert (await state(client, h, m2))["status"] == "final"
    reopened = (await session.execute(select(func.count()).select_from(LedgerEntry).where(
        LedgerEntry.entity_id == m2, LedgerEntry.event_type == "doc.reopened"))).scalar_one()
    assert reopened == 1


async def test_unvoid_refused_after_drawn_goods_went_back(client, session):
    h = await auth(session)
    memo, lids, lots = await memo_out(client, h, [(2, 10.0)], lots_per_line=2)
    inv = await drawn(client, h, memo, {lids[0]: 1})
    await ok(await client.post(f"/docs/{inv}/finalize", headers=h))
    await ok(await client.post(f"/docs/{inv}/void", headers=h, json={}))

    r = await client.post(f"/docs/{inv}/unvoid", headers=h, json={})

    assert r.status_code == 409, r.text
    text = r.json()["detail"]["message"]
    assert "Invoice them from the memo" in text
    assert (await state(client, h, inv))["status"] == "void"


@pytest.mark.parametrize("then", ["finalize", "back_to_memo"])
async def test_close_refused_while_draft_holds_drawn_goods(client, session, then):
    h = await auth(session)
    memo, lids, lots = await memo_out(client, h, [(2, 10.0)], lots_per_line=2)
    pf = await drawn(client, h, memo, line_ids=lids)

    r = await client.post(f"/docs/{memo}/close", headers=h, json={})
    assert r.status_code == 409, r.text
    assert (await state(client, h, pf))["ref_id"] in r.json()["detail"]["message"]

    if then == "finalize":
        await ok(await client.post(f"/docs/{pf}/finalize", headers=h))
    else:
        await back_all(client, h, pf, memo)
    await ok(await client.post(f"/docs/{memo}/close", headers=h, json={}))


async def test_draw_replay_after_delete_moves_nothing(client, session):
    h = await auth(session)
    memo, lids, lots = await memo_out(client, h, [(2, 10.0)], lots_per_line=2, shipping=50.0)
    key = uuid.uuid4().hex
    inv = await drawn(client, h, memo, {lids[0]: 1}, key=key)
    await ok(await client.delete(f"/docs/{inv}", headers=h))
    v = (await state(client, h, memo))["version"]

    r = await ok(await draw(client, h, memo, {lids[0]: 1}, key=key))

    assert r["target_doc_id"] == inv
    assert (await client.get(f"/docs/{inv}", headers=h)).status_code == 404
    for i in lots[0]:
        assert await owner(client, h, i) == ("memo_out", memo)
    assert (await state(client, h, memo))["version"] == v
    assert await released(client, h, memo) == [True]


async def test_set_available_then_void_releases_take(client, session):
    h = await auth(session)
    memo, lids, lots = await memo_out(client, h, [(3, 3.3333333333)], lots_per_line=3, shipping=50.0)
    pf = await drawn(client, h, memo, {lids[0]: 2})
    await ok(await client.post(f"/docs/{pf}/finalize", headers=h))
    line = drawn_lines(await state(client, h, pf), memo)[0]
    await ok(await client.post(f"/docs/{pf}/set-available", headers=h,
                               json={"line_ids": [line["line_id"]], "quantities": {line["line_id"]: 1}}))
    in_stock = [i for i in lots[0] if (await item(client, h, i))["status"] == "available"]
    assert len(in_stock) == 1

    await ok(await client.post(f"/docs/{pf}/void", headers=h, json={}))

    m = await state(client, h, memo)
    assert await released(client, h, memo) == [True]
    assert m["memo_fixed_amounts"]["shipping"] == 50.0 and not m["memo_fixed_amounts"]["takes"]
    stock = await item(client, h, in_stock[0])
    assert (stock["status"], stock.get("status_doc_id")) == ("available", None)
    returned = (await session.execute(select(LedgerEntry).where(
        LedgerEntry.entity_id == memo, LedgerEntry.event_type == "doc.lines_returned"))).scalars().all()
    named = [ln["invoice_line_id"] for e in returned for ln in (e.data or {}).get("lines") or []]
    assert len(named) == 1
    nxt = await state(client, h, await drawn(client, h, memo, {lids[0]: 1}))
    assert drawn_lines(nxt, memo)[0]["line_total"] == pytest.approx(3.33)


# ── Several memos on one draft ────────────────────────────────────────────────

@pytest.mark.parametrize("action", ["back_one_memo", "void", "delete", "bulk_delete", "finalize_set_available"])
async def test_multi_memo_draft_end_to_end(client, session, action):
    h = await auth(session)
    pf, (m1, l1, lots1), (m2, l2, lots2) = await _two_memos_one_draft(client, h, new_invoice=False)

    if action == "back_one_memo":
        await back_all(client, h, pf, m1)
        assert await owner(client, h, lots2[0]) == ("memo_out", pf)
        assert await released(client, h, m2) == [False]
    elif action == "void":
        await ok(await client.post(f"/docs/{pf}/finalize", headers=h))
        await ok(await client.post(f"/docs/{pf}/void", headers=h, json={}))
    elif action == "delete":
        await ok(await client.delete(f"/docs/{pf}", headers=h))
    elif action == "bulk_delete":
        await ok(await client.delete("/docs/bulk-draft", headers=h, params={"doc_ids": pf}))
    else:
        await ok(await client.post(f"/docs/{pf}/finalize", headers=h))
        doc = await state(client, h, pf)
        ids = [li["line_id"] for li in drawn_lines(doc)]
        await ok(await client.post(f"/docs/{pf}/set-available", headers=h, json={"line_ids": ids}))
        for i in [*lots1, lots2[0]]:
            assert (await item(client, h, i))["status"] == "available"
        assert await released(client, h, m1) == [False]
        return

    for i in lots1:
        assert await owner(client, h, i) == ("memo_out", m1)
    assert await released(client, h, m1) == [True]
    if action != "back_one_memo":
        assert await owner(client, h, lots2[0]) == ("memo_out", m2)
        assert await released(client, h, m2) == [True]


# ── After the goods are invoiced ──────────────────────────────────────────────

async def test_credit_note_against_drawn_invoice_works(client, session):
    h = await auth(session)
    memo, lids, lots = await memo_out(client, h, [(2, 40.0)], lots_per_line=2)
    inv = await drawn(client, h, memo, line_ids=lids)
    await ok(await client.post(f"/docs/{inv}/finalize", headers=h))
    lot_id = drawn_lines(await state(client, h, inv), memo)[0]["item_id"]
    assert (await item(client, h, lot_id))["status"] == "sold"
    sku = (await item(client, h, lot_id))["sku"]

    cn = await ok(await client.post("/docs", headers=h, json={
        "doc_type": "credit_note", "original_doc_id": inv, "ref_id": f"CN-{uuid.uuid4().hex[:6]}",
        "line_items": [{"name": "Lot", "sku": sku, "entity_id": lot_id, "quantity": 1, "unit_price": 40.0,
                        "line_total": 40.0, "sell_by": "piece"}], "total": 40.0}))
    await ok(await client.post(f"/docs/{cn['id']}/finalize", headers=h, json={}))

    note = await state(client, h, cn["id"])
    assert not drawn_lines(note)
    assert (await item(client, h, lot_id))["status"] == "available"


# ── The grant Back to memo writes with ────────────────────────────────────────

async def test_back_to_memo_grant_shape(client, session):
    h = await auth(session)
    pf, (m1, l1, lots1), (m2, l2, lots2) = await _two_memos_one_draft(client, h, new_invoice=False)
    doc = await state(client, h, pf)
    assert doc["shipping"] == pytest.approx(100.0)
    m2_line = drawn_lines(doc, m2)[0]

    await back_all(client, h, pf, m1)

    after = await state(client, h, pf)
    assert len(after["line_items"]) == 2
    assert drawn_lines(after, m2)[0]["drawn_from"] == m2_line["drawn_from"]
    assert [li["description"] for li in after["line_items"] if not li.get("drawn_from")] == ["Setting fee"]
    assert after["shipping"] == pytest.approx(50.0)


async def test_back_to_memo_grant_shape_engine(client, session):
    from celerp.events.engine import MemoDraw, MemoReturn, emit_event

    a = await auth_full(session)
    h = a["headers"]
    pf, (m1, _, _), (m2, _, _) = await _two_memos_one_draft(client, h, new_invoice=False)
    doc = await state(client, h, pf)
    one, two = drawn_lines(doc, m1)[0], drawn_lines(doc, m2)[0]

    async def emitted(lines, **grant):
        async with session.begin_nested():
            await emit_event(session, company_id=a["company_id"], entity_id=pf, entity_type="doc",
                             event_type="doc.updated",
                             data={"fields_changed": {"line_items": {"old": doc["line_items"], "new": lines}}},
                             actor_id=None, location_id=None, source="api",
                             idempotency_key=uuid.uuid4().hex, metadata_={}, **grant)

    without_one = [li for li in doc["line_items"] if li["line_id"] != one["line_id"]]
    added = [*without_one, {**service_line(), "line_id": str(uuid.uuid4()),
                            "drawn_from": {**two["drawn_from"], "memo_line_id": "forged"}}]
    changed = [{**li, "unit_price": 1.0} if li["line_id"] == two["line_id"] else li for li in without_one]
    for lines in (added, changed):
        with pytest.raises(HTTPException) as e:
            await emitted(lines, memo_draw=MemoReturn(removed=frozenset({one["line_id"]})))
        assert e.value.detail["message_key"] == "docs.memo_provenance_forged"
    with pytest.raises(HTTPException) as e:
        await emitted(without_one, memo_draw=MemoDraw(memo_id=m1, draw_id="d", lines={}))
    assert e.value.detail["message_key"] == "docs.drawn_line_back_to_memo"
    with pytest.raises(AssertionError):
        await emitted(doc["line_items"], memo_draw=MemoReturn(removed=frozenset({one["line_id"]})))
    assert (await state(client, h, pf))["version"] == doc["version"]
