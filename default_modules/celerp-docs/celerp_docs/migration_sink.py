# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Migration sink for operational documents and their settlements.

Documents are written by the same import service as the document batch route and
settlements by the same payment service as the record-payment route. An issued
document posts the entry its source books carry, on the accounts its lines name,
in place of Celerp's default posting. An issued document arrives unpaid; its paid
state comes from the settlements imported after it and from the credit and debit
notes applied to it. A debit note has no Celerp document: its entry posts on the
bill it notes as a journal fallback, applied to the bill as a payment.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from decimal import Decimal

from celerp.importers.results import RecordOutcome
from celerp.importers.schema import (
    CIFDocument,
    CIFSettlement,
    CIFSourceRecord,
    DocumentType,
    ReconciliationExpectations,
    ReconciliationMeasure,
    SettlementType,
)
from celerp.importers.sinks import DestinationMeasurement, SinkBatchResult, SinkContext
from celerp.models.company import Company
from celerp.models.projections import Projection
from celerp.services import auto_je
from celerp.services.migration_core_sink import (
    acting_member,
    deterministic_id,
    import_prepared,
    mapped_targets,
    run_targets,
    sink_result,
)
from celerp.services.money import round_money, to_stored_float
from celerp_docs import import_service
from celerp_docs.import_service import DOC_CREATED
from celerp_docs.routes import DocImportRecord, apply_credit_note, apply_doc_payment

DOC = "doc"
JOURNAL = "journal_entry"
SETTLEMENT = "settlement"
ITEM = "item"
CONTACT = "contact"
ACCOUNT = "account"
TAX = "tax"
PAYABLE_CODE = "2110"

# Celerp status of an imported document, by type and source status. Issued sales
# and purchase documents arrive unpaid; orders and quotes carry no money.
_STATUS = {
    DocumentType.INVOICE: {"draft": "draft", "awaiting_payment": "final", "paid": "final", "void": "void"},
    DocumentType.CREDIT_NOTE: {"draft": "draft", "awaiting_payment": "final", "paid": "final", "void": "void"},
    DocumentType.BILL: {"draft": "draft", "awaiting_payment": "awaiting_payment", "paid": "awaiting_payment", "void": "void"},
    DocumentType.PURCHASE_ORDER: {"draft": "draft", "awaiting_payment": "sent", "paid": "sent", "void": "void"},
    DocumentType.QUOTATION: {"draft": "draft", "awaiting_payment": "sent", "paid": "sent", "void": "void"},
}
# Sign of a line's net and tax on the ledger, debit positive, for documents that post.
_LINE_SIGN = {DocumentType.INVOICE: -1, DocumentType.CREDIT_NOTE: 1, DocumentType.BILL: 1, DocumentType.DEBIT_NOTE: -1}
_SETTLES = {SettlementType.RECEIPT: DocumentType.INVOICE, SettlementType.PAYMENT: DocumentType.BILL}
_SETTLEMENT_KIND = {DocumentType.INVOICE.value: SettlementType.RECEIPT.value, DocumentType.BILL.value: SettlementType.PAYMENT.value}
# Source status a Celerp status reconciles against.
_SOURCE_STATUS = {"paid": "paid", "draft": "draft", "void": "void"}
_DOC_MEASURES = {
    ReconciliationMeasure.DOCUMENT_COUNT,
    ReconciliationMeasure.DOCUMENT_TOTAL,
    ReconciliationMeasure.DOCUMENT_STATUS,
    ReconciliationMeasure.SETTLEMENT_ALLOCATION,
}


