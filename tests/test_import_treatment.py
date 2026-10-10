# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A bill, or a purchase order with goods received on it, imported into live books says how
its value enters them.

Initial conditions per test: a fresh company with the seeded chart and a lot of 10 units
booked as opening stock (_item). Documents are imported through /docs/import and
/docs/import/batch with ``import_treatment``:

- "opening_balances": the opening balances already hold the document, so the import posts
  nothing and the goods received on it are carried at their imported value.
- "record_now": the document is booked now, through the same entries the app posts for
  it: a bill through its recognition (Dr goods or expense / Cr payables), the goods
  received through the receipt (a purchase order's receipt books Dr the lot's account /
  Cr payables). What the file says was paid comes off payables against retained earnings.

A bill or received purchase order with no treatment, or one the app does not know, is
refused with a message key naming the choice and writes nothing. A purchase order with
nothing received posts nothing and needs no choice; an invoice or credit note posts as
before unless its treatment says the opening balances hold it. A staged migration posts
what its own reconciliation contract says and needs no choice. The treatment is recorded
on the document, and a document booked now is never taken for an earlier release's import.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from celerp.models.ledger import LedgerEntry
from celerp.services import auto_je
from test_cost_restatement import _item, _state
from test_imported_document_cutover import (
    AP, PRICE, _check, _doc_jes, _lot, _opening, _owed, _repair, _ret,
)
from test_imported_document_cutover import _snapshot as _imported
from test_receipt_accounting import _OPENING, _books

pytestmark = pytest.mark.asyncio

_REQUIRED, _UNKNOWN = "doc_import.treatment_required", "doc_import.treatment_unknown"
RE = "3200"


def _snapshot(*args, **kwargs) -> dict:
    """The document with no treatment of its own; each test names the one it sends."""
    return _imported(*args, treatment=None, **kwargs)


def _rec(data: dict, treatment: str | None = None) -> dict:
    if treatment is not None:
        data = {**data, "import_treatment": treatment}
    return {"entity_id": f"doc:{uuid.uuid4()}", "event_type": "doc.created", "source": "test",
            "idempotency_key": uuid.uuid4().hex, "data": data}


async def _post(client, auth, rec: dict, batch: bool):
    if batch:
        return await client.post("/docs/import/batch", headers=auth["headers"], json={"records": [rec]})
    return await client.post("/docs/import", headers=auth["headers"], json=rec)


async def _events(session, auth) -> int:
    session.expire_all()
    return (await session.execute(select(func.count()).select_from(LedgerEntry).where(
        LedgerEntry.company_id == auth["company_id"]))).scalar_one()


def _posted(jes: dict) -> dict:
    return {s: je for s, je in jes.items() if je.get("status") == "posted"}


def _on(jes: dict, account: str) -> float:
    return sum(float(e.get("debit") or 0) - float(e.get("credit") or 0)
               for je in jes.values() for e in je["entries"] if e["account"] == account)


@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize("doc_type,received", [("bill", 2), ("bill", 0), ("purchase_order", 2)])
async def test_a_live_import_with_no_treatment_is_refused_and_writes_nothing(
        client, session, auth, batch, doc_type, received):
    lot = await _item(client, auth, _OPENING, qty=10)
    rec = _rec(_snapshot(lot, doc_type, 5, received))
    n, before = await _events(session, auth), await _lot(session, auth, lot)
    r = await _post(client, auth, rec, batch)
    if batch:
        assert r.status_code == 200 and r.json()["created"] == 0, r.text
        assert "Already in my opening balances" in r.json()["errors"][0], r.text
    else:
        assert r.status_code == 422, r.text
        assert r.json()["detail"]["message_key"] == _REQUIRED, r.text
        assert set(r.json()["detail"]["params"]) >= {"number"}, r.text
    assert await _state(session, auth, rec["entity_id"]) == {}
    assert await _events(session, auth) == n
    assert await _lot(session, auth, lot) == before


