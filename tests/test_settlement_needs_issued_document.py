"""A draft or void document never holds a payment, on any path.

Initial conditions per test: a fresh company with the seeded chart. The import (single
and batch) refuses a draft or void document that says it was paid, bulk payment refuses
a draft or void document by name, and the projection never reads a draft or void
document as paid, even from history an older release wrote.
"""
from __future__ import annotations

import uuid

import pytest

from celerp.events.engine import emit_event
from test_cost_restatement import _state
from test_credit_note_settlement_owned import _pay, _post, _svc_invoice
from test_set_aside_older_paths import _net

pytestmark = pytest.mark.asyncio


def _record(status: str, paid: float, doc_type: str = "invoice") -> dict:
    eid = f"doc:IMP-{uuid.uuid4().hex[:8]}"
    return {"entity_id": eid, "event_type": "doc.created", "source": "csv", "idempotency_key": uuid.uuid4().hex,
            "data": {"doc_type": doc_type, "status": status, "total": 100.0, "amount_paid": paid,
                     "amount_outstanding": 100.0 - paid,
                     "line_items": [{"name": "Service", "quantity": 1, "unit_price": 100.0, "line_total": 100.0}]}}


async def _books(session, auth) -> dict:
    return {c: await _net(session, auth, c, prefix="je:") for c in ("1111", "1120", "2110")}


async def _draft(client, auth, total=50.0) -> str:
    r = await client.post("/docs", headers=auth["headers"], json={
        "doc_type": "invoice", "ref_id": f"INV-{uuid.uuid4().hex[:6]}", "total": total,
        "line_items": [{"name": "Service", "quantity": 1, "unit_price": total, "line_total": total}]})
    assert r.status_code == 200, r.text
    return r.json()["id"]


@pytest.mark.parametrize("doc_type", ["invoice", "bill"])
@pytest.mark.parametrize("status", ["draft", "void"])
async def test_the_import_refuses_a_draft_or_void_document_that_says_it_was_paid(client, session, auth, status, doc_type):
    rec = _record(status, 40.0, doc_type)
    r = await client.post("/docs/import", headers=auth["headers"], json=rec)
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["message_key"] == "doc_import.settlement_not_issued"
    assert await _state(session, auth, rec["entity_id"]) == {}
    assert await _books(session, auth) == {"1111": 0.0, "1120": 0.0, "2110": 0.0}


@pytest.mark.parametrize("status", ["draft", "void"])
async def test_the_batch_import_refuses_the_row_and_keeps_the_rest(client, session, auth, status):
    bad, good = _record(status, 40.0), _record("draft", 0.0)
    r = await client.post("/docs/import/batch", headers=auth["headers"], json={"records": [bad, good]})
    assert r.status_code == 200, r.text
    assert await _state(session, auth, bad["entity_id"]) == {}
    assert (await _state(session, auth, good["entity_id"])).get("status") == "draft"
    assert "holds no payment" in r.text


@pytest.mark.parametrize("status", ["draft", "void"])
async def test_bulk_payment_refuses_a_draft_or_void_invoice_by_name(client, session, auth, status):
    doc = await _draft(client, auth)
    if status == "void":
        doc = await _svc_invoice(client, auth, 60.0)
        assert (await _post(client, auth, f"/docs/{doc}/void")).status_code == 200
    before = await _state(session, auth, doc)
    r = await _post(client, auth, "/docs/bulk-payment", {"doc_ids": [doc], "amount": 10.0, "payment_date": "2026-10-09",
                                                         "method": "cash", "bank_account": "1111"})
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["message_key"] == "docs.payment_not_issued", r.text
    after = await _state(session, auth, doc)
    assert (after.get("status"), after.get("amount_paid")) == (before.get("status"), before.get("amount_paid"))
    assert (await _books(session, auth))["1111"] == 0.0


