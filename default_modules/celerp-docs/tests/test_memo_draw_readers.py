# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""What other parts of Celerp read from an invoice drawn from a memo: the draft list by
currency, unshipped demand, and cost of sales, which is booked only when the invoice is
finalized and only once per finalize."""
from __future__ import annotations

import uuid

import pytest

from celerp.models.projections import Projection
from celerp.services.fulfill import outstanding_physical_lines
from memo_draw_support import (
    auth, auth_full, back_to_memo, draft, draw, drawn, drawn_lines, key_of, legacy_convert, lots_out_on, memo_out, ok, service_line,
    state,
)

pytestmark = pytest.mark.asyncio


async def test_list_docs_filters_by_currency(client, session):
    h = await auth(session)
    ids = {cur: await draft(client, h, lines=[service_line(5.0)], currency=cur) for cur in ("USD", "EUR")}

    body = await ok(await client.get("/docs", headers=h, params={"doc_type": "invoice", "status": "draft", "currency": "EUR"}))

    rows = body if isinstance(body, list) else body.get("items") or body.get("docs") or []
    listed = {row.get("id") or row.get("entity_id") for row in rows}
    assert ids["EUR"] in listed
    assert ids["USD"] not in listed


async def test_drawn_lines_skip_outstanding_physical_lines(client, session):
    a = await auth_full(session)
    h = a["headers"]
    memo, lids, _ = await memo_out(client, h, [(2, 50.0)], lots_per_line=2)
    pf = await drawn(client, h, memo, {lids[0]: 1}, target=await draft(client, h, lines=[service_line()]))
    assert len(drawn_lines(await state(client, h, pf))) == 1

    session.expire_all()
    row = await session.get(Projection, {"company_id": a["company_id"], "entity_id": pf})
    lines = (await outstanding_physical_lines(session, a["company_id"], [row])).get(pf, [])

    assert [ln for ln in lines if ln.outstanding > 0] == []


async def _net_for(client, h, doc_id: str) -> dict[str, float]:
    """Net debit less credit per account over the live journal entries that name ``doc_id``."""
    led = (await ok(await client.get("/ledger?entity_type=journal_entry&limit=500", headers=h)))["items"]
    led = [e for e in led if doc_id in str(e.get("data"))]
    voided = {e["entity_id"] for e in led if str(e.get("event_type", "")).endswith(".voided")}
    net: dict[str, float] = {}
    for e in led:
        if not str(e.get("event_type", "")).endswith(".created") or e["entity_id"] in voided:
            continue
        for x in (e.get("data") or {}).get("entries", []):
            net[x["account"]] = round(net.get(x["account"], 0.0) + float(x.get("debit") or 0) - float(x.get("credit") or 0), 2)
    return {k: v for k, v in net.items() if abs(v) > 1e-9}


async def test_cogs_only_at_finalize_and_once_per_cycle(client, session):
    h = await auth(session)
    memo, lids, _ = await memo_out(client, h, [(3, 50.0)], lots_per_line=3)
    pf = await drawn(client, h, memo, {lids[0]: 2})
    assert await _net_for(client, h, pf) == {}

    back = drawn_lines(await state(client, h, pf))[0]["line_id"]
    await ok(await back_to_memo(client, h, pf, [back]))
    assert await _net_for(client, h, pf) == {}
    await drawn(client, h, memo, {lids[0]: 2}, target=pf)

    once = None
    for _ in range(3):
        await ok(await client.post(f"/docs/{pf}/finalize", headers=h))
        booked = await _net_for(client, h, pf)
        assert booked, "finalize books revenue and cost of sales"
        assert once is None or booked == once
        once = booked
        await ok(await client.post(f"/docs/{pf}/revert-to-draft", headers=h, json={}))
        assert await _net_for(client, h, pf) == {}


# ── Memos converted before partial invoicing ─────────────────────────────────

async def invoice_all(client, h, memo: str, key: str | None = None):
    return await client.post(f"/docs/{memo}/convert", headers=h, json={"idempotency_key": key or uuid.uuid4().hex})


async def test_legacy_converted_invoice_still_returns_on_void(client, session):
    a = await auth_full(session)
    h = a["headers"]
    memo, _, lots = await memo_out(client, h, [(2, 10.0)], lots_per_line=2)
    inv = await legacy_convert(client, session, a, memo, lots[0])

    await ok(await client.post(f"/docs/{inv}/void", headers=h, json={"reason": "wrong customer"}))

    assert await lots_out_on(client, h, memo, lots[0]) == lots[0]
    m = await state(client, h, memo)
    assert m["status"] == "final" and not m.get("converted_to")


async def test_legacy_converted_memo_shows_no_fixed_amounts_left(client, session):
    a = await auth_full(session)
    h = a["headers"]
    memo, lids, lots = await memo_out(client, h, [(2, 10.0)], lots_per_line=2, shipping=50.0,
                                      discount=5.0, discount_type="flat")
    inv = await legacy_convert(client, session, a, memo, lots[0])

    f = (await state(client, h, memo))["memo_fixed_amounts"]
    assert (f["shipping"], f["discount"], f["tax"]) == (0, 0, 0)
    assert [(t["invoice_id"], t["shipping"], t["discount"]) for t in f["takes"]] == [(inv, 50.0, 5.0)]
    assert not f["still_on_memo"]

    await ok(await client.post(f"/docs/{inv}/void", headers=h, json={"reason": "wrong customer"}))
    f = (await state(client, h, memo))["memo_fixed_amounts"]
    assert (f["shipping"], f["discount"], f["takes"]) == (50.0, 5.0, [])
    assert f["still_on_memo"] == pytest.approx(20.0 + 50.0 - 5.0)

    await ok(await client.post(f"/docs/{memo}/set-available", headers=h, json={"line_ids": lids}))
    await ok(await client.post(f"/docs/{memo}/close", headers=h, json={}))
    f = (await state(client, h, memo))["memo_fixed_amounts"]
    assert f["takes"] == [] and not f["still_on_memo"]


async def _legacy_column(client, session, a, column: str):
    """A memo in one column of the legacy state table: its id, line ids and lots."""
    h = a["headers"]
    memo, lids, lots = await memo_out(client, h, [(2, 10.0)], lots_per_line=2, shipping=50.0)
    if column in ("converted", "reopened"):
        inv = await legacy_convert(client, session, a, memo, lots[0])
        if column == "reopened":
            await ok(await client.post(f"/docs/{inv}/void", headers=h, json={"reason": "wrong customer"}))
    elif column in ("closed", "voided"):
        await ok(await client.post(f"/docs/{memo}/set-available", headers=h, json={"line_ids": lids}))
        await ok(await client.post(f"/docs/{memo}/{'close' if column == 'closed' else 'void'}", headers=h,
                                   json={} if column == "closed" else {"reason": "not sent"}))
    return memo, lids, lots


@pytest.mark.parametrize("column", ["never_drawn", "converted", "reopened", "closed", "voided"])
async def test_new_readers_over_legacy_memo_states(client, session, column):
    a = await auth_full(session)
    h = a["headers"]
    memo, lids, _ = await _legacy_column(client, session, a, column)
    m = await state(client, h, memo)
    f = m["memo_fixed_amounts"]

    # memo_fixed_amounts and the c5 rows
    if column in ("never_drawn", "reopened"):
        assert (f["shipping"], f["takes"]) == (50.0, [])
        assert f["still_on_memo"] == pytest.approx(70.0)
    elif column == "converted":
        assert f["shipping"] == 0 and [t["invoice_id"] for t in f["takes"]] == [m["converted_to"]]
        assert not f["still_on_memo"]
    else:
        assert f["takes"] == [] and not f["still_on_memo"]

    # Draw eligibility, through the preview so nothing is written
    r = await draw(client, h, memo, line_ids=lids, dry_run=True)
    expected_key = {"converted": "documents.err_memo_nothing_out", "closed": "documents.err_memo_issue_first",
                    "voided": "documents.err_memo_issue_first"}.get(column)
    assert r.status_code == 200, r.text
    problems = r.json()["problems"]
    if expected_key:
        assert problems and problems[0]["message_key"] == expected_key, r.text
    else:
        assert not problems, r.text

    # Close: refused in every column but closing a memo with nothing out, which none of these is
    if column != "closed":
        assert (await client.post(f"/docs/{memo}/close", headers=h, json={})).status_code == 409

    # Invoice all
    key = uuid.uuid4().hex
    r = await invoice_all(client, h, memo, key)
    if expected_key:
        assert r.status_code == 409, r.text
        assert key_of(r) == expected_key
        return
    inv = (await ok(r))["target_doc_id"]
    assert (await state(client, h, inv))["shipping"] == 50.0
    m = await state(client, h, memo)
    assert m["status"] in ("final", "sent")
    assert [t["invoice_id"] for t in m["memo_fixed_amounts"]["takes"]] == [inv]
    assert (await ok(await invoice_all(client, h, memo, key)))["target_doc_id"] == inv
    assert len((await state(client, h, memo))["memo_fixed_amounts"]["takes"]) == 1


# ── Numbers shown by id ───────────────────────────────────────────────────────

async def test_renumbered_documents_named_by_current_number(client, session, monkeypatch):
    h = await auth(session)
    memo, lids, _ = await memo_out(client, h, [(2, 10.0), (1, 10.0)], lots_per_line=1, shipping=50.0)
    pf = await drawn(client, h, memo, line_ids=lids)
    old_memo_no, old_pf_no = (await state(client, h, memo))["ref_id"], (await state(client, h, pf))["ref_id"]
    await ok(await client.post(f"/docs/{memo}/renumber", headers=h, json={"ref_id": "MEMO-RENUMBERED"}))
    await ok(await client.post(f"/docs/{pf}/renumber", headers=h, json={"ref_id": "PF-RENUMBERED"}))

    m = await state(client, h, memo)
    assert m["memo_fixed_amounts"]["takes"][0]["doc_number"] == "PF-RENUMBERED"
    assert "PF-RENUMBERED" in m["line_items"][0]["shipped_label"]
    inv = await state(client, h, pf)
    assert [t["memo_number"] for t in inv["memo_fixed_taken"]] == ["MEMO-RENUMBERED"]
    r = await client.post(f"/docs/{memo}/close", headers=h, json={})
    assert r.status_code == 409 and "PF-RENUMBERED" in r.json()["detail"]["message"]

    await ok(await client.post(f"/docs/{pf}/finalize", headers=h))
    final_no = (await state(client, h, pf))["ref_id"]
    m = await state(client, h, memo)
    assert m["memo_fixed_amounts"]["takes"][0]["doc_number"] == final_no
    assert final_no in m["line_items"][0]["shipped_label"]

    from celerp_docs import routes as _routes

    async def _boom(*args, **kwargs):
        raise RuntimeError("ledger read failed")

    monkeypatch.setattr(_routes, "_current_doc_numbers", _boom)
    m = await state(client, h, memo)
    assert m["memo_fixed_amounts"]["takes"][0]["doc_number"] == old_pf_no
    assert [t["memo_number"] for t in (await state(client, h, pf))["memo_fixed_taken"]] == [old_memo_no]


async def test_invoice_note_omitted_when_memo_unreadable(client, session, monkeypatch):
    h = await auth(session)
    memo, lids, _ = await memo_out(client, h, [(2, 10.0)], lots_per_line=2, shipping=50.0)
    pf = await drawn(client, h, memo, {lids[0]: 1})
    stored = await state(client, h, pf)
    assert stored["memo_fixed_taken"]

    from celerp_docs import routes as _routes

    async def _boom(*args, **kwargs):
        raise RuntimeError("ledger read failed")

    monkeypatch.setattr(_routes, "_memo_fixed_taken", _boom)
    r = await client.get(f"/docs/{pf}", headers=h)

    assert r.status_code == 200, r.text
    doc = r.json()
    assert "memo_fixed_taken" not in doc
    assert (doc["total"], doc["shipping"]) == (stored["total"], stored["shipping"])
