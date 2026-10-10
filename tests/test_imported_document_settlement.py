# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Imported invoices and credit notes after the import: what was paid before it stays
against retained earnings (3200) and never against the bank, an imported figure that
cannot be true is refused, a credit note is in its invoice's currency and rate, and the
Doctor finds nothing to repair in what an import posted.

Oracle at every step: 1120 equals open invoices less open credit note balances; a full
reversal brings every leg back to where it started; running a step again changes nothing."""
from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from test_cost_restatement import _state
from test_credit_note_settlement_owned import _ar, _pay, _post
from test_set_aside_older_paths import _net

pytestmark = pytest.mark.asyncio

LOCALES = Path(__file__).resolve().parents[1] / "ui" / "locales"


async def _import(client, auth, eid, data, key=None):
    return await client.post("/docs/import", headers=auth["headers"], json={
        "entity_id": eid, "event_type": "doc.created", "source": "csv",
        "idempotency_key": key or uuid.uuid4().hex, "data": data})


async def _batch(client, auth, *rows):
    return await client.post("/docs/import/batch", headers=auth["headers"], json={"records": [
        {"entity_id": eid, "event_type": "doc.created", "source": "csv", "idempotency_key": uuid.uuid4().hex,
         "data": data} for eid, data in rows]})


def _line(total, name="Service"):
    return [{"name": name, "quantity": 1, "unit_price": total, "line_total": total}]


def _ids(prefix):
    tag = uuid.uuid4().hex[:6]
    return f"doc:INV-{prefix}{tag}", f"doc:CN-{prefix}{tag}"


def _inv(eid, total, paid, out, status, **x):
    return {"doc_type": "invoice", "ref_id": eid[4:], "status": status, "total": total, "amount_paid": paid,
            "amount_outstanding": out, "line_items": _line(total), "issue_date": "2026-09-01", **x}


def _note(eid, inv, total, paid, out, status, **x):
    return {"doc_type": "credit_note", "ref_id": eid[4:], "status": status, "original_doc_id": inv, "total": total,
            "amount_paid": paid, "amount_outstanding": out, "line_items": _line(total, "Credit"),
            "issue_date": "2026-09-02", **x}


def _cash(amount, index=0):
    return [{"index": index, "amount": amount, "method": "cash", "status": "active", "payment_date": "2026-09-05",
             "bank_account": "1111"}]


async def _books(session, auth) -> dict:
    """Every leg these flows touch, and the receivable tie, asserted on each read."""
    gl, sub = await _ar(session, auth)
    assert gl == sub, f"1120 {gl} != open invoices less open credit note balances {sub}"
    return {"1120": gl, **{a: await _net(session, auth, a, prefix="je:") for a in ("3200", "4100", "1111", "6960")}}


async def _doc(session, auth, doc) -> tuple:
    s = await _state(session, auth, doc)
    return s.get("status"), float(s.get("amount_outstanding") or 0), float(s.get("amount_paid") or 0)


def _locale_has(key: str) -> None:
    for path in sorted(LOCALES.glob("*.json")):
        assert key in json.loads(path.read_text(encoding="utf-8")), f"{path.name} has no {key}"


async def _doctor(client, auth) -> list:
    r = await client.post("/admin/doctor?fix=true", headers=auth["headers"])
    assert r.status_code == 200, r.text
    return [(x["check"], x["found"], x.get("details")) for x in r.json()["results"] if x.get("found")]


# N1: the Doctor finds nothing to repair in what an import posted


IMPORT_SHAPES = {
    "open credit note": lambda inv, cn: [(inv, _inv(inv, 100.0, 0.0, 100.0, "final")),
                                         (cn, _note(cn, inv, 40.0, 0.0, 40.0, "final"))],
    "paid with its payment": lambda inv, cn: [(inv, _inv(inv, 100.0, 100.0, 0.0, "paid", payments=_cash(100.0)))],
    "paid": lambda inv, cn: [(inv, _inv(inv, 100.0, 100.0, 0.0, "paid"))],
    "partly paid": lambda inv, cn: [(inv, _inv(inv, 100.0, 30.0, 70.0, "partial"))],
    "refunded credit note": lambda inv, cn: [(inv, _inv(inv, 100.0, 0.0, 100.0, "final")),
                                             (cn, _note(cn, inv, 40.0, 40.0, 0.0, "paid"))],
    "used credit note": lambda inv, cn: [(inv, _inv(inv, 100.0, 0.0, 60.0, "partial")),
                                         (cn, _note(cn, inv, 40.0, 0.0, 0.0, "paid"))],
}


@pytest.mark.parametrize("shape", sorted(IMPORT_SHAPES))
async def test_doctor_fix_changes_nothing_after_an_import(client, session, auth, shape):
    inv, cn = _ids("D")
    for eid, data in IMPORT_SHAPES[shape](inv, cn):
        assert (await _import(client, auth, eid, data)).status_code == 200
    before = await _books(session, auth)
    assert await _doctor(client, auth) == []
    assert await _books(session, auth) == before
    assert await _doctor(client, auth) == []
    assert await _books(session, auth) == before


async def test_doctor_still_voids_an_entry_nothing_caused(client, session, auth):
    """The neighbour the fix must keep: an imported credit note's entry is caused by its
    import, but a credit note entry with no finalize and no import is still voided."""
    from celerp.services import auto_je

    inv, cn = _ids("E")
    assert (await _import(client, auth, inv, _inv(inv, 100.0, 0.0, 100.0, "final"))).status_code == 200
    r = await client.post("/docs", headers=auth["headers"], json={
        "doc_type": "credit_note", "original_doc_id": inv, "ref_id": cn[4:], "line_items": _line(40.0, "Credit"),
        "total": 40.0})
    draft = r.json()["id"]
    await auto_je.create_for_credit_note_finalized(session, company_id=auth["company_id"], user_id=auth["user_id"],
                                                   doc_id=draft, doc=await _state(session, auth, draft))
    await session.commit()
    found = await _doctor(client, auth)
    assert [c for c, _n, _d in found] == ["uncaused_recognition_jes"], found
    assert await _books(session, auth) == {"1120": 100.0, "3200": 0.0, "4100": -100.0, "1111": 0.0, "6960": 0.0}


# N2: an imported paid amount above the total, or below zero, is refused


@pytest.mark.parametrize("paid,key", [(150.0, "doc_import.paid_over_total"), (-10.0, "doc_import.paid_negative")])
async def test_invoice_import_refuses_a_paid_amount_it_cannot_have(client, session, auth, paid, key):
    inv, _ = _ids("O")
    r = await _import(client, auth, inv, _inv(inv, 100.0, paid, 0.0, "paid"))
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["message_key"] == key, r.text
    if paid > 0:
        assert "credit note" in r.json()["detail"]["message"], r.text
    _locale_has(key)
    assert await _state(session, auth, inv) == {}
    assert await _books(session, auth) == {"1120": 0.0, "3200": 0.0, "4100": 0.0, "1111": 0.0, "6960": 0.0}
    b = await _batch(client, auth, (inv, _inv(inv, 100.0, paid, 0.0, "paid")))
    assert b.status_code == 200 and b.json()["created"] == 0 and len(b.json()["errors"]) == 1, b.text
    assert await _state(session, auth, inv) == {}
    assert await _books(session, auth) == {"1120": 0.0, "3200": 0.0, "4100": 0.0, "1111": 0.0, "6960": 0.0}


async def test_credit_note_import_refuses_more_refunded_than_its_total(client, session, auth):
    inv, cn = _ids("O")
    assert (await _import(client, auth, inv, _inv(inv, 100.0, 0.0, 100.0, "final"))).status_code == 200
    r = await _import(client, auth, cn, _note(cn, inv, 40.0, 50.0, 0.0, "paid"))
    assert r.status_code == 422 and r.json()["detail"]["message_key"] == "doc_import.refunded_over_total", r.text
    _locale_has("doc_import.refunded_over_total")
    assert await _books(session, auth) == {"1120": 100.0, "3200": 0.0, "4100": -100.0, "1111": 0.0, "6960": 0.0}


async def test_paid_in_full_still_imports(client, session, auth):
    inv, _ = _ids("O")
    assert (await _import(client, auth, inv, _inv(inv, 100.0, 100.0, 0.0, "paid"))).status_code == 200
    assert await _books(session, auth) == {"1120": 0.0, "3200": 100.0, "4100": -100.0, "1111": 0.0, "6960": 0.0}
    b = await _batch(client, auth, *[(i, _inv(i, 100.0, 100.0, 0.0, "paid")) for i in (inv + "b", inv + "c")])
    assert b.json()["created"] == 2, b.text
    assert await _books(session, auth) == {"1120": 0.0, "3200": 300.0, "4100": -300.0, "1111": 0.0, "6960": 0.0}


# N3: a used credit note imported without its invoice


@pytest.mark.parametrize("refunded", [0.0, 15.0])
async def test_used_credit_note_without_its_invoice(client, session, auth, refunded):
    inv, cn = _ids("T")
    key = uuid.uuid4().hex
    r = await _import(client, auth, cn, _note(cn, inv, 40.0, refunded, 0.0, "paid"), key=key)
    assert r.status_code == 200, r.text
    alone = await _books(session, auth)
    assert alone == {"1120": 0.0, "3200": -40.0, "4100": 40.0, "1111": 0.0, "6960": 0.0}
    assert (await _import(client, auth, cn, _note(cn, inv, 40.0, refunded, 0.0, "paid"), key=key)).status_code == 200
    assert await _books(session, auth) == alone
    # Its invoice imported later takes the used part off its own balance: the
    # retained-earnings leg for it goes, the refunded part stays.
    assert (await _import(client, auth, inv, _inv(inv, 100.0, 0.0, 100.0 - (40.0 - refunded), "partial"))).status_code == 200
    assert await _books(session, auth) == {"1120": 100.0 - (40.0 - refunded), "3200": -refunded, "4100": -60.0,
                                           "1111": 0.0, "6960": 0.0}
    assert await _doc(session, auth, cn) == ("paid", 0.0, refunded)
    assert await _doctor(client, auth) == []


async def test_open_credit_note_without_its_invoice_is_unchanged(client, session, auth):
    inv, cn = _ids("T")
    assert (await _import(client, auth, cn, _note(cn, inv, 40.0, 0.0, 40.0, "final"))).status_code == 200
    assert await _books(session, auth) == {"1120": -40.0, "3200": 0.0, "4100": 40.0, "1111": 0.0, "6960": 0.0}
    assert (await _import(client, auth, inv, _inv(inv, 100.0, 0.0, 100.0, "final"))).status_code == 200
    assert await _books(session, auth) == {"1120": 60.0, "3200": 0.0, "4100": -60.0, "1111": 0.0, "6960": 0.0}


# N4: a credit note imported at another rate than its invoice


@pytest.mark.parametrize("note_first", [False, True])
async def test_credit_note_import_at_another_rate_is_refused(client, session, auth, note_first):
    inv, cn = _ids("U")
    invoice = (inv, _inv(inv, 100.0, 0.0, 100.0, "final", currency="EUR", conversion_rate=1.1))
    note = (cn, _note(cn, inv, 40.0, 0.0, 40.0, "final", currency="EUR", conversion_rate=1.3))
    first, second = (note, invoice) if note_first else (invoice, note)
    assert (await _import(client, auth, *first)).status_code == 200
    before = await _books(session, auth)
    r = await _import(client, auth, *second)
    assert r.status_code == 422, r.text
    assert "Leave the currency and rate as the invoice's" in r.text, r.text
    assert await _books(session, auth) == before
    b = await _batch(client, auth, second)
    assert b.json()["created"] == 0 and "rate" in b.json()["errors"][0], b.text
    assert await _books(session, auth) == before


async def test_credit_note_import_at_its_invoice_rate(client, session, auth):
    inv, cn = _ids("U")
    assert (await _import(client, auth, inv, _inv(inv, 100.0, 0.0, 60.0, "partial", currency="EUR",
                                                   conversion_rate=1.1))).status_code == 200
    assert (await _import(client, auth, cn, _note(cn, inv, 40.0, 0.0, 0.0, "paid", currency="EUR",
                                                   conversion_rate=1.1))).status_code == 200
    assert await _books(session, auth) == {"1120": 66.0, "3200": 0.0, "4100": -66.0, "1111": 0.0, "6960": 0.0}
    assert (await _pay(client, auth, inv, 60.0, conversion_rate=1.1)).status_code == 200
    assert await _books(session, auth) == {"1120": 0.0, "3200": 0.0, "4100": -66.0, "1111": 66.0, "6960": 0.0}


# N5: undoing a payment the import carried


@pytest.mark.parametrize("undo", ["void", "delete"])
async def test_undoing_an_imported_payment_reverses_retained_earnings(client, session, auth, undo):
    inv, _ = _ids("P")
    assert (await _import(client, auth, inv, _inv(inv, 100.0, 100.0, 0.0, "paid", payments=_cash(100.0)))).status_code == 200
    assert await _books(session, auth) == {"1120": 0.0, "3200": 100.0, "4100": -100.0, "1111": 0.0, "6960": 0.0}
    key = uuid.uuid4().hex
    if undo == "void":
        call = lambda: _post(client, auth, f"/docs/{inv}/void-payment", {"payment_index": 0, "idempotency_key": key})
    else:
        call = lambda: client.request("DELETE", f"/docs/{inv}/payments/0", headers=auth["headers"],
                                      json={"idempotency_key": key})
    r = await call()
    assert r.status_code == 200, r.text
    undone = {"1120": 100.0, "3200": 0.0, "4100": -100.0, "1111": 0.0, "6960": 0.0}
    assert await _books(session, auth) == undone
    assert await _doc(session, auth, inv) == ("final", 100.0, 0.0)
    await call()
    assert await _books(session, auth) == undone
    # Paid again here, through the bank; voided, the invoice and every leg are where the import left them less the payment.
    assert (await _pay(client, auth, inv, 100.0)).status_code == 200
    assert await _books(session, auth) == {"1120": 0.0, "3200": 0.0, "4100": -100.0, "1111": 100.0, "6960": 0.0}
    v = await _post(client, auth, f"/docs/{inv}/void-payment", {"payment_index": 1})
    assert v.status_code == 200, v.text
    assert await _books(session, auth) == undone
    assert (await _post(client, auth, f"/docs/{inv}/void")).status_code == 200
    assert await _books(session, auth) == {"1120": 0.0, "3200": 0.0, "4100": 0.0, "1111": 0.0, "6960": 0.0}


async def test_refund_of_an_imported_payment_pays_out_of_the_bank(client, session, auth):
    inv, _ = _ids("P")
    assert (await _import(client, auth, inv, _inv(inv, 100.0, 100.0, 0.0, "paid", payments=_cash(100.0)))).status_code == 200
    r = await _post(client, auth, f"/docs/{inv}/refund", {"payment_index": 0, "amount": 30.0, "payment_date": "2026-10-09"})
    assert r.status_code == 200, r.text
    assert await _books(session, auth) == {"1120": 30.0, "3200": 100.0, "4100": -100.0, "1111": -30.0, "6960": 0.0}
    # The rest voided: only what is left of the imported payment comes back off retained earnings.
    v = await _post(client, auth, f"/docs/{inv}/void-payment", {"payment_index": 0})
    assert v.status_code == 200, v.text
    assert await _books(session, auth) == {"1120": 100.0, "3200": 30.0, "4100": -100.0, "1111": -30.0, "6960": 0.0}
    assert await _doctor(client, auth) == []


async def test_payment_made_after_the_import_voids_against_the_bank(client, session, auth):
    inv, _ = _ids("Q")
    assert (await _import(client, auth, inv, _inv(inv, 100.0, 30.0, 70.0, "partial"))).status_code == 200
    assert (await _pay(client, auth, inv, 70.0)).status_code == 200
    assert await _books(session, auth) == {"1120": 0.0, "3200": 30.0, "4100": -100.0, "1111": 70.0, "6960": 0.0}
    index = (await _state(session, auth, inv))["payments"][-1]["index"]
    v = await _post(client, auth, f"/docs/{inv}/void-payment", {"payment_index": index})
    assert v.status_code == 200, v.text
    assert await _books(session, auth) == {"1120": 70.0, "3200": 30.0, "4100": -100.0, "1111": 0.0, "6960": 0.0}


# N8 at import: a credit note whose invoice is void keeps only what it paid out


@pytest.mark.parametrize("paid,out", [(10.0, 30.0), (0.0, 0.0), (0.0, 40.0)])
async def test_credit_note_imported_against_a_void_invoice(client, session, auth, paid, out):
    inv, cn = _ids("V")
    assert (await _import(client, auth, inv, _inv(inv, 100.0, 0.0, 100.0, "void"))).status_code == 200
    status = "paid" if not out else ("partial" if paid else "final")
    assert (await _import(client, auth, cn, _note(cn, inv, 40.0, paid, out, status))).status_code == 200
    used = 40.0 - paid - out
    books = {"1120": 0.0, "3200": -(paid + used), "4100": paid + used, "1111": 0.0, "6960": 0.0}
    assert await _books(session, auth) == books
    assert await _doc(session, auth, cn) == ("paid", 0.0, paid)
    for path, body in ((f"/docs/{cn}/cn-refund", {"amount": 5.0, "date": "2026-10-09", "bank_account": "1111"}),):
        assert (await _post(client, auth, path, body)).status_code in (409, 422)
    assert await _books(session, auth) == books
    assert await _doctor(client, auth) == []
