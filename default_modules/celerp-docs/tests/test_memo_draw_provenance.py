# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Where a line came from: only Invoice selected on a memo can put a memo line on an invoice,
no other writer can add, change or drop that link, and a drawn line keeps it through every
price edit."""
from __future__ import annotations

import uuid

import pytest

from memo_draw_support import (
    auth, back_to_memo, draft, drawn, drawn_lines, key_of, lot, memo_out, ok, owner, service_line, state,
)

pytestmark = pytest.mark.asyncio


def line(item_id: str | None, qty: float, *, price: float = 10.0, sku: str = "", **extra) -> dict:
    return {"item_id": item_id, "sku": sku, "description": sku or "Line", "quantity": qty,
            "unit_price": price, "sell_by": "piece", **extra}


def _record(data: dict, entity_id: str | None = None) -> dict:
    return {"entity_id": entity_id or f"doc:IMP-{uuid.uuid4().hex[:8]}", "event_type": "doc.created",
            "source": "memo_draw", "idempotency_key": uuid.uuid4().hex, "data": data}


def _forged_line(lot_id: str, sku: str, memo: str) -> dict:
    return line(lot_id, 1, price=300.0, sku=sku, drawn_from={
        "memo_id": memo, "memo_number": "M-1", "memo_line_id": "x", "draw_id": "d"})


async def _has_drawn_from(client, h, entity_id: str) -> bool | None:
    r = await client.get(f"/docs/{entity_id}" if entity_id.startswith("doc:") else f"/lists/{entity_id}", headers=h)
    if r.status_code != 200:
        return None
    return any("drawn_from" in li for li in r.json().get("line_items") or [])


async def _patch_lines(client, h, doc_id: str, lines: list[dict]):
    st = await state(client, h, doc_id)
    return await client.patch(f"/docs/{doc_id}", headers=h, json={
        "fields_changed": {"line_items": {"new": lines}}, "expected_version": st["version"]})


async def _upsert_lines(client, h, doc_id: str, lines: list[dict]):
    st = await state(client, h, doc_id)
    data = {k: v for k, v in st.items() if k in ("doc_type", "status", "contact_id", "currency", "shipping")}
    rec = _record({**data, "line_items": lines}, entity_id=doc_id)
    return await client.post("/docs/import/batch", headers=h, json={"records": [rec], "upsert": True})


def _batch_refused(r) -> bool:
    """A batch import answers 200 and names each refused record in ``errors``."""
    return r.status_code == 200 and bool(r.json().get("errors")) and not r.json().get("updated") and not r.json().get("created")


# ── Adding provenance ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("writer", ["patch", "raw_import", "batch_import", "list_import", "upsert"])
async def test_writer_bypass_forged_memo_provenance_refused(client, session, writer):
    h = await auth(session)
    memo, _, _ = await memo_out(client, h)
    sku = f"BY-{uuid.uuid4().hex[:6]}"
    forged = _forged_line(await lot(client, h, sku, 1), sku, memo)

    if writer == "patch":
        target = await draft(client, h, lines=[service_line()])
        r = await _patch_lines(client, h, target, [*(await state(client, h, target))["line_items"], forged])
    elif writer == "raw_import":
        rec = _record({"doc_type": "invoice", "status": "draft", "line_items": [forged]})
        target, r = rec["entity_id"], await client.post("/docs/import", headers=h, json=rec)
    elif writer == "batch_import":
        rec = _record({"doc_type": "invoice", "status": "draft", "line_items": [forged]})
        target, r = rec["entity_id"], await client.post("/docs/import/batch", headers=h, json={"records": [rec]})
    elif writer == "list_import":
        target = f"list:IMP-{uuid.uuid4().hex[:8]}"
        r = await client.post("/lists/import", headers=h, json={
            "entity_id": target, "event_type": "list.created", "source": "memo_draw", "idempotency_key": uuid.uuid4().hex,
            "data": {"list_type": "quotation", "status": "draft", "line_items": [forged]}})
    else:
        plain = _record({"doc_type": "invoice", "status": "draft", "line_items": [line(None, 1, price=5.0)]})
        await ok(await client.post("/docs/import", headers=h, json=plain))
        target = plain["entity_id"]
        r = await _upsert_lines(client, h, target, [*(await state(client, h, target))["line_items"], forged])

    if writer in ("batch_import", "upsert"):
        assert _batch_refused(r), r.text
        assert "memo" in str(r.json()["errors"]).lower()
    else:
        assert r.status_code == 409, r.text
        assert key_of(r) == "docs.memo_provenance_forged"
    assert not await _has_drawn_from(client, h, target)


async def test_writer_bypass_page_create_drops_drawn_from(client, session):
    """Green guard: the page's create path drops a client ``drawn_from``."""
    h = await auth(session)
    memo, _, _ = await memo_out(client, h)
    sku = f"BY-{uuid.uuid4().hex[:6]}"
    r = await client.post("/docs", headers=h, json={
        "doc_type": "invoice", "line_items": [_forged_line(await lot(client, h, sku, 1), sku, memo)], "total": 300})
    created = await ok(r)
    assert await _has_drawn_from(client, h, created["id"]) is False