class DocsMigrationSink:
    key = "celerp-docs"
    groups = frozenset({"documents", "settlements"})
    batch_size = 500

    async def import_batch(self, context: SinkContext, records: Sequence[CIFSourceRecord]) -> SinkBatchResult:
        if all(isinstance(r, CIFSettlement) for r in records):
            return sink_result(records, await _import_settlements(context, records), SETTLEMENT)
        return sink_result(records, await _import_documents(context, records), DOC)

    async def reconcile(
        self, context: SinkContext, expectations: ReconciliationExpectations
    ) -> list[DestinationMeasurement]:
        """Count, total and status per document type, and settlement allocations per
        kind, over the documents this run imported."""
        wanted = [e for e in expectations.expectations if e.measure in _DOC_MEASURES]
        if not wanted:
            return []
        counts: dict[tuple[str, str], Decimal] = defaultdict(Decimal)
        totals: dict[tuple[str, str], Decimal] = defaultdict(Decimal)
        statuses: dict[str, Decimal] = defaultdict(Decimal)
        allocated: dict[tuple[str, str], Decimal] = defaultdict(Decimal)
        for doc_id in await run_targets(context, DOC):
            row = await context.session.get(Projection, (context.company_id, doc_id))
            if row is None:
                continue
            state = row.state or {}
            doc_type, currency = str(state.get("doc_type") or ""), str(state.get("currency") or "")
            counts[(doc_type, currency)] += 1
            totals[(doc_type, currency)] += Decimal(str(state.get("total") or 0))
            status = str(state.get("status") or "")
            statuses[f"{doc_type}:{_SOURCE_STATUS.get(status, 'awaiting_payment')}"] += 1
            for payment in state.get("payments", []):
                if payment.get("method") == "migration" and payment.get("status") == "active":
                    kind = _SETTLEMENT_KIND.get(doc_type, doc_type)
                    allocated[(kind, str(payment.get("currency") or currency))] += Decimal(str(payment["amount"]))
        out = []
        for e in wanted:
            if e.measure == ReconciliationMeasure.DOCUMENT_COUNT:
                actual = counts.get((e.key, e.currency or ""), Decimal(0))
            elif e.measure == ReconciliationMeasure.DOCUMENT_TOTAL:
                actual = totals.get((e.key, e.currency or ""), Decimal(0))
            elif e.measure == ReconciliationMeasure.DOCUMENT_STATUS:
                actual = statuses.get(e.key, Decimal(0))
            else:
                actual = allocated.get((e.key, e.currency or ""), Decimal(0))
            out.append(DestinationMeasurement(e.measure, e.key, e.currency, actual))
        return out


async def _tax_rates(context: SinkContext, docs: Sequence[CIFDocument]) -> dict[str, tuple[str, float]]:
    """Source tax code -> (Celerp tax name, rate) for the tax codes this run imported."""
    names = await mapped_targets(context, TAX, [li.tax_code_external_id for d in docs for li in d.line_items])
    company = await context.session.get(Company, context.company_id)
    rates = {str(t.get("name", "")).strip().lower(): (t.get("name"), float(t.get("rate") or 0))
             for t in (company.settings or {}).get("taxes") or []}
    return {code: rates[name.strip().lower()] for code, name in names.items() if name.strip().lower() in rates}


async def _base_currency(context: SinkContext) -> str:
    company = await context.session.get(Company, context.company_id)
    return str((company.settings or {}).get("currency") or "USD").upper()


def _money(amount: Decimal, currency: str) -> float:
    return to_stored_float(round_money(amount, currency))


# ── Documents ─────────────────────────────────────────────────────────────────

async def _import_documents(context: SinkContext, records: Sequence[CIFSourceRecord]) -> list[RecordOutcome]:
    """Write the batch's documents, post their entries, then apply its notes."""
    member = await acting_member(context)
    base = await _base_currency(context)
    docs = [r for r in records if isinstance(r, CIFDocument)]
    contacts = await mapped_targets(context, CONTACT, [d.contact_external_id for d in docs])
    items = await mapped_targets(context, ITEM, [li.item_external_id for d in docs for li in d.line_items])
    accounts = await mapped_targets(context, ACCOUNT, [
        a for d in docs for li in d.line_items for a in (li.account_external_id, li.tax_account_external_id)
    ])
    taxes = await _tax_rates(context, docs)
    names = {}
    for contact_id in contacts.values():
        row = await context.session.get(Projection, (context.company_id, contact_id))
        names[contact_id] = (row.state or {}).get("name") if row else None
    prepared = [
        "" if isinstance(r, CIFDocument) and r.doc_type == DocumentType.DEBIT_NOTE
        else _doc_record(context, r, base, contacts, items, accounts, taxes, names)
        for r in records
    ]

    async def write(ready: list[DocImportRecord]):
        return await import_service.import_doc_records(
            context.session, context.company_id, member.user, member.role, member.settings, ready,
            post_ledger=False,
        )

    outcomes = await import_prepared([p for p in prepared if p != ""], write)
    written = iter(outcomes)
    outcomes = [None if p == "" else next(written) for p in prepared]
    imported = {r.source_external_id: o.entity_id for r, o in zip(records, outcomes)
                if o is not None and o.status in ("created", "skipped")}
    imported = {**await mapped_targets(context, DOC, [d.metadata.get("applies_to") for d in docs]), **imported}
    result = []
    for record, outcome in zip(records, outcomes):
        if outcome is None:
            outcome = await _import_debit_note(context, record, base, contacts, accounts, imported)
        elif outcome.status in ("created", "skipped"):
            outcome = await _post_document(context, record, base, contacts, accounts, imported, outcome)
        result.append(outcome)
    return result


