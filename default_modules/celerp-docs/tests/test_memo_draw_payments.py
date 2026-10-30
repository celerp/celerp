# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Payments and Invoice all on a memo with goods out: money paid on the memo is settled on the
memo before any of its goods move to an invoice, a payment never lands on a memo whose goods
sit on a live invoice, and Invoice all is one draw of everything that leaves the memo live."""
from __future__ import annotations

import uuid

import pytest

from memo_draw_support import (
    auth, draw, drawn, key_of, memo_out, ok, owner, pay, state,
)


async def _invoice_all(client, h, memo: str, key: str | None = None):
    return await client.post(f"/docs/{memo}/convert", headers=h, json={"idempotency_key": key or uuid.uuid4().hex})


async def _rate(client, h, memo: str, rate: float = 7.0, *, dry_run: bool = False):
    v = (await state(client, h, memo))["version"]
    return await client.post(f"/docs/{memo}/tax-rate", headers=h,
                             json={"tax_rate": rate, "expected_version": v, "dry_run": dry_run})


async def _void_payments(client, h, memo: str) -> None:
    for p in (await state(client, h, memo)).get("payments") or []:
        if p.get("status", "active") == "active" and p.get("amount", 0) > 0:
            await ok(await client.post(f"/docs/{memo}/void-payment", headers=h, json={"payment_index": p["index"]}))


async def _snapshot(client, h, memo: str, lots: list[str]) -> tuple:
    m = await state(client, h, memo)
    return m["version"], m["status"], [await owner(client, h, i) for i in lots]


# ── A payment on the memo comes first ─────────────────────────────────────────

async def test_draw_refused_while_memo_holds_payment(client, session):
    h = await auth(session)
    memo, lids, lots = await memo_out(client, h, [(2, 300.0)], lots_per_line=2)
    await ok(await pay(client, h, memo, 50.0))
    before = await _snapshot(client, h, memo, lots[0])

    for r in (await draw(client, h, memo, {lids[0]: 1}), await _invoice_all(client, h, memo)):
        assert r.status_code == 409, r.text
        assert key_of(r) == "docs.draw_memo_has_payment"
        text = r.json()["detail"]["message"]
        assert "50.00" in text and "50.00 on 2026-03-02" in text
    assert await _snapshot(client, h, memo, lots[0]) == before

    await _void_payments(client, h, memo)
    inv = await drawn(client, h, memo, {lids[0]: 1})
    assert await owner(client, h, lots[0][0]) == ("memo_out", inv) or await owner(client, h, lots[0][1]) == ("memo_out", inv)

    # Paid in full with an unrated tax amount left: the payment is the primary, never the status.
    paid, plids, plots = await memo_out(client, h, [(2, 300.0)], lots_per_line=2, tax=35.0)
    total = (await state(client, h, paid))["total"]
    await ok(await pay(client, h, paid, total))
    assert (await state(client, h, paid))["status"] == "paid"
    for r in (await draw(client, h, paid, {plids[0]: 1}), await _invoice_all(client, h, paid)):
        assert r.status_code == 409, r.text
        assert key_of(r) == "docs.draw_memo_has_payment"
    await _void_payments(client, h, paid)
    assert (await state(client, h, paid))["status"] == "final"
    await drawn(client, h, paid, line_ids=plids)


async def _to_final(client, h, memo):
    return None


async def _to_partial_deposit(client, h, memo):
    await ok(await pay(client, h, memo, 50.0))


async def _to_partial_refund(client, h, memo):
    total = (await state(client, h, memo))["total"]
    await ok(await pay(client, h, memo, total))
    p = (await state(client, h, memo))["payments"][0]
    await ok(await client.post(f"/docs/{memo}/refund", headers=h, json={
        "payment_index": p["index"], "amount": 100.0, "payment_date": "2026-03-03"}))


async def _to_partial_from_sent(client, h, memo):
    await ok(await client.post(f"/docs/{memo}/send", headers=h, json={}))
    await ok(await pay(client, h, memo, 50.0))


async def _to_paid(client, h, memo):
    await ok(await pay(client, h, memo, (await state(client, h, memo))["total"]))


async def _to_final_after_voids(client, h, memo):
    await ok(await pay(client, h, memo, 50.0))
    await _void_payments(client, h, memo)


# Every status the payment writers give a memo with goods out (P2 item 1c, status universe).
# A status a payment writer can reach that is missing here is a gap in this table.
_PAYMENT_STATES = [
    ("final", _to_final, False),
    ("partial", _to_partial_deposit, True),
    ("partial", _to_partial_refund, True),
    ("partial", _to_partial_from_sent, True),
    ("paid", _to_paid, True),
    ("final", _to_final_after_voids, False),
]


@pytest.mark.parametrize(("status", "reach", "held"), _PAYMENT_STATES,
                         ids=["final", "deposit", "refund", "from_sent", "paid", "voided"])
async def test_payment_refusal_primary_for_every_payment_status(client, session, status, reach, held):
    h = await auth(session)
    memo, lids, lots = await memo_out(client, h, [(2, 300.0)], lots_per_line=2, tax=35.0)
    await reach(client, h, memo)
    assert (await state(client, h, memo))["status"] == status

    if not held:
        r = await _rate(client, h, memo, dry_run=True)
        assert r.status_code == 200, r.text
        await drawn(client, h, memo, line_ids=lids)
        return

    before = await _snapshot(client, h, memo, lots[0])
    for r in (await draw(client, h, memo, {lids[0]: 1}), await _invoice_all(client, h, memo)):
        assert r.status_code == 409, r.text
        assert key_of(r) == "docs.draw_memo_has_payment"
    for dry in (True, False):
        r = await _rate(client, h, memo, dry_run=dry)
        assert r.status_code == 409, r.text
        assert key_of(r) == "docs.memo_rate_paid"
    assert await _snapshot(client, h, memo, lots[0]) == before


@pytest.mark.parametrize("status", ["final", "sent", "partial", "paid"])
def test_payment_refusal_primary_for_every_payment_status_page(status):
    """The page half of the class guard: both memo buttons on every status of the parameter set."""
    from fasthtml.common import to_xml
    from ui.routes.documents import _doc_detail
    doc = {"entity_id": "doc:memo-1", "doc_type": "memo", "status": status, "ref_id": "M-1",
           "amount_paid": 0 if status in ("final", "sent") else 50,
           "line_items": [{"line_id": "11111111-1111-4111-8111-111111111111", "sku": "S-1", "item_id": "item:1",
                           "entity_id": "item:1", "quantity": 1, "sell_by": "piece", "unit_price": 100}]}
    html = to_xml(_doc_detail(doc, item_status_map={"item:1": "memo_out"}))
    assert '/docs/doc:memo-1/convert"' in html
    assert "invoice-lines" in html


async def test_memo_payment_refused_while_drawn(client, session):
    h = await auth(session)
    memo, lids, _ = await memo_out(client, h, [(4, 100.0)], lots_per_line=4)
    await drawn(client, h, memo, {lids[0]: 2})
    v = (await state(client, h, memo))["version"]

    r = await pay(client, h, memo, 50.0)
    assert r.status_code == 409, r.text
    assert key_of(r) == "docs.memo_payment_while_drawn"
    r = await client.post("/docs/bulk-payment", headers=h, json={
        "doc_ids": [memo], "amount": 50.0, "payment_date": "2026-03-02", "bank_account": "1111"})
    assert r.status_code == 409, r.text
    assert key_of(r) == "docs.memo_payment_while_drawn"
    m = await state(client, h, memo)
    assert (m["version"], m.get("amount_paid") or 0) == (v, 0)


# ── Invoice all is one draw of everything ─────────────────────────────────────

async def test_invoice_all_retry_after_success_returns_first_target(client, session):
    h = await auth(session)
    memo, _, lots = await memo_out(client, h, [(2, 10.0)], lots_per_line=2)
    key = uuid.uuid4().hex

    first = await ok(await _invoice_all(client, h, memo, key))
    assert (await state(client, h, memo))["status"] == "final"
    again = await ok(await _invoice_all(client, h, memo, key))

    assert again["target_doc_id"] == first["target_doc_id"]
    assert {(await owner(client, h, i))[1] for i in lots[0]} == {first["target_doc_id"]}
    invoices = (await ok(await client.get("/docs", headers=h, params={"doc_type": "invoice"})))
    rows = invoices["items"] if isinstance(invoices, dict) else invoices
    assert len(rows) == 1


async def test_invoice_all_same_key_other_memo_refused(client, session):
    h = await auth(session)
    a, _, _ = await memo_out(client, h)
    b, _, lots_b = await memo_out(client, h)
    key = uuid.uuid4().hex
    await ok(await _invoice_all(client, h, a, key))

    r = await _invoice_all(client, h, b, key)
    assert r.status_code == 409, r.text
    assert (await state(client, h, b))["status"] == "final"
    assert await owner(client, h, lots_b[0][0]) == ("memo_out", b)


async def test_invoice_all_leaves_memo_live_and_closable(client, session):
    h = await auth(session)
    memo, _, lots = await memo_out(client, h, [(3, 10.0)], lots_per_line=3)

    inv = (await ok(await _invoice_all(client, h, memo)))["target_doc_id"]

    assert (await state(client, h, memo))["status"] == "final"
    assert {(await owner(client, h, i))[1] for i in lots[0]} == {inv}
    await ok(await client.post(f"/docs/{memo}/close", headers=h, json={}))
    assert (await state(client, h, memo))["status"] == "closed"
