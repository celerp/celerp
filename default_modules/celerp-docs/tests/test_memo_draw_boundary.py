# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Every operation that touches a line drawn from a memo, on both sides: on the invoice the
drawn line keeps its quantity, item and link and only leaves by Back to memo; on the memo a
line with goods on an invoice can't be changed, removed or paid around. Each rule is checked
once where it is enforced, then once refused and once allowed through each way a change
reaches it."""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from celerp.events.engine import emit_event
from memo_draw_support import (
    auth_full, back_to_memo, contact, draft, drawn, drawn_lines, key_of, memo_out, ok, owner, service_line, state,
)

pytestmark = pytest.mark.asyncio

PAGE_KEYS = ("line_id", "item_id", "sku", "description", "quantity", "unit_price", "sell_by")


async def _emit(session, a, doc_id: str, changes: dict) -> None:
    """A ``doc.updated`` straight into the engine, as every writer of a line set ends."""
    await emit_event(session, company_id=a["company_id"], entity_id=doc_id, entity_type="doc", event_type="doc.updated",
                     data={"fields_changed": {k: {"new": v} for k, v in changes.items()}}, actor_id=a["user_id"],
                     location_id=None, source="api", idempotency_key=uuid.uuid4().hex)


async def _engine_refuses(session, a, doc_id: str, changes: dict) -> str | None:
    try:
        await _emit(session, a, doc_id, changes)
    except HTTPException as exc:
        await session.rollback()
        return exc.detail.get("message_key") if isinstance(exc.detail, dict) else str(exc.detail)
    await session.commit()
    return None


async def _patch(client, h, doc_id: str, changes: dict):
    st = await state(client, h, doc_id)
    return await client.patch(f"/docs/{doc_id}", headers=h, json={
        "expected_version": st["version"], "fields_changed": {k: {"new": v} for k, v in changes.items()}})


async def _upsert(client, h, doc_id: str, changes: dict):
    st = await state(client, h, doc_id)
    data = {k: v for k, v in st.items() if k in ("doc_type", "status", "contact_id", "currency", "line_items")}
    rec = {"entity_id": doc_id, "event_type": "doc.created", "source": "import", "idempotency_key": uuid.uuid4().hex,
           "data": {**data, **changes}}
    return await client.post("/docs/import/batch", headers=h, json={"records": [rec], "upsert": True})


def _refused_in_batch(r, text: str) -> bool:
    body = r.json()
    return r.status_code == 200 and not body.get("updated") and any(text in str(e) for e in body.get("errors") or [])


def _page_lines(doc: dict) -> list[dict]:
    return [{k: li.get(k) for k in PAGE_KEYS} for li in doc["line_items"]]


# ── (a) The invoice side ──────────────────────────────────────────────────────

async def _drawn_draft(client, h):
    memo, lids, lots = await memo_out(client, h, [(2, 50.0)], lots_per_line=2)
    pf = await drawn(client, h, memo, {lids[0]: 1}, target=await draft(client, h, lines=[service_line()]))
    return pf, memo, lots[0]


def _edit_line(doc: dict, **change) -> list[dict]:
    lines = [dict(li) for li in doc["line_items"]]
    for li in lines:
        if li.get("drawn_from"):
            li.update(change)
    return lines


INVOICE_ROWS = {
    "price": (lambda d: {"line_items": _edit_line(d, unit_price=60.0)}, None),
    "line_discount": (lambda d: {"line_items": _edit_line(d, discount_pct=5)}, None),
    "description": (lambda d: {"line_items": _edit_line(d, description="Ring, resized")}, None),
    "quantity": (lambda d: {"line_items": _edit_line(d, quantity=2)}, "docs.drawn_line_locked"),
    "weight": (lambda d: {"line_items": _edit_line(d, weight=9.9)}, "docs.drawn_line_locked"),
    "item": (lambda d: {"line_items": _edit_line(d, item_id=None, sku="OTHER")}, "docs.drawn_line_locked"),
    "omit": (lambda d: {"line_items": [li for li in d["line_items"] if not li.get("drawn_from")]},
             "docs.drawn_line_back_to_memo"),
    "add_plain": (lambda d: {"line_items": [*d["line_items"], service_line(5.0)]}, None),
    "change_drawn_from": (lambda d: {"line_items": _edit_line(d, drawn_from={
        **drawn_lines(d)[0]["drawn_from"], "memo_line_id": "other"})}, "docs.memo_provenance_forged"),
    "shipping": (lambda d: {"shipping": 12.0}, None),
    "discount": (lambda d: {"discount": 3.0, "discount_type": "flat"}, None),
    "currency": (lambda d: {"currency": "EUR"}, "docs.drawn_header_locked"),
}


async def test_drawn_line_operations_boundary(client, session):
    a = await auth_full(session)
    h = a["headers"]
    for name, (build, refusal) in INVOICE_ROWS.items():
        pf, memo, lots = await _drawn_draft(client, h)
        before = await state(client, h, pf)

        assert await _engine_refuses(session, a, pf, build(before)) == refusal, name

        after = await state(client, h, pf)
        if refusal:
            assert after["version"] == before["version"], name
        else:
            assert [li["drawn_from"] for li in drawn_lines(after)] == [li["drawn_from"] for li in drawn_lines(before)], name
    pf, memo, lots = await _drawn_draft(client, h)
    assert await _engine_refuses(session, a, pf, {"contact_id": await contact(client, h, "Store B")}) \
        == "docs.drawn_header_locked"

    from celerp.services.document_lines import refuse_drawn_line_action

    doc = await state(client, h, pf)
    index = next(i for i, li in enumerate(doc["line_items"]) if li.get("drawn_from"))
    with pytest.raises(HTTPException) as exc:
        refuse_drawn_line_action(doc, [index])
    assert exc.value.detail["message_key"] == "docs.drawn_line_back_to_memo"
    refuse_drawn_line_action(doc, [i for i, li in enumerate(doc["line_items"]) if not li.get("drawn_from")])
    for route in ("reserve-lines", "set-available", "fulfill-lines"):
        r = await client.post(f"/docs/{pf}/{route}", headers=h, json={"line_ids": [doc["line_items"][index]["line_id"]]})
        assert key_of(r) == "docs.drawn_line_back_to_memo", (route, r.text)


TRANSPORTS_A = {
    # (transport, refused change, refusal key, allowed change)
    "page_line_save": ("page", lambda d: {"line_items": [li for li in _page_lines(d) if li["line_id"] not in
                                                          {x["line_id"] for x in drawn_lines(d)}],
                                          "discount": 0.0, "discount_type": "flat", "doc_taxes": []},
                       "docs.drawn_line_back_to_memo",
                       lambda d: {"line_items": [{**li, "unit_price": 70.0} if li["line_id"] in
                                                 {x["line_id"] for x in drawn_lines(d)} else li for li in _page_lines(d)],
                                  "discount": 0.0, "discount_type": "flat", "doc_taxes": []}),
    "bulk_delete_autosave": ("page", lambda d: {"line_items": [li for li in _page_lines(d) if li["line_id"] not in
                                                                {x["line_id"] for x in drawn_lines(d)}]},
                             "docs.drawn_line_back_to_memo",
                             lambda d: {"line_items": [li for li in _page_lines(d) if li["line_id"] in
                                                       {x["line_id"] for x in drawn_lines(d)}]}),
    "one_field_edit": ("page", lambda d: {"line_items": _edit_line(d, quantity=2)}, "docs.drawn_line_locked",
                       lambda d: {"line_items": _edit_line(d, description="Ring, resized")}),
    "api_patch": ("page", lambda d: {"line_items": [li for li in d["line_items"] if not li.get("drawn_from")]},
                  "docs.drawn_line_back_to_memo", lambda d: {"line_items": _edit_line(d, unit_price=55.0)}),
    "import_upsert": ("upsert", lambda d: {"line_items": [li for li in d["line_items"] if not li.get("drawn_from")]},
                      "docs.drawn_line_back_to_memo", lambda d: {"line_items": _edit_line(d, unit_price=55.0)}),
    "customer_field": ("page", None, "docs.drawn_header_locked", lambda d: {"notes": "Deliver Tuesday"}),
}


@pytest.mark.parametrize("transport", list(TRANSPORTS_A))
async def test_drawn_line_operations_boundary_transports(client, session, transport):
    a = await auth_full(session)
    h = a["headers"]
    pf, memo, lots = await _drawn_draft(client, h)
    how, refused, key, allowed = TRANSPORTS_A[transport]
    send = _patch if how == "page" else _upsert
    before = await state(client, h, pf)
    change = refused(before) if refused else {"contact_id": await contact(client, h, "Store B")}

    r = await send(client, h, pf, change)

    if how == "upsert":
        assert _refused_in_batch(r, "Back to memo"), r.text
    else:
        assert r.status_code == 409 and key_of(r) == key, r.text
    after = await state(client, h, pf)
    assert after["version"] == before["version"]
    assert drawn_lines(after) == drawn_lines(before)

    r = await send(client, h, pf, allowed(after))
    assert r.status_code == 200 and not r.json().get("errors"), r.text
    assert [li["drawn_from"] for li in drawn_lines(await state(client, h, pf))] == \
        [li["drawn_from"] for li in drawn_lines(before)]


# ── (c) The memo side ─────────────────────────────────────────────────────────

async def _memo_with_draws(client, h):
    """Memo lines A, B, C of one lot each and D of two; all of B and one unit of D on a draft."""
    store = await contact(client, h, "Store A")
    memo, lids, _ = await memo_out(client, h, [(1, 10.0), (1, 20.0), (1, 30.0), (2, 40.0)], contact_id=store)
    pf = await drawn(client, h, memo, {lids[1]: 1, lids[3]: 1}, target=await draft(client, h, contact_id=store))
    return SimpleNamespace(memo=memo, lids=dict(zip("ABCD", lids)), pf=pf, store=store)


async def _unchanged_then_back(client, h, s, before: dict) -> None:
    after = await state(client, h, s.memo)
    assert [li["line_id"] for li in after["line_items"]] == [li["line_id"] for li in before["line_items"]]
    assert (after.get("contact_id"), after.get("amount_paid") or 0, after["version"]) == \
        (before.get("contact_id"), before.get("amount_paid") or 0, before["version"])
    inv = await state(client, h, s.pf)
    b_line = next(li for li in drawn_lines(inv, s.memo) if li["drawn_from"]["memo_line_id"] == s.lids["B"])
    assert (await owner(client, h, b_line["item_id"]))[1] == s.pf
    await ok(await back_to_memo(client, h, s.pf, [b_line["line_id"]]))
    assert (await owner(client, h, b_line["item_id"]))[1] == s.memo


def _without(doc: dict, *line_ids: str) -> list[dict]:
    return [li for li in doc["line_items"] if li["line_id"] not in line_ids]


async def test_memo_line_with_draws_operations_boundary(client, session):
    a = await auth_full(session)
    h = a["headers"]
    s = await _memo_with_draws(client, h)
    m = await state(client, h, s.memo)

    # Line set and customer, at the engine
    for changes, refusal in (
        ({"line_items": _without(m, s.lids["B"])}, "docs.memo_line_drawn"),
        ({"line_items": _without(m, s.lids["D"])}, "docs.memo_line_drawn"),
        ({"line_items": [{**li, "quantity": 3} if li["line_id"] == s.lids["B"] else li for li in m["line_items"]]},
         "docs.memo_line_drawn"),
        ({"contact_id": await contact(client, h, "Store B")}, "docs.memo_contact_drawn"),
    ):
        assert await _engine_refuses(session, a, s.memo, changes) == refusal, changes
    assert await _engine_refuses(session, a, s.memo, {"line_items": [
        {**li, "description": "Ring B, resized"} if li["line_id"] == s.lids["B"] else li
        for li in m["line_items"]]}) is None

    # The four line actions: a fully drawn line is refused, a partly drawn one passes
    from celerp.services.document_lines import refuse_drawn_memo_line_action

    m = await state(client, h, s.memo)
    with pytest.raises(HTTPException) as exc:
        refuse_drawn_memo_line_action(m, [1])
    assert exc.value.detail["message_key"] == "docs.memo_line_drawn"
    refuse_drawn_memo_line_action(m, [3])
    for route in ("fulfill-lines", "reserve-lines", "set-available", "revert-lines"):
        r = await client.post(f"/docs/{s.memo}/{route}", headers=h, json={"line_ids": [s.lids["B"]]})
        assert key_of(r) == "docs.memo_line_drawn", (route, r.text)

    # Lifecycle and money
    for path, body, refusal in (
        ("revert-to-draft", {}, "docs.memo_revert_while_drawn"),
        ("void", {"reason": "not sent"}, "docs.memo_void_while_drawn"),
        ("close", {}, "docs.close_drawn_draft"),
        ("payment", {"amount": 10.0, "payment_date": "2026-03-02", "bank_account": "1111"},
         "docs.memo_payment_while_drawn"),
        ("refund", {"amount": 10.0, "refund_date": "2026-03-02"}, "docs.memo_payment_while_drawn"),
        ("tax-rate", {"tax_rate": 8, "expected_version": m["version"]}, "docs.memo_rate_no_unrated_tax"),
    ):
        r = await client.post(f"/docs/{s.memo}/{path}", headers=h, json=body)
        assert r.status_code == 409 and key_of(r) == refusal, (path, r.text)
    assert (await state(client, h, s.memo))["version"] == m["version"]


TRANSPORTS_C = ("api_patch", "import_upsert", "peer_sync", "page_line_save", "bulk_delete_autosave",
                "one_field_edit", "customer_field", "bulk_payment")


async def _peer_sync(session, a, memo: str, data: dict, monkeypatch):
    from celerp.services import peer_documents

    async def claim(*args, **kwargs):
        return SimpleNamespace(replay=False, target_id=memo)

    monkeypatch.setattr(peer_documents, "claim_peer_revision", claim)
    grant = SimpleNamespace(company_id=a["company_id"], source_company_id=uuid.uuid4(), label="Books")
    try:
        await peer_documents.receive_document(session, grant=grant, source_entity_id=memo,
                                              source_revision=2, data=data)
    except HTTPException as exc:
        await session.rollback()
        return exc
    await session.commit()
    return None


@pytest.mark.parametrize("transport", TRANSPORTS_C)
async def test_memo_line_with_draws_operations_boundary_transports(client, session, transport, monkeypatch):
    a = await auth_full(session)
    h = a["headers"]
    s = await _memo_with_draws(client, h)
    before = await state(client, h, s.memo)
    other = await contact(client, h, "Store B")

    if transport == "api_patch":
        r = await _patch(client, h, s.memo, {"line_items": _without(before, s.lids["D"])})
        assert r.status_code == 409 and key_of(r) == "docs.memo_line_drawn", r.text
        allowed = await _patch(client, h, s.memo, {"notes": "Back Friday"})
    elif transport == "import_upsert":
        r = await _upsert(client, h, s.memo, {"contact_id": other})
        assert _refused_in_batch(r, "Send them back to the memo"), r.text
        allowed = await _upsert(client, h, s.memo, {"notes": "Back Friday"})
    elif transport == "peer_sync":
        exc = await _peer_sync(session, a, s.memo, {"doc_type": "memo", "status": before["status"],
                                                    "contact_id": other}, monkeypatch)
        assert exc is not None and exc.status_code == 409
        allowed = None
    elif transport in ("page_line_save", "bulk_delete_autosave"):
        r = await _patch(client, h, s.memo, {"line_items": [li for li in _page_lines(before)
                                                            if li["line_id"] != s.lids["D"]],
                                             "discount": 0.0, "discount_type": "flat", "doc_taxes": []})
        assert r.status_code == 409 and key_of(r) == "docs.edit_locked", r.text
        allowed = None
    elif transport == "one_field_edit":
        r = await _patch(client, h, s.memo, {"line_items": [
            {**li, "quantity": 2} if li["line_id"] == s.lids["B"] else li for li in before["line_items"]]})
        assert r.status_code == 409 and key_of(r) == "documents.err_line_field_finalized", r.text
        allowed = await _patch(client, h, s.memo, {"line_items": [
            {**li, "description": "Ring B, resized"} if li["line_id"] == s.lids["B"] else li
            for li in before["line_items"]]})
    elif transport == "customer_field":
        r = await _patch(client, h, s.memo, {"contact_id": other})
        assert r.status_code == 409 and key_of(r) == "docs.memo_contact_drawn", r.text
        allowed = await _patch(client, h, s.memo, {"notes": "Back Friday"})
    else:
        plain = await draft(client, h, lines=[service_line(30.0)])
        await ok(await client.post(f"/docs/{plain}/finalize", headers=h))
        r = await client.post("/docs/bulk-payment", headers=h, json={
            "doc_ids": [s.memo, plain], "amount": 30.0, "payment_date": "2026-03-02", "bank_account": "1111"})
        body = await ok(r)
        assert [x["doc_id"] for x in body.get("skipped") or []] == [s.memo], r.text
        assert (await state(client, h, plain))["amount_paid"] == 30.0
        allowed = None

    if allowed is not None:
        assert allowed.status_code == 200 and not allowed.json().get("errors"), allowed.text
        before = await state(client, h, s.memo)
    await _unchanged_then_back(client, h, s, before)
