# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A migration brings the source's own chart, never Celerp's default one.

Control accounts keep their source codes, a code already taken by another account is
never reused, and the source controls are recorded so the books can be mapped when the
migration finishes. Imported documents and stock sit on the source's own receivable,
payable and inventory accounts.
"""
from __future__ import annotations

import uuid
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


_TWO_REVENUE_CHART = [*_SOURCE_CHART, ("sales-a", "4000", "Product sales", "revenue", None),
                      ("sales-b", "4010", "Service sales", "revenue", None)]


async def _imported_invoice(context, split: dict[str, str], *, ext: str = "inv-1") -> str:
    """An imported invoice whose lines post ``split`` ({source account: net}) in the source
    books, in a company whose posting accounts are chosen as finishing the migration
    chooses them. Its Celerp id."""
    from celerp.importers.schema import CIFContact, CIFDocument, CIFLineItem
    from celerp.services.account_roles import current_settings, role_map
    from celerp.services.posting_readiness import apply_choices, readiness
    from migration_support import posting_choices_from

    contact = CIFContact(**_PROVENANCE, source_type="Customer", source_external_id="party-1", name="Harbor Traders",
                         roles=["customer"])
    assert (await _via(context, "contacts", [contact])).errors == []
    total = sum(Decimal(v) for v in split.values())
    doc = CIFDocument(
        **_PROVENANCE, source_type="SalesInvoice", source_external_id=ext, doc_type="invoice",
        status="awaiting_payment", contact_external_id="party-1", ref=ext.upper(), issue_date="2026-01-04",
        total=total, amount_paid=Decimal("0"), amount_outstanding=total,
        line_items=[CIFLineItem(description=account, account_external_id=account, quantity=Decimal("1"),
                                unit_price=Decimal(net), total_price=Decimal(net)) for account, net in split.items()],
    )
    result = await _via(context, "documents", [doc])
    assert result.errors == [], result.errors
    if not role_map(await current_settings(context.session, context.company_id)):
        choices = posting_choices_from(await readiness(context.session, context.company_id) or [])
        await apply_choices(context.session, context.company_id, choices)
    return result.mappings[0].target_entity_id


async def _credit_note_revenue(context, invoice: str, total: float, *, release: float | None = None) -> dict[str, float]:
    """What a credit note of ``total`` on ``invoice`` puts on each revenue account (debit
    positive), or with ``release`` what releasing that much of it takes back. The books
    balance after it."""
    from celerp.models.projections import Projection
    from celerp.services import auto_je
    from celerp.services.auto_je import company_currency

    base = await company_currency(context.session, context.company_id)
    note = f"doc:CN-{uuid.uuid4().hex[:6]}"
    doc = {"doc_type": "credit_note", "original_doc_id": invoice, "currency": base, "total": total, "tax": 0}
    await auto_je.create_for_credit_note_finalized(context.session, company_id=context.company_id,
                                                   user_id=context.user_id, doc_id=note, doc=doc, base_currency=base)
    je_id = f"je:auto:{note}:fin"
    if release is not None:
        await auto_je.release_credit_note_open(context.session, company_id=context.company_id, user_id=context.user_id,
                                               doc_id=note, doc=doc, amount=release, base_currency=base)
        je_id = f"je:auto:{note}:release"
    debit = credit = Decimal(0)
    for row in (await context.session.execute(select(Projection).where(
            Projection.company_id == context.company_id, Projection.entity_type == "journal_entry"))).scalars():
        if (row.state or {}).get("status") == "posted":
            for e in row.state.get("entries") or []:
                debit += Decimal(str(e.get("debit") or 0))
                credit += Decimal(str(e.get("credit") or 0))
    assert debit == credit, f"trial balance off: debits {debit}, credits {credit}"
    out: dict[str, float] = {}
    for e in await _entries(context, je_id):
        if e["account"] in ("4000", "4010", "400"):
            out[e["account"]] = out.get(e["account"], 0.0) + e["debit"] - e["credit"]
    return out


@pytest.mark.asyncio
async def test_a_credit_note_on_an_imported_invoice_reverses_each_revenue_account_it_was_sold_on(session):
    """An invoice the source books split 60/40 over two revenue accounts is credited back
    60/40 on those accounts, never all on the account sales post to today."""
    context = await _staged_context(session)
    await _import_chart(context, _TWO_REVENUE_CHART)
    invoice = await _imported_invoice(context, {"sales-a": "60.00", "sales-b": "40.00"})
    assert await _credit_note_revenue(context, invoice, 100.0) == {"4000": 60.0, "4010": 40.0}
    # Part of it splits the same way, to the cent.
    assert await _credit_note_revenue(context, invoice, 25.0) == {"4000": 15.0, "4010": 10.0}
    # Releasing an open credit takes it back off the same accounts.
    assert await _credit_note_revenue(context, invoice, 100.0, release=50.0) == {"4000": -30.0, "4010": -20.0}


@pytest.mark.asyncio
async def test_a_credit_note_on_an_imported_invoice_with_a_discount_line_reverses_the_discount_too(session):
    """An invoice that sold 100 on 4000 and 20 on 400 and gave a 20 discount on 4010 nets 100.
    Credited in full, every revenue account goes back to where it was, the discount
    included: the discount account is credited back, never left out with the rest spread
    over the other accounts."""
    context = await _staged_context(session)
    await _import_chart(context, _TWO_REVENUE_CHART)
    invoice = await _imported_invoice(context, {"sales-a": "100.00", "sales-b": "-20.00", "sales": "20.00"})
    assert await _credit_note_revenue(context, invoice, 100.0) == {"400": 20.0, "4000": 100.0, "4010": -20.0}
    # Half of it moves each account half as far, to the cent.
    assert await _credit_note_revenue(context, invoice, 50.0) == {"400": 10.0, "4000": 50.0, "4010": -10.0}
    # Releasing an open credit takes it back off the same accounts.
    assert await _credit_note_revenue(context, invoice, 100.0, release=100.0) == {
        "400": -20.0, "4000": -100.0, "4010": 20.0}


@pytest.mark.asyncio
async def test_a_credit_note_on_an_imported_invoice_sold_on_an_account_sales_do_not_post_to(session):
    from celerp.services.account_roles import current_settings, role_map

    context = await _staged_context(session)
    await _import_chart(context, _TWO_REVENUE_CHART)
    invoice = await _imported_invoice(context, {"sales-b": "100.00"})
    assert role_map(await current_settings(session, context.company_id)).get("sales_revenue") != "4010"
    assert await _credit_note_revenue(context, invoice, 100.0) == {"4010": 100.0}


@pytest.mark.asyncio
async def test_a_credit_note_after_sales_move_to_another_account_reverses_where_the_invoice_sold(session):
    from celerp.models.company import Company
    from celerp.services.account_roles import current_settings, role_map, with_role

    context = await _staged_context(session)
    await _import_chart(context, _TWO_REVENUE_CHART)
    on_role = await _imported_invoice(context, {"sales": "100.00"}, ext="inv-1")
    split = await _imported_invoice(context, {"sales-a": "60.00", "sales-b": "40.00"}, ext="inv-2")
    settings = await current_settings(session, context.company_id)
    assert role_map(settings).get("sales_revenue") == "400"
    company = await session.get(Company, context.company_id)
    company.settings = with_role(settings, "sales_revenue", "4010")
    await session.flush()
    assert await _credit_note_revenue(context, on_role, 100.0) == {"400": 100.0}
    assert await _credit_note_revenue(context, split, 100.0) == {"4000": 60.0, "4010": 40.0}