@pytest.mark.parametrize("doc_type", ["bill", "purchase_order", "invoice"])
async def test_a_treatment_the_app_does_not_know_is_refused(client, session, auth, doc_type):
    lot = await _item(client, auth, _OPENING, qty=10)
    data = _snapshot(lot, doc_type, 5, 0 if doc_type == "invoice" else 2)
    if doc_type == "invoice":
        data = {**data, "contact_id": "customer:1", "status": "final", "received_items": []}
    rec = _rec(data, "later")
    r = await _post(client, auth, rec, False)
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["message_key"] == _UNKNOWN, r.text
    assert await _state(session, auth, rec["entity_id"]) == {}


async def test_an_order_with_nothing_received_needs_no_treatment_and_posts_nothing(client, session, auth):
    lot = await _item(client, auth, _OPENING, qty=10)
    rec = _rec(_snapshot(lot, "purchase_order", 5, 0))
    rec["data"]["status"] = "final"
    r = await _post(client, auth, rec, False)
    assert r.status_code == 200, r.text
    assert await _doc_jes(session, auth, rec["entity_id"]) == {}


@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize("doc_type", ["bill", "purchase_order"])
async def test_opening_balances_posts_nothing_and_records_the_treatment(client, session, auth, batch, doc_type):
    lot = await _item(client, auth, _OPENING, qty=10)
    await _opening(client, auth, *_owed(doc_type, 5, 2))
    rec = _rec(_snapshot(lot, doc_type, 5, 2), "opening_balances")
    r = await _post(client, auth, rec, batch)
    assert r.status_code == 200, r.text
    st = await _state(session, auth, rec["entity_id"])
    assert st.get("import_treatment") == "opening_balances"
    assert await _doc_jes(session, auth, rec["entity_id"]) == {}
    assert await _lot(session, auth, lot) == (10.0, _OPENING)
    await _check(session, auth, [rec["entity_id"]], "imported")


@pytest.mark.parametrize("batch", [False, True])
async def test_record_now_books_a_bill_through_its_recognition_and_receipt(client, session, auth, batch):
    """A bill for 5 at 14.00 with 2 received: its recognition credits payables 70.00 and the
    2 received come into stock through the receipt, a parcel at 28.00."""
    lot = await _item(client, auth, _OPENING, qty=10)
    rec = _rec(_snapshot(lot, "bill", 5, 2), "record_now")
    r = await _post(client, auth, rec, batch)
    assert r.status_code == 200, r.text
    doc = rec["entity_id"]
    st = await _state(session, auth, doc)
    assert st.get("import_treatment") == "record_now"
    assert [float(x["quantity_received"]) for x in st.get("received_items") or []] == [2.0]
    assert float(st["line_items"][0]["quantity_received"]) == 2.0
    parcels = [await _state(session, auth, i) for i in st.get("received_item_ids") or []]
    assert [(float(p["quantity"]), float(p["cost_total"])) for p in parcels] == [(2.0, 2 * PRICE)]
    posted = _posted(await _doc_jes(session, auth, doc))
    assert set(posted) == {"bill"}, posted
    assert _on(posted, AP) == -5 * PRICE
    assert (await _books(session, auth, AP))[AP] == -5 * PRICE
    assert await _lot(session, auth, lot) == (10.0, _OPENING)
    await _check(session, auth, [doc], "booked now")