# ── Changing or dropping a drawn line ─────────────────────────────────────────

async def _drawn_draft(client, h):
    """A draft with a drawn line from a memo and an ordinary line."""
    memo, lids, lots = await memo_out(client, h, [(2, 50.0)], lots_per_line=2)
    pf = await drawn(client, h, memo, {lids[0]: 1}, target=await draft(client, h, lines=[service_line()]))
    return pf, memo, lots[0]


@pytest.mark.parametrize("transport", ["patch", "upsert"])
@pytest.mark.parametrize("change", ["drawn_from", "remove", "quantity"])
async def test_writer_bypass_drawn_line_edits_refused(client, session, transport, change):
    h = await auth(session)
    pf, memo, _ = await _drawn_draft(client, h)
    before = await state(client, h, pf)
    lines = [dict(li) for li in before["line_items"]]
    dl = next(li for li in lines if li.get("drawn_from"))
    if change == "drawn_from":
        dl["drawn_from"] = {**dl["drawn_from"], "memo_line_id": "other"}
    elif change == "remove":
        lines = [li for li in lines if not li.get("drawn_from")]
    else:
        dl["quantity"] = 2

    r = await (_patch_lines if transport == "patch" else _upsert_lines)(client, h, pf, lines)

    expected = {"drawn_from": "docs.memo_provenance_forged", "remove": "docs.drawn_line_back_to_memo",
                "quantity": "docs.drawn_line_locked"}[change]
    if transport == "patch":
        assert r.status_code == 409, r.text
        assert key_of(r) == expected
    else:
        assert _batch_refused(r), r.text
    after = await state(client, h, pf)
    assert after["version"] == before["version"]
    assert drawn_lines(after) == drawn_lines(before)


async def test_forged_import_drawn_line_refused(client, session):
    h = await auth(session)
    memo, _, lots = await memo_out(client, h)
    out_lot = lots[0][0]
    sku = (await state(client, h, memo))["line_items"][0]["sku"]

    free = await lot(client, h, sku, 1)
    rec = _record({"doc_type": "invoice", "status": "draft", "line_items": [_forged_line(free, sku, memo)]})
    r = await client.post("/docs/import", headers=h, json=rec)
    assert r.status_code == 409, r.text
    assert key_of(r) == "docs.memo_provenance_forged"
    assert (await client.get(f"/docs/{rec['entity_id']}", headers=h)).status_code == 404

    rec = _record({"doc_type": "invoice", "status": "draft", "line_items": [_forged_line(out_lot, sku, memo)]})
    r = await client.post("/docs/import", headers=h, json=rec)
    assert r.status_code == 409, r.text
    assert key_of(r) == "docs.memo_provenance_forged"
    assert await owner(client, h, out_lot) == ("memo_out", memo)


