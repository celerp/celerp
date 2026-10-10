# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""What a credit note settles comes only from issuing it: `credited` is never entered,
an invoice cannot be voided or sent back to draft under an issued credit note, both
documents read paid or partial from what is settled, credit notes issued by an earlier
release are brought onto the same footing once, a credit note carries its invoice's
currency and rate, and a lot an invoice holds cannot be merged away.

Oracle (to the cent): 1120 == open invoice balances less open credit note balances."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select

from celerp.events.engine import emit_event
from celerp.models.ledger import LedgerEntry
from celerp.models.projections import Projection
from stock_books import assert_settled
from test_cost_follows_goods import _invoice
from test_cost_restatement import _state
from test_invoice_unshipped_books import _lot
from test_set_aside_older_paths import _held, _net

pytestmark = pytest.mark.asyncio


async def _post(client, auth, path, json=None):
    return await client.post(path, headers=auth["headers"], json=json if json is not None else {})


async def _ar(session, auth) -> tuple[float, float]:
    """(GL 1120 over every posted entry, open invoices less open credit notes)."""
    gl = await _net(session, auth, "1120", prefix="je:")
    rows = (await session.execute(select(Projection).where(
        Projection.company_id == auth["company_id"], Projection.entity_type == "doc"))).scalars().all()
    sub = 0.0
    for p in rows:
        s = p.state or {}
        if s.get("status") in (None, "draft", "void"):
            continue
        rate = float(s.get("conversion_rate") or 1)
        if s.get("doc_type") == "invoice":
            sub += float(s.get("amount_outstanding") or 0) * rate
        elif s.get("doc_type") == "credit_note":
            sub -= float(s.get("amount_outstanding") or 0) * rate
    return gl, round(sub, 2)


async def _tie(session, auth):
    gl, sub = await _ar(session, auth)
    assert gl == sub, f"1120 {gl} != open invoices less credit balances {sub}"


def _cn_body(invoice, total, **extra) -> dict:
    return {"doc_type": "credit_note", "original_doc_id": invoice, "ref_id": f"CN-{uuid.uuid4().hex[:6]}",
            "line_items": [{"name": "Credit", "quantity": 1, "unit_price": total, "line_total": total}],
            "total": total, **extra}


async def _cn(client, auth, invoice, total, *, finalize=True, **extra) -> str:
    r = await client.post("/docs", headers=auth["headers"], json=_cn_body(invoice, total, **extra))
    assert r.status_code == 200, r.text
    cn = r.json()["id"]
    if finalize:
        f = await _post(client, auth, f"/docs/{cn}/finalize")
        assert f.status_code == 200, f.text
    return cn


async def _svc_invoice(client, auth, total, **extra) -> str:
    r = await client.post("/docs", headers=auth["headers"], json={
        "doc_type": "invoice", "ref_id": f"INV-{uuid.uuid4().hex[:6]}",
        "line_items": [{"name": "Service", "quantity": 1, "unit_price": total, "line_total": total}],
        "total": total, **extra})
    assert r.status_code == 200, r.text
    doc = r.json()["id"]
    f = await _post(client, auth, f"/docs/{doc}/finalize")
    assert f.status_code == 200, f.text
    return doc


async def _pay(client, auth, doc, amount, **extra):
    return await _post(client, auth, f"/docs/{doc}/payment", {
        "amount": amount, "method": "cash", "payment_date": "2026-10-09", "bank_account": "1111", **extra})


async def _status(session, auth, doc) -> tuple:
    s = await _state(session, auth, doc)
    return s.get("status"), float(s.get("amount_outstanding") or 0), float(s.get("credited") or 0)


# 1. Settlement fields are never entered


async def test_credited_is_refused_when_a_document_is_made(client, session, auth):
    r = await client.post("/docs", headers=auth["headers"], json={
        "doc_type": "invoice", "ref_id": f"INV-{uuid.uuid4().hex[:6]}",
        "line_items": [{"name": "Service", "quantity": 1, "unit_price": 80.0, "line_total": 80.0}],
        "total": 80.0, "credited": 70.0})
    assert r.status_code == 422, r.text
    assert "credit note" in r.text.lower(), r.text


async def _import(client, auth, eid, data):
    r = await client.post("/docs/import", headers=auth["headers"], json={
        "entity_id": eid, "event_type": "doc.created", "source": "csv", "idempotency_key": uuid.uuid4().hex,
        "data": data})
    assert r.status_code == 200, r.text