@pytest.mark.parametrize("batch", [False, True])
async def test_record_now_receives_an_orders_goods_through_the_receipt(client, session, auth, batch):
    """An order for 5 at 14.00, all received: the receipt adds them to the lot and books
    Dr the lot's account / Cr payables 70.00."""
    lot = await _item(client, auth, _OPENING, qty=10)
    rec = _rec(_snapshot(lot, "purchase_order", 5, 5), "record_now")
    r = await _post(client, auth, rec, batch)
    assert r.status_code == 200, r.text
    doc = rec["entity_id"]
    st = await _state(session, auth, doc)
    assert (st.get("status"), st.get("import_treatment")) == ("received", "record_now")
    assert await _lot(session, auth, lot) == (15.0, _OPENING + 5 * PRICE)
    posted = _posted(await _doc_jes(session, auth, doc))
    assert {k.split(":")[0] for k in posted} == {"rcv"}, posted
    assert (await _books(session, auth, AP))[AP] == -5 * PRICE
    await _check(session, auth, [doc], "booked now")


async def test_record_now_takes_what_was_paid_off_payables(client, session, auth):
    lot = await _item(client, auth, _OPENING, qty=10)
    data = {**_snapshot(lot, "bill", 5, 5, paid=30.0), "status": "partial"}
    rec = _rec(data, "record_now")
    r = await _post(client, auth, rec, False)
    assert r.status_code == 200, r.text
    posted = _posted(await _doc_jes(session, auth, rec["entity_id"]))
    assert set(posted) == {"bill", "opening-paid"}, posted
    assert _on({"p": posted["opening-paid"]}, AP) == 30.0
    assert _on({"p": posted["opening-paid"]}, RE) == -30.0
    assert (await _books(session, auth, AP))[AP] == -(5 * PRICE - 30.0)
    await _check(session, auth, [rec["entity_id"]], "booked now")


async def test_record_now_resent_books_it_once(client, session, auth):
    lot = await _item(client, auth, _OPENING, qty=10)
    rec = _rec(_snapshot(lot, "purchase_order", 5, 5), "record_now")
    assert (await _post(client, auth, rec, False)).status_code == 200
    again = await _post(client, auth, rec, False)
    assert again.status_code == 200 and again.json()["idempotency_hit"] is True, again.text
    b = await _post(client, auth, rec, True)
    assert b.json()["skipped"] == 1, b.text
    assert await _lot(session, auth, lot) == (15.0, _OPENING + 5 * PRICE)
    assert (await _books(session, auth, AP))[AP] == -5 * PRICE


async def test_a_document_booked_now_is_not_taken_for_an_earlier_import(client, session, auth):
    lot = await _item(client, auth, _OPENING, qty=10)
    bill = _rec(_snapshot(lot, "bill", 5, 2), "record_now")
    order = _rec(_snapshot(lot, "purchase_order", 5, 5), "record_now")
    for rec in (bill, order):
        assert (await _post(client, auth, rec, False)).status_code == 200
        assert await auto_je.imported_document(session, auth["company_id"], rec["entity_id"]) is None
    before = await _books(session, auth, AP)
    await _repair(session)
    assert await _books(session, auth, AP) == before
    await _check(session, auth, [bill["entity_id"], order["entity_id"]], "after repair")


async def test_goods_booked_now_go_back_and_the_bill_owes_less(client, session, auth):
    lot = await _item(client, auth, _OPENING, qty=10)
    rec = _rec(_snapshot(lot, "purchase_order", 5, 5), "record_now")
    assert (await _post(client, auth, rec, False)).status_code == 200
    doc = rec["entity_id"]
    r = await client.post(f"/docs/{doc}/return-items", headers=auth["headers"], **_ret(2, lot))
    assert r.status_code == 200, r.text
    assert await _lot(session, auth, lot) == (13.0, _OPENING + 3 * PRICE)
    assert (await _books(session, auth, AP))[AP] == -3 * PRICE


async def _lock(client, auth, day: str = "2026-06-30") -> None:
    r = await client.post("/accounting/period-lock", headers=auth["headers"], json={"lock_date": day})
    assert r.status_code == 200, r.text


