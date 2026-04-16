# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

from __future__ import annotations

import asyncio
import csv
import io
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.db import get_session
from celerp.events.engine import emit_event
from celerp.models.company import Company
from celerp.modules.slots import fire_lifecycle
from celerp.models.projections import Projection
from celerp_docs.taxes import TaxApplication, compute_tax_amounts
from celerp.services import auto_je
from celerp.services.auth import get_current_company_id, get_current_user, require_manager
from celerp_docs.sequences import next_doc_ref, get_all_sequences, update_sequence, validate_pattern
from celerp.services.fulfill import execute_fulfill, execute_unfulfill
from celerp.services.pick import compute_pick_plan
from celerp_docs.doc_constants import FULFILLABLE_STATUSES

router = APIRouter(dependencies=[Depends(get_current_user)])


class LineItem(BaseModel):
    item_id: str | None = None
    sku: str | None = None
    name: str | None = None
    description: str | None = None
    quantity: float = 0
    unit: str | None = None
    unit_price: float = 0
    tax_rate: float | None = None  # deprecated: kept for backward compat; prefer taxes list
    taxes: list[TaxApplication] = Field(default_factory=list)
    sell_by: str | None = None
    line_total: float | None = None


class DocCreatePayload(BaseModel):
    doc_type: str
    ref_id: str | None = None
    contact_id: str | None = None
    contact_name: str | None = None
    purchase_kind: str | None = None  # inventory|expense|asset (purchase_order only)
    line_items: list[LineItem] = Field(default_factory=list)
    subtotal: float = 0
    tax: float = 0  # deprecated: kept for backward compat; prefer doc_taxes list
    doc_taxes: list[TaxApplication] = Field(default_factory=list)
    discount: float = 0
    shipping: float = 0
    total: float = 0
    payment_terms: str | None = None
    due_date: str | None = None
    currency: str | None = None
    notes: str | None = None
    expected_delivery: str | None = None
    valid_until: str | None = None
    carrier: str | None = None
    tracking: str | None = None
    from_location_id: str | None = None
    to_address: dict | None = None
    original_doc_id: str | None = None
    reason: str | None = None
    status: str = "draft"
    amount_paid: float = 0
    amount_outstanding: float | None = None
    idempotency_key: str | None = None
    model_config = {"extra": "allow"}


class DocPatch(BaseModel):
    fields_changed: dict[str, dict] = Field(default_factory=dict)
    idempotency_key: str | None = None


class DocSendBody(BaseModel):
    sent_via: str | None = None
    sent_to: str | None = None
    idempotency_key: str | None = None


class DocVoidBody(BaseModel):
    reason: str | None = None
    idempotency_key: str | None = None


class DocRevertBody(BaseModel):
    reason: str | None = None
    idempotency_key: str | None = None


class DocUnvoidBody(BaseModel):
    reason: str | None = None
    idempotency_key: str | None = None


class DocPaymentBody(BaseModel):
    amount: float
    currency: str | None = None
    method: str | None = None
    reference: str | None = None
    payment_date: str | None = None
    bank_account: str | None = None
    source_doc_id: str | None = None
    target_doc_id: str | None = None
    idempotency_key: str | None = None


class ReceivedItem(BaseModel):
    po_line_index: int
    item_id: str | None = None
    quantity_received: float
    condition: str = "good"
    sku: str | None = None
    name: str | None = None


class ReceiveBody(BaseModel):
    location_id: str
    received_items: list[ReceivedItem]
    notes: str | None = None
    idempotency_key: str | None = None


class DocImportRecord(BaseModel):
    entity_id: str
    event_type: str
    data: dict
    source: str
    idempotency_key: str
    source_ts: str | None = None


class BatchImportResult(BaseModel):
    created: int
    skipped: int
    updated: int = 0
    errors: list[str]


class DocBatchImportRequest(BaseModel):
    records: list[DocImportRecord]
    upsert: bool = False


async def _get_doc(session: AsyncSession, company_id, entity_id: str) -> Projection:
    row = await session.get(Projection, {"company_id": company_id, "entity_id": entity_id})
    if row is None or row.entity_type != "doc":
        raise HTTPException(status_code=404, detail="Document not found")
    return row