@pytest.mark.parametrize("credit_note_first", [False, True])
async def test_import_takes_credited_from_the_imported_credit_notes(client, session, auth, credit_note_first):
    """An exported invoice of 80 a credit note of 30 settled, with 20 paid: the snapshot's
    own `credited` (here a wrong 70) is ignored, and what the credit note settled is
    recomputed from the two snapshots whichever comes in first."""
    tag = uuid.uuid4().hex[:6]
    inv, cn = f"doc:INV-I{tag}", f"doc:CN-I{tag}"
    line = [{"name": "Service", "quantity": 1, "unit_price": 80.0, "line_total": 80.0}]
    invoice = {"doc_type": "invoice", "ref_id": inv[4:], "status": "partial", "total": 80.0, "amount_paid": 20.0,
               "amount_outstanding": 30.0, "credited": 70.0, "line_items": line}
    note = {"doc_type": "credit_note", "ref_id": cn[4:], "status": "paid", "original_doc_id": inv, "total": 30.0,
            "amount_paid": 0.0, "amount_outstanding": 0.0, "credited": 70.0,
            "line_items": [{"name": "Credit", "quantity": 1, "unit_price": 30.0, "line_total": 30.0}]}
    for eid, data in ((cn, note), (inv, invoice)) if credit_note_first else ((inv, invoice), (cn, note)):
        await _import(client, auth, eid, data)
    assert (await _state(session, auth, inv)).get("credited") == 30.0
    assert (await _state(session, auth, cn)).get("credited") == 30.0
    p = await _pay(client, auth, inv, 30.0)
    assert p.status_code == 200, p.text
    assert await _status(session, auth, inv) == ("paid", 0.0, 30.0)
    over = await _pay(client, auth, inv, 1.0)
    assert over.status_code != 200, over.text


# 2. An invoice under an issued credit note is not undone


@pytest.mark.parametrize("action", ["void", "revert-to-draft"])
async def test_an_invoice_with_an_issued_credit_note_is_undone_only_after_it(client, session, auth, action):
    inv = await _svc_invoice(client, auth, 80.0)
    cn = await _cn(client, auth, inv, 40.0)
    gl = await _net(session, auth, "1120", prefix="je:")
    r = await _post(client, auth, f"/docs/{inv}/{action}")
    assert r.status_code == 409, r.text
    assert "void the credit note first" in r.text.lower(), r.text
    assert await _net(session, auth, "1120", prefix="je:") == gl
    assert (await _post(client, auth, f"/docs/{cn}/void")).status_code == 200
    r = await _post(client, auth, f"/docs/{inv}/{action}")
    assert r.status_code == 200, r.text
    await _tie(session, auth)
    assert await _net(session, auth, "1120", prefix="je:") == 0.0


# 3. Status follows what is settled


async def test_status_reads_what_the_credit_note_settled(client, session, auth):
    inv = await _svc_invoice(client, auth, 80.0)
    cn = await _cn(client, auth, inv, 40.0)
    assert await _status(session, auth, inv) == ("partial", 40.0, 40.0)
    assert await _status(session, auth, cn) == ("paid", 0.0, 40.0)
    assert (await _pay(client, auth, inv, 40.0)).status_code == 200
    assert await _status(session, auth, inv) == ("paid", 0.0, 40.0)
    # A credit note settled in full by its credit carries no payment: it can still be voided.
    v = await _post(client, auth, f"/docs/{cn}/void")
    assert v.status_code == 200, v.text
    assert await _status(session, auth, cn) == ("void", 40.0, 0.0)
    assert (await _status(session, auth, inv))[:2] == ("partial", 40.0)
    await _tie(session, auth)


async def test_a_credit_note_settled_by_its_credit_alone_goes_back_to_draft(client, session, auth):
    inv = await _svc_invoice(client, auth, 80.0)
    cn = await _cn(client, auth, inv, 80.0)
    assert await _status(session, auth, inv) == ("paid", 0.0, 80.0)
    assert (await _status(session, auth, cn))[0] == "paid"
    r = await _post(client, auth, f"/docs/{cn}/revert-to-draft")
    assert r.status_code == 200, r.text
    assert (await _status(session, auth, cn))[0] == "draft"
    assert await _status(session, auth, inv) == ("final", 80.0, 0.0)
    await _tie(session, auth)