async def test_forged_header_source_memo_is_inert(client, session):
    h = await auth(session)
    memo, _, lots = await memo_out(client, h)
    out_lot = lots[0][0]
    sku = (await state(client, h, memo))["line_items"][0]["sku"]
    rec = _record({"doc_type": "invoice", "status": "draft", "source_memo_id": memo,
                   "line_items": [line(out_lot, 1, price=300.0, sku=sku)]})

    r = await client.post("/docs/import", headers=h, json=rec)

    assert r.status_code == 422, r.text
    assert (await client.get(f"/docs/{rec['entity_id']}", headers=h)).status_code == 404
    assert await owner(client, h, out_lot) == ("memo_out", memo)


# ── Price edits keep the link ─────────────────────────────────────────────────

def _page_line(li: dict, price: float) -> dict:
    """The line as the draft page's line builder rebuilds it from its inputs: no ``drawn_from``."""
    return {"line_id": li["line_id"], "description": li.get("description") or "", "sku": li.get("sku"),
            "quantity": li["quantity"], "unit": li.get("unit"), "unit_price": price, "discount_pct": 0,
            "tax_rate": 0, "taxes": [], "line_total": price * float(li["quantity"]), "entity_id": li.get("item_id")}


@pytest.mark.parametrize("new_invoice", [True, False])
@pytest.mark.parametrize("transport", ["page_save", "field_edit"])
async def test_page_price_edit_keeps_drawn_from(client, session, new_invoice, transport):
    h = await auth(session)
    memo, lids, _ = await memo_out(client, h, [(2, 50.0)], lots_per_line=2)
    target = None if new_invoice else await draft(client, h, lines=[service_line()])
    pf = await drawn(client, h, memo, {lids[0]: 1}, target=target)
    st = await state(client, h, pf)
    link = drawn_lines(st)[0]["drawn_from"]
    if transport == "page_save":
        lines = [_page_line(li, 80.0) if li.get("drawn_from") else li for li in st["line_items"]]
    else:  # the one-field edit copies the stored line and sets one field
        lines = [{**li, "unit_price": 80.0} if li.get("drawn_from") else li for li in st["line_items"]]

    await ok(await _patch_lines(client, h, pf, lines))

    after = await state(client, h, pf)
    dl = drawn_lines(after)
    assert len(dl) == 1 and dl[0]["drawn_from"] == link
    assert float(dl[0]["unit_price"]) == 80.0
    assert float(after["subtotal"]) == pytest.approx(sum(float(li.get("line_total") or 0) for li in after["line_items"]))


# ── Memo void ─────────────────────────────────────────────────────────────────

async def test_memo_void_refused_while_goods_on_invoice(client, session):
    h = await auth(session)
    memo, lids, _ = await memo_out(client, h, [(1, 10.0), (1, 20.0), (1, 30.0)])
    pf = await drawn(client, h, memo, {lids[0]: 1})
    fin = await drawn(client, h, memo, {lids[1]: 1})
    await ok(await client.post(f"/docs/{fin}/finalize", headers=h))

    r = await client.post(f"/docs/{memo}/void", headers=h, json={})
    assert r.status_code == 409, r.text
    assert key_of(r) == "docs.memo_void_while_drawn"
    assert (await state(client, h, memo))["status"] != "void"

    await ok(await client.post(f"/docs/{fin}/void", headers=h, json={}))
    r = await client.post(f"/docs/{memo}/void", headers=h, json={})
    assert r.status_code == 409 and key_of(r) == "docs.memo_void_while_drawn"
    assert (await state(client, h, pf))["doc_number"] in r.text

    await ok(await back_to_memo(client, h, pf, [li["line_id"] for li in drawn_lines(await state(client, h, pf))]))
    await ok(await client.post(f"/docs/{memo}/void", headers=h, json={}))
    assert (await state(client, h, memo))["status"] == "void"
