# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Reading the balance sheet changes nothing, and stock the books do not carry is reported.

Opening the balance sheet never posts an entry to make the books agree with the stock:
stock with no entry behind it, from an older release, another system's books or an
import, is listed by the books check for the user to resolve. A receipt on a bill that is
still a draft is refused, because the draft books nothing and the goods would sit on no
entry.
"""
from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from celerp.models.ledger import LedgerEntry
from celerp.services.auto_je import _emit_auto_posted_je
from stock_books import older_release_lot
from test_helpers import in_language
from test_migration_sinks import _PROVENANCE, _no_attachments, _persist_mappings, _sink_context
from test_receipt_accounting import _doc, _finalize, _receive

pytestmark = pytest.mark.asyncio


async def _events(session, company_id) -> int:
    return await session.scalar(select(func.count()).select_from(LedgerEntry).where(
        LedgerEntry.company_id == company_id))


async def _read_balance_sheet_writes_nothing(client, session, headers, company_id) -> None:
    before = await _events(session, company_id)
    r = await client.get("/accounting/balance-sheet", headers=headers)
    assert r.status_code == 200, r.text
    session.expire_all()
    assert await _events(session, company_id) == before


async def _stock_check(client, headers) -> dict:
    r = await client.post("/admin/doctor?checks=stock_on_books", headers=headers)
    assert r.status_code == 200, r.text
    (result,) = r.json()["results"]
    assert (result["check"], result["auto_fixable"], result["fixed"]) == ("stock_on_books", False, 0)
    return result


async def test_the_balance_sheet_books_nothing_for_stock_an_older_release_left(client, session, auth):
    lot = await older_release_lot(session, auth["company_id"], auth["user_id"], 40.0)
    await _read_balance_sheet_writes_nothing(client, session, auth["headers"], auth["company_id"])
    result = await _stock_check(client, auth["headers"])
    assert [(d["kind"], d["entity_id"]) for d in result["details"]] == [("unplaced_lot", lot)]


async def test_the_balance_sheet_books_nothing_for_migrated_stock(client, session):
    from celerp.importers.schema import CIFItem
    from celerp.importers.sinks import sink_for

    headers, context = await _sink_context(client, session, _no_attachments)
    item = CIFItem(**_PROVENANCE, source_type="InventoryItem", source_external_id="item-1", sku="W-1",
                   name="Widget", status="available", total_cost=Decimal("40"))
    result = await sink_for("items").import_batch(context, [item])
    assert result.errors == [], result.errors
    await _persist_mappings(session, context.run_id, result)
    await session.commit()
    await _read_balance_sheet_writes_nothing(client, session, headers, context.company_id)


async def test_the_balance_sheet_books_nothing_for_imported_stock(client, session):
    from celerp.events.engine import emit_event

    headers, context = await _sink_context(client, session, _no_attachments)
    await emit_event(session, company_id=context.company_id, entity_id=f"item:{uuid.uuid4()}", entity_type="item",
                     event_type="item.snapshot", data={"sku": "IMP", "name": "Imported", "quantity": 1,
                                                       "sell_by": "piece", "status": "available", "cost_total": 5.0},
                     actor_id=None, location_id=None, source="import:bundle", idempotency_key=f"cif:{uuid.uuid4()}")
    await session.commit()
    await _read_balance_sheet_writes_nothing(client, session, headers, context.company_id)


async def test_the_books_check_finds_nothing_when_the_books_carry_the_stock(client, auth):
    r = await client.post("/items", headers=auth["headers"], json={
        "status": "available", "sku": "CARRIED", "name": "Carried", "quantity": 2, "sell_by": "piece",
        "cost_total": 20.0})
    assert r.status_code == 200, r.text
    assert (await _stock_check(client, auth["headers"]))["details"] == []


async def test_the_books_check_reports_an_account_carrying_more_than_its_stock(client, session, auth):
    cid, po = auth["company_id"], f"doc:{uuid.uuid4()}"
    before = (await _stock_check(client, auth["headers"]))["details"]
    await _emit_auto_posted_je(
        session, company_id=cid, user_id=auth["user_id"], je_id=f"je:auto:{po}:rcv:1",
        idem_create=f"{po}:rcv:c", idem_posted=f"{po}:rcv:p", memo=f"Auto JE for {po} received",
        entries=[{"account": "1130-P", "debit": 40.0, "credit": 0.0},
                 {"account": "2110", "debit": 0.0, "credit": 40.0}],
        metadata_={"trigger": "doc.received", "doc_id": po})
    await session.commit()
    (finding,) = (await _stock_check(client, auth["headers"]))["details"]
    assert before == []
    assert (finding["kind"], finding["account"], finding["books"] - finding["stock"]) == ("stock_gap", "1130-P", 40.0)


async def test_a_receipt_on_a_draft_bill_is_refused(client, session, auth):
    bill = await _doc(client, auth, "bill", [{"sku": "DRAFT-G", "name": "Goods", "quantity": 4, "unit_price": 10}])
    line = {"po_line_index": 0, "sku": "DRAFT-G", "name": "Goods", "quantity_received": 4}
    before = await _events(session, auth["company_id"])
    r = await _receive(client, auth, bill, line)
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert "Finalize the bill" in detail["message"]
    assert detail["message_key"] == "docs.receive_draft_bill"
    assert in_language("de", detail) == "Diese Rechnung ist noch ein Entwurf und hat diese Waren nicht gebucht. " \
        "Schließen Sie die Rechnung zuerst ab und nehmen Sie die Waren dann an."
    session.expire_all()
    assert await _events(session, auth["company_id"]) == before

    await _finalize(client, auth, bill)
    r = await _receive(client, auth, bill, line)
    assert r.status_code == 200, r.text


async def test_a_further_receipt_on_a_draft_an_earlier_release_received_is_refused(client, session, auth, monkeypatch):
    import celerp_docs.routes as docs_routes

    refuse = docs_routes._refuse_receipt_on_a_draft_bill
    bill = await _doc(client, auth, "bill", [{"sku": "OLD-G", "name": "Goods", "quantity": 4, "unit_price": 10}])
    line = {"po_line_index": 0, "sku": "OLD-G", "name": "Goods", "quantity_received": 2}
    monkeypatch.setattr(docs_routes, "_refuse_receipt_on_a_draft_bill", lambda state: None)
    assert (await _receive(client, auth, bill, line)).status_code == 200  # as the earlier release took it in
    monkeypatch.setattr(docs_routes, "_refuse_receipt_on_a_draft_bill", refuse)
    r = await _receive(client, auth, bill, line)
    assert r.status_code == 409, r.text
    assert "Finalize the bill" in r.json()["detail"]["message"]
