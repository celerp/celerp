# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A migration brings the source's own chart, never Celerp's default one.

Control accounts keep their source codes, a code already taken by another account is
never reused, and the source controls are recorded so the books can be mapped when the
migration finishes. Imported documents and stock sit on the source's own receivable,
payable and inventory accounts.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import func, select

from test_migration_sinks import _PROVENANCE, _no_attachments, _persist_mappings


async def _staged_context(session):
    """A sink context for a migration into a freshly staged company, as migration start creates it."""
    import uuid

    from celerp.importers.sinks import SinkContext, register_sink
    from celerp.models.company import Company, User
    from celerp.models.migration import MigrationRun
    from celerp.services import migration_core_sink
    from celerp.services.provisioning import provision_migration_company

    user = User(id=uuid.uuid4(), email=f"owner-{uuid.uuid4().hex[:8]}@example.com", name="Owner",
                auth_hash="x", is_active=True)
    session.add(user)
    await session.flush()
    company = await provision_migration_company(session, owner=user, company_name="Source Books")
    run = MigrationRun(
        company_id=company.id, created_by_user_id=user.id, scan_claim_sha256=uuid.uuid4().hex * 2,
        source_system="manager_io", source_artifact_sha256="0" * 64, adapter_version="test", cif_version="2",
        mode="full_history",
    )
    session.add(run)
    await session.flush()
    register_sink(migration_core_sink.SINK)
    assert (await session.get(Company, company.id)).is_migration_staged
    return SinkContext(session=session, company_id=company.id, user_id=user.id, run_id=run.id,
                       read_attachment=_no_attachments)


async def _via(context, group: str, records: list):
    from celerp.importers.sinks import sink_for

    result = await sink_for(group).import_batch(context, records)
    await _persist_mappings(context.session, context.run_id, result)
    return result


def _account(ext: str, code: str, name: str, account_type: str, control: str | None = None, parent: str | None = None):
    from celerp.importers.schema import CIFAccount

    return CIFAccount(**_PROVENANCE, source_type="Account", source_external_id=ext, code=code, name=name,
                      account_type=account_type, control=control, parent_external_id=parent)


_SOURCE_CHART = [
    ("assets", "100", "Assets", "asset", None),
    ("ar", "120", "Trade debtors", "asset", "receivable"),
    ("stock", "130", "Stock on hand", "asset", "inventory"),
    ("vat-in", "150", "VAT recoverable", "asset", "tax"),
    ("ap", "210", "Trade creditors", "liability", "payable"),
    ("vat-out", "220", "VAT payable", "liability", "tax"),
    ("re", "320", "Retained profits", "equity", "retained_earnings"),
    ("sales", "400", "Sales", "revenue", None),
    ("cos", "500", "Cost of sales", "cogs", None),
]


async def _import_chart(context, rows=_SOURCE_CHART):
    header = "assets" if any(ext == "assets" for ext, *_ in rows) else None
    result = await _via(context, "accounts", [
        _account(ext, code, name, t, control, header if t == "asset" and ext != "assets" else None)
        for ext, code, name, t, control in rows])
    assert result.errors == [], result.errors
    return {m.source_external_id: m.target_entity_id for m in result.mappings}


async def _controls(context) -> dict:
    from celerp.accounting_roles import SOURCE_CONTROLS_KEY
    from celerp.models.company import Company

    company = await context.session.get(Company, context.company_id, populate_existing=True)
    return (company.settings or {}).get(SOURCE_CONTROLS_KEY)


@pytest.mark.asyncio
async def test_a_migrated_chart_holds_only_the_sources_accounts_under_their_own_codes(session):
    from celerp_accounting.models import Account

    context = await _staged_context(session)
    codes = await _import_chart(context)
    held = set((await session.execute(select(Account.code).where(
        Account.company_id == context.company_id))).scalars())
    assert held == {code for _, code, *_ in _SOURCE_CHART}
    assert codes == {ext: code for ext, code, *_ in _SOURCE_CHART}
    assert await _controls(context) == {
        "receivable": ["120"], "inventory_purchased": ["130"], "tax_input": ["150"],
        "payable": ["210"], "tax_output": ["220"], "retained_earnings": ["320"]}


