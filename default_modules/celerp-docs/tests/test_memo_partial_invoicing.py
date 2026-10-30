# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Invoice part of a memo: the draw moves chosen goods from an issued memo onto a new or an
existing draft invoice, leaves the rest out on the memo, and keeps every unit, amount and
lot traceable to the memo line it came from."""
from __future__ import annotations

import uuid

import pytest

from memo_draw_support import (
    auth, auth_full, contact, draft, draw, drawn, drawn_lines, item, key_of, memo_out, ok, owner, revoke, service_line, state,
)


# ── The draw itself ───────────────────────────────────────────────────────────

async def test_draw_partial_lot_splits_child_onto_invoice(client, session):
    h = await auth(session)
    memo, lids, lots = await memo_out(client, h, [(10, 10.0)])
    parent = lots[0][0]
    before = await item(client, h, parent)

    inv = await drawn(client, h, memo, {lids[0]: 4})

    lines = drawn_lines(await state(client, h, inv), memo)
    assert len(lines) == 1 and lines[0]["quantity"] == 4
    child = lines[0]["item_id"]
    assert child != parent
    assert await owner(client, h, child) == ("memo_out", inv)
    rest = await item(client, h, parent)
    assert (rest["status"], rest["status_doc_id"], rest["quantity"]) == ("memo_out", memo, 6)
    kid = await item(client, h, child)
    assert kid["quantity"] == 4
    assert kid["cost_total"] + rest["cost_total"] == pytest.approx(before["cost_total"])


async def test_draw_leaves_rest_on_memo_without_set_available(client, session):
    h = await auth(session)
    memo, lids, lots = await memo_out(client, h, [(5, 10.0)], lots_per_line=5)

    inv = await drawn(client, h, memo, {lids[0]: 3})

    m = await state(client, h, memo)
    assert m["status"] == "final"
    on_memo = [i for i in lots[0] if (await owner(client, h, i)) == ("memo_out", memo)]
    on_inv = [i for i in lots[0] if (await owner(client, h, i)) == ("memo_out", inv)]
    assert (len(on_memo), len(on_inv)) == (2, 3)
    assert not [i for i in lots[0] if (await item(client, h, i))["status"] == "available"]


async def test_draw_onto_existing_same_customer_draft(client, session):
    h = await auth(session)
    cid = await contact(client, h)
    memo, lids, _ = await memo_out(client, h, [(2, 10.0), (3, 20.0)], lots_per_line=1, contact_id=cid)
    pf = await draft(client, h, contact_id=cid, lines=[service_line(20.0)])

    assert await drawn(client, h, memo, {lids[0]: 2}, target=pf) == pf
    assert await drawn(client, h, memo, {lids[1]: 1}, target=pf) == pf

    doc = await state(client, h, pf)
    assert [li["description"] for li in doc["line_items"] if not li.get("drawn_from")] == ["Setting fee"]
    assert sorted(li["quantity"] for li in drawn_lines(doc, memo)) == [1, 2]


async def test_draw_refuses_other_customer_target(client, session):
    h = await auth(session)
    cid = await contact(client, h, "Store A")
    other = await contact(client, h, "Store B")
    memo, lids, lots = await memo_out(client, h, [(2, 10.0)], contact_id=cid)
    for target in (await draft(client, h, contact_id=other),
                   await draft(client, h, contact_id=cid, currency="EUR")):
        v = (await state(client, h, target))["version"]
        r = await draw(client, h, memo, {lids[0]: 1}, target=target)
        assert r.status_code == 409, r.text
        assert key_of(r) == "docs.draw_target_other_customer"
        assert (await state(client, h, target))["version"] == v
        assert await owner(client, h, lots[0][0]) == ("memo_out", memo)


async def test_draw_refuses_non_draft_target(client, session):
    h = await auth(session)
    memo, lids, lots = await memo_out(client, h, [(2, 10.0)])
    final = await draft(client, h, lines=[service_line()])
    await ok(await client.post(f"/docs/{final}/finalize", headers=h))
    void = await draft(client, h, lines=[service_line()])
    await ok(await client.post(f"/docs/{void}/void", headers=h, json={}))
    quote = (await ok(await client.post("/docs", headers=h, json={"doc_type": "quotation", "line_items": []})))["id"]
    stranger = await auth(session)
    # Named apart from this company's own documents, so only the company tells it apart.
    foreign = await draft(client, stranger, ref_id=f"PF-{uuid.uuid4().hex[:8]}")
    for target in (final, void, quote):
        r = await draw(client, h, memo, {lids[0]: 1}, target=target)
        assert r.status_code == 409 and key_of(r) == "docs.draw_target_not_draft", r.text
        assert await owner(client, h, lots[0][0]) == ("memo_out", memo)
    # Another company's draft is not found for this company, and the memo route itself is.
    r = await draw(client, h, memo, {lids[0]: 1}, target=foreign)
    assert r.status_code == 404 and (await draw(client, h, memo, {lids[0]: 1}, dry_run=True)).status_code == 200, r.text
    assert await owner(client, h, lots[0][0]) == ("memo_out", memo)
    assert (await state(client, h, final))["line_items"][0]["description"] == "Setting fee"


async def test_draw_quantity_more_than_out_refused(client, session):
    h = await auth(session)
    memo, lids, lots = await memo_out(client, h, [(3, 10.0), (2, 10.0)])
    r = await draw(client, h, memo, {lids[0]: 4, lids[1]: 5})
    assert r.status_code == 409, r.text
    problems = [r.json()["detail"], *r.json()["detail"].get("also", [])]
    assert [p["message_key"] for p in problems] == ["docs.draw_quantity_more_than_out"] * 2
    assert [await owner(client, h, ids[0]) for ids in lots] == [("memo_out", memo)] * 2


async def test_draw_with_no_lines_refused(client, session):
    h = await auth(session)
    memo, _, lots = await memo_out(client, h)
    r = await draw(client, h, memo, line_ids=[])
    assert r.status_code in (409, 422), r.text
    assert await owner(client, h, lots[0][0]) == ("memo_out", memo)


@pytest.mark.parametrize("removed", ["edit_documents", "fulfill_documents"])
async def test_draw_requires_edit_and_fulfill(client, session, removed):
    a = await auth_full(session)
    h = a["headers"]
    memo, lids, lots = await memo_out(client, h)
    await revoke(session, a["company_id"], removed)
    r = await draw(client, h, memo, {lids[0]: 1})
    assert r.status_code == 403, r.text
    assert await owner(client, h, lots[0][0]) == ("memo_out", memo)


async def test_invoice_all_requires_edit_and_fulfill(client, session):
    a = await auth_full(session)
    h = a["headers"]
    memo, _, _ = await memo_out(client, h)
    await revoke(session, a["company_id"], "fulfill_documents")
    r = await client.post(f"/docs/{memo}/convert", headers=h)
    assert r.status_code == 403, r.text
    assert (await state(client, h, memo))["status"] == "final"


def test_invoice_lines_request_has_no_remaining_count():
    from celerp_docs.routes import InvoiceLinesRequest

    fields = set(InvoiceLinesRequest.model_fields)
    assert {"line_ids", "quantities", "target_doc_id", "dry_run", "expected_versions"} <= fields
    assert not {f for f in fields if "remain" in f or "left" in f}


async def test_repeated_draws_over_time_never_rebill(client, session):
    h = await auth(session)
    memo, lids, lots = await memo_out(client, h, [(3, 10.0)], lots_per_line=3)
    invoices = [await drawn(client, h, memo, {lids[0]: 1}) for _ in range(3)]
    billed = []
    for inv in invoices:
        billed += [li["item_id"] for li in drawn_lines(await state(client, h, inv), memo)]
    assert sorted(billed) == sorted(lots[0])
    r = await draw(client, h, memo, {lids[0]: 1})
    assert r.status_code == 409, r.text


async def test_draw_same_key_different_request_refused(client, session):
    h = await auth(session)
    memo, lids, lots = await memo_out(client, h, [(4, 10.0)], lots_per_line=4)
    pf = await draft(client, h)
    key = uuid.uuid4().hex
    first = await ok(await draw(client, h, memo, {lids[0]: 1}, key=key))
    again = await ok(await draw(client, h, memo, {lids[0]: 1}, key=key))
    assert again == first
    r = await draw(client, h, memo, {lids[0]: 1}, key=key, target=pf)
    assert r.status_code == 409, r.text
    assert drawn_lines(await state(client, h, pf)) == []
    assert len([i for i in lots[0] if (await owner(client, h, i))[1] == memo]) == 3


@pytest.mark.parametrize("memo_state", ["draft", "void", "closed", "closed_with_deposit"])
async def test_draw_refuses_memo_not_live(client, session, memo_state):
    h = await auth(session)
    if memo_state == "draft":
        memo = (await ok(await client.post("/docs", headers=h, json={"doc_type": "memo", "line_items": [
            {"description": "Ring", "quantity": 1, "unit_price": 10.0}]})))["id"]
        lid = (await state(client, h, memo))["line_items"][0]["line_id"]
    else:
        memo, lids, _ = await memo_out(client, h, [(1, 100.0)])
        lid = lids[0]
        if memo_state == "closed_with_deposit":
            from memo_draw_support import pay
            await ok(await pay(client, h, memo, 50.0))
        await ok(await client.post(f"/docs/{memo}/revert-lines", headers=h, json={"line_ids": [lid]}))
        if memo_state == "void":
            await ok(await client.post(f"/docs/{memo}/void", headers=h, json={}))
        else:
            await ok(await client.post(f"/docs/{memo}/close", headers=h, json={}))
    v = (await state(client, h, memo))["version"]
    r = await draw(client, h, memo, {lid: 1})
    assert r.status_code == 409, r.text
    assert key_of(r) == "documents.err_memo_issue_first"
    assert not r.json()["detail"].get("also")
    assert (await state(client, h, memo))["version"] == v
