# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""The migration sinks write through the same domain services as the batch routes."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from test_helpers import register_admin
from ui.i18n import t

_PROVENANCE = {"source_system": "manager_io"}


def _spy(monkeypatch, module, name: str, calls: list[str]) -> None:
    """Record every call to `module.name` while still running the real service."""
    real = getattr(module, name)

    async def recording(*args, **kwargs):
        calls.append(name)
        return await real(*args, **kwargs)

    monkeypatch.setattr(module, name, recording)


def _no_attachments(key: str) -> bytes:
    raise AssertionError("This test imports no attachments.")


async def _sink_context(client, session, read_attachment):
    """Auth headers and a sink context for a fresh migration run into the admin's company."""
    from celerp.importers.sinks import SinkContext, register_sink
    from celerp.models.accounting import UserCompany
    from celerp.models.company import User
    from celerp.models.migration import MigrationRun
    from celerp.services import migration_core_sink

    token = await register_admin(client)
    user = (await session.execute(select(User).where(User.email == "admin@perm.example"))).scalar_one()
    company_id = (await session.execute(
        select(UserCompany.company_id).where(UserCompany.user_id == user.id)
    )).scalar_one()
    run = MigrationRun(
        company_id=company_id, created_by_user_id=user.id, scan_claim_sha256="1" * 64, source_system="manager_io",
        source_artifact_sha256="0" * 64, adapter_version="test", cif_version="2", mode="full_history",
    )
    session.add(run)
    await session.flush()
    register_sink(migration_core_sink.SINK)
    context = SinkContext(session=session, company_id=company_id, user_id=user.id, run_id=run.id,
                          read_attachment=read_attachment)
    return {"Authorization": f"Bearer {token}"}, context


async def _persist_mappings(session, run_id, result) -> None:
    """What the runner does after every batch: store the returned entity mappings once."""
    from celerp.models.migration import MigrationEntityMap

    for m in result.mappings:
        stored = (await session.execute(select(MigrationEntityMap).where(
            MigrationEntityMap.migration_run_id == run_id,
            MigrationEntityMap.source_type == m.source_type,
            MigrationEntityMap.source_external_id == m.source_external_id,
        ))).scalar_one_or_none()
        if stored is None:
            session.add(MigrationEntityMap(
                migration_run_id=run_id,
                source_type=m.source_type,
                source_external_id=m.source_external_id,
                target_entity_type=m.target_entity_type,
                target_entity_id=m.target_entity_id,
                status=m.status,
            ))
    await session.flush()


async def _ledger_rows(session, company_id) -> int:
    from celerp.models.ledger import LedgerEntry

    return (await session.execute(
        select(func.count()).select_from(LedgerEntry).where(LedgerEntry.company_id == company_id)
    )).scalar_one()