@router.get("")
async def list_docs(
    doc_type: str | None = None,
    status: str | None = None,
    exclude_status: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    q: str | None = None,
    contact_id: str | None = None,
    limit: int | None = None,
    offset: int = 0,
    company_id: str = Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    rows = (await session.execute(select(Projection).where(Projection.company_id == company_id, Projection.entity_type == "doc"))).scalars().all()
    out = [r.state | {"id": r.entity_id} for r in rows]
    if doc_type:
        out = [x for x in out if x.get("doc_type") == doc_type]
    if status:
        out = [x for x in out if x.get("status") == status]
    if exclude_status:
        out = [x for x in out if x.get("status") != exclude_status]
    if date_from:
        out = [x for x in out if (x.get("created_at") or x.get("date") or "")[:10] >= date_from]
    if date_to:
        out = [x for x in out if (x.get("created_at") or x.get("date") or "")[:10] <= date_to]
    if contact_id:
        out = [x for x in out if x.get("contact_id") == contact_id]
    if q:
        ql = q.lower()
        out = [x for x in out if ql in str(x.get("doc_number") or x.get("ref") or "").lower()
               or ql in str(x.get("contact_name") or x.get("contact_id") or "").lower()]
    out.sort(key=lambda x: x.get("created_at") or x.get("date") or "", reverse=True)
    total = len(out)
    if offset:
        out = out[offset:]
    if limit is not None:
        out = out[:limit]
    return {"items": out, "total": total}


@router.get("/summary")
async def get_doc_summary(
    doc_type: str | None = None,
    company_id: str = Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    rows = (await session.execute(select(Projection).where(Projection.company_id == company_id, Projection.entity_type == "doc"))).scalars().all()
    ar_gross = ar_paid = ar_outstanding = 0.0
    count_by_status: dict[str, int] = {}
    invoice_count = 0
    for row in rows:
        state = row.state
        # Filter by doc_type if specified
        if doc_type and state.get("doc_type") != doc_type:
            continue
        st = state.get("status", "")
        count_by_status[st] = count_by_status.get(st, 0) + 1
        if st in ("void", "draft"):
            continue
        if state.get("doc_type") == "invoice":
            invoice_count += 1
            ar_gross += float(state.get("total", 0) or 0)
            ar_paid += float(state.get("amount_paid", 0) or 0)
            ar_outstanding += float(state.get("amount_outstanding", 0) or 0)
    draft_count = count_by_status.get("draft", 0)
    total_rows = sum(count_by_status.values())
    live_count = total_rows - draft_count
    return {
        "total_count": live_count,
        "draft_count": draft_count,
        "non_void_count": sum(v for k, v in count_by_status.items() if k not in ("void", "draft")),
        "ar_total": ar_gross,
        "ar_paid": ar_paid,
        "ar_outstanding": ar_outstanding,
        "invoice_count": invoice_count,
        "count_by_status": count_by_status,
    }



# ---------------------------------------------------------------------------
# Document numbering sequences (must be before /{entity_id} catch-all)
# ---------------------------------------------------------------------------


class SequencePatch(BaseModel):
    prefix: str | None = None
    pattern: str | None = None
    next: int | None = None


@router.get("/sequences")
async def get_sequences(company_id: str = Depends(get_current_company_id), user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> list[dict]:
    company = await session.get(Company, company_id)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    return get_all_sequences(company)


@router.patch("/sequences/{doc_type}")
async def patch_sequence(doc_type: str, payload: SequencePatch, company_id: str = Depends(get_current_company_id), user=Depends(require_manager), session: AsyncSession = Depends(get_session)) -> dict:
    company = await session.get(Company, company_id)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    try:
        result = update_sequence(company, doc_type, prefix=payload.prefix, pattern=payload.pattern, next_num=payload.next)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    await session.commit()
    return result


@router.get("/{entity_id}")
async def get_doc(entity_id: str, company_id: str = Depends(get_current_company_id), session: AsyncSession = Depends(get_session)) -> dict:
    row = await _get_doc(session, company_id, entity_id)
    return row.state | {"id": row.entity_id}


@router.get("/{entity_id}/pdf")
async def get_doc_pdf(
    entity_id: str,
    company_id: str = Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
):
    """Return a PDF of the document with 'Powered by Celerp' footer branding."""
    from fastapi.responses import Response as _Resp
    from celerp_docs.pdf import generate_document_pdf

    row = await _get_doc(session, company_id, entity_id)
    doc = row.state | {"entity_id": row.entity_id}

    company_row = await session.get(Company, company_id)
    company = ({"name": company_row.name} | (company_row.settings or {}) if company_row else {}) | {"id": company_id}

    pdf_bytes = generate_document_pdf(doc, company)
    doc_ref = doc.get("ref_id") or doc.get("doc_number") or entity_id
    filename = f"{doc_ref}.pdf".replace("/", "-").replace(" ", "_")
    return _Resp(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


@router.post("")
async def create_doc(
    payload: DocCreatePayload,
    company_id: str = Depends(get_current_company_id),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    if payload.doc_type == "credit_note":
        if not payload.original_doc_id:
            raise HTTPException(status_code=422, detail="Credit note requires original_doc_id")
        inv = await _get_doc(session, company_id, payload.original_doc_id)
        original_total = float(inv.state.get("total", 0) or 0)
        if payload.total > original_total + 1e-9:
            raise HTTPException(status_code=409, detail="Credit note total cannot exceed original invoice total")

    company = await session.get(Company, company_id)
    # Invoices get proforma numbering at draft stage; real INV number assigned on finalize
    seq_type = "proforma" if payload.doc_type == "invoice" and not payload.ref_id else payload.doc_type
    ref_id = payload.ref_id or next_doc_ref(company, seq_type)
    entity_id = f"doc:{ref_id}"

    # Uniqueness check: reject if a doc with this ref_id already exists
    existing = await session.get(Projection, {"company_id": company_id, "entity_id": entity_id})
    if existing is not None:
        raise HTTPException(status_code=409, detail=f"Document number '{ref_id}' already exists")

    data = payload.model_dump(exclude_none=True)
    data["ref_id"] = ref_id
    data.setdefault("currency", company.settings.get("currency", "USD"))

    # Auto-compute total from line items if not explicitly provided (or zero)
    if not payload.total and payload.line_items:
        # If any line provides line_total, it is pre-computed (discount already applied).
        # Header discount only applies when computing from quantity * unit_price.
        has_explicit_line_totals = any(li.line_total is not None for li in payload.line_items)
        computed = sum(
            (li.line_total if li.line_total is not None else li.quantity * li.unit_price)
            for li in payload.line_items
        )
        if not has_explicit_line_totals:
            computed = computed - payload.discount

        # Compute per-line tax amounts (compound-aware), update data
        line_tax_total = 0.0
        if data.get("line_items"):
            resolved_line_items = []
            for li_data, li_model in zip(data["line_items"], payload.line_items):
                if li_model.taxes:
                    line_base = float(li_data.get("line_total") or li_model.quantity * li_model.unit_price)
                    resolved = compute_tax_amounts(li_model.taxes, line_base)
                    li_data = {**li_data, "taxes": [item.model_dump() for item in resolved]}
                    line_tax_total += sum(item.amount for item in resolved)
                resolved_line_items.append(li_data)
            data["line_items"] = resolved_line_items

        # doc_taxes: compute compound-aware amounts against subtotal, take precedence over legacy tax
        if payload.doc_taxes:
            subtotal_base = computed  # pre-doc-tax subtotal
            resolved_doc_taxes = compute_tax_amounts(payload.doc_taxes, subtotal_base)
            data["doc_taxes"] = [item.model_dump() for item in resolved_doc_taxes]
            effective_tax = sum(item.amount for item in resolved_doc_taxes) + line_tax_total
        else:
            effective_tax = payload.tax + line_tax_total

        computed = computed + effective_tax + payload.shipping
        data["total"] = computed
        data["subtotal"] = computed - effective_tax - payload.shipping

    data["amount_outstanding"] = payload.amount_outstanding if payload.amount_outstanding is not None else float(data.get("total", 0))

    entry = await emit_event(
        session,
        company_id=company_id,
        entity_id=entity_id,
        entity_type="doc",
        event_type="doc.created",
        data=data,
        actor_id=user.id,
        location_id=None,
        source="api",
        idempotency_key=payload.idempotency_key or str(uuid.uuid4()),
        metadata_={},
    )

    if payload.doc_type == "credit_note" and payload.original_doc_id:
        inv = await _get_doc(session, company_id, payload.original_doc_id)
        outstanding = max(0.0, float(inv.state.get("amount_outstanding", inv.state.get("total", 0)) or 0) - float(payload.total))
        await emit_event(
            session,
            company_id=company_id,
            entity_id=payload.original_doc_id,
            entity_type="doc",
            event_type="doc.updated",
            data={"fields_changed": {"amount_outstanding": {"old": inv.state.get("amount_outstanding"), "new": outstanding}}},
            actor_id=user.id,
            location_id=None,
            source="api",
            idempotency_key=str(uuid.uuid4()),
            metadata_={"source_credit_note": entity_id},
        )
    await session.commit()
    return {"event_id": entry.id, "id": entity_id}


@router.patch("/{entity_id}")
async def patch_doc(entity_id: str, payload: DocPatch, company_id: str = Depends(get_current_company_id), user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    row = await _get_doc(session, company_id, entity_id)
    if row.state.get("status") != "draft":
        raise HTTPException(status_code=409, detail="Cannot edit non-draft document")
    # Uniqueness check when ref_id is being changed
    new_ref = (payload.fields_changed.get("ref_id") or {}).get("new")
    if new_ref:
        new_eid = f"doc:{new_ref}"
        if new_eid != entity_id:
            existing = await session.execute(
                select(Projection).where(Projection.company_id == company_id, Projection.entity_id == new_eid)
            )
            if existing.scalar_one_or_none():
                raise HTTPException(status_code=409, detail=f"Document number '{new_ref}' already exists")
    entry = await emit_event(
        session, company_id=company_id, entity_id=entity_id, entity_type="doc", event_type="doc.updated",
        data=payload.model_dump(exclude_none=True), actor_id=user.id, location_id=None, source="api",
        idempotency_key=payload.idempotency_key or str(uuid.uuid4()), metadata_={},
    )
    await session.commit()
    return {"event_id": entry.id}


@router.post("/{entity_id}/send")
async def send_doc(entity_id: str, payload: DocSendBody, company_id: str = Depends(get_current_company_id), user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    row = await _get_doc(session, company_id, entity_id)
    if row.state.get("status") == "void":
        raise HTTPException(status_code=409, detail="Cannot send void document")
    entry = await emit_event(
        session, company_id=company_id, entity_id=entity_id, entity_type="doc", event_type="doc.sent",
        data=payload.model_dump(exclude_none=True), actor_id=user.id, location_id=None, source="api",
        idempotency_key=payload.idempotency_key or str(uuid.uuid4()), metadata_={},
    )
    await session.commit()

    # Fire-and-forget email notification if a recipient was supplied
    sent_to = payload.sent_to
    if sent_to:
        doc_number = row.state.get("ref_id") or entity_id.split(":")[-1][:8].upper()
        total = row.state.get("total", 0)
        currency = row.state.get("currency", "USD")
        doc_type = row.state.get("doc_type", "document").replace("_", " ").title()
        contact_name = row.state.get("contact_name") or "there"
        company_row = await session.get(Company, company_id)
        sender_name = company_row.name if company_row else "Your supplier"
        subject = f"{doc_type} #{doc_number} from {sender_name}"
        body_html = (
            f"<p>Hi {contact_name},</p>"
            f"<p>Please find your <strong>{doc_type} #{doc_number}</strong> from "
            f"<strong>{sender_name}</strong>.</p>"
            f"<p>Amount: <strong>{currency} {total}</strong></p>"
            f"<p style='color:#888;font-size:13px;'>Questions about this document? "
            f"Reply to this email and we'll get back to you.</p>"
        )
        body_text = (
            f"Hi {contact_name},\n\n"
            f"Please find your {doc_type} #{doc_number} from {sender_name}.\n"
            f"Amount: {currency} {total}\n\n"
            f"Questions? Reply to this email."
        )
        from celerp.services.email import send_email
        asyncio.create_task(send_email(sent_to, subject, body_html, body_text=body_text))

    return {"event_id": entry.id}


@router.post("/{entity_id}/finalize")
async def finalize_doc(entity_id: str, company_id: str = Depends(get_current_company_id), _: None = Depends(require_manager), user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    row = await _get_doc(session, company_id, entity_id)
    if row.state.get("status") == "void":
        raise HTTPException(status_code=409, detail="Cannot finalize void document")

    # Snapshot scalar values early — avoids ORM lazy-load issues after multiple flush() calls.
    _initial_doc_state = dict(row.state)
    _user_id = user.id  # Capture before flushes may expire session objects
    doc_type = _initial_doc_state.get("doc_type", "")
    finalize_data: dict = {}
    event_type = "doc.finalized"

    # Invoices: assign real INV number on finalize, preserving PF ref
    if doc_type == "invoice":
        company = await session.get(Company, company_id)
        inv_ref = next_doc_ref(company, "invoice")
        finalize_data["ref_id"] = inv_ref
        finalize_data["source_proforma_ref"] = _initial_doc_state.get("ref_id", "")
        await session.flush()

    # Purchase Orders: "Convert to Bill" - assign BILL number, change doc_type
    elif doc_type == "purchase_order":
        company = await session.get(Company, company_id)
        bill_ref = next_doc_ref(company, "bill")
        finalize_data["ref_id"] = bill_ref
        finalize_data["source_po_ref"] = _initial_doc_state.get("ref_id", "")
        finalize_data["doc_type"] = "bill"
        event_type = "doc.converted_to_bill"
        await session.flush()

    entry = await emit_event(
        session, company_id=company_id, entity_id=entity_id, entity_type="doc", event_type=event_type,
        data=finalize_data,
        actor_id=_user_id, location_id=None, source="api", idempotency_key=str(uuid.uuid4()), metadata_={},
    )
    # Auto-JE on finalize (invoices) or convert to bill (POs)
    if doc_type == "invoice":
        await auto_je.create_for_doc_finalized(session, company_id=company_id, user_id=_user_id, doc_id=entity_id, doc=_initial_doc_state)
    elif doc_type == "purchase_order":
        # Bill conversion JE: debit expense/inventory accounts, credit AP (2110)
        await auto_je.create_for_bill_conversion(session, company_id=company_id, user_id=_user_id, doc_id=entity_id, doc=_initial_doc_state)
    # Fire doc_finalize_hook for modules (e.g. warehousing) to react — before commit.
    await fire_lifecycle(
        "doc_finalize_hook",
        session=session,
        entity_id=entity_id,
        doc_state=_initial_doc_state,
        company_id=company_id,
        user_id=_user_id,
        doc_type=doc_type,
    )
    await session.commit()
    return {"event_id": entry.id}


@router.post("/{entity_id}/void")
async def void_doc(entity_id: str, payload: DocVoidBody, company_id: str = Depends(get_current_company_id), _: None = Depends(require_manager), user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    row = await _get_doc(session, company_id, entity_id)
    current_status = row.state.get("status")
    if current_status in ("paid", "partial"):
        raise HTTPException(status_code=409, detail="Cannot void a document with payments; void the payments first")

    # Un-fulfill before voiding if doc was fulfilled
    fulfillment_status = row.state.get("fulfillment_status")
    if fulfillment_status and fulfillment_status != "unfulfilled":
        await execute_unfulfill(
            session, doc_entity_id=entity_id, doc_state=row.state,
            company_id=company_id, user_id=user.id, reason="void",
        )

    event_data = payload.model_dump(exclude_none=True)
    event_data["pre_void_status"] = current_status
    if fulfillment_status:
        event_data["pre_void_fulfillment"] = fulfillment_status
    entry = await emit_event(
        session, company_id=company_id, entity_id=entity_id, entity_type="doc", event_type="doc.voided",
        data=event_data, actor_id=user.id, location_id=None, source="api",
        idempotency_key=payload.idempotency_key or str(uuid.uuid4()), metadata_={},
    )
    await session.commit()
    return {"event_id": entry.id}


@router.post("/{entity_id}/revert-to-draft")
async def revert_doc_to_draft(entity_id: str, payload: DocRevertBody, company_id: str = Depends(get_current_company_id), _: None = Depends(require_manager), user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    row = await _get_doc(session, company_id, entity_id)
    state = row.state
    previous_status = state.get("status")
    _REVERTABLE = {"final", "sent", "awaiting_payment"}
    if previous_status not in _REVERTABLE:
        raise HTTPException(status_code=409, detail="Can only revert documents in 'final', 'sent', or 'awaiting_payment' status")
    if float(state.get("amount_paid", 0) or 0) != 0:
        raise HTTPException(status_code=409, detail="Cannot revert document with existing payments")
    if state.get("received_items"):
        raise HTTPException(status_code=409, detail="Cannot revert document with received items")

    # Un-fulfill before reverting if doc was fulfilled
    fulfillment_status = state.get("fulfillment_status")
    if fulfillment_status and fulfillment_status != "unfulfilled":
        await execute_unfulfill(
            session, doc_entity_id=entity_id, doc_state=state,
            company_id=company_id, user_id=user.id, reason="revert_to_draft",
        )

    event_data: dict = {"reverted_by": str(user.id), "previous_status": previous_status}
    if payload.reason:
        event_data["reason"] = payload.reason

    # PO->bill revert: restore doc_type and ref_id
    extra_data: dict = {}
    doc_type = state.get("doc_type")
    if doc_type == "bill" and state.get("source_po_ref"):
        extra_data["doc_type"] = "purchase_order"
        extra_data["ref_id"] = state["source_po_ref"]

    entry = await emit_event(
        session, company_id=company_id, entity_id=entity_id, entity_type="doc",
        event_type="doc.reverted_to_draft",
        data={**event_data, **extra_data},
        actor_id=user.id, location_id=None, source="api",
        idempotency_key=payload.idempotency_key or str(uuid.uuid4()), metadata_={},
    )
    # Void the finalize JE
    await auto_je.void_for_doc_finalized(session, company_id=company_id, user_id=user.id, doc_id=entity_id)
    await session.commit()
    return {"event_id": entry.id}


@router.post("/{entity_id}/unvoid")
async def unvoid_doc(entity_id: str, payload: DocUnvoidBody, company_id: str = Depends(get_current_company_id), _: None = Depends(require_manager), user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    row = await _get_doc(session, company_id, entity_id)
    state = row.state
    if state.get("status") != "void":
        raise HTTPException(status_code=409, detail="Can only unvoid documents in 'void' status")
    restored_status = state.get("pre_void_status")
    if not restored_status:
        raise HTTPException(status_code=409, detail="Cannot unvoid: document was voided before unvoid support was added (no pre_void_status)")

    entry = await emit_event(
        session, company_id=company_id, entity_id=entity_id, entity_type="doc",
        event_type="doc.unvoided",
        data={"unvoided_by": str(user.id), "restored_status": restored_status},
        actor_id=user.id, location_id=None, source="api",
        idempotency_key=payload.idempotency_key or str(uuid.uuid4()), metadata_={},
    )
    # Re-apply JEs for the restored status (idempotent - uses doc-scoped keys)
    await auto_je.create_for_doc_unvoided(session, company_id=company_id, user_id=user.id, doc_id=entity_id, doc=state)

    # TODO: actual re-fulfillment after unvoid would need inventory availability check.
    # For now, restore the fulfillment_status field so the UI reflects prior state.
    pre_void_fulfillment = state.get("pre_void_fulfillment")
    if pre_void_fulfillment and pre_void_fulfillment != "unfulfilled":
        await emit_event(
            session, company_id=company_id, entity_id=entity_id, entity_type="doc",
            event_type="doc.fulfilled" if pre_void_fulfillment == "fulfilled" else "doc.partially_fulfilled",
            data={
                "fulfilled_items": state.get("fulfilled_items", []),
                "fulfilled_by": str(user.id),
                "fulfilled_at": state.get("fulfilled_at", ""),
                "strategy": "restored",
                **({"total_cogs": 0.0} if pre_void_fulfillment == "fulfilled" else {"unfulfilled_items": []}),
            },
            actor_id=user.id, location_id=None, source="api",
            idempotency_key=str(uuid.uuid4()), metadata_={"restored_from_unvoid": True},
        )

    await session.commit()
    return {"event_id": entry.id}


@router.delete("/{entity_id}")
async def delete_doc(entity_id: str, company_id: str = Depends(get_current_company_id), _: None = Depends(require_manager), user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    row = await _get_doc(session, company_id, entity_id)
    if row.state.get("status") != "draft":
        raise HTTPException(status_code=409, detail="Only draft documents can be deleted")
    from celerp.models.ledger import LedgerEntry
    import sqlalchemy as _sa
    await session.execute(_sa.delete(Projection).where(Projection.company_id == company_id, Projection.entity_id == entity_id))
    await session.execute(_sa.delete(LedgerEntry).where(LedgerEntry.company_id == company_id, LedgerEntry.entity_id == entity_id))
    await session.commit()
    return {"deleted": entity_id}


@router.post("/{entity_id}/payment")
async def record_payment(entity_id: str, payload: DocPaymentBody, company_id: str = Depends(get_current_company_id), _: None = Depends(require_manager), user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    row = await _get_doc(session, company_id, entity_id)
    if row.state.get("status") not in {"sent", "final", "partial", "paid", "received", "partially_received", "awaiting_payment"}:
        raise HTTPException(status_code=409, detail="Cannot record payment in current status")
    outstanding = float(row.state.get("amount_outstanding", row.state.get("total", 0)) or 0)
    if outstanding <= 0:
        raise HTTPException(status_code=409, detail="Invoice already fully paid")
    if payload.amount > outstanding + 1e-9:
        raise HTTPException(status_code=409, detail="Payment exceeds amount outstanding")

    body = payload.model_dump(exclude_none=True)
    body.setdefault("currency", row.state.get("currency", "USD"))
    body["remaining_balance"] = max(0.0, outstanding - payload.amount)
    entry = await emit_event(
        session, company_id=company_id, entity_id=entity_id, entity_type="doc", event_type="doc.payment.received",
        data=body, actor_id=user.id, location_id=None, source="api",
        idempotency_key=payload.idempotency_key or str(uuid.uuid4()), metadata_={},
    )
    cumulative_paid = float(row.state.get("amount_paid", 0) or 0) + payload.amount
    bank_code = payload.bank_account or "1110"
    doc_type = row.state.get("doc_type", "invoice")
    await auto_je.create_for_doc_payment(
        session, company_id=company_id, user_id=user.id, doc_id=entity_id,
        amount=payload.amount, cumulative_paid=cumulative_paid,
        bank_account_code=bank_code, doc_type=doc_type,
    )
    # Lifecycle hook for modules (e.g. multicurrency FX gain/loss)
    from celerp.modules.slots import fire_lifecycle
    await fire_lifecycle(
        "on_doc_payment",
        session=session,
        company_id=company_id,
        user_id=user.id,
        doc_id=entity_id,
        doc=row.state,
        amount=payload.amount,
        bank_account_code=bank_code,
    )
    await session.commit()
    return {"event_id": entry.id}


@router.post("/{entity_id}/refund")
async def refund_payment(entity_id: str, payload: DocPaymentBody, company_id: str = Depends(get_current_company_id), _: None = Depends(require_manager), user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    row = await _get_doc(session, company_id, entity_id)
    paid = float(row.state.get("amount_paid", 0) or 0)
    if payload.amount > paid + 1e-9:
        raise HTTPException(status_code=409, detail="Refund exceeds amount paid")
    refund_data = payload.model_dump(exclude_none=True)
    refund_data.setdefault("currency", row.state.get("currency", "USD"))
    entry = await emit_event(
        session, company_id=company_id, entity_id=entity_id, entity_type="doc", event_type="doc.payment.refunded",
        data=refund_data, actor_id=user.id, location_id=None, source="api",
        idempotency_key=payload.idempotency_key or str(uuid.uuid4()), metadata_={},
    )
    await session.commit()
    return {"event_id": entry.id}


# ---------------------------------------------------------------------------
# Void individual payment
# ---------------------------------------------------------------------------


class VoidPaymentBody(BaseModel):
    payment_index: int
    void_reason: str | None = None
    idempotency_key: str | None = None


@router.post("/{entity_id}/void-payment")
async def void_payment(entity_id: str, payload: VoidPaymentBody, company_id: str = Depends(get_current_company_id), _: None = Depends(require_manager), user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    row = await _get_doc(session, company_id, entity_id)
    payments = row.state.get("payments", [])
    if payload.payment_index < 0 or payload.payment_index >= len(payments):
        raise HTTPException(status_code=422, detail="Invalid payment index")
    payment = payments[payload.payment_index]
    if payment.get("status") != "active":
        raise HTTPException(status_code=409, detail="Payment is already voided")

    entry = await emit_event(
        session, company_id=company_id, entity_id=entity_id, entity_type="doc",
        event_type="doc.payment.voided",
        data={"payment_index": payload.payment_index, "void_reason": payload.void_reason},
        actor_id=user.id, location_id=None, source="api",
        idempotency_key=payload.idempotency_key or str(uuid.uuid4()), metadata_={},
    )
    # Reverse the payment JE
    doc_type = row.state.get("doc_type", "invoice")
    bank_code = payment.get("bank_account", "1110")
    await auto_je.void_for_doc_payment(
        session, company_id=company_id, user_id=user.id, doc_id=entity_id,
        payment_index=payload.payment_index, amount=payment["amount"],
        bank_account_code=bank_code, doc_type=doc_type,
    )

    # If this was a credit_note application (paired payment), void the other side too
    source_doc_id = payment.get("source_doc_id")
    target_doc_id = payment.get("target_doc_id")
    paired_doc_id = source_doc_id or target_doc_id
    if paired_doc_id and payment.get("method") in ("credit_note", "applied"):
        paired_row = await session.get(
            Projection, {"company_id": company_id, "entity_id": paired_doc_id}
        )
        if paired_row and paired_row.entity_type == "doc":
            paired_payments = paired_row.state.get("payments", [])
            # Find the matching payment on the other doc
            for pi, pp in enumerate(paired_payments):
                if pp.get("status") == "active" and (
                    (pp.get("source_doc_id") == entity_id) or (pp.get("target_doc_id") == entity_id)
                ):
                    await emit_event(
                        session, company_id=company_id, entity_id=paired_doc_id, entity_type="doc",
                        event_type="doc.payment.voided",
                        data={"payment_index": pi, "void_reason": payload.void_reason or "Paired void"},
                        actor_id=user.id, location_id=None, source="api",
                        idempotency_key=str(uuid.uuid4()), metadata_={},
                    )
                    break

    await session.commit()
    return {"event_id": entry.id}


# ---------------------------------------------------------------------------
# Credit note: apply to invoice
# ---------------------------------------------------------------------------


class ApplyToInvoiceBody(BaseModel):
    target_doc_id: str
    amount: float
    date: str | None = None
    idempotency_key: str | None = None


@router.post("/{entity_id}/apply-to-invoice")
async def apply_cn_to_invoice(entity_id: str, payload: ApplyToInvoiceBody, company_id: str = Depends(get_current_company_id), _: None = Depends(require_manager), user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    cn_row = await _get_doc(session, company_id, entity_id)
    cn = cn_row.state
    if cn.get("doc_type") != "credit_note":
        raise HTTPException(status_code=409, detail="Only credit notes can be applied to invoices")
    if cn.get("status") in ("draft", "void"):
        raise HTTPException(status_code=409, detail="Credit note must be issued before applying")

    inv_row = await _get_doc(session, company_id, payload.target_doc_id)
    inv = inv_row.state
    if inv.get("doc_type") != "invoice":
        raise HTTPException(status_code=409, detail="Target must be an invoice")
    if inv.get("status") in ("draft", "void"):
        raise HTTPException(status_code=409, detail="Invoice must be in a payable status")

    # Validate same contact
    cn_contact = cn.get("contact_id")
    inv_contact = inv.get("contact_id")
    if cn_contact and inv_contact and cn_contact != inv_contact:
        raise HTTPException(status_code=422, detail="Credit note and invoice must belong to the same contact")

    cn_outstanding = float(cn.get("amount_outstanding", cn.get("total", 0)) or 0)
    inv_outstanding = float(inv.get("amount_outstanding", inv.get("total", 0)) or 0)
    if payload.amount > cn_outstanding + 1e-9:
        raise HTTPException(status_code=409, detail="Amount exceeds credit note balance")
    if payload.amount > inv_outstanding + 1e-9:
        raise HTTPException(status_code=409, detail="Amount exceeds invoice outstanding")

    payment_date = payload.date or datetime.now(UTC).date().isoformat()

    # Emit paired events: payment on invoice (credit_note method), payment on CN (applied method)
    await emit_event(
        session, company_id=company_id, entity_id=payload.target_doc_id, entity_type="doc",
        event_type="doc.payment.received",
        data={
            "amount": payload.amount, "method": "credit_note",
            "source_doc_id": entity_id, "payment_date": payment_date,
            "currency": cn.get("currency", "USD"),
        },
        actor_id=user.id, location_id=None, source="api",
        idempotency_key=str(uuid.uuid4()), metadata_={},
    )
    entry = await emit_event(
        session, company_id=company_id, entity_id=entity_id, entity_type="doc",
        event_type="doc.payment.received",
        data={
            "amount": payload.amount, "method": "applied",
            "target_doc_id": payload.target_doc_id, "payment_date": payment_date,
            "currency": cn.get("currency", "USD"),
        },
        actor_id=user.id, location_id=None, source="api",
        idempotency_key=payload.idempotency_key or str(uuid.uuid4()), metadata_={},
    )
    # JE: AR-to-AR transfer
    await auto_je.create_for_cn_application(
        session, company_id=company_id, user_id=user.id,
        doc_id=payload.target_doc_id, cn_id=entity_id, amount=payload.amount,
    )
    await session.commit()
    return {"event_id": entry.id}


# ---------------------------------------------------------------------------
# Credit note: refund to customer
# ---------------------------------------------------------------------------


class CnRefundBody(BaseModel):
    amount: float
    date: str | None = None
    method: str | None = None
    bank_account: str | None = None
    reference: str | None = None
    idempotency_key: str | None = None


@router.post("/{entity_id}/cn-refund")
async def refund_cn(entity_id: str, payload: CnRefundBody, company_id: str = Depends(get_current_company_id), _: None = Depends(require_manager), user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    row = await _get_doc(session, company_id, entity_id)
    cn = row.state
    if cn.get("doc_type") != "credit_note":
        raise HTTPException(status_code=409, detail="Only credit notes can be refunded")
    if cn.get("status") in ("draft", "void"):
        raise HTTPException(status_code=409, detail="Credit note must be issued before refunding")
    cn_outstanding = float(cn.get("amount_outstanding", cn.get("total", 0)) or 0)
    if payload.amount > cn_outstanding + 1e-9:
        raise HTTPException(status_code=409, detail="Refund amount exceeds credit note balance")

    payment_date = payload.date or datetime.now(UTC).date().isoformat()
    bank_code = payload.bank_account or "1110"

    entry = await emit_event(
        session, company_id=company_id, entity_id=entity_id, entity_type="doc",
        event_type="doc.payment.received",
        data={
            "amount": payload.amount, "method": "refund",
            "bank_account": bank_code, "reference": payload.reference,
            "payment_date": payment_date, "currency": cn.get("currency", "USD"),
        },
        actor_id=user.id, location_id=None, source="api",
        idempotency_key=payload.idempotency_key or str(uuid.uuid4()), metadata_={},
    )
    # JE: debit AR, credit bank
    cumulative = float(cn.get("amount_paid", 0) or 0) + payload.amount
    await auto_je.create_for_doc_payment(
        session, company_id=company_id, user_id=user.id, doc_id=entity_id,
        amount=payload.amount, cumulative_paid=cumulative,
        bank_account_code=bank_code, doc_type="invoice",
    )
    await session.commit()
    return {"event_id": entry.id}


# ---------------------------------------------------------------------------
# Bulk payment
# ---------------------------------------------------------------------------


class BulkPaymentBody(BaseModel):
    doc_ids: list[str]
    amount: float
    payment_date: str | None = None
    method: str | None = None
    bank_account: str | None = None
    reference: str | None = None
    idempotency_key: str | None = None


@router.post("/bulk-payment")
async def bulk_payment(payload: BulkPaymentBody, company_id: str = Depends(get_current_company_id), _: None = Depends(require_manager), user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    if not payload.doc_ids:
        raise HTTPException(status_code=422, detail="No documents specified")

    # Fetch all docs
    docs = []
    for doc_id in payload.doc_ids:
        row = await session.get(Projection, {"company_id": company_id, "entity_id": doc_id})
        if row and row.entity_type == "doc":
            docs.append((doc_id, row.state))

    if not docs:
        raise HTTPException(status_code=404, detail="No valid documents found")

    # Validate same contact
    contact_ids = {s.get("contact_id") for _, s in docs if s.get("contact_id")}
    if len(contact_ids) > 1:
        raise HTTPException(status_code=422, detail="All documents must belong to the same contact")

    # Filter to payable docs
    _PAYABLE = {"sent", "final", "partial", "awaiting_payment"}
    payable = [(did, s) for did, s in docs if s.get("status") in _PAYABLE and float(s.get("amount_outstanding", s.get("total", 0)) or 0) > 0.005]
    if not payable:
        raise HTTPException(status_code=409, detail="No documents in payable status")

    # Sort by due_date asc (oldest first), then issue_date
    payable.sort(key=lambda x: (x[1].get("due_date") or x[1].get("issue_date") or "9999", x[1].get("issue_date") or "9999"))

    # Allocate amount oldest-first
    remaining = payload.amount
    allocations = []
    payment_date = payload.date if hasattr(payload, 'date') else payload.payment_date
    bank_code = payload.bank_account or "1110"

    for doc_id, state in payable:
        if remaining <= 0.005:
            break
        outstanding = float(state.get("amount_outstanding", state.get("total", 0)) or 0)
        alloc = min(remaining, outstanding)
        if alloc <= 0.005:
            continue

        body = {
            "amount": alloc,
            "method": payload.method,
            "reference": payload.reference,
            "payment_date": payment_date,
            "bank_account": bank_code,
            "currency": state.get("currency", "USD"),
        }
        await emit_event(
            session, company_id=company_id, entity_id=doc_id, entity_type="doc",
            event_type="doc.payment.received",
            data={k: v for k, v in body.items() if v is not None},
            actor_id=user.id, location_id=None, source="api",
            idempotency_key=str(uuid.uuid4()), metadata_={},
        )
        cumulative = float(state.get("amount_paid", 0) or 0) + alloc
        doc_type = state.get("doc_type", "invoice")
        await auto_je.create_for_doc_payment(
            session, company_id=company_id, user_id=user.id, doc_id=doc_id,
            amount=alloc, cumulative_paid=cumulative,
            bank_account_code=bank_code, doc_type=doc_type,
        )
        allocations.append({"doc_id": doc_id, "amount": alloc})
        remaining -= alloc

    await session.commit()
    return {"allocations": allocations, "total_allocated": payload.amount - remaining, "remaining": remaining}


@router.post("/{entity_id}/receive")
async def receive_po(entity_id: str, payload: ReceiveBody, company_id: str = Depends(get_current_company_id), user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    row = await _get_doc(session, company_id, entity_id)
    doc_type = row.state.get("doc_type")
    if doc_type not in ("purchase_order", "bill", "consignment_in"):
        raise HTTPException(status_code=409, detail="receive is only valid for bills, purchase orders, and consignment_in documents")

    try:
        location_uuid = uuid.UUID(payload.location_id)
    except Exception:
        location_uuid = None

    is_consignment = doc_type == "consignment_in"

    for it in payload.received_items:
        if it.item_id:
            item = await session.get(Projection, {"company_id": company_id, "entity_id": it.item_id})
            if item is None:
                raise HTTPException(status_code=404, detail=f"Item not found: {it.item_id}")
            new_qty = float(item.state.get("quantity", 0) or 0) + float(it.quantity_received)
            await emit_event(
                session, company_id=company_id, entity_id=it.item_id, entity_type="item", event_type="item.quantity.adjusted",
                data={"new_qty": new_qty, **({"consignment_flag": "in"} if is_consignment else {})},
                actor_id=user.id, location_id=None, source="api",
                idempotency_key=str(uuid.uuid4()), metadata_={"source_doc": entity_id},
            )
        else:
            if not it.sku or not it.name:
                raise HTTPException(status_code=422, detail="sku and name required when creating received item")
            item_data: dict = {"sku": it.sku, "name": it.name, "quantity": it.quantity_received, "location_id": payload.location_id}
            if is_consignment:
                item_data["consignment_flag"] = "in"
            await emit_event(
                session,
                company_id=company_id,
                entity_id=f"item:{uuid.uuid4()}",
                entity_type="item",
                event_type="item.created",
                data=item_data,
                actor_id=user.id,
                location_id=location_uuid,
                source="api",
                idempotency_key=str(uuid.uuid4()),
                metadata_={"source_doc": entity_id},
            )

    entry = await emit_event(
        session, company_id=company_id, entity_id=entity_id, entity_type="doc", event_type="doc.received",
        data={
            "received_items": [x.model_dump(exclude_none=True) for x in payload.received_items],
            "location_id": payload.location_id,
            "received_by": str(user.id),
            "notes": payload.notes,
        },
        actor_id=user.id, location_id=location_uuid, source="api",
        idempotency_key=payload.idempotency_key or str(uuid.uuid4()), metadata_={},
    )

    if not is_consignment:
        # PO/Bill: create accounting journal entry; consignment_in has no JE (goods not owned)
        po_total = float(row.state.get("total", 0) or 0)
        if po_total == 0:
            po_total = sum(
                float(li.get("quantity", 0) or 0) * float(li.get("unit_price", 0) or 0)
                for li in row.state.get("line_items", [])
            )
        await auto_je.create_for_po_received(
            session,
            company_id=company_id,
            user_id=user.id,
            po_id=entity_id,
            doc=row.state,
            total=po_total,
        )
    await session.commit()
    return {"event_id": entry.id}


class ReturnItem(BaseModel):
    item_id: str
    quantity_returned: float


class ReturnBody(BaseModel):
    items: list[ReturnItem]
    notes: str | None = None
    idempotency_key: str | None = None


@router.post("/{entity_id}/return-items")
async def return_consignment_items(entity_id: str, payload: ReturnBody, company_id: str = Depends(get_current_company_id), user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    row = await _get_doc(session, company_id, entity_id)
    doc_type = row.state.get("doc_type")
    if doc_type not in ("consignment_in", "bill", "purchase_order"):
        raise HTTPException(status_code=409, detail="return-items is only valid for bills, POs, and consignment_in documents")
    if row.state.get("status") not in ("received", "partially_received", "partial_returned", "awaiting_payment"):
        raise HTTPException(status_code=409, detail="Document must be in received/partial/awaiting_payment status to return items")

    for it in payload.items:
        item = await session.get(Projection, {"company_id": company_id, "entity_id": it.item_id})
        if item is None:
            raise HTTPException(status_code=404, detail=f"Item not found: {it.item_id}")
        current_qty = float(item.state.get("quantity", 0) or 0)
        if it.quantity_returned > current_qty + 1e-9:
            raise HTTPException(status_code=409, detail=f"Cannot return more than on-hand quantity for {it.item_id}")
        new_qty = max(0.0, current_qty - it.quantity_returned)
        await emit_event(
            session, company_id=company_id, entity_id=it.item_id, entity_type="item",
            event_type="item.quantity.adjusted",
            data={"new_qty": new_qty, "consignment_flag": None if new_qty == 0 else "in"},
            actor_id=user.id, location_id=None, source="api",
            idempotency_key=str(uuid.uuid4()), metadata_={"source_return": entity_id},
        )

    entry = await emit_event(
        session, company_id=company_id, entity_id=entity_id, entity_type="doc",
        event_type="doc.items_returned",
        data={
            "items": [it.model_dump() for it in payload.items],
            "returned_by": str(user.id),
            "notes": payload.notes,
        },
        actor_id=user.id, location_id=None, source="api",
        idempotency_key=payload.idempotency_key or str(uuid.uuid4()), metadata_={},
    )
    await session.commit()
    return {"event_id": entry.id}


@router.post("/{entity_id}/convert")
async def convert_doc(entity_id: str, company_id: str = Depends(get_current_company_id), user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    row = await _get_doc(session, company_id, entity_id)
    state = row.state
    if state.get("doc_type") == "quotation":
        if state.get("status") in {"void", "converted"}:
            raise HTTPException(status_code=409, detail="Cannot convert quotation in current status")
        valid_until = state.get("valid_until")
        if valid_until and valid_until < datetime.now(UTC).date().isoformat():
            raise HTTPException(status_code=409, detail="Cannot convert expired quotation")
        company = await session.get(Company, company_id)
        ref = next_doc_ref(company, "invoice")
        new_doc_id = f"doc:{ref}"
        new_data = {k: v for k, v in state.items() if k not in {"status", "entity_type"}}
        new_data.update({"doc_type": "invoice", "ref_id": ref, "source_quotation_id": entity_id, "status": "draft"})
        await emit_event(
            session, company_id=company_id, entity_id=new_doc_id, entity_type="doc", event_type="doc.created", data=new_data,
            actor_id=user.id, location_id=None, source="api", idempotency_key=str(uuid.uuid4()), metadata_={},
        )
        entry = await emit_event(
            session, company_id=company_id, entity_id=entity_id, entity_type="doc", event_type="doc.converted",
            data={"target_doc_id": new_doc_id, "target_doc_type": "invoice"}, actor_id=user.id, location_id=None,
            source="api", idempotency_key=str(uuid.uuid4()), metadata_={},
        )
        await session.commit()
        return {"event_id": entry.id, "target_doc_id": new_doc_id}

    if state.get("doc_type") == "memo":
        if state.get("status") not in ("final", "sent", "received", "partially_received"):
            raise HTTPException(status_code=409, detail="Memo must be issued before converting to invoice")
        company = await session.get(Company, company_id)
        ref = next_doc_ref(company, "invoice")
        new_doc_id = f"doc:{ref}"
        new_data = {k: v for k, v in state.items() if k not in {"status", "entity_type"}}
        new_data.update({"doc_type": "invoice", "ref_id": ref, "source_memo_id": entity_id, "status": "draft"})
        await emit_event(
            session, company_id=company_id, entity_id=new_doc_id, entity_type="doc", event_type="doc.created", data=new_data,
            actor_id=user.id, location_id=None, source="api", idempotency_key=str(uuid.uuid4()), metadata_={},
        )
        entry = await emit_event(
            session, company_id=company_id, entity_id=entity_id, entity_type="doc", event_type="doc.converted",
            data={"target_doc_id": new_doc_id, "target_doc_type": "invoice"}, actor_id=user.id, location_id=None,
            source="api", idempotency_key=str(uuid.uuid4()), metadata_={},
        )
        await session.commit()
        return {"event_id": entry.id, "target_doc_id": new_doc_id}

    if state.get("doc_type") == "consignment_in":
        if state.get("status") not in ("final", "sent", "received", "partially_received"):
            raise HTTPException(status_code=409, detail="Consignment In must be issued before converting to vendor bill")
        company = await session.get(Company, company_id)
        ref = next_doc_ref(company, "bill")
        new_doc_id = f"doc:{ref}"
        new_data = {k: v for k, v in state.items() if k not in {"status", "entity_type"}}
        new_data.update({"doc_type": "bill", "ref_id": ref, "source_consignment_id": entity_id, "status": "awaiting_payment"})
        await emit_event(
            session, company_id=company_id, entity_id=new_doc_id, entity_type="doc", event_type="doc.created", data=new_data,
            actor_id=user.id, location_id=None, source="api", idempotency_key=str(uuid.uuid4()), metadata_={},
        )
        # Create accounting JE for the bill
        bill_total = float(state.get("total", 0) or 0)
        if bill_total == 0:
            bill_total = sum(
                float(li.get("quantity", 0) or 0) * float(li.get("unit_price", 0) or 0)
                for li in state.get("line_items", [])
            )
        await auto_je.create_for_bill_conversion(
            session, company_id=company_id, user_id=user.id, po_id=new_doc_id, doc=state, total=bill_total,
        )
        entry = await emit_event(
            session, company_id=company_id, entity_id=entity_id, entity_type="doc", event_type="doc.converted",
            data={"target_doc_id": new_doc_id, "target_doc_type": "bill"}, actor_id=user.id, location_id=None,
            source="api", idempotency_key=str(uuid.uuid4()), metadata_={},
        )
        await session.commit()
        return {"event_id": entry.id, "target_doc_id": new_doc_id}

    raise HTTPException(status_code=409, detail="Unsupported document conversion")


class NoteAddBody(BaseModel):
    text: str


@router.post("/{entity_id}/notes")
async def add_doc_note(
    entity_id: str,
    payload: NoteAddBody,
    company_id: str = Depends(get_current_company_id),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    row = await _get_doc(session, company_id, entity_id)
    if not payload.text.strip():
        raise HTTPException(status_code=422, detail="Note text cannot be empty")
    now = datetime.now(UTC).isoformat()
    entry = await emit_event(
        session, company_id=company_id, entity_id=entity_id, entity_type="doc",
        event_type="doc.note_added",
        data={"text": payload.text.strip(), "created_at": now, "created_by": str(user.id)},
        actor_id=user.id, location_id=None, source="api",
        idempotency_key=str(uuid.uuid4()), metadata_={},
    )
    await session.commit()
    return {"event_id": entry.id}


@router.post("/import")
async def import_doc(
    body: DocImportRecord,
    company_id: str = Depends(get_current_company_id),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    # Entity guard: reject duplicate doc.created for same entity_id.
    # Exception: allow idempotent retry (same idempotency_key already exists).
    if body.event_type == "doc.created":
        existing = await session.get(Projection, {"company_id": company_id, "entity_id": body.entity_id})
        if existing is not None:
            from sqlalchemy import select as _select

            from celerp.models.ledger import LedgerEntry

            existing_event = (
                await session.execute(
                    _select(LedgerEntry).where(
                        LedgerEntry.company_id == company_id,
                        LedgerEntry.idempotency_key == body.idempotency_key,
                    )
                )
            ).scalar_one_or_none()
            if existing_event is not None:
                return {"event_id": existing_event.id, "id": body.entity_id, "idempotency_hit": True}

            raise HTTPException(
                status_code=409,
                detail=f"Document {body.entity_id} already exists (status: {existing.state.get('status', 'unknown')}). "
                f"Use PATCH to update or lifecycle endpoints to advance its state.",
            )

    entry = await emit_event(
        session,
        company_id=company_id,
        entity_id=body.entity_id,
        entity_type="doc",
        event_type=body.event_type,
        data=body.data,
        actor_id=user.id,
        location_id=None,
        source=body.source,
        idempotency_key=body.idempotency_key,
        metadata_={"source_ts": body.source_ts} if body.source_ts else {},
    )

    # Post-import auto-JE hook: if the imported doc is already in a final state, create JEs
    if body.event_type == "doc.created":
        await _import_auto_je(session, company_id, user.id, body.entity_id, body.data)

    await session.commit()
    return {"event_id": entry.id, "id": body.entity_id, "idempotency_hit": False}


async def _import_auto_je(session: AsyncSession, company_id, user_id, entity_id: str, data: dict) -> None:
    """Create auto-JEs for imported docs that arrive in a final state.

    Uses doc-scoped idempotency keys - safe to call multiple times.
    """
    doc_type = data.get("doc_type", "")
    status = data.get("status", "draft")
    total = float(data.get("total", 0) or 0)

    if status in ("void", "draft", "converted", "expired") or total <= 0:
        return

    if doc_type == "invoice" and status in ("sent", "final", "partial", "paid", "awaiting_payment"):
        await auto_je.create_for_doc_finalized(
            session, company_id=company_id, user_id=user_id, doc_id=entity_id, doc=data,
        )
        amount_paid = float(data.get("amount_paid", 0) or 0)
        if amount_paid > 0:
            await auto_je.create_for_doc_payment(
                session, company_id=company_id, user_id=user_id, doc_id=entity_id,
                amount=amount_paid, cumulative_paid=amount_paid,
            )

    elif doc_type == "purchase_order" and status in ("received", "partially_received", "final"):
        await auto_je.create_for_po_received(
            session, company_id=company_id, user_id=user_id, po_id=entity_id, doc=data, total=total,
        )


@router.post("/import/batch", response_model=BatchImportResult)
async def batch_import_docs(
    body: DocBatchImportRequest,
    company_id: str = Depends(get_current_company_id),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> BatchImportResult:
    from sqlalchemy import select as _select

    from celerp.models.ledger import LedgerEntry

    keys = [r.idempotency_key for r in body.records]
    existing_keys = set((await session.execute(
        _select(LedgerEntry.idempotency_key).where(LedgerEntry.idempotency_key.in_(keys))
    )).scalars().all())

    # Pre-check existing entity_ids for doc.created events (entity guard)
    create_entity_ids = [r.entity_id for r in body.records if r.event_type == "doc.created"]
    existing_entities: set[str] = set()
    if create_entity_ids:
        existing_entities = set((await session.execute(
            _select(Projection.entity_id).where(
                Projection.company_id == company_id,
                Projection.entity_id.in_(create_entity_ids),
            )
        )).scalars().all())

    created = skipped = updated = 0
    skipped_existing = 0
    errors: list[str] = []
    for rec in body.records:
        if rec.idempotency_key in existing_keys:
            if body.upsert:
                upsert_idem = f"{rec.idempotency_key}:upsert"
                upsert_already = set(
                    (await session.execute(
                        _select(LedgerEntry.idempotency_key).where(
                            LedgerEntry.idempotency_key == upsert_idem
                        )
                    )).scalars().all()
                )
                if upsert_idem in upsert_already:
                    skipped += 1
                    continue
                try:
                    await emit_event(
                        session,
                        company_id=company_id,
                        entity_id=rec.entity_id,
                        entity_type="doc",
                        event_type="doc.patched",
                        data=rec.data,
                        actor_id=user.id,
                        location_id=None,
                        source=rec.source,
                        idempotency_key=upsert_idem,
                        metadata_={"source_ts": rec.source_ts} if rec.source_ts else {},
                    )
                    updated += 1
                except Exception as exc:
                    if len(errors) < 10:
                        errors.append(f"{rec.entity_id}: {exc}")
            else:
                skipped += 1
            continue
        if rec.event_type == "doc.created" and rec.entity_id in existing_entities:
            skipped_existing += 1
            continue
        try:
            await emit_event(
                session,
                company_id=company_id,
                entity_id=rec.entity_id,
                entity_type="doc",
                event_type=rec.event_type,
                data=rec.data,
                actor_id=user.id,
                location_id=None,
                source=rec.source,
                idempotency_key=rec.idempotency_key,
                metadata_={"source_ts": rec.source_ts} if rec.source_ts else {},
            )
            existing_keys.add(rec.idempotency_key)
            if rec.event_type == "doc.created":
                existing_entities.add(rec.entity_id)
                await _import_auto_je(session, company_id, user.id, rec.entity_id, rec.data)
            created += 1
        except Exception as exc:
            if len(errors) < 10:
                errors.append(f"{rec.entity_id}: {exc}")

    await session.commit()
    return BatchImportResult(created=created, skipped=skipped + skipped_existing, updated=updated, errors=errors)


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------


@router.get("/export/csv")
async def export_docs_csv(
    company_id: str = Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
    q: str | None = None,
    doc_type: str | None = None,
    status: str | None = None,
) -> StreamingResponse:
    rows = (await session.execute(
        select(Projection).where(Projection.company_id == company_id, Projection.entity_type == "doc")
    )).scalars().all()
    docs = [r.state | {"entity_id": r.entity_id} for r in rows]
    if q:
        ql = q.lower()
        docs = [d for d in docs if ql in str(d.get("doc_number", "")).lower() or ql in str(d.get("contact_name", "")).lower()]
    if doc_type:
        docs = [d for d in docs if d.get("doc_type", d.get("type", "")) == doc_type]
    if status:
        docs = [d for d in docs if d.get("status") == status]

    _COLS = ["entity_id", "doc_number", "doc_type", "contact_name", "date", "due_date", "total", "amount_outstanding", "status"]
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=_COLS, extrasaction="ignore")
    writer.writeheader()
    for d in docs:
        writer.writerow({c: d.get(c, "") for c in _COLS})
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=documents.csv"},
    )


# ---------------------------------------------------------------------------
# List routes (formerly list_routes.py) - merged here to eliminate WET copy
# ---------------------------------------------------------------------------

lists_router = APIRouter(dependencies=[Depends(get_current_user)])


class ListCreatePayload(BaseModel):
    list_type: str | None = None
    ref_id: str | None = None
    customer_id: str | None = None
    customer_name: str | None = None
    line_items: list[dict] = Field(default_factory=list)
    subtotal: float = 0
    discount: float = 0
    discount_type: str = "flat"
    tax: float = 0
    total: float = 0
    currency: str | None = None
    notes: str | None = None
    status: str = "draft"
    share_token: str | None = None
    idempotency_key: str | None = None
    model_config = {"extra": "allow"}


ListPatch = DocPatch
ListSendBody = DocSendBody
ListVoidBody = DocVoidBody


class ListConvertBody(BaseModel):
    target_type: str  # invoice or memo


ListImportRecord = DocImportRecord
ListBatchImportRequest = DocBatchImportRequest


async def _get_list(session: AsyncSession, company_id, entity_id: str) -> Projection:
    row = await session.get(Projection, {"company_id": company_id, "entity_id": entity_id})
    if row is None or row.entity_type != "list":
        raise HTTPException(status_code=404, detail="List not found")
    return row


async def _emit_list(session, company_id, entity_id, event_type, data, user, idem_key=None, meta=None):
    return await emit_event(
        session, company_id=company_id, entity_id=entity_id, entity_type="list",
        event_type=event_type, data=data, actor_id=user.id, location_id=None,
        source="api", idempotency_key=idem_key or str(uuid.uuid4()), metadata_=meta or {},
    )


@lists_router.get("")
async def list_lists(
    list_type: str | None = None,
    status: str | None = None,
    exclude_status: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    q: str | None = None,
    limit: int | None = None,
    offset: int = 0,
    company_id: str = Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    rows = (await session.execute(
        select(Projection).where(Projection.company_id == company_id, Projection.entity_type == "list")
    )).scalars().all()
    out = [r.state | {"id": r.entity_id} for r in rows]
    if list_type:
        out = [x for x in out if x.get("list_type") == list_type]
    if status:
        out = [x for x in out if x.get("status") == status]
    if exclude_status:
        out = [x for x in out if x.get("status") != exclude_status]
    if date_from:
        out = [x for x in out if (x.get("created_at") or x.get("date") or "")[:10] >= date_from]
    if date_to:
        out = [x for x in out if (x.get("created_at") or x.get("date") or "")[:10] <= date_to]
    if q:
        ql = q.lower()
        out = [x for x in out if ql in str(x.get("ref_id") or "").lower()
               or ql in str(x.get("customer_name") or x.get("customer_id") or "").lower()]
    out.sort(key=lambda x: x.get("created_at") or x.get("date") or "", reverse=True)
    total = len(out)
    if offset:
        out = out[offset:]
    if limit is not None:
        out = out[:limit]
    return {"items": out, "total": total}


@lists_router.get("/summary")
async def get_list_summary(
    company_id: str = Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    rows = (await session.execute(
        select(Projection).where(Projection.company_id == company_id, Projection.entity_type == "list")
    )).scalars().all()
    count_by_status: dict[str, int] = {}
    total_value = 0.0
    for row in rows:
        st = row.state.get("status", "")
        count_by_status[st] = count_by_status.get(st, 0) + 1
        if st != "void":
            total_value += float(row.state.get("total", 0) or 0)
    return {
        "total_count": len(rows),
        "draft_count": count_by_status.get("draft", 0),
        "total_value": total_value,
        "count_by_status": count_by_status,
    }


@lists_router.get("/export/csv")
async def export_lists_csv(
    company_id: str = Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
    q: str | None = None,
    list_type: str | None = None,
    status: str | None = None,
) -> StreamingResponse:
    rows = (await session.execute(
        select(Projection).where(Projection.company_id == company_id, Projection.entity_type == "list")
    )).scalars().all()
    items = [r.state | {"id": r.entity_id} for r in rows]
    if q:
        ql = q.lower()
        items = [d for d in items if ql in str(d.get("ref_id", "")).lower() or ql in str(d.get("customer_name", "")).lower()]
    if list_type:
        items = [d for d in items if d.get("list_type") == list_type]
    if status:
        items = [d for d in items if d.get("status") == status]
    _COLS = ["id", "ref_id", "list_type", "customer_name", "date", "total", "status"]
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=_COLS, extrasaction="ignore")
    writer.writeheader()
    for d in items:
        writer.writerow({c: d.get(c, "") for c in _COLS})
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=lists.csv"},
    )


@lists_router.get("/{entity_id}")
async def get_list(
    entity_id: str,
    company_id: str = Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    row = await _get_list(session, company_id, entity_id)
    return row.state | {"id": row.entity_id}


@lists_router.post("")
async def create_list(
    payload: ListCreatePayload,
    company_id: str = Depends(get_current_company_id),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    company = await session.get(Company, company_id)
    ref_id = payload.ref_id or next_doc_ref(company, "list")
    entity_id = f"list:{ref_id}"

    # Uniqueness check
    existing = await session.get(Projection, {"company_id": company_id, "entity_id": entity_id})
    if existing is not None:
        raise HTTPException(status_code=409, detail=f"List number '{ref_id}' already exists")

    data = payload.model_dump(exclude_none=True)
    data["ref_id"] = ref_id
    data.setdefault("currency", company.settings.get("currency", "USD"))
    entry = await _emit_list(session, company_id, entity_id, "list.created", data, user, payload.idempotency_key)
    await session.commit()
    return {"event_id": entry.id, "id": entity_id}


@lists_router.patch("/{entity_id}")
async def patch_list(
    entity_id: str,
    payload: ListPatch,
    company_id: str = Depends(get_current_company_id),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    row = await _get_list(session, company_id, entity_id)
    if row.state.get("status") != "draft":
        raise HTTPException(status_code=409, detail="Cannot edit non-draft list")
    entry = await _emit_list(session, company_id, entity_id, "list.updated",
                             payload.model_dump(exclude_none=True), user, payload.idempotency_key)
    await session.commit()
    return {"event_id": entry.id}


@lists_router.post("/{entity_id}/send")
async def send_list(
    entity_id: str,
    payload: ListSendBody = ListSendBody(),
    company_id: str = Depends(get_current_company_id),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    row = await _get_list(session, company_id, entity_id)
    if row.state.get("status") not in ("draft",):
        raise HTTPException(status_code=409, detail="Can only send draft lists")
    entry = await _emit_list(session, company_id, entity_id, "list.sent",
                             payload.model_dump(exclude_none=True), user, payload.idempotency_key)
    await session.commit()
    return {"event_id": entry.id}


@lists_router.post("/{entity_id}/accept")
async def accept_list(
    entity_id: str,
    company_id: str = Depends(get_current_company_id),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    row = await _get_list(session, company_id, entity_id)
    if row.state.get("status") != "sent":
        raise HTTPException(status_code=409, detail="Can only accept sent lists")
    entry = await _emit_list(session, company_id, entity_id, "list.accepted", {}, user)
    await session.commit()
    return {"event_id": entry.id}


@lists_router.post("/{entity_id}/complete")
async def complete_list(
    entity_id: str,
    company_id: str = Depends(get_current_company_id),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    row = await _get_list(session, company_id, entity_id)
    if row.state.get("status") != "accepted":
        raise HTTPException(status_code=409, detail="Can only complete accepted lists")
    entry = await _emit_list(session, company_id, entity_id, "list.completed", {}, user)
    await session.commit()
    return {"event_id": entry.id}


@lists_router.post("/{entity_id}/void")
async def void_list(
    entity_id: str,
    payload: ListVoidBody = ListVoidBody(),
    company_id: str = Depends(get_current_company_id),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    row = await _get_list(session, company_id, entity_id)
    if row.state.get("status") == "void":
        raise HTTPException(status_code=409, detail="Already voided")
    entry = await _emit_list(session, company_id, entity_id, "list.voided",
                             payload.model_dump(exclude_none=True), user, payload.idempotency_key)
    await session.commit()
    return {"event_id": entry.id}


@lists_router.delete("/{entity_id}")
async def delete_list(
    entity_id: str,
    company_id: str = Depends(get_current_company_id),
    _: None = Depends(require_manager),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    row = await _get_list(session, company_id, entity_id)
    if row.state.get("status") != "draft":
        raise HTTPException(status_code=409, detail="Only draft lists can be deleted")
    from celerp.models.ledger import LedgerEntry
    import sqlalchemy as _sa
    await session.execute(_sa.delete(Projection).where(Projection.company_id == company_id, Projection.entity_id == entity_id))
    await session.execute(_sa.delete(LedgerEntry).where(LedgerEntry.company_id == company_id, LedgerEntry.entity_id == entity_id))
    await session.commit()
    return {"deleted": entity_id}


@lists_router.post("/{entity_id}/convert")
async def convert_list(
    entity_id: str,
    payload: ListConvertBody,
    company_id: str = Depends(get_current_company_id),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    row = await _get_list(session, company_id, entity_id)
    state = row.state
    if state.get("status") in ("void", "converted"):
        raise HTTPException(status_code=409, detail="Cannot convert list in current status")
    if payload.target_type not in ("invoice", "memo"):
        raise HTTPException(status_code=422, detail="target_type must be 'invoice' or 'memo'")

    company = await session.get(Company, company_id)
    ref = next_doc_ref(company, payload.target_type)
    new_doc_id = f"doc:{ref}"
    new_data = {k: v for k, v in state.items() if k not in {"status", "entity_type", "list_type"}}
    new_data.update({"doc_type": payload.target_type, "ref_id": ref, "source_list_id": entity_id, "status": "draft"})
    await emit_event(
        session, company_id=company_id, entity_id=new_doc_id, entity_type="doc",
        event_type="doc.created", data=new_data, actor_id=user.id, location_id=None,
        source="api", idempotency_key=str(uuid.uuid4()), metadata_={},
    )
    entry = await _emit_list(session, company_id, entity_id, "list.converted",
                             {"target_doc_id": new_doc_id, "target_doc_type": payload.target_type}, user)
    await session.commit()
    return {"event_id": entry.id, "target_doc_id": new_doc_id}


@lists_router.post("/{entity_id}/duplicate")
async def duplicate_list(
    entity_id: str,
    company_id: str = Depends(get_current_company_id),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    row = await _get_list(session, company_id, entity_id)
    state = row.state
    company = await session.get(Company, company_id)
    ref_id = next_doc_ref(company, "list")
    new_entity_id = f"list:{ref_id}"
    new_data = {k: v for k, v in state.items() if k not in {"status", "entity_type", "ref_id", "share_token"}}
    new_data.update({"ref_id": ref_id, "status": "draft", "source_list_id": entity_id})
    entry = await _emit_list(session, company_id, new_entity_id, "list.created", new_data, user)
    await session.commit()
    return {"event_id": entry.id, "id": new_entity_id}


@lists_router.post("/{entity_id}/notes")
async def add_list_note(
    entity_id: str,
    payload: NoteAddBody,
    company_id: str = Depends(get_current_company_id),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    row = await _get_list(session, company_id, entity_id)
    if not payload.text.strip():
        raise HTTPException(status_code=422, detail="Note text cannot be empty")
    now = datetime.now(UTC).isoformat()
    entry = await _emit_list(
        session, company_id, entity_id, "doc.note_added",
        {"text": payload.text.strip(), "created_at": now, "created_by": str(user.id)},
        user,
    )
    await session.commit()
    return {"event_id": entry.id}


@lists_router.post("/import")
async def import_list(
    body: ListImportRecord,
    company_id: str = Depends(get_current_company_id),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    if body.event_type == "list.created":
        existing = await session.get(Projection, {"company_id": company_id, "entity_id": body.entity_id})
        if existing is not None:
            from sqlalchemy import select as _select
            from celerp.models.ledger import LedgerEntry
            existing_event = (await session.execute(
                _select(LedgerEntry).where(
                    LedgerEntry.company_id == company_id,
                    LedgerEntry.idempotency_key == body.idempotency_key,
                )
            )).scalar_one_or_none()
            if existing_event is not None:
                return {"event_id": existing_event.id, "id": body.entity_id, "idempotency_hit": True}
            raise HTTPException(status_code=409, detail=f"List {body.entity_id} already exists")

    entry = await emit_event(
        session, company_id=company_id, entity_id=body.entity_id, entity_type="list",
        event_type=body.event_type, data=body.data, actor_id=user.id, location_id=None,
        source=body.source, idempotency_key=body.idempotency_key,
        metadata_={"source_ts": body.source_ts} if body.source_ts else {},
    )
    await session.commit()
    return {"event_id": entry.id, "id": body.entity_id, "idempotency_hit": False}


@lists_router.get("/import/template", include_in_schema=False)
async def import_lists_template():
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse(
        "entity_id,event_type,idempotency_key,ref_id,list_type,customer_id,customer_name,total,currency,status\n",
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=lists.csv"},
    )


@lists_router.post("/import/batch", response_model=BatchImportResult)
async def batch_import_lists(
    body: ListBatchImportRequest,
    company_id: str = Depends(get_current_company_id),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> BatchImportResult:
    from sqlalchemy import select as _select
    from celerp.models.ledger import LedgerEntry

    keys = [r.idempotency_key for r in body.records]
    existing_keys = set((await session.execute(
        _select(LedgerEntry.idempotency_key).where(LedgerEntry.idempotency_key.in_(keys))
    )).scalars().all())

    create_entity_ids = [r.entity_id for r in body.records if r.event_type == "list.created"]
    existing_entities: set[str] = set()
    if create_entity_ids:
        existing_entities = set((await session.execute(
            _select(Projection.entity_id).where(
                Projection.company_id == company_id,
                Projection.entity_id.in_(create_entity_ids),
            )
        )).scalars().all())

    created = skipped = updated = 0
    errors: list[str] = []
    for rec in body.records:
        if rec.idempotency_key in existing_keys:
            if body.upsert:
                upsert_idem = f"{rec.idempotency_key}:upsert"
                upsert_already = set(
                    (await session.execute(
                        _select(LedgerEntry.idempotency_key).where(
                            LedgerEntry.idempotency_key == upsert_idem
                        )
                    )).scalars().all()
                )
                if upsert_idem in upsert_already:
                    skipped += 1
                    continue
                try:
                    await emit_event(
                        session, company_id=company_id, entity_id=rec.entity_id, entity_type="list",
                        event_type="list.patched", data=rec.data, actor_id=user.id, location_id=None,
                        source=rec.source, idempotency_key=upsert_idem,
                        metadata_={"source_ts": rec.source_ts} if rec.source_ts else {},
                    )
                    updated += 1
                except Exception as exc:
                    if len(errors) < 10:
                        errors.append(f"{rec.entity_id}: {exc}")
            else:
                skipped += 1
            continue
        if rec.event_type == "list.created" and rec.entity_id in existing_entities:
            skipped += 1
            continue
        try:
            await emit_event(
                session, company_id=company_id, entity_id=rec.entity_id, entity_type="list",
                event_type=rec.event_type, data=rec.data, actor_id=user.id, location_id=None,
                source=rec.source, idempotency_key=rec.idempotency_key,
                metadata_={"source_ts": rec.source_ts} if rec.source_ts else {},
            )
            existing_keys.add(rec.idempotency_key)
            if rec.event_type == "list.created":
                existing_entities.add(rec.entity_id)
            created += 1
        except Exception as exc:
            if len(errors) < 10:
                errors.append(f"{rec.entity_id}: {exc}")

    await session.commit()
    return BatchImportResult(created=created, skipped=skipped, updated=updated, errors=errors)


# ---------------------------------------------------------------------------
# Fulfillment endpoints
# ---------------------------------------------------------------------------


@router.post("/{entity_id}/fulfill")
async def fulfill_doc(
    entity_id: str,
    company_id: str = Depends(get_current_company_id),
    _: None = Depends(require_manager),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Fulfill a document: compute pick plan and deduct inventory."""
    row = await _get_doc(session, company_id, entity_id)
    state = row.state
    if state.get("status") not in FULFILLABLE_STATUSES:
        raise HTTPException(status_code=409, detail="Document must be finalized before fulfillment")
    if state.get("fulfillment_status") == "fulfilled":
        raise HTTPException(status_code=409, detail="Document is already fully fulfilled")

    # Gather available inventory for SKUs in line items
    skus: set[str] = set()
    for li in state.get("line_items", []):
        sku = li.get("sku") or ""
        if sku and (li.get("sell_by") or "") not in ("service", "hour"):
            skus.add(sku)

    available_inv: list[dict] = []
    if skus:
        from sqlalchemy import select as _sel
        rows = (await session.execute(
            _sel(Projection).where(
                Projection.company_id == company_id,
                Projection.entity_type == "item",
            )
        )).scalars().all()
        for r in rows:
            s = r.state
            qty = float(s.get("quantity") or 0)
            item_sku = s.get("sku") or ""
            if qty <= 0 or not item_sku:
                continue
            # Match exact or child prefix
            matched = any(item_sku == sku or item_sku.startswith(f"{sku}.") for sku in skus)
            if matched:
                available_inv.append({
                    "entity_id": r.entity_id,
                    "sku": item_sku,
                    "quantity": qty,
                    "created_at": s.get("created_at") or "",
                    "expires_at": s.get("expires_at"),
                    "cost_price": float(s.get("cost_price") or 0),
                })

    pick_result = compute_pick_plan(state.get("line_items", []), available_inv)
    result = await execute_fulfill(
        session,
        doc_entity_id=entity_id,
        doc_state=state,
        pick_result=pick_result,
        company_id=company_id,
        user_id=user.id,
    )
    await session.commit()
    return result


@router.post("/{entity_id}/unfulfill")
async def unfulfill_doc(
    entity_id: str,
    company_id: str = Depends(get_current_company_id),
    _: None = Depends(require_manager),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Reverse fulfillment: restore inventory and clear fulfillment state."""
    row = await _get_doc(session, company_id, entity_id)
    state = row.state
    fs = state.get("fulfillment_status")
    if not fs or fs == "unfulfilled":
        raise HTTPException(status_code=409, detail="Document is not fulfilled")

    result = await execute_unfulfill(
        session,
        doc_entity_id=entity_id,
        doc_state=state,
        company_id=company_id,
        user_id=user.id,
        reason="manual",
    )
    await session.commit()
    return result
