"""A credit note whose invoice is void reads "closed", with what it released on record.

Initial conditions per test: a fresh company with the seeded chart; invoices are made
through the API and voided, credit notes are imported against them (single and batch) or
written the way an earlier release wrote them (_legacy_cn) and settled at start-up
(_backfill). Whatever such a credit note still had open is released, never refunded or
applied: it reads "closed" (nothing was paid), ``released`` holds the amount, and its
balance is 0. One derivation serves the import and the start-up settlement, a rebuild of
the projections gives the same state, and a closed credit note cannot be reopened as if it
were a memo.
"""
from __future__ import annotations

import uuid

import pytest

from celerp.projections.engine import ProjectionEngine
from test_cost_restatement import _state
from test_credit_note_settlement_owned import _backfill, _cn, _legacy_cn, _post, _svc_invoice
from test_legacy_credit_note_edges import _refund, _void_on_main

pytestmark = pytest.mark.asyncio


def _note(original: str, total: float, outstanding: float, status: str = "final") -> dict:
    eid = f"doc:CNV-{uuid.uuid4().hex[:8]}"
    return {"entity_id": eid, "event_type": "doc.created", "source": "csv", "idempotency_key": uuid.uuid4().hex,
            "data": {"doc_type": "credit_note", "status": status, "original_doc_id": original, "total": total,
                     "amount_outstanding": outstanding,
                     "line_items": [{"name": "Credit", "quantity": 1, "unit_price": total, "line_total": total}]}}


async def _void_invoice(client, auth, total=100.0) -> str:
    inv = await _svc_invoice(client, auth, total)
    assert (await _post(client, auth, f"/docs/{inv}/void")).status_code == 200
    return inv


def _shown(s: dict) -> tuple:
    return (s.get("status"), float(s.get("amount_outstanding") or 0), float(s.get("released") or 0),
            float(s.get("amount_paid") or 0))


async def test_an_imported_credit_note_on_a_void_invoice_is_closed_with_its_release(client, session, auth):
    inv = await _void_invoice(client, auth)
    rec = _note(inv, 20.0, 20.0)
    r = await client.post("/docs/import", headers=auth["headers"], json=rec)
    assert r.status_code == 200, r.text
    assert _shown(await _state(session, auth, rec["entity_id"])) == ("closed", 0.0, 20.0, 0.0)


async def test_the_batch_import_closes_it_the_same_way(client, session, auth):
    inv = await _void_invoice(client, auth)
    rec = _note(inv, 20.0, 20.0)
    r = await client.post("/docs/import/batch", headers=auth["headers"], json={"records": [rec]})
    assert r.status_code == 200 and r.json()["created"] == 1, r.text
    assert _shown(await _state(session, auth, rec["entity_id"])) == ("closed", 0.0, 20.0, 0.0)


async def test_an_earlier_credit_note_on_a_void_invoice_is_closed_at_start(client, session, auth):
    """Q04: 40.00 credit note, 10.00 refunded, invoice voided the way main voided it."""
    inv = await _svc_invoice(client, auth, 80.0)
    cn = await _legacy_cn(client, session, auth, inv, 40.0)
    assert (await _refund(client, auth, cn, 10.0)).status_code == 200
    await _void_on_main(session, auth, inv)
    await _backfill(session, auth)
    assert _shown(await _state(session, auth, cn)) == ("closed", 0.0, 30.0, 10.0)


async def test_a_rebuild_reads_the_closed_credit_note_the_same(client, session, auth):
    inv = await _void_invoice(client, auth)
    rec = _note(inv, 20.0, 20.0)
    assert (await client.post("/docs/import", headers=auth["headers"], json=rec)).status_code == 200
    before = _shown(await _state(session, auth, rec["entity_id"]))
    await ProjectionEngine.rebuild(session, auth["company_id"])
    await session.commit()
    session.expire_all()
    assert _shown(await _state(session, auth, rec["entity_id"])) == before == ("closed", 0.0, 20.0, 0.0)


async def test_a_closed_credit_note_cannot_be_reopened(client, session, auth):
    inv = await _void_invoice(client, auth)
    rec = _note(inv, 20.0, 20.0)
    assert (await client.post("/docs/import", headers=auth["headers"], json=rec)).status_code == 200
    r = await _post(client, auth, f"/docs/{rec['entity_id']}/reopen", {})
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["message_key"] == "docs.reopen_memo_only", r.text
    assert _shown(await _state(session, auth, rec["entity_id"]))[0] == "closed"


# Neighbouring rules: a credit note that paid out all it had keeps reading paid (nothing was
# released), one on a live invoice is settled as before, and a closed memo still reopens.


async def test_a_credit_note_that_paid_out_everything_stays_paid(client, session, auth):
    inv = await _void_invoice(client, auth)
    rec = _note(inv, 20.0, 0.0, "paid")
    rec["data"]["amount_paid"] = 20.0
    assert (await client.post("/docs/import", headers=auth["headers"], json=rec)).status_code == 200
    s = await _state(session, auth, rec["entity_id"])
    assert (s.get("status"), float(s.get("released") or 0)) == ("paid", 0.0)


async def test_a_credit_note_on_a_live_invoice_still_settles_it(client, session, auth):
    inv = await _svc_invoice(client, auth, 100.0)
    cn = await _cn(client, auth, inv, 30.0)
    s = await _state(session, auth, cn)
    assert (s.get("status"), float(s.get("credited") or 0), s.get("released")) == ("paid", 30.0, None)


async def test_a_closed_memo_still_reopens(client, session, auth):
    r = await client.post("/docs", headers=auth["headers"], json={
        "doc_type": "memo", "ref_id": f"M-{uuid.uuid4().hex[:6]}", "contact_id": "customer:1",
        "line_items": [{"name": "Service", "quantity": 1, "unit_price": 10.0, "line_total": 10.0}], "total": 10.0})
    assert r.status_code == 200, r.text
    memo = r.json()["id"]
    assert (await _post(client, auth, f"/docs/{memo}/finalize")).status_code == 200
    assert (await _post(client, auth, f"/docs/{memo}/close", {})).status_code == 200
    assert (await _post(client, auth, f"/docs/{memo}/reopen", {})).status_code == 200
    assert (await _state(session, auth, memo)).get("status") != "closed"