def _doc_record(
    context: SinkContext,
    record: CIFSourceRecord,
    base: str,
    contacts: dict[str, str],
    items: dict[str, str],
    accounts: dict[str, str],
    taxes: dict[str, tuple[str, float]],
    names: dict[str, str | None],
) -> DocImportRecord | str:
    """The document import record for one source document, or why there is none.

    Lines take the normal Celerp shape: quantity, the source unit price, a percentage
    discount, the line's tax by code and rate, and the line total, which is exact."""
    if not isinstance(record, CIFDocument):
        return f"Record type {type(record).__name__} is not a document."
    label = f"Document {record.ref or record.source_external_id}"
    statuses = _STATUS.get(record.doc_type)
    if statuses is None:
        return f"{label}: {record.doc_type.value} documents cannot be imported as Celerp documents."
    if record.currency and record.currency.upper() != base:
        return f"{label} is in {record.currency}; only {base} documents can be imported."
    problem = _posting_problem(record, contacts, accounts)
    if problem is not None:
        return f"{label}: {problem}"
    contact_id = contacts.get(record.contact_external_id) if record.contact_external_id else None
    lines = []
    for line in record.line_items:
        item_id = None
        if line.item_external_id:
            item_id = items.get(line.item_external_id)
            if item_id is None:
                return f"{label}: item {line.item_external_id} was not imported."
        line_taxes = None
        if line.tax_code_external_id:
            if line.tax_code_external_id not in taxes:
                return f"{label}: tax code {line.tax_code_external_id} was not imported."
            code, rate = taxes[line.tax_code_external_id]
            line_taxes = [{"code": code, "rate": rate, "amount": _money(line.tax_amount or Decimal(0), base)}]
        lines.append({k: v for k, v in {
            "item_id": item_id,
            "description": line.description,
            "account_code": accounts.get(line.account_external_id) if line.account_external_id else None,
            "quantity": float(line.quantity),
            "unit_price": to_stored_float(line.unit_price),
            "discount_pct": to_stored_float(line.discount_percent) if line.discount_percent else None,
            "taxes": line_taxes,
            "line_total": _money(line.total_price, base),
        }.items() if v is not None})
    tax = record.tax_total or Decimal(0)
    data = {
        "doc_type": record.doc_type.value,
        "status": statuses[record.status],
        "contact_id": contact_id,
        "contact_name": names.get(contact_id) if contact_id else None,
        "ref_id": record.ref,
        "doc_number": record.ref,
        "issue_date": record.issue_date.isoformat() if record.issue_date else None,
        "due_date": record.payment_due_date.isoformat() if record.payment_due_date else None,
        "currency": base,
        "line_items": lines,
        "subtotal": _money(record.total - tax, base),
        "tax": _money(tax, base),
        "total": _money(record.total, base),
    }
    return DocImportRecord(
        entity_id=f"doc:{deterministic_id(context, record.source_type, record.source_external_id)}",
        event_type=DOC_CREATED,
        data={k: v for k, v in data.items() if v is not None},
        source="migration",
        idempotency_key=context.idempotency_key(record, "created"),
    )