@pytest.mark.parametrize("treatment", ["opening_balances", "record_now"])
async def test_a_locked_period_bill_is_booked_once_under_either_treatment(client, session, auth, treatment):
    """L3-06: a bill imported with its treatment and then locked into a closed period is not
    booked again by the start-up correction of earlier imports: payables, stock and the
    ledger stay where the import left them."""
    lot = await _item(client, auth, _OPENING, qty=10)
    if treatment == "opening_balances":
        await _opening(client, auth, *_owed("bill", 5, 2))
    rec = _rec(_snapshot(lot, "bill", 5, 2), treatment)
    assert (await _post(client, auth, rec, False)).status_code == 200
    await _lock(client, auth)
    before, lot_before, n = await _books(session, auth, AP), await _lot(session, auth, lot), await _events(session, auth)
    result = await _repair(session)
    assert (result["corrected"], result["deferred"]) == (0, 0), result
    assert await _events(session, auth) == n
    assert await _books(session, auth, AP) == before
    assert await _lot(session, auth, lot) == lot_before
    await _check(session, auth, [rec["entity_id"]], "locked, after the start-up correction")


async def test_record_now_dated_in_a_locked_period_is_refused_and_writes_nothing(client, session, auth):
    """Booking now posts on the bill's own date; inside a locked period that is refused,
    so nothing is booked and the import can be resent once the date is open."""
    lot = await _item(client, auth, _OPENING, qty=10)
    await _lock(client, auth)
    n, before = await _events(session, auth), await _books(session, auth, AP)
    rec = _rec(_snapshot(lot, "bill", 5, 2), "record_now")
    r = await _post(client, auth, rec, False)
    assert r.status_code == 422 and "locked" in r.text.lower(), r.text
    assert await _events(session, auth) == n
    assert await _books(session, auth, AP) == before
    assert await _state(session, auth, rec["entity_id"]) == {}


async def test_a_staged_migration_needs_no_treatment(client, session, auth):
    from celerp_docs import import_service
    from celerp_docs.routes import DocImportRecord

    lot = await _item(client, auth, _OPENING, qty=10)
    rec = _rec(_snapshot(lot, "bill", 5, 2))
    outcome = await import_service.import_doc_records(
        session, auth["company_id"], SimpleNamespace(id=auth["user_id"]), "admin", {},
        [DocImportRecord(**rec)], post_ledger=False)
    assert [r.status for r in outcome.records] == ["created"], outcome.records


# Neighbouring rules: an invoice or credit note posts as before with no choice; the importer
# may say the opening balances hold one, and that is recorded and posts nothing.


async def test_an_invoice_with_no_treatment_still_posts(client, session, auth):
    rec = _rec({"doc_type": "invoice", "contact_id": "customer:1", "status": "final",
                "doc_number": f"I-{uuid.uuid4().hex[:6]}", "issue_date": "2026-01-01", "total": 50.0,
                "line_items": [{"name": "Service", "quantity": 1, "unit_price": 50.0, "line_total": 50.0}]})
    assert (await _post(client, auth, rec, False)).status_code == 200
    assert "fin" in {s.split(":")[0] for s in _posted(await _doc_jes(session, auth, rec["entity_id"]))}


@pytest.mark.parametrize("batch", [False, True])
async def test_an_invoice_the_opening_balances_hold_posts_nothing(client, session, auth, batch):
    rec = _rec({"doc_type": "invoice", "contact_id": "customer:1", "status": "partial", "amount_paid": 10.0,
                "amount_outstanding": 40.0, "doc_number": f"I-{uuid.uuid4().hex[:6]}", "issue_date": "2026-01-01", "total": 50.0,
                "line_items": [{"name": "Service", "quantity": 1, "unit_price": 50.0, "line_total": 50.0}]},
               "opening_balances")
    assert (await _post(client, auth, rec, batch)).status_code == 200
    assert await _doc_jes(session, auth, rec["entity_id"]) == {}
    st = await _state(session, auth, rec["entity_id"])
    assert (st.get("import_treatment"), float(st.get("amount_outstanding") or 0)) == ("opening_balances", 40.0)