@pytest.mark.asyncio
async def test_internal_sinks_share_domain_import_services(client, session, monkeypatch):
    from celerp.importers.schema import (
        CIFAccount, CIFContact, CIFDocument, CIFItem, CIFJournalEntry, CIFJournalLine,
        CIFLineItem, CIFLocation,
    )
    from celerp.importers.sinks import sink_for
    from celerp.models.projections import Projection
    from celerp_accounting import import_service as accounting_import
    from celerp_contacts import services as contact_services
    from celerp_docs import import_service as doc_import
    from celerp_inventory import services as inventory_services

    headers, context = await _sink_context(client, session, _no_attachments)
    company_id = context.company_id

    calls: list[str] = []
    _spy(monkeypatch, contact_services, "import_contact_records", calls)
    _spy(monkeypatch, accounting_import, "import_journal_records", calls)
    _spy(monkeypatch, inventory_services, "write_import_batch", calls)
    _spy(monkeypatch, doc_import, "import_doc_records", calls)

    async def via_sink(group: str, records: list):
        result = await sink_for(group).import_batch(context, records)
        await _persist_mappings(session, context.run_id, result)
        return result

    # Core: a location is created once and maps to its Celerp location id.
    location = CIFLocation(**_PROVENANCE, source_type="Location", source_external_id="loc-1", name="Main store")
    result = await via_sink("locations", [location])
    assert (result.created, result.skipped, result.errors) == (1, 0, [])
    assert result.mappings[0].target_entity_type == "location"

    # Contacts: the route and the sink both run the contacts import service.
    calls.clear()
    r = await client.post("/crm/contacts/import/batch", headers=headers, json={"records": [{
        "entity_id": "contact:route-1", "event_type": "crm.contact.created",
        "data": {"name": "Route Customer"}, "source": "import", "idempotency_key": "route-contact-1",
    }]})
    assert r.status_code == 200, r.text
    assert r.json() == {"created": 1, "skipped": 0, "updated": 0, "errors": []}
    assert calls == ["import_contact_records"]
    calls.clear()
    customer = CIFContact(
        **_PROVENANCE, source_type="Customer", source_external_id="cust-1", name="Harbor Traders",
        roles=["customer"], email="billing@harbor.example",
    )
    result = await via_sink("contacts", [customer])
    assert calls == ["import_contact_records"]
    assert (result.created, result.skipped, result.errors) == (1, 0, [])
    contact_id = result.mappings[0].target_entity_id
    contact = await session.get(Projection, (company_id, contact_id))
    assert contact.state["name"] == "Harbor Traders"
    assert contact.state["contact_type"] == "customer"

    # Accounting: the route and the sink share one journal writer, including its
    # refusal of a line naming a contact that does not exist.
    calls.clear()
    r = await client.post("/accounting/import/batch", headers=headers, json={"records": [{
        "entity_id": "je:route-1", "event_type": "acc.journal_entry.created",
        "data": {"memo": "Route entry", "ts": "2026-01-02", "entries": [
            {"account": "1120", "debit": 10, "credit": 0, "contact": "contact:missing"},
            {"account": "4100", "debit": 0, "credit": 10},
        ]},
        "source": "import", "idempotency_key": "route-je-1",
    }]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["created"] == 0 and t("error.contacts_not_found", "en", names="contact:missing") in body["errors"][0]
    assert calls == ["import_journal_records"]
    receivable = CIFAccount(
        **_PROVENANCE, source_type="Account", source_external_id="acct-ar", code="1100",
        name="Accounts receivable", account_type="asset", control="receivable",
    )
    sales = CIFAccount(
        **_PROVENANCE, source_type="Account", source_external_id="acct-sales", code="4000",
        name="Sales", account_type="revenue",
    )
    result = await via_sink("accounts", [receivable, sales])
    assert (result.created, result.errors) == (2, [])
    codes = {m.source_external_id: m.target_entity_id for m in result.mappings}
    assert codes["acct-ar"] == "1120"
    calls.clear()
    journal = CIFJournalEntry(
        **_PROVENANCE, source_type="JournalEntry", source_external_id="je-1", entry_date="2026-01-03",
        narration="Opening receivable", lines=[
            CIFJournalLine(account_external_id="acct-ar", debit=Decimal("25.50"), contact_external_id="cust-1"),
            CIFJournalLine(account_external_id="acct-sales", credit=Decimal("25.50")),
        ],
    )
    result = await via_sink("journals", [journal])
    assert calls == ["import_journal_records"]
    assert (result.created, result.skipped, result.errors) == (1, 0, [])
    je = await session.get(Projection, (company_id, result.mappings[0].target_entity_id))
    assert [(e["account"], e.get("contact")) for e in je.state["entries"]] == [
        ("1120", contact_id), (codes["acct-sales"], None),
    ]
    orphan = journal.model_copy(update={"source_external_id": "je-2", "lines": [
        CIFJournalLine(account_external_id="acct-ar", debit=Decimal("1"), contact_external_id="cust-unknown"),
        CIFJournalLine(account_external_id="acct-sales", credit=Decimal("1")),
    ]})
    result = await via_sink("journals", [orphan])
    assert result.created == 0 and result.errors[0].source_external_id == "je-2"

    # Inventory: the route transport and the sink share the item writer.
    calls.clear()
    r = await client.post("/items/import/batch", headers=headers, json={"records": [{
        "entity_id": f"item:{uuid.uuid4()}", "event_type": "item.created",
        "data": {"sku": "ROUTE-1", "name": "Route item", "sell_by": "piece", "quantity": 1},
        "source": "import", "idempotency_key": "route-item-1",
    }]})
    assert r.status_code == 200, r.text
    assert r.json()["created"] == 1
    assert calls == ["write_import_batch"]
    calls.clear()
    item = CIFItem(
        **_PROVENANCE, source_type="InventoryItem", source_external_id="item-1", sku="WIDGET-1",
        name="Widget", status="available", retail_price=Decimal("12.00"), location_name="Main store",
    )
    result = await via_sink("items", [item])
    assert calls == ["write_import_batch"]
    assert (result.created, result.skipped, result.errors) == (1, 0, [])
    item_row = await session.get(Projection, (company_id, result.mappings[0].target_entity_id))
    assert item_row.state["sku"] == "WIDGET-1"

    # Documents: the route and the sink share the document import service.
    calls.clear()
    r = await client.post("/docs/import/batch", headers=headers, json={"records": [{
        "entity_id": f"doc:{uuid.uuid4()}", "event_type": "doc.created",
        "data": {"doc_type": "invoice", "status": "draft", "total": 5, "line_items": []},
        "source": "import", "idempotency_key": "route-doc-1",
    }]})
    assert r.status_code == 200, r.text
    assert r.json()["created"] == 1
    assert calls == ["import_doc_records"]
    calls.clear()
    invoice = CIFDocument(
        **_PROVENANCE, source_type="SalesInvoice", source_external_id="inv-1", doc_type="invoice",
        status="draft", contact_external_id="cust-1", ref="INV-1", issue_date="2026-01-04",
        total=Decimal("12.00"), amount_paid=Decimal("0"), amount_outstanding=Decimal("12.00"),
        line_items=[CIFLineItem(
            item_external_id="item-1", description="Widget", quantity=Decimal("1"),
            unit_price=Decimal("12.00"), total_price=Decimal("12.00"),
        )],
    )
    result = await via_sink("documents", [invoice])
    assert calls == ["import_doc_records"]
    assert (result.created, result.skipped, result.errors) == (1, 0, [])
    doc = await session.get(Projection, (company_id, result.mappings[0].target_entity_id))
    assert doc.state["contact_id"] == contact_id
    assert doc.state["doc_type"] == "invoice"

    # A replayed batch is idempotent through the per-company keys: every record
    # skips to the same target and no ledger row is written.
    before = await _ledger_rows(session, company_id)
    first_targets = {}
    for group, records in (
        ("contacts", [customer]), ("journals", [journal]), ("items", [item]), ("documents", [invoice]),
    ):
        replay = await sink_for(group).import_batch(context, records)
        assert (replay.created, replay.skipped, replay.errors) == (0, 1, []), group
        first_targets[group] = replay.mappings[0].target_entity_id
    assert first_targets["contacts"] == contact_id
    assert await _ledger_rows(session, company_id) == before


_PNG = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02"
    b"\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00"
    b"\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
)
_PDF = b"%PDF-1.4\n%synthetic\n"


@pytest.mark.asyncio
async def test_core_sink_attaches_source_files_like_the_upload_routes(client, session, monkeypatch, tmp_path):
    import hashlib

    from celerp.config import settings
    from celerp.importers.adapters.base import ScanError
    from celerp.importers.schema import CIFAttachment, CIFContact, CIFDocument, CIFItem
    from celerp.importers.sinks import sink_for
    from celerp.models.projections import Projection

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    files = {"att-doc": _PDF, "att-contact": _PDF, "att-item": _PNG, "att-hash": _PDF, "att-exe": b"MZ\x90\x00"}
    read_keys: list[str] = []

    def read_attachment(key: str) -> bytes:
        read_keys.append(key)
        if key not in files:
            raise ScanError("No such attachment in this business file.")
        return files[key]

    _, context = await _sink_context(client, session, read_attachment)
    company_id = context.company_id

    async def via_sink(group: str, records: list):
        result = await sink_for(group).import_batch(context, records)
        await _persist_mappings(session, context.run_id, result)
        return result

    customer = CIFContact(**_PROVENANCE, source_type="Customer", source_external_id="cust-1", name="Harbor Traders",
                          roles=["customer"])
    item = CIFItem(**_PROVENANCE, source_type="InventoryItem", source_external_id="item-1", sku="WIDGET-1",
                   name="Widget", status="available")
    invoice = CIFDocument(**_PROVENANCE, source_type="SalesInvoice", source_external_id="inv-1", doc_type="invoice",
                          status="draft", contact_external_id="cust-1", ref="INV-1", issue_date="2026-01-04",
                          total=Decimal("0"), amount_paid=Decimal("0"), amount_outstanding=Decimal("0"))
    targets = {}
    for group, record in (("contacts", customer), ("items", item), ("documents", invoice)):
        result = await via_sink(group, [record])
        assert result.errors == [], group
        targets[record.source_external_id] = result.mappings[0].target_entity_id

    def attachment(key: str, name: str, target_type: str, target: str, content_type: str | None, sha: str | None = None):
        return CIFAttachment(**_PROVENANCE, source_type="Attachment", source_external_id=key, file_name=name,
                             declared_content_type=content_type, size_bytes=len(files.get(key, b"")),
                             sha256=sha or hashlib.sha256(files.get(key, b"")).hexdigest(),
                             target_source_type=target_type, target_source_external_id=target)

    records = [
        attachment("att-doc", "invoice.pdf", "SalesInvoice", "inv-1", "application/pdf"),
        attachment("att-contact", "terms.pdf", "Customer", "cust-1", None),
        attachment("att-item", "widget.png", "InventoryItem", "item-1", "image/png"),
        attachment("att-missing", "lost.pdf", "Customer", "cust-unknown", "application/pdf"),
        attachment("att-hash", "changed.pdf", "Customer", "cust-1", "application/pdf", sha="0" * 64),
        attachment("att-exe", "setup.exe", "Customer", "cust-1", "application/x-msdownload"),
    ]
    result = await via_sink("attachments", records)

    # Each file lands on its target the way the upload routes attach it; an unsafe or
    # unmatched file is rejected on its own, with its reason, and nothing else is.
    assert (result.created, result.skipped) == (3, 0)
    errors = {e.source_external_id: e.message for e in result.errors}
    assert set(errors) == {"att-missing", "att-hash", "att-exe"}
    assert "target record was not imported" in errors["att-missing"]
    assert "does not match its recorded hash" in errors["att-hash"]
    assert "can't be attached" in errors["att-exe"]
    assert "att-missing" not in read_keys
    doc = await session.get(Projection, (company_id, targets["inv-1"]))
    contact = await session.get(Projection, (company_id, targets["cust-1"]))
    item_row = await session.get(Projection, (company_id, targets["item-1"]))
    assert [(f["filename"], f["mime"]) for f in doc.state["files"]] == [("invoice.pdf", "application/pdf")]
    assert [(f["filename"], f["mime"]) for f in contact.state["files"]] == [("terms.pdf", "application/pdf")]
    [image] = item_row.state["files"]
    assert (image["filename"], image["is_hero"], image["document_tag"]) == ("widget.png", True, "product_images")
    stored = {m.source_external_id: m.target_entity_id for m in result.mappings}
    assert stored["att-doc"] == doc.state["files"][0]["id"]
    assert (tmp_path / "static" / "attachments" / str(company_id)).is_dir()

    # A replayed batch attaches nothing twice and maps each file to the same stored file.
    before = await _ledger_rows(session, company_id)
    read_keys.clear()
    replay = await sink_for("attachments").import_batch(context, records)
    assert (replay.created, replay.skipped, len(replay.errors)) == (0, 3, 3)
    assert {m.source_external_id: m.target_entity_id for m in replay.mappings} == stored
    assert not {"att-doc", "att-contact", "att-item"} & set(read_keys)
    assert await _ledger_rows(session, company_id) == before
