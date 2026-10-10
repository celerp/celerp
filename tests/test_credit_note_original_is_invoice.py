"""A credit note credits an issued invoice of its own company, and nothing else.

Initial conditions per test: a fresh company with the seeded chart; documents are made
through the API (finalized service invoices and bills, credit notes against them) or, for
a second company, through POST /companies with the same user. A credit note whose original
document is a bill, a draft invoice, or an id that names nothing in this company is refused
with a message key on every writer (create, edit, finalize, single import, batch import) and
writes nothing. A bill owes its total less its payments and supplier returns: an older
release's ``credited`` on a bill takes nothing off it, so a supplier return reduces the
bill once.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select

from celerp.events.engine import emit_event
from celerp.models.ledger import LedgerEntry
from test_cost_restatement import _state
from test_credit_note_settlement_owned import _cn, _cn_body, _pay, _post, _svc_invoice
from test_set_aside_older_paths import _net
from test_void_once import _svc_bill

pytestmark = pytest.mark.asyncio

_CODES = ("1120", "2110", "4100")


async def _books(session, auth) -> dict:
    return {c: await _net(session, auth, c, prefix="je:") for c in _CODES}


async def _events(session, auth) -> int:
    session.expire_all()
    return (await session.execute(select(func.count()).select_from(LedgerEntry).where(
        LedgerEntry.company_id == auth["company_id"]))).scalar_one()


async def _draft_invoice(client, auth, total=50.0) -> str:
    r = await client.post("/docs", headers=auth["headers"], json={
        "doc_type": "invoice", "ref_id": f"INV-{uuid.uuid4().hex[:6]}", "total": total,
        "line_items": [{"name": "Service", "quantity": 1, "unit_price": total, "line_total": total}]})
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _foreign_invoice(client, auth) -> tuple[str, dict]:
    """An issued invoice in a second company the same user owns; its id names nothing here."""
    created = await client.post("/companies", headers=auth["headers"], json={"name": f"Other {uuid.uuid4().hex[:6]}"})
    assert created.status_code == 200, created.text
    other = {"headers": {"Authorization": f"Bearer {created.json()['access_token']}"}}
    return await _svc_invoice(client, other, 50.0), other


def _imported_cn(original: str, total: float = 50.0) -> dict:
    eid = f"doc:CNI-{uuid.uuid4().hex[:8]}"
    return {"entity_id": eid, "event_type": "doc.created", "source": "csv", "idempotency_key": uuid.uuid4().hex,
            "data": {"doc_type": "credit_note", "status": "final", "original_doc_id": original, "total": total,
                     "amount_paid": 0.0, "amount_outstanding": 0.0, "issue_date": "2026-10-01",
                     "line_items": [{"name": "Credit", "quantity": 1, "unit_price": total, "line_total": total}]}}


def _key(r) -> str:
    d = r.json().get("detail")
    return str(d.get("message_key") or "") if isinstance(d, dict) else ""


async def _targets(client, auth) -> dict:
    """original id -> the message key refusing it."""
    foreign, _ = await _foreign_invoice(client, auth)
    return {await _svc_bill(client, auth, 77.0): "credit_note.original_not_invoice",
            await _draft_invoice(client, auth): "credit_note.original_not_issued",
            foreign: "credit_note.original_missing"}


async def test_create_refuses_a_credit_note_that_does_not_credit_an_issued_invoice(client, session, auth):
    for original, key in (await _targets(client, auth)).items():
        before, n = await _books(session, auth), await _events(session, auth)
        r = await client.post("/docs", headers=auth["headers"], json=_cn_body(original, 28.0))
        assert r.status_code == 422, r.text
        assert _key(r) == key, r.text
        assert await _events(session, auth) == n
        assert await _books(session, auth) == before


async def test_an_edit_cannot_point_a_credit_note_at_anything_but_an_issued_invoice(client, session, auth):
    inv = await _svc_invoice(client, auth, 100.0)
    cn = await _cn(client, auth, inv, 30.0, finalize=False)
    for original, key in (await _targets(client, auth)).items():
        r = await client.patch(f"/docs/{cn}", headers=auth["headers"], json={
            "fields_changed": {"original_doc_id": {"old": inv, "new": original}}})
        assert r.status_code == 422, r.text
        assert _key(r) == key, r.text
    assert (await _state(session, auth, cn)).get("original_doc_id") == inv


async def test_finalize_refuses_an_older_credit_note_naming_a_bill(client, session, auth):
    """An older release created a credit note naming a bill. Issuing it is refused: the
    bill keeps what it owes and the books do not move."""
    bill = await _svc_bill(client, auth, 77.0)
    cn = f"doc:CN-{uuid.uuid4().hex[:6]}"
    body = _cn_body(bill, 28.0)
    await emit_event(session, company_id=auth["company_id"], entity_id=cn, entity_type="doc",
                     event_type="doc.created", data={**body, "status": "draft"}, actor_id=auth["user_id"],
                     location_id=None, source="api", idempotency_key=f"older-cn-{uuid.uuid4().hex}")
    await session.commit()
    before = await _books(session, auth)
    r = await _post(client, auth, f"/docs/{cn}/finalize")
    assert r.status_code == 422, r.text
    assert _key(r) == "credit_note.original_not_invoice", r.text
    s = await _state(session, auth, bill)
    assert (float(s.get("amount_outstanding") or 0), float(s.get("credited") or 0)) == (77.0, 0.0)
    assert await _books(session, auth) == before
    assert (await _state(session, auth, cn)).get("status") == "draft"


async def test_the_import_refuses_a_credit_note_whose_original_is_not_an_issued_invoice_here(client, session, auth):
    """L3-07: an id naming another company's invoice does not resolve here, so the credit
    note is refused rather than booked as used on an invoice these books do not hold."""
    targets = await _targets(client, auth)
    for original, key in targets.items():
        rec = _imported_cn(original)
        before = await _books(session, auth)
        r = await client.post("/docs/import", headers=auth["headers"], json=rec)
        assert r.status_code == 422, r.text
        assert _key(r) == key, r.text
        assert await _state(session, auth, rec["entity_id"]) == {}
        assert await _books(session, auth) == before


async def test_the_batch_import_refuses_each_such_credit_note_and_keeps_the_rest(client, session, auth):
    targets = await _targets(client, auth)
    inv = await _svc_invoice(client, auth, 100.0)
    bad = [_imported_cn(o) for o in targets]
    good = _imported_cn(inv, 40.0)
    r = await client.post("/docs/import/batch", headers=auth["headers"], json={"records": [*bad, good]})
    assert r.status_code == 200, r.text
    assert r.json()["created"] == 1 and len(r.json()["errors"]) == len(bad), r.text
    for rec in bad:
        assert await _state(session, auth, rec["entity_id"]) == {}
    assert (await _state(session, auth, good["entity_id"])).get("status") == "final"


async def test_the_foreign_company_is_untouched_by_a_refused_reference(client, session, auth):
    foreign, other = await _foreign_invoice(client, auth)
    r = await client.post("/docs/import", headers=auth["headers"], json=_imported_cn(foreign))
    assert r.status_code == 422, r.text
    seen = await client.get(f"/docs/{foreign}", headers=other["headers"])
    assert seen.status_code == 200, seen.text
    assert (float(seen.json().get("credited") or 0), seen.json().get("status")) == (0.0, "final")


async def test_an_older_credited_on_a_bill_takes_nothing_off_it(client, session, auth):
    """An older release recorded a credit note's settlement on a bill (``credited`` 28.00,
    outstanding 49.00). A bill owes its total less its payments and supplier returns, so the
    next payment reads it as owing 77.00 less that payment."""
    bill = await _svc_bill(client, auth, 77.0)
    status = (await _state(session, auth, bill)).get("status")
    await emit_event(session, company_id=auth["company_id"], entity_id=bill, entity_type="doc",
                     event_type="doc.updated",
                     data={"fields_changed": {"credited": {"old": None, "new": 28.0},
                                              "amount_outstanding": {"old": 77.0, "new": 49.0}}},
                     actor_id=auth["user_id"], location_id=None, source="api",
                     idempotency_key=f"older-credit-{uuid.uuid4().hex}")
    await session.commit()
    s = await _state(session, auth, bill)
    assert (s.get("status"), float(s.get("amount_outstanding") or 0)) == (status, 77.0)
    assert (await _pay(client, auth, bill, 10.0)).status_code == 200
    assert float((await _state(session, auth, bill)).get("amount_outstanding") or 0) == 67.0


# Neighbouring rules: a credit note on an issued invoice of this company is still created,
# edited, issued and imported, and settles that invoice once; a void invoice still closes
# an imported credit note (EF-11), and a plain bill payment is untouched.


async def test_a_credit_note_on_an_issued_invoice_still_settles_it_once(client, session, auth):
    inv = await _svc_invoice(client, auth, 100.0)
    other = await _svc_invoice(client, auth, 80.0)
    cn = await _cn(client, auth, inv, 30.0, finalize=False)
    r = await client.patch(f"/docs/{cn}", headers=auth["headers"], json={
        "fields_changed": {"original_doc_id": {"old": inv, "new": other}}})
    assert r.status_code == 200, r.text
    assert (await _post(client, auth, f"/docs/{cn}/finalize")).status_code == 200
    s = await _state(session, auth, other)
    assert (float(s.get("amount_outstanding") or 0), float(s.get("credited") or 0)) == (50.0, 30.0)
    assert float((await _state(session, auth, inv)).get("credited") or 0) == 0.0


async def test_an_imported_credit_note_on_an_issued_invoice_still_imports(client, session, auth):
    """The invoice's own figures did not take the credit off, so the credit stays open on
    the credit note as the customer's credit (settle_imported_credit)."""
    inv = await _svc_invoice(client, auth, 100.0)
    rec = _imported_cn(inv, 40.0)
    rec["data"]["amount_outstanding"] = 40.0
    r = await client.post("/docs/import", headers=auth["headers"], json=rec)
    assert r.status_code == 200, r.text
    s = await _state(session, auth, rec["entity_id"])
    assert (s.get("status"), float(s.get("amount_outstanding") or 0)) == ("final", 40.0)
    assert float((await _state(session, auth, inv)).get("amount_outstanding") or 0) == 100.0


async def test_a_plain_bill_payment_still_settles_it(client, session, auth):
    bill = await _svc_bill(client, auth, 77.0)
    assert (await _pay(client, auth, bill, 77.0)).status_code == 200
    s = await _state(session, auth, bill)
    assert (s.get("status"), float(s.get("amount_outstanding") or 0)) == ("paid", 0.0)