async def test_bulk_payment_names_the_draft_it_skipped_beside_one_it_paid(client, session, auth):
    draft = await _draft(client, auth)
    live = await _svc_invoice(client, auth, 30.0)
    r = await _post(client, auth, "/docs/bulk-payment", {"doc_ids": [draft, live], "amount": 30.0,
                                                         "payment_date": "2026-10-09", "method": "cash",
                                                         "bank_account": "1111"})
    assert r.status_code == 200, r.text
    assert [a["doc_id"] for a in r.json()["allocations"]] == [live]
    skipped = r.json()["skipped"]
    assert [s["doc_id"] for s in skipped] == [draft]
    assert skipped[0]["reason"]["message_key"] == "docs.payment_not_issued"
    assert (await _state(session, auth, draft)).get("status") == "draft"


async def test_a_single_payment_on_a_draft_is_refused_with_a_key(client, session, auth):
    doc = await _draft(client, auth)
    r = await _pay(client, auth, doc, 10.0)
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["message_key"] == "docs.payment_not_issued"


@pytest.mark.parametrize("status", ["draft", "void"])
async def test_history_paying_a_draft_or_void_document_never_reads_paid(client, session, auth, status):
    """An older release could record a payment on a document that was not issued. The
    projection keeps the payment on record, so it can still be voided, and keeps the
    document's own status."""
    doc = await _draft(client, auth)
    if status == "void":
        doc = await _svc_invoice(client, auth, 50.0)
        assert (await _post(client, auth, f"/docs/{doc}/void")).status_code == 200
    await emit_event(session, company_id=auth["company_id"], entity_id=doc, entity_type="doc",
                     event_type="doc.payment.received",
                     data={"amount": 50.0, "method": "cash", "payment_date": "2026-10-09", "bank_account": "1111"},
                     actor_id=auth["user_id"], location_id=None, source="api",
                     idempotency_key=f"older-pay-{uuid.uuid4().hex}")
    await session.commit()
    s = await _state(session, auth, doc)
    assert s.get("status") == status
    assert [p.get("status") for p in s.get("payments") or []] == ["active"]
    if status == "void":
        assert float(s.get("amount_outstanding") or 0) == 0.0


@pytest.mark.parametrize("status", ["draft", "void"])
async def test_history_importing_a_paid_draft_or_void_document_reads_it_unpaid(client, session, auth, status):
    """An older import stored a draft or void document with a paid amount. The projection
    takes no payment from it: no payment was ever recorded."""
    rec = _record(status, 40.0)
    await emit_event(session, company_id=auth["company_id"], entity_id=rec["entity_id"], entity_type="doc",
                     event_type="doc.created", data=rec["data"], actor_id=auth["user_id"], location_id=None,
                     source="csv", idempotency_key=rec["idempotency_key"])
    await session.commit()
    s = await _state(session, auth, rec["entity_id"])
    assert (s.get("status"), float(s.get("amount_paid") or 0)) == (status, 0.0)
    assert float(s.get("amount_outstanding") or 0) == (100.0 if status == "draft" else 0.0)


# Neighbouring rules: an issued document still imports with what was paid on it, and a
# finalized invoice still takes a payment by itself and in a bulk run.


async def test_an_issued_invoice_still_imports_paid(client, session, auth):
    rec = _record("final", 40.0)
    rec["data"]["status"] = "partial"
    r = await client.post("/docs/import", headers=auth["headers"], json=rec)
    assert r.status_code == 200, r.text
    s = await _state(session, auth, rec["entity_id"])
    assert (s.get("status"), float(s.get("amount_paid") or 0)) == ("partial", 40.0)


async def test_a_finalized_invoice_still_takes_payments(client, session, auth):
    one = await _svc_invoice(client, auth, 30.0)
    two = await _svc_invoice(client, auth, 20.0)
    assert (await _pay(client, auth, one, 30.0)).status_code == 200
    r = await _post(client, auth, "/docs/bulk-payment", {"doc_ids": [two], "amount": 20.0,
                                                         "payment_date": "2026-10-09", "method": "cash",
                                                         "bank_account": "1111"})
    assert r.status_code == 200 and r.json()["skipped"] == [], r.text
    assert (await _state(session, auth, one)).get("status") == "paid"
    assert (await _state(session, auth, two)).get("status") == "paid"
