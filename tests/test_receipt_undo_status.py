# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Undoing a bill's receipt puts the bill back where its receipt found it.

A draft bill whose goods an earlier release received before it was issued is still a
draft once the receipt is undone: it has not been issued and books no payable. An
issued bill goes back to what its payments make it, so a paid bill stays paid and an
unpaid one awaiting payment stays awaiting payment.
"""
from __future__ import annotations

import uuid

import pytest

from test_helpers import default_location_id

pytestmark = pytest.mark.asyncio


async def _owner(client) -> dict:
    r = await client.post("/auth/register", json={
        "company_name": "Bills Co", "email": f"a-{uuid.uuid4().hex[:8]}@bills.test", "name": "Admin",
        "password": "pwvalid1"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def _bill(client, h: dict, sku: str) -> str:
    r = await client.post("/docs", headers=h, json={"doc_type": "bill", "line_items": [
        {"sku": sku, "name": "Widget", "quantity": 2, "unit_price": 50, "line_total": 100, "receive_as": "stock"}],
        "subtotal": 100, "tax": 0, "total": 100})
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _post(client, h: dict, path: str, body: dict | None = None) -> dict:
    r = await client.post(path, headers=h, json=body or {})
    assert r.status_code == 200, r.text
    return r.json()


def _receives_on_drafts(monkeypatch) -> None:
    """Receive as an earlier release did, which took goods in on a bill still a draft."""
    import celerp_docs.routes as docs_routes

    monkeypatch.setattr(docs_routes, "_refuse_receipt_when_not_open", lambda state: None)


async def _receive_and_undo(client, h: dict, bill: str, sku: str) -> dict:
    loc = await default_location_id(client, h)
    await _post(client, h, f"/docs/{bill}/receive", {"location_id": loc, "received_items": [
        {"po_line_index": 0, "sku": sku, "name": "Widget", "quantity_received": 2, "receive_as": "stock"}]})
    assert (await client.get(f"/docs/{bill}", headers=h)).json()["status"] == "received"
    r = await client.delete(f"/docs/{bill}/receive", headers=h)
    assert r.status_code == 200, r.text
    return (await client.get(f"/docs/{bill}", headers=h)).json()


async def test_undoing_an_earlier_receipt_on_a_draft_bill_leaves_it_a_draft(client, monkeypatch):
    h = await _owner(client)
    _receives_on_drafts(monkeypatch)
    bill = await _bill(client, h, "RU-DRAFT")

    doc = await _receive_and_undo(client, h, bill, "RU-DRAFT")

    assert doc["status"] == "draft"
    assert not doc.get("finalized")
    await _post(client, h, f"/docs/{bill}/finalize")
    doc = (await client.get(f"/docs/{bill}", headers=h)).json()
    assert (doc["status"], doc["finalized"]) == ("final", True)


async def test_undoing_a_receipt_on_an_issued_bill_leaves_it_final(client):
    h = await _owner(client)
    bill = await _bill(client, h, "RU-FINAL")
    await _post(client, h, f"/docs/{bill}/finalize")

    doc = await _receive_and_undo(client, h, bill, "RU-FINAL")

    assert doc["status"] == "final"


async def test_undoing_a_receipt_on_a_paid_bill_leaves_it_paid(client):
    h = await _owner(client)
    bill = await _bill(client, h, "RU-PAID")
    await _post(client, h, f"/docs/{bill}/finalize")
    await _post(client, h, f"/docs/{bill}/payment", {"payment_date": "2026-01-15", "amount": 100, "bank_account": "1111"})
    assert (await client.get(f"/docs/{bill}", headers=h)).json()["status"] == "paid"

    doc = await _receive_and_undo(client, h, bill, "RU-PAID")

    assert doc["status"] == "paid"


# Bills whose goods came in before this release: their projections were written by code that
# did not record what the receipt found, so the upgrade fills it in from each bill's ledger.

async def _company_id(client, h: dict) -> uuid.UUID:
    return uuid.UUID((await client.get("/companies/me", headers=h)).json()["id"])


async def _as_before_the_upgrade(session, company_id, bill: str, **state) -> None:
    """Leave the bill's projection as the earlier release wrote it: nothing recorded about
    the receipt, and an issued bill not yet flagged as issued (that flag came later still)."""
    from celerp.models.projections import Projection

    row = await session.get(Projection, {"company_id": company_id, "entity_id": bill})
    old = {k: v for k, v in row.state.items() if k not in ("pre_receipt_status", "finalized")}
    row.state = {**old, **state}
    await session.commit()


async def _upgrade(session) -> None:
    """What booting the new release does over the earlier data (its on_modules_ready step)."""
    from celerp.migrations._data_reconcile import set_meta
    from celerp_docs.legacy_receipts import LEGACY_RECEIPTS_KEY, record_legacy_receipts_hook

    conn = await session.connection()
    await conn.run_sync(lambda c: set_meta(c, LEGACY_RECEIPTS_KEY, ""))
    await record_legacy_receipts_hook(session=session)
    await session.commit()


async def _receive(client, h: dict, bill: str, sku: str) -> None:
    loc = await default_location_id(client, h)
    await _post(client, h, f"/docs/{bill}/receive", {"location_id": loc, "received_items": [
        {"po_line_index": 0, "sku": sku, "name": "Widget", "quantity_received": 2, "receive_as": "stock"}]})


async def _undo_receipt(client, h: dict, bill: str) -> dict:
    r = await client.delete(f"/docs/{bill}/receive", headers=h)
    assert r.status_code == 200, r.text
    return (await client.get(f"/docs/{bill}", headers=h)).json()


async def test_an_earlier_draft_bill_with_goods_in_is_still_a_draft_when_its_receipt_is_undone(client, session, monkeypatch):
    h = await _owner(client)
    _receives_on_drafts(monkeypatch)
    cid = await _company_id(client, h)
    bill = await _bill(client, h, "RU-OLD-DRAFT")
    await _receive(client, h, bill, "RU-OLD-DRAFT")
    await _as_before_the_upgrade(session, cid, bill)

    await _upgrade(session)
    doc = await _undo_receipt(client, h, bill)

    assert doc["status"] == "draft"
    assert not doc.get("finalized")


async def test_an_earlier_draft_bill_whose_receipt_was_undone_shows_as_a_draft_again(client, session, monkeypatch):
    """The earlier release marked such a bill final although it was never issued."""
    h = await _owner(client)
    _receives_on_drafts(monkeypatch)
    cid = await _company_id(client, h)
    bill = await _bill(client, h, "RU-OLD-UNDONE")
    doc = await _receive_and_undo(client, h, bill, "RU-OLD-UNDONE")
    assert doc["status"] == "draft"
    await _as_before_the_upgrade(session, cid, bill, status="final")

    await _upgrade(session)

    doc = (await client.get(f"/docs/{bill}", headers=h)).json()
    assert doc["status"] == "draft"
    assert not doc.get("finalized")


async def test_an_earlier_bill_issued_after_its_goods_came_in_stays_final(client, session, monkeypatch):
    h = await _owner(client)
    _receives_on_drafts(monkeypatch)
    cid = await _company_id(client, h)
    bill = await _bill(client, h, "RU-OLD-ISSUED")
    await _receive(client, h, bill, "RU-OLD-ISSUED")
    await _post(client, h, f"/docs/{bill}/finalize")
    await _as_before_the_upgrade(session, cid, bill)

    await _upgrade(session)
    doc = await _undo_receipt(client, h, bill)

    assert doc["status"] == "final"
    assert doc["finalized"] is True


async def test_an_earlier_paid_bill_stays_paid(client, session):
    h = await _owner(client)
    cid = await _company_id(client, h)
    bill = await _bill(client, h, "RU-OLD-PAID")
    await _post(client, h, f"/docs/{bill}/finalize")
    await _post(client, h, f"/docs/{bill}/payment", {"payment_date": "2026-01-15", "amount": 100, "bank_account": "1111"})
    await _receive(client, h, bill, "RU-OLD-PAID")
    await _as_before_the_upgrade(session, cid, bill)

    await _upgrade(session)
    doc = await _undo_receipt(client, h, bill)

    assert doc["status"] == "paid"


async def test_an_earlier_bill_imported_as_issued_stays_final(client, session):
    """An imported bill arrives issued, with no finalize step of its own."""
    h = await _owner(client)
    cid = await _company_id(client, h)
    bill = f"doc:imp-{uuid.uuid4().hex[:8]}"
    r = await client.post("/docs/import/batch", headers=h, json={"records": [{
        "entity_id": bill, "event_type": "doc.created", "source": "import", "idempotency_key": f"imp-{bill}",
        "data": {"doc_type": "bill", "status": "final", "doc_number": f"IMP-{bill[-8:]}", "issue_date": "2026-01-10",
                 "currency": "USD", "subtotal": 100, "tax": 0, "total": 100, "import_treatment": "record_now", "line_items": [
                     {"sku": "RU-OLD-IMP", "name": "Widget", "quantity": 2, "unit_price": 50, "line_total": 100,
                      "receive_as": "stock"}]}}]})
    assert r.status_code == 200 and r.json()["created"] == 1, r.text
    await _receive(client, h, bill, "RU-OLD-IMP")
    await _as_before_the_upgrade(session, cid, bill)

    await _upgrade(session)
    doc = await _undo_receipt(client, h, bill)

    assert doc["status"] == "final"


async def test_the_upgrade_fills_in_earlier_receipts_once(client, session, monkeypatch):
    from celerp.models.projections import Projection
    from celerp_docs.legacy_receipts import record_legacy_receipts

    h = await _owner(client)
    _receives_on_drafts(monkeypatch)
    cid = await _company_id(client, h)
    bill = await _bill(client, h, "RU-OLD-ONCE")
    await _receive(client, h, bill, "RU-OLD-ONCE")
    await _as_before_the_upgrade(session, cid, bill)

    await _upgrade(session)
    state = dict((await session.get(Projection, {"company_id": cid, "entity_id": bill})).state)
    assert state["pre_receipt_status"] == "draft"

    assert (await record_legacy_receipts(session))["changed"] is False
    await _as_before_the_upgrade(session, cid, bill)
    assert (await record_legacy_receipts(session))["changed"] is False
    assert "pre_receipt_status" not in (await session.get(Projection, {"company_id": cid, "entity_id": bill})).state


async def test_undoing_a_receipt_on_a_bill_converted_from_an_order_leaves_it_awaiting_payment(client):
    h = await _owner(client)
    r = await client.post("/docs", headers=h, json={"doc_type": "purchase_order", "line_items": [
        {"sku": "RU-PO", "name": "Widget", "quantity": 2, "unit_price": 50, "line_total": 100, "receive_as": "stock"}],
        "subtotal": 100, "tax": 0, "total": 100})
    assert r.status_code == 200, r.text
    bill = r.json()["id"]
    await _post(client, h, f"/docs/{bill}/finalize")
    assert (await client.get(f"/docs/{bill}", headers=h)).json()["status"] == "awaiting_payment"

    doc = await _receive_and_undo(client, h, bill, "RU-PO")

    assert doc["status"] == "awaiting_payment"
