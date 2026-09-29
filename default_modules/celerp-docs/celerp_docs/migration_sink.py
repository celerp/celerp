# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Migration sink for operational documents and their settlements.

Documents are written by the same import service as the document batch route and
settlements by the same payment service as the record-payment route, so their
accounting effect comes from Celerp's normal posting. An issued document arrives
unpaid; its paid state comes from the settlements imported after it.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from celerp.importers.schema import (
    CIFDocument,
    CIFSettlement,
    CIFSourceRecord,
    DocumentType,
    ReconciliationExpectations,
    SettlementType,
)
from celerp.importers.sinks import DestinationMeasurement, SinkBatchResult, SinkContext
from celerp.models.company import Company
from celerp.models.projections import Projection
from celerp.services.migration_core_sink import (
    RecordOutcome,
    acting_member,
    deterministic_id,
    import_prepared,
    mapped_targets,
    sink_result,
)
from celerp.services.money import round_money, to_stored_float
from celerp_docs import import_service
from celerp_docs.import_service import DOC_CREATED
from celerp_docs.routes import DocImportRecord, apply_doc_payment

DOC = "doc"
SETTLEMENT = "settlement"
ITEM = "item"
CONTACT = "contact"
ACCOUNT = "account"

# Celerp status of an imported document, by type and source status. Issued sales
# and purchase invoices arrive unpaid; orders and quotes carry no money.
_STATUS = {
    DocumentType.INVOICE: {"draft": "draft", "awaiting_payment": "final", "paid": "final", "void": "void"},
    DocumentType.BILL: {"draft": "draft", "awaiting_payment": "awaiting_payment", "paid": "awaiting_payment", "void": "void"},
    DocumentType.PURCHASE_ORDER: {"draft": "draft", "awaiting_payment": "sent", "paid": "sent", "void": "void"},
    DocumentType.QUOTATION: {"draft": "draft", "awaiting_payment": "sent", "paid": "sent", "void": "void"},
}
_SETTLES = {SettlementType.RECEIPT: DocumentType.INVOICE, SettlementType.PAYMENT: DocumentType.BILL}


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
        # Document and settlement effects are measured on the ledger by the accounting sink.
        return []


async def _base_currency(context: SinkContext) -> str:
    company = await context.session.get(Company, context.company_id)
    return str((company.settings or {}).get("currency") or "USD").upper()


# ── Documents ─────────────────────────────────────────────────────────────────

async def _import_documents(context: SinkContext, records: Sequence[CIFSourceRecord]) -> list[RecordOutcome]:
    member = await acting_member(context)
    base = await _base_currency(context)
    docs = [r for r in records if isinstance(r, CIFDocument)]
    contacts = await mapped_targets(context, CONTACT, [d.contact_external_id for d in docs])
    items = await mapped_targets(context, ITEM, [li.item_external_id for d in docs for li in d.line_items])
    names = {}
    for contact_id in contacts.values():
        row = await context.session.get(Projection, (context.company_id, contact_id))
        names[contact_id] = (row.state or {}).get("name") if row else None
    prepared = [_doc_record(context, r, base, contacts, items, names) for r in records]

    async def write(ready: list[DocImportRecord]):
        return await import_service.import_doc_records(
            context.session, context.company_id, member.user, member.role, member.settings, ready,
        )

    return await import_prepared(prepared, write)


def _doc_record(
    context: SinkContext,
    record: CIFSourceRecord,
    base: str,
    contacts: dict[str, str],
    items: dict[str, str],
    names: dict[str, str | None],
) -> DocImportRecord | str:
    """The document import record for one source document, or why there is none."""
    if not isinstance(record, CIFDocument):
        return f"Record type {type(record).__name__} is not a document."
    label = f"Document {record.ref or record.source_external_id}"
    statuses = _STATUS.get(record.doc_type)
    if statuses is None:
        return f"{label}: {record.doc_type.value} documents cannot be imported as Celerp documents."
    if record.currency and record.currency.upper() != base:
        return f"{label} is in {record.currency}; only {base} documents can be imported."
    contact_id = None
    if record.contact_external_id:
        contact_id = contacts.get(record.contact_external_id)
        if contact_id is None:
            return f"{label}: contact {record.contact_external_id} was not imported."
    lines = []
    for line in record.line_items:
        item_id = None
        if line.item_external_id:
            item_id = items.get(line.item_external_id)
            if item_id is None:
                return f"{label}: item {line.item_external_id} was not imported."
        lines.append({k: v for k, v in {
            "item_id": item_id,
            "description": line.description,
            "quantity": float(line.quantity),
            "unit_price": _money(line.unit_price, base),
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


def _money(amount: Decimal, currency: str) -> float:
    return to_stored_float(round_money(amount, currency))


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