@pytest.mark.asyncio
async def test_a_control_whose_code_another_account_holds_gets_its_own_code(session):
    from celerp_accounting.models import Account

    context = await _staged_context(session)
    await _import_chart(context, [("prepaid", "120", "Prepayments", "asset", None)])
    codes = await _import_chart(context, [("ar", "120", "Trade debtors", "asset", "receivable")])
    assert codes["ar"] == "120-1"
    prepaid = (await session.execute(select(Account).where(
        Account.company_id == context.company_id, Account.code == "120"))).scalar_one()
    assert prepaid.name == "Prepayments"
    assert await _controls(context) == {"receivable": ["120-1"]}
    count = (await session.execute(select(func.count()).select_from(Account).where(
        Account.company_id == context.company_id))).scalar()
    assert count == 2


@pytest.mark.asyncio
async def test_re_importing_the_chart_records_each_control_once(session):
    context = await _staged_context(session)
    await _import_chart(context)
    await _import_chart(context)
    assert (await _controls(context))["receivable"] == ["120"]


async def _customer_and_invoice(context, *, doc_type="invoice", total="50.00"):
    from celerp.importers.schema import CIFContact, CIFDocument, CIFLineItem

    contact = CIFContact(**_PROVENANCE, source_type="Customer", source_external_id="party-1", name="Harbor Traders",
                         roles=["customer", "supplier"])
    assert (await _via(context, "contacts", [contact])).errors == []
    doc = CIFDocument(
        **_PROVENANCE, source_type="SalesInvoice" if doc_type == "invoice" else "PurchaseInvoice",
        source_external_id=f"{doc_type}-1", doc_type=doc_type, status="awaiting_payment",
        contact_external_id="party-1", ref="DOC-1", issue_date="2026-01-04",
        total=Decimal(total), amount_paid=Decimal("0"), amount_outstanding=Decimal(total),
        line_items=[CIFLineItem(description="Service", account_external_id="sales" if doc_type == "invoice" else "cos",
                                quantity=Decimal("1"), unit_price=Decimal(total), total_price=Decimal(total))],
    )
    return await _via(context, "documents", [doc])


async def _entries(context, je_id: str) -> list[dict]:
    from celerp.models.projections import Projection

    row = await context.session.get(Projection, (context.company_id, je_id))
    return list((row.state if row is not None else {}).get("entries") or [])


@pytest.mark.asyncio
async def test_an_imported_invoice_sits_on_the_sources_receivable_account(session):
    context = await _staged_context(session)
    await _import_chart(context)
    result = await _customer_and_invoice(context)
    assert result.errors == []
    entries = await _entries(context, f"je:auto:{result.mappings[0].target_entity_id}:fin")
    assert sorted((e["account"], e["debit"], e["credit"]) for e in entries) == [
        ("120", 50.0, 0.0), ("400", 0.0, 50.0)]


@pytest.mark.asyncio
async def test_an_imported_bill_sits_on_the_sources_payable_account(session):
    context = await _staged_context(session)
    await _import_chart(context)
    result = await _customer_and_invoice(context, doc_type="bill")
    assert result.errors == []
    entries = await _entries(context, f"je:auto:{result.mappings[0].target_entity_id}:bill")
    assert sorted((e["account"], e["debit"], e["credit"]) for e in entries) == [
        ("210", 0.0, 50.0), ("500", 50.0, 0.0)]


@pytest.mark.asyncio
async def test_an_imported_document_is_refused_when_the_source_has_no_single_receivable_account(session):
    context = await _staged_context(session)
    await _import_chart(context, [*_SOURCE_CHART, ("ar2", "121", "Other debtors", "asset", "receivable")])
    result = await _customer_and_invoice(context)
    assert result.created == 0
    assert "more than one receivable account" in result.errors[0].message


@pytest.mark.asyncio
async def test_migrated_stock_records_the_sources_inventory_account(session):
    from celerp.importers.schema import CIFItem
    from celerp.models.projections import Projection

    context = await _staged_context(session)
    await _import_chart(context)
    item = CIFItem(**_PROVENANCE, source_type="InventoryItem", source_external_id="item-1", sku="WID-1",
                   name="Widget", status="available", total_cost=Decimal("40"))
    result = await _via(context, "items", [item])
    assert result.errors == []
    row = await session.get(Projection, (context.company_id, result.mappings[0].target_entity_id))
    assert row.state["inventory_account_code"] == "130"
