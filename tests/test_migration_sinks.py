# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""The migration sinks write through the same domain services as the batch routes."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from test_helpers import register_admin

_PROVENANCE = {"source_system": "manager_io"}


def _spy(monkeypatch, module, name: str, calls: list[str]) -> None:
    """Record every call to `module.name` while still running the real service."""
    real = getattr(module, name)

    async def recording(*args, **kwargs):
        calls.append(name)
        return await real(*args, **kwargs)

    monkeypatch.setattr(module, name, recording)


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
    from celerp.importers.sinks import SinkContext, register_sink, sink_for
    from celerp.models.accounting import UserCompany
    from celerp.models.company import User
    from celerp.models.migration import MigrationRun
    from celerp.models.projections import Projection
    from celerp.services import migration_core_sink
    from celerp_accounting import import_service as accounting_import
    from celerp_contacts import services as contact_services
    from celerp_docs import import_service as doc_import
    from celerp_inventory import services as inventory_services

    token = await register_admin(client)
    headers = {"Authorization": f"Bearer {token}"}
    user = (await session.execute(select(User).where(User.email == "admin@perm.example"))).scalar_one()
    company_id = (await session.execute(
        select(UserCompany.company_id).where(UserCompany.user_id == user.id)
    )).scalar_one()
    run = MigrationRun(
        company_id=company_id, created_by_user_id=user.id, source_system="manager_io",
        source_artifact_sha256="0" * 64, adapter_version="test", cif_version="2", mode="full_history",
    )
    session.add(run)
    await session.flush()
    context = SinkContext(session=session, company_id=company_id, user_id=user.id, run_id=run.id)
    register_sink(migration_core_sink.SINK)

    calls: list[str] = []
    _spy(monkeypatch, contact_services, "import_contact_records", calls)
    _spy(monkeypatch, accounting_import, "import_journal_records", calls)
    _spy(monkeypatch, inventory_services, "write_import_batch", calls)
    _spy(monkeypatch, doc_import, "import_doc_records", calls)

    async def via_sink(group: str, records: list):
        result = await sink_for(group).import_batch(context, records)
        await _persist_mappings(session, run.id, result)
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
    assert body["created"] == 0 and "No contact matches contact:missing" in body["errors"][0]
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