# 4. Credit notes issued by an earlier release


async def _legacy_cn(client, session, auth, inv, amount, *, issued=True, **extra) -> str:
    """A credit note as an earlier release stored it: it reduced the invoice's balance
    when it was made (no `credited`), and issuing it posted nothing."""
    cn = await _cn(client, auth, inv, amount, finalize=False, **extra)
    state = await _state(session, auth, inv)
    old = float(state.get("amount_outstanding"))
    await emit_event(session, company_id=auth["company_id"], entity_id=inv, entity_type="doc",
                     event_type="doc.updated",
                     data={"fields_changed": {"amount_outstanding": {"old": old, "new": max(0.0, old - amount)}}},
                     actor_id=auth["user_id"], location_id=None, source="api",
                     idempotency_key=f"legacy-{uuid.uuid4().hex}:credit-note-original",
                     metadata_={"source_credit_note": cn})
    if issued:
        await emit_event(session, company_id=auth["company_id"], entity_id=cn, entity_type="doc",
                         event_type="doc.finalized", data={}, actor_id=auth["user_id"], location_id=None,
                         source="api", idempotency_key=f"legacy-{uuid.uuid4().hex}:finalize")
    await session.commit()
    return cn


async def _backfill(session, auth) -> dict:
    from celerp_docs.legacy_credit_notes import settle_legacy_credit_notes

    out = await settle_legacy_credit_notes(session, company_id=auth["company_id"])
    await session.commit()
    return out


async def _events(session, auth) -> int:
    return (await session.execute(select(func.count()).select_from(LedgerEntry).where(
        LedgerEntry.company_id == auth["company_id"]))).scalar_one()


async def test_an_earlier_credit_note_is_settled_once_and_runs_twice_the_same(client, session, auth):
    inv = await _svc_invoice(client, auth, 80.0)
    cn = await _legacy_cn(client, session, auth, inv, 40.0)
    first = await _backfill(session, auth)
    assert first["settled"] == 1, first
    assert await _status(session, auth, inv) == ("partial", 40.0, 40.0)
    assert await _status(session, auth, cn) == ("paid", 0.0, 40.0)
    assert (await _state(session, auth, inv))["total"] == 80.0
    assert await _net(session, auth, "4100", prefix="je:") == -40.0
    await _tie(session, auth)
    before = await _events(session, auth)
    again = await _backfill(session, auth)
    assert again["settled"] == 0, again
    assert await _events(session, auth) == before
    r = await _post(client, auth, f"/docs/{cn}/apply-to-invoice", {"target_doc_id": inv, "amount": 40.0})
    assert r.status_code != 200, "one credit note spent twice: " + r.text


async def test_an_earlier_credit_note_cannot_be_refunded_after_its_invoice_is_paid(client, session, auth):
    inv = await _svc_invoice(client, auth, 80.0)
    cn = await _legacy_cn(client, session, auth, inv, 40.0)
    await _backfill(session, auth)
    assert (await _pay(client, auth, inv, 40.0)).status_code == 200
    r = await _post(client, auth, f"/docs/{cn}/cn-refund", {"amount": 40.0, "date": "2026-10-09", "bank_account": "1111"})
    assert r.status_code != 200, "credit paid out in cash after it reduced the invoice: " + r.text
    await _tie(session, auth)


async def test_paying_an_invoice_an_earlier_credit_note_reduced_owes_what_is_left(client, session, auth):
    inv = await _svc_invoice(client, auth, 80.0)
    await _legacy_cn(client, session, auth, inv, 40.0)
    await _backfill(session, auth)
    p = await _pay(client, auth, inv, 40.0)
    assert p.status_code == 200, p.text
    assert await _status(session, auth, inv) == ("paid", 0.0, 40.0)
    await _tie(session, auth)


async def test_an_earlier_credit_note_in_a_locked_period_posts_on_the_business_date(client, session, auth):
    """The locked period is never changed and the entry is never backdated to the day
    after the lock: it posts on the company's business date today."""
    from celerp.models.company import Company
    from celerp.services.business_time import business_date_of

    inv = await _svc_invoice(client, auth, 80.0, issue_date="2026-01-10")
    cn = await _legacy_cn(client, session, auth, inv, 30.0, issue_date="2026-01-15")
    r = await _post(client, auth, "/accounting/period-lock", {"lock_date": "2026-03-31"})
    assert r.status_code == 200, r.text
    await _backfill(session, auth)
    je = await _state(session, auth, f"je:auto:{cn}:fin")
    number = (await _state(session, auth, cn)).get("ref_id")
    company = await session.get(Company, auth["company_id"])
    today = business_date_of(None, (company.settings or {}).get("timezone"))
    assert str(je.get("ts"))[:10] == today != "2026-04-01", je
    assert number in je.get("memo", ""), je
    await _tie(session, auth)