def _posts(record: CIFDocument) -> bool:
    return record.doc_type in _LINE_SIGN and record.status in ("awaiting_payment", "paid")


def _posting_problem(record: CIFDocument, contacts: dict[str, str], accounts: dict[str, str]) -> str | None:
    """Why the document's contact or posting accounts are missing, or None."""
    if record.contact_external_id and record.contact_external_id not in contacts:
        return f"contact {record.contact_external_id} was not imported."
    if not _posts(record):
        return None
    for line in record.line_items:
        for account, amount in ((line.account_external_id, line.total_price), (line.tax_account_external_id, line.tax_amount)):
            if amount and account not in accounts:
                return f"account {account} was not imported."
    return None


def _entries(record: CIFDocument, base: str, accounts: dict[str, str]) -> list[dict]:
    """The document's own line postings, debit positive by its type."""
    sign = _LINE_SIGN[record.doc_type]
    entries = []
    for line in record.line_items:
        for account, amount in ((line.account_external_id, line.total_price), (line.tax_account_external_id, line.tax_amount)):
            value = _money(sign * (amount or Decimal(0)), base)
            if value:
                entries.append({"account": accounts[account], "debit": max(value, 0.0), "credit": max(-value, 0.0)})
    return entries


async def _post_document(
    context: SinkContext, record: CIFDocument, base: str, contacts: dict[str, str], accounts: dict[str, str],
    imported: dict[str, str], outcome: RecordOutcome,
) -> RecordOutcome:
    """Post an issued document's entry and apply a credit note to its invoice."""
    if not _posts(record):
        return outcome
    label = f"Document {record.ref or record.source_external_id}"
    applies_to = record.metadata.get("applies_to")
    try:
        async with context.session.begin_nested():
            await auto_je.create_for_imported_document(
                context.session, company_id=context.company_id, user_id=context.user_id,
                doc_id=outcome.entity_id, doc_type=record.doc_type.value,
                contact_id=contacts.get(record.contact_external_id or ""),
                entries=_entries(record, base, accounts), ts=_date(record),
            )
            if record.doc_type == DocumentType.CREDIT_NOTE and applies_to:
                if applies_to not in imported:
                    raise ValueError(f"invoice {applies_to} was not imported.")
                await apply_credit_note(
                    context.session, context.company_id, outcome.entity_id, imported[applies_to],
                    _money(record.total, base), payment_date=_date(record), actor_id=context.user_id,
                    source="migration", idempotency_key=context.idempotency_key(record, "applied"),
                )
    except Exception as exc:
        return RecordOutcome("", "failed", f"{label}: {getattr(exc, 'detail', exc)}")
    return outcome


async def _import_debit_note(
    context: SinkContext, record: CIFDocument, base: str, contacts: dict[str, str], accounts: dict[str, str],
    imported: dict[str, str],
) -> RecordOutcome:
    """Post a debit note as a journal fallback on its bill and apply it as a payment."""
    label = f"Debit note {record.ref or record.source_external_id}"
    if record.currency and record.currency.upper() != base:
        return RecordOutcome("", "rejected", f"{label} is in {record.currency}; only {base} documents can be imported.")
    bill_id = imported.get(record.metadata.get("applies_to") or "")
    if bill_id is None:
        return RecordOutcome("", "rejected", f"{label}: the bill it notes was not imported.")
    problem = _posting_problem(record, contacts, accounts)
    if problem is not None:
        return RecordOutcome("", "rejected", f"{label}: {problem}")
    suffix = f"dn:{deterministic_id(context, record.source_type, record.source_external_id)}"
    try:
        async with context.session.begin_nested():
            await auto_je.create_for_imported_document(
                context.session, company_id=context.company_id, user_id=context.user_id,
                doc_id=bill_id, doc_type=record.doc_type.value,
                contact_id=contacts.get(record.contact_external_id or ""),
                entries=_entries(record, base, accounts), ts=_date(record), suffix=suffix,
            )
            entry, _amount = await apply_doc_payment(
                context.session, context.company_id, bill_id,
                {"amount": _money(record.total, base), "payment_date": _date(record),
                 "bank_account": PAYABLE_CODE, "method": "debit_note"},
                source="migration", actor_id=context.user_id,
                idempotency_key=context.idempotency_key(record, "applied"), commit=False,
            )
    except Exception as exc:
        return RecordOutcome("", "failed", f"{label}: {getattr(exc, 'detail', exc)}")
    status = "skipped" if getattr(entry, "was_deduped", False) else "created"
    return RecordOutcome(f"je:auto:{bill_id}:{suffix}", status, entity_type=JOURNAL)


