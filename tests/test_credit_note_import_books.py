# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Imported invoices and credit notes enter the books as the documents say: an imported
credit note posts its entry like one issued here, and what an imported document says was
already paid before the import comes off the receivable against retained earnings, as other
value the books first recognize does. After any import 1120 equals open invoices less open
credit note balances, and the customer owes exactly total less paid less credited."""
from __future__ import annotations

import uuid

import pytest

from test_cost_restatement import _state
from test_credit_note_settlement_owned import _ar, _pay, _post
from test_set_aside_older_paths import _net

pytestmark = pytest.mark.asyncio


async def _import(client, auth, eid, data):
    r = await client.post("/docs/import", headers=auth["headers"], json={
        "entity_id": eid, "event_type": "doc.created", "source": "csv", "idempotency_key": uuid.uuid4().hex,
        "data": data})
    assert r.status_code == 200, r.text
    return r


def _line(total, name="Service"):
    return [{"name": name, "quantity": 1, "unit_price": total, "line_total": total}]


def _invoice(eid, total, paid, outstanding, status):
    return {"doc_type": "invoice", "ref_id": eid[4:], "status": status, "total": total, "amount_paid": paid,
            "amount_outstanding": outstanding, "line_items": _line(total), "issue_date": "2026-09-01"}


def _note(eid, invoice, total, paid, outstanding, status):
    return {"doc_type": "credit_note", "ref_id": eid[4:], "status": status, "original_doc_id": invoice,
            "total": total, "amount_paid": paid, "amount_outstanding": outstanding,
            "line_items": _line(total, "Credit"), "issue_date": "2026-09-02"}


async def _tie(session, auth) -> float:
    gl, sub = await _ar(session, auth)
    assert gl == sub, f"1120 {gl} != open invoices less open credit note balances {sub}"
    return gl


def _ids(prefix):
    tag = uuid.uuid4().hex[:6]
    return f"doc:INV-{prefix}{tag}", f"doc:CN-{prefix}{tag}"


@pytest.mark.parametrize("credit_note_first", [False, True])
async def test_invoice_settled_only_by_its_credit_note(client, session, auth, credit_note_first):
    inv, cn = _ids("J")
    docs = [(inv, _invoice(inv, 100.0, 0.0, 60.0, "partial")), (cn, _note(cn, inv, 40.0, 0.0, 0.0, "paid"))]
    for eid, data in reversed(docs) if credit_note_first else docs:
        await _import(client, auth, eid, data)
    assert await _tie(session, auth) == 60.0
    assert await _net(session, auth, "4100", prefix="je:") == -60.0
    assert (await _pay(client, auth, inv, 60.0)).status_code == 200
    assert await _tie(session, auth) == 0.0


async def test_credit_note_with_open_credit(client, session, auth):
    inv, cn = _ids("K")
    await _import(client, auth, inv, _invoice(inv, 100.0, 0.0, 100.0, "final"))
    await _import(client, auth, cn, _note(cn, inv, 40.0, 0.0, 40.0, "final"))
    assert await _tie(session, auth) == 60.0
    r = await _post(client, auth, f"/docs/{cn}/cn-refund", {"amount": 40.0, "date": "2026-10-09", "bank_account": "1111"})
    assert r.status_code == 200, r.text
    assert await _tie(session, auth) == 100.0


async def test_credit_note_claiming_more_than_its_invoice_explains_keeps_the_rest_open(client, session, auth):
    inv, c1 = _ids("L")
    c2 = c1 + "b"
    await _import(client, auth, inv, _invoice(inv, 100.0, 60.0, 0.0, "paid"))
    for c in (c1, c2):
        await _import(client, auth, c, _note(c, inv, 40.0, 0.0, 0.0, "paid"))
    assert float((await _state(session, auth, inv)).get("credited") or 0) == 40.0
    second = await _state(session, auth, c2)
    assert (second.get("status"), float(second.get("amount_outstanding") or 0)) == ("final", 40.0)
    assert await _tie(session, auth) == -40.0


async def test_imported_paid_invoice_owes_nothing_in_the_books(client, session, auth):
    inv, _ = _ids("P")
    await _import(client, auth, inv, _invoice(inv, 100.0, 100.0, 0.0, "paid"))
    assert await _tie(session, auth) == 0.0
    assert await _net(session, auth, "4100", prefix="je:") == -100.0


async def test_imported_partly_paid_invoice_owes_what_is_left(client, session, auth):
    inv, _ = _ids("Q")
    await _import(client, auth, inv, _invoice(inv, 100.0, 30.0, 70.0, "partial"))
    assert await _tie(session, auth) == 70.0
    assert (await _pay(client, auth, inv, 70.0)).status_code == 200
    assert await _tie(session, auth) == 0.0


async def test_imported_refunded_credit_note(client, session, auth):
    inv, cn = _ids("R")
    await _import(client, auth, inv, _invoice(inv, 100.0, 0.0, 100.0, "final"))
    await _import(client, auth, cn, _note(cn, inv, 40.0, 40.0, 0.0, "paid"))
    assert await _tie(session, auth) == 100.0
    assert await _net(session, auth, "4100", prefix="je:") == -60.0


async def test_batch_import_posts_the_same(client, session, auth):
    inv, cn = _ids("S")
    r = await client.post("/docs/import/batch", headers=auth["headers"], json={"records": [
        {"entity_id": inv, "event_type": "doc.created", "source": "csv", "idempotency_key": uuid.uuid4().hex,
         "data": _invoice(inv, 100.0, 30.0, 30.0, "partial")},
        {"entity_id": cn, "event_type": "doc.created", "source": "csv", "idempotency_key": uuid.uuid4().hex,
         "data": _note(cn, inv, 40.0, 0.0, 0.0, "paid")}]})
    assert r.status_code == 200, r.text
    assert await _tie(session, auth) == 30.0