async def test_an_earlier_credit_note_already_posted_is_not_posted_again(client, session, auth):
    inv = await _svc_invoice(client, auth, 80.0)
    cn = await _legacy_cn(client, session, auth, inv, 40.0, issued=False)
    from celerp.services import auto_je

    await auto_je.create_for_credit_note_finalized(
        session, company_id=auth["company_id"], user_id=auth["user_id"], doc_id=cn,
        doc=await _state(session, auth, cn))
    await emit_event(session, company_id=auth["company_id"], entity_id=cn, entity_type="doc",
                     event_type="doc.finalized", data={}, actor_id=auth["user_id"], location_id=None,
                     source="api", idempotency_key=f"legacy-{uuid.uuid4().hex}:finalize")
    await session.commit()
    await _backfill(session, auth)
    assert await _net(session, auth, "4100", prefix="je:") == -40.0
    assert await _status(session, auth, cn) == ("paid", 0.0, 40.0)
    await _tie(session, auth)


async def test_an_earlier_draft_credit_note_gives_its_reduction_back(client, session, auth):
    inv = await _svc_invoice(client, auth, 80.0)
    cn = await _legacy_cn(client, session, auth, inv, 40.0, issued=False)
    await _backfill(session, auth)
    assert await _status(session, auth, inv) == ("final", 80.0, 0.0)
    await _tie(session, auth)
    assert (await _post(client, auth, f"/docs/{cn}/finalize")).status_code == 200
    assert await _status(session, auth, inv) == ("partial", 40.0, 40.0)
    await _tie(session, auth)


# 5 and 6. A credit note carries its invoice's currency and rate


async def test_a_credit_note_takes_its_invoices_currency_and_rate(client, session, auth):
    inv = await _svc_invoice(client, auth, 100.0, currency="EUR", conversion_rate=1.1)
    cn = await _cn(client, auth, inv, 50.0, finalize=False)
    state = await _state(session, auth, cn)
    assert (state.get("currency"), float(state.get("conversion_rate"))) == ("EUR", 1.1), state


@pytest.mark.parametrize("extra", [{"currency": "USD"}, {"currency": "EUR", "conversion_rate": 1.3},
                                   {"conversion_rate": 1.3}])
async def test_a_credit_note_in_another_currency_or_rate_is_refused(client, session, auth, extra):
    inv = await _svc_invoice(client, auth, 100.0, currency="EUR", conversion_rate=1.1)
    r = await client.post("/docs", headers=auth["headers"], json=_cn_body(inv, 50.0, **extra))
    assert r.status_code == 422, r.text
    assert "EUR" in r.text and "1.1" in r.text, r.text


# 7. A held lot is not merged away


async def test_merging_a_lot_an_invoice_holds_is_refused(client, session, auth):
    sku = f"D6M-{uuid.uuid4().hex[:4]}"
    a = await _lot(client, auth, sku, 3, 30.0)
    b = await _lot(client, auth, sku, 2, 20.0)
    inv = await _invoice(client, auth, [(a, sku, 3)])
    body = {"source_entity_ids": [a, b], "target_sku_from": a}
    p = await client.post("/items/merge/preview", headers=auth["headers"], json=body)
    r = p if p.status_code != 200 else await client.post("/items/merge", headers=auth["headers"], json={
        **body, "plan_fingerprint": p.json()["plan_fingerprint"], "idempotency_key": uuid.uuid4().hex})
    assert r.status_code == 409, r.text
    assert "cannot leave stock" in r.text, r.text
    await session.rollback()  # production never commits a refused request; the test client shares the session
    assert (await _state(session, auth, a)).get("status") == "available"
    assert sum((await _held(session, auth, a)).values()) == 3.0
    s = await client.post(f"/docs/{inv}/fulfill-lines", headers=auth["headers"], json={"line_entity_ids": [a]})
    assert s.status_code == 200, s.text
    await assert_settled(client, session, auth)