def _date(record: CIFDocument) -> str | None:
    return record.issue_date.isoformat() if record.issue_date else None


# ── Settlements ───────────────────────────────────────────────────────────────

async def _import_settlements(context: SinkContext, records: Sequence[CIFSourceRecord]) -> list[RecordOutcome]:
    base = await _base_currency(context)
    settlements = [r for r in records if isinstance(r, CIFSettlement)]
    docs = await mapped_targets(context, DOC, [a.document_external_id for s in settlements for a in s.allocations])
    banks = await mapped_targets(context, ACCOUNT, [s.bank_account_external_id for s in settlements])
    outcomes = []
    for record in records:
        if not isinstance(record, CIFSettlement):
            outcomes.append(RecordOutcome("", "rejected", f"Record type {type(record).__name__} is not a settlement."))
            continue
        reason = await _settlement_problem(context, record, base, docs, banks)
        if reason is not None:
            outcomes.append(RecordOutcome("", "rejected", reason))
            continue
        outcomes.append(await _settle(context, record, docs, banks[record.bank_account_external_id]))
    return outcomes


async def _settlement_problem(
    context: SinkContext, record: CIFSettlement, base: str, docs: dict[str, str], banks: dict[str, str]
) -> str | None:
    """Why a settlement cannot be imported as payments on its documents, or None."""
    label = f"{record.settlement_type.value.capitalize()} {record.source_ref or record.source_external_id}"
    if record.currency and record.currency.upper() != base:
        return f"{label} is in {record.currency}; only {base} settlements can be imported."
    if record.bank_account_external_id not in banks:
        return f"{label}: bank account {record.bank_account_external_id} was not imported."
    allocated = sum((a.amount for a in record.allocations), Decimal(0))
    if not record.allocations or allocated != record.amount:
        return f"{label}: {record.amount - allocated} is not allocated to a document, which Celerp payments cannot hold."
    settles = _SETTLES[record.settlement_type]
    for allocation in record.allocations:
        doc_id = docs.get(allocation.document_external_id)
        if doc_id is None:
            return f"{label}: document {allocation.document_external_id} was not imported."
        row = await context.session.get(Projection, (context.company_id, doc_id))
        if row is None or (row.state or {}).get("doc_type") != settles.value:
            return f"{label}: document {allocation.document_external_id} is not a {settles.value}."
    return None


async def _settle(
    context: SinkContext, record: CIFSettlement, docs: dict[str, str], bank_code: str
) -> RecordOutcome:
    """Record every allocation as a payment on its document, all or none."""
    settlement_id = str(deterministic_id(context, record.source_type, record.source_external_id))
    applied = 0
    try:
        async with context.session.begin_nested():
            for index, allocation in enumerate(record.allocations):
                doc_id = docs[allocation.document_external_id]
                entry, _amount = await apply_doc_payment(
                    context.session, context.company_id, doc_id,
                    {
                        "amount": to_stored_float(allocation.amount),
                        "payment_date": record.settlement_date.isoformat(),
                        "bank_account": bank_code,
                        "method": "migration",
                    },
                    source="migration",
                    actor_id=context.user_id,
                    idempotency_key=context.idempotency_key(record, f"allocation:{index}"),
                    commit=False,
                )
                if not getattr(entry, "was_deduped", False):
                    applied += 1
    except Exception as exc:
        return RecordOutcome("", "failed", f"Settlement {record.source_external_id}: {getattr(exc, 'detail', exc)}")
    return RecordOutcome(settlement_id, "created" if applied else "skipped")


SINK = DocsMigrationSink()
