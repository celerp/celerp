# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

from __future__ import annotations

import asyncio
import csv
import io
import uuid
from datetime import UTC, datetime, date as _date

from fastapi import APIRouter, Depends, Form, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import select, func as _func
import sqlalchemy as _sa
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.db import get_session
from celerp.events.engine import emit_event
from celerp.models.company import Company
from celerp.modules.slots import fire_lifecycle
from celerp.models.projections import Projection
from celerp_docs.taxes import TaxApplication, compute_tax_amounts
from celerp.services import auto_je
from celerp.services.attachments import store_upload
from ui.components.currency import CURRENCY_CODES
from celerp.services.auth import get_current_company_id, get_current_user, require_manager, require_operator
from celerp_docs.sequences import next_doc_ref, get_all_sequences, update_sequence, validate_pattern
from celerp.services.units import DEFAULT_UNITS, SERVICE_SELL_BY, build_unit_map, is_pieces_unit, is_weight_unit, validate_line_quantity
from celerp.services.money import round_money, to_decimal, to_stored_float
from celerp_docs.doc_constants import INBOUND_DOC_TYPES, FULFILLABLE_STATUSES, FULFILLED_ITEM_STATUSES, NON_FINANCIAL_DOC_TYPES

router = APIRouter(dependencies=[Depends(get_current_user)])


class LineItem(BaseModel):
    item_id: str | None = None
    entity_id: str | None = None  # alias sent by the frontend; resolved to item_id below
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
    # Invoice fulfillment: how much of the linked parcel this line draws, by piece
    # and by weight. Drive the split-on-fulfill (child_pieces / child_weight). Only
    # editable when the parcel actually tracks that measure and splitting is allowed.
    pieces: float | None = None
    weight: float | None = None

    @model_validator(mode="after")
    def _resolve_entity_id(self) -> "LineItem":
        """Frontend sends entity_id; normalise to item_id so the stored state is consistent."""
        if self.entity_id and not self.item_id:
            self.item_id = self.entity_id
        self.entity_id = None  # never persist entity_id; always use item_id
        return self


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
    cc: str | None = None
    bcc: str | None = None
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
    payment_date: str  # ISO date (YYYY-MM-DD), always required
    currency: str | None = None
    method: str | None = None
    reference: str | None = None
    bank_account: str | None = None
    conversion_rate: float | None = None  # pass-through for premium multicurrency module
    source_doc_id: str | None = None
    target_doc_id: str | None = None
    idempotency_key: str | None = None


class ReceivedItem(BaseModel):
    po_line_index: int = -1  # optional; -1 means not specified (e.g. one-click bill receive)
    item_id: str | None = None
    quantity_received: float
    condition: str = "good"
    sku: str | None = None
    name: str | None = None
    cost_price: float | None = None
    receive_as: str = "stock"
    category: str | None = None
    attributes: dict | None = None


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


class FulfillLinesRequest(BaseModel):
    line_entity_ids: list[str]

    @field_validator("line_entity_ids")
    @classmethod
    def strip_empty(cls, v: list[str]) -> list[str]:
        """Drop empty strings and de-duplicate — JS bulk selects may include value="" rows."""
        return list(dict.fromkeys(eid for eid in v if eid))


def _assert_date_order(patch: dict, current: dict | None = None) -> None:
    """Raise 422 if due_date is set and earlier than issue_date.

    Merges patch over current so partial updates are validated against the full
    resulting state, not just the fields being changed.
    """
    merged = {**(current or {}), **patch}
    issue = merged.get("issue_date") or ""
    due = merged.get("due_date") or ""
    if issue and due and due < issue:
        raise HTTPException(
            status_code=422,
            detail="due_date cannot be earlier than issue_date",
        )


async def _get_doc(session: AsyncSession, company_id, entity_id: str) -> Projection:
    row = await session.get(Projection, {"company_id": company_id, "entity_id": entity_id})
    if row is None or row.entity_type != "doc":
        raise HTTPException(status_code=404, detail="Document not found")
    return row


async def _get_unit_map(session: AsyncSession, company_id: str) -> dict[str, dict]:
    """Return a name-keyed unit map for the company (falls back to DEFAULT_UNITS)."""
    company = await session.get(Company, company_id)
    units = (company.settings or {}).get("units") if company else None
    return build_unit_map(units if units else DEFAULT_UNITS)


async def _get_item_sell_by_map(session: AsyncSession, company_id: str) -> dict[str, str]:
    """Return a SKU -> sell_by mapping for all inventory items in the company.

    Used to resolve sell_by when it is not present on a document line item.
    """
    rows = (
        await session.execute(
            select(Projection).where(
                Projection.company_id == company_id,
                Projection.entity_type == "item",
            )
        )
    ).scalars().all()
    result: dict[str, str] = {}
    for row in rows:
        sku = row.state.get("sku")
        sell_by = row.state.get("sell_by")
        if sku and sell_by:
            result[sku] = sell_by
    return result


async def _assert_ref_id_unique(
    session: AsyncSession,
    company_id: str,
    ref_id: str,
    *,
    exclude_entity_id: str | None = None,
) -> None:
    """Raise HTTP 409 if any doc in the company already has the given ref_id in its state.

    Uses a state JSON index scan (same pattern as SKU uniqueness in inventory).
    exclude_entity_id: the doc being renamed - excluded so renaming to own current
    number is treated as a no-op and does not raise.
    """
    existing = (
        await session.execute(
            select(Projection).where(
                Projection.company_id == company_id,
                Projection.entity_type == "doc",
                Projection.state["ref_id"].as_string() == ref_id,
            )
        )
    ).scalars().first()
    if existing and existing.entity_id != exclude_entity_id:
        raise HTTPException(status_code=409, detail=f"Document number '{ref_id}' already exists")


@router.get("")
async def list_docs(
    doc_type: str | None = None,
    status: str | None = None,
    status_in: str | None = None,
    exclude_status: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    due_from: str | None = None,
    due_to: str | None = None,
    q: str | None = None,
    contact_id: str | None = None,
    limit: int | None = None,
    offset: int = 0,
    overdue_only: bool = False,
    all_issued: bool = False,
    unfulfilled_only: bool = False,
    not_restocked: bool = False,
    not_stocked: bool = False,
    converted_to_type: str | None = None,
    company_id: str = Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    from datetime import date as _date_cls
    today = _date_cls.today().isoformat()

    # Build SQL WHERE conditions - push all indexable filters into the DB.
    # Complex post-filters (overdue_only, unfulfilled_only, etc.) still run in
    # Python because they reference nested JSON fields or multi-column logic.
    base_where = [
        Projection.company_id == company_id,
        Projection.entity_type == "doc",
    ]
    if doc_type:
        base_where.append(Projection.state["doc_type"].as_string() == doc_type)
    if status:
        base_where.append(Projection.state["status"].as_string() == status)
    if status_in:
        _allowed = set(status_in.split(","))
        base_where.append(Projection.state["status"].as_string().in_(_allowed))
    if exclude_status:
        base_where.append(Projection.state["status"].as_string() != exclude_status)
    if contact_id:
        base_where.append(Projection.state["contact_id"].as_string() == contact_id)
    if date_from:
        base_where.append(Projection.state["issue_date"].as_string() >= date_from)
    if date_to:
        base_where.append(Projection.state["issue_date"].as_string() <= date_to)
    if due_from:
        base_where.append(Projection.state["due_date"].as_string() >= due_from)
    if due_to:
        base_where.append(Projection.state["due_date"].as_string() <= due_to)
    if q:
        ql = f"%{q.lower()}%"
        base_where.append(
            _sa.or_(
                _sa.func.lower(Projection.state["doc_number"].as_string()).like(ql),
                _sa.func.lower(Projection.state["contact_name"].as_string()).like(ql),
                _sa.func.lower(Projection.state["contact_id"].as_string()).like(ql),
                _sa.func.lower(Projection.state["ref"].as_string()).like(ql),
            )
        )

    # Remaining filters still need Python evaluation (multi-field logic).
    needs_python_filter = any([all_issued, overdue_only, unfulfilled_only, not_restocked, not_stocked, converted_to_type])

    if needs_python_filter:
        # Fetch only needed columns to reduce deserialization cost.
        rows = (await session.execute(select(Projection).where(*base_where))).scalars().all()
        out = [r.state | {"id": r.entity_id} for r in rows]
        if all_issued:
            out = [x for x in out if x.get("status") not in ("draft", "void")]
        if overdue_only:
            out = [x for x in out if x.get("due_date") and x["due_date"] < today and x.get("status") not in ("draft", "void")]
        if unfulfilled_only:
            out = [x for x in out if x.get("status") not in ("draft", "void") and x.get("fulfillment_status") != "fulfilled"]
        if not_restocked:
            out = [x for x in out if x.get("status") not in ("draft", "void") and not (x.get("return_received_items") or [])]
        if not_stocked:
            out = [x for x in out if x.get("status") not in ("draft", "void") and not (x.get("received_items") or [])]
        if converted_to_type:
            out = [x for x in out if x.get("converted_to_type") == converted_to_type]
        out.sort(key=lambda x: x.get("issue_date") or x.get("created_at") or x.get("date") or "", reverse=True)
        total = len(out)
        if offset:
            out = out[offset:]
        if limit is not None:
            out = out[:limit]
        return {"items": out, "total": total}

    # Fast path: all filters in SQL - use COUNT + paginated SELECT.
    count_q = select(_func.count()).select_from(Projection).where(*base_where)
    total = (await session.execute(count_q)).scalar_one()

    list_q = (
        select(Projection)
        .where(*base_where)
        .order_by(
            Projection.state["issue_date"].as_string().desc(),
        )
        .offset(offset)
    )
    if limit is not None:
        list_q = list_q.limit(limit)

    rows = (await session.execute(list_q)).scalars().all()
    out = [r.state | {"id": r.entity_id, "_updated_at": r.updated_at.isoformat() if r.updated_at else None} for r in rows]
    return {"items": out, "total": total}


@router.get("/summary")
async def get_doc_summary(
    doc_type: str | None = None,
    company_id: str = Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    from datetime import date as _date_cls
    today = _date_cls.today().isoformat()
    summary_where = [
        Projection.company_id == company_id,
        Projection.entity_type == "doc",
    ]
    if doc_type:
        summary_where.append(Projection.state["doc_type"].as_string() == doc_type)
    rows = (await session.execute(select(Projection).where(*summary_where))).scalars().all()
    ar_gross = ar_paid = ar_outstanding = 0.0
    count_by_status: dict[str, int] = {}
    invoice_count = 0
    _AWAITING_STATUSES = {"final", "sent", "awaiting_payment", "partial"}
    awaiting_payment_count = 0
    awaiting_payment_total = 0.0
    overdue_count = 0
    overdue_total = 0.0
    paid_count = 0
    paid_total = 0.0
    sent_total = 0.0
    draft_total = 0.0
    void_total = 0.0
    memo_total = 0.0
    unfulfilled_count = 0
    unfulfilled_total = 0.0
    not_restocked_count = 0
    not_stocked_count = 0
    converted_to_memo_count = 0
    converted_to_invoice_count = 0
    for row in rows:
        state = row.state
        st = state.get("status", "")
        count_by_status[st] = count_by_status.get(st, 0) + 1
        dt = state.get("doc_type")
        if dt == "invoice":
            total_ = float(state.get("total", 0) or 0)
            outstanding_ = float(state.get("amount_outstanding", 0) or 0)
            paid_ = float(state.get("amount_paid", 0) or 0)
            if st == "draft":
                draft_total += total_
                continue
            if st == "void":
                void_total += total_
                continue
            invoice_count += 1
            ar_gross += total_
            ar_paid += paid_
            ar_outstanding += outstanding_
            if state.get("fulfillment_status") != "fulfilled":
                unfulfilled_count += 1
                unfulfilled_total += total_
            if st in _AWAITING_STATUSES:
                awaiting_payment_count += 1
                awaiting_payment_total += outstanding_
                due = state.get("due_date") or ""
                if due and due < today:
                    overdue_count += 1
                    overdue_total += outstanding_
                if st == "sent":
                    sent_total += outstanding_
            elif st == "paid":
                paid_count += 1
                paid_total += total_
        else:
            if st in ("void", "draft"):
                continue
            if dt == "memo":
                memo_total += float(state.get("total", 0) or 0)
            if dt in ("memo", "consignment_in"):
                due = state.get("due_date") or ""
                if due and due < today:
                    overdue_count += 1
            if dt == "credit_note":
                if not (state.get("return_received_items") or []):
                    not_restocked_count += 1
            if dt == "bill":
                if not (state.get("received_items") or []):
                    not_stocked_count += 1
            if dt == "list" and st == "converted":
                ctt = state.get("converted_to_type") or ""
                if ctt == "memo":
                    converted_to_memo_count += 1
                elif ctt == "invoice":
                    converted_to_invoice_count += 1
    draft_count = count_by_status.get("draft", 0)
    total_rows = sum(count_by_status.values())
    live_count = total_rows - draft_count
    all_issued_count = live_count - count_by_status.get("void", 0)
    return {
        "total_count": live_count,
        "draft_count": draft_count,
        "non_void_count": sum(v for k, v in count_by_status.items() if k not in ("void", "draft")),
        "all_issued_count": all_issued_count,
        "all_issued_total": ar_gross,
        "awaiting_payment_count": awaiting_payment_count,
        "awaiting_payment_total": awaiting_payment_total,
        "overdue_count": overdue_count,
        "overdue_total": overdue_total,
        "paid_count": paid_count,
        "paid_total": paid_total,
        "sent_total": sent_total,
        "draft_total": draft_total,
        "void_total": void_total,
        "ar_total": ar_gross,
        "ar_paid": ar_paid,
        "ar_outstanding": ar_outstanding,
        "invoice_count": invoice_count,
        "unfulfilled_count": unfulfilled_count,
        "unfulfilled_total": unfulfilled_total,
        "not_restocked_count": not_restocked_count,
        "not_stocked_count": not_stocked_count,
        "converted_to_memo_count": converted_to_memo_count,
        "converted_to_invoice_count": converted_to_invoice_count,
        "memo_all_total": memo_total,
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
    if payload.doc_type == "credit_note" and payload.original_doc_id:
        inv = await _get_doc(session, company_id, payload.original_doc_id)
        original_total = float(inv.state.get("total", 0) or 0)
        if payload.total > original_total + 1e-9:
            raise HTTPException(status_code=409, detail="Credit note total cannot exceed original invoice total")

    # Reject if contact is deleted
    if payload.contact_id:
        contact_row = await session.get(Projection, {"company_id": company_id, "entity_id": payload.contact_id})
        if contact_row is not None and contact_row.state.get("deleted"):
            raise HTTPException(status_code=422, detail="This contact has been deleted and cannot be used on new documents.")

    if payload.currency and payload.currency not in CURRENCY_CODES:
        raise HTTPException(status_code=422, detail=f"Invalid currency code: {payload.currency}")

    _assert_date_order(payload.model_dump(exclude_none=True))

    # Validate line item quantities against sell_by unit precision
    if payload.line_items:
        unit_map = await _get_unit_map(session, company_id)
        sell_by_map = await _get_item_sell_by_map(session, company_id)
        for li in payload.line_items:
            resolved_sell_by = li.sell_by or (sell_by_map.get(li.sku) if li.sku else None)
            validate_line_quantity(li.quantity, resolved_sell_by, unit_map, label=li.name or li.sku or "Line item")

    # Lock the company row (SELECT ... FOR UPDATE) for the rest of the
    # transaction so concurrent doc creation can't read the same numbering
    # counter and mint duplicate refs (e.g. two CN-2606-0002).
    company = (
        await session.execute(
            select(Company).where(Company.id == company_id).with_for_update()
        )
    ).scalar_one_or_none()
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
    # Default issue_date to today so date filters and sorting work correctly on new docs
    data.setdefault("issue_date", _date.today().isoformat())

    # Auto-compute total from line items if not explicitly provided (or zero)
    if not payload.total and payload.line_items:
        currency = data.get("currency", "USD")
        # If any line provides line_total, it is pre-computed (discount already applied).
        # Header discount only applies when computing from quantity * unit_price.
        has_explicit_line_totals = any(li.line_total is not None for li in payload.line_items)

        # Round each line_total to currency precision at source
        rounded_line_totals = [
            round_money(
                to_decimal(li.line_total) if li.line_total is not None
                else to_decimal(li.quantity) * to_decimal(li.unit_price),
                currency,
            )
            for li in payload.line_items
        ]

        subtotal_d = sum(rounded_line_totals, to_decimal(0))
        if not has_explicit_line_totals:
            subtotal_d = subtotal_d - round_money(payload.discount, currency)

        # Compute per-line tax amounts (compound-aware), update data with rounded values
        from decimal import Decimal as _Dec
        line_tax_total_d = _Dec(0)
        if data.get("line_items"):
            resolved_line_items = []
            for li_data, li_model, lt_d in zip(data["line_items"], payload.line_items, rounded_line_totals):
                li_data = {**li_data, "line_total": to_stored_float(lt_d)}
                if li_model.taxes:
                    resolved = compute_tax_amounts(li_model.taxes, to_stored_float(lt_d), currency)
                    li_data["taxes"] = [item.model_dump() for item in resolved]
                    line_tax_total_d += sum(to_decimal(item.amount) for item in resolved)
                resolved_line_items.append(li_data)
            data["line_items"] = resolved_line_items

        # doc_taxes: compute compound-aware amounts against subtotal, take precedence over legacy tax
        if payload.doc_taxes:
            resolved_doc_taxes = compute_tax_amounts(payload.doc_taxes, to_stored_float(subtotal_d), currency)
            data["doc_taxes"] = [item.model_dump() for item in resolved_doc_taxes]
            effective_tax_d = sum(to_decimal(item.amount) for item in resolved_doc_taxes) + line_tax_total_d
        else:
            effective_tax_d = round_money(payload.tax, currency) + line_tax_total_d

        shipping_d = round_money(payload.shipping, currency)
        total_d = round_money(subtotal_d + effective_tax_d + shipping_d, currency)
        data["total"] = to_stored_float(total_d)
        data["subtotal"] = to_stored_float(round_money(subtotal_d, currency))

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
    # Fields editable on finalized docs (cosmetic/corrective, no financial impact on totals or inventory)
    _FINALIZED_EDITABLE_FIELDS = {
        "description", "customer_note", "internal_note",
        "shipping_attn", "contact_shipping_address", "ref_id",
        "line_items",  # partial: only description/account_code per line
        # Contact snapshot fields - stored on doc, no JE impact
        "contact_id", "contact_name", "contact_company_name", "contact_billing_address",
        "contact_phone", "contact_email", "contact_tax_id", "payment_terms",
    }
    _LI_FINALIZED_EDITABLE = {"description", "account_code"}
    _PROTECTED_FIELDS = {"status", "entity_type", "company_id"}
    protected_attempted = _PROTECTED_FIELDS & set(payload.fields_changed)
    if protected_attempted:
        raise HTTPException(
            status_code=422,
            detail=f"Fields {sorted(protected_attempted)} cannot be changed via patch. Use the appropriate lifecycle endpoints.",
        )
    row = await _get_doc(session, company_id, entity_id)
    is_draft = row.state.get("status") == "draft"
    if not is_draft:
        locked_fields = set(payload.fields_changed) - _FINALIZED_EDITABLE_FIELDS
        if locked_fields:
            status_label = (row.state.get("status") or "finalized").replace("_", " ").title()
            raise HTTPException(
                status_code=409,
                detail=f"This document is in {status_label} status and cannot be edited. To make changes, revert it to Draft first.",
            )
        # Guard: line_items patch on finalized doc may only touch _LI_FINALIZED_EDITABLE fields
        if "line_items" in payload.fields_changed:
            incoming_lis = (payload.fields_changed["line_items"].get("new") or [])
            existing_lis = row.state.get("line_items") or []
            existing_by_idx = {i: li for i, li in enumerate(existing_lis)}
            for i, incoming in enumerate(incoming_lis):
                original = existing_by_idx.get(i, {})
                for k, v in incoming.items():
                    if k in _LI_FINALIZED_EDITABLE:
                        continue
                    orig_v = original.get(k)
                    if v != orig_v:
                        raise HTTPException(
                            status_code=409,
                            detail=f"Field '{k}' in line item {i} cannot be changed on a finalized document.",
                        )
    # Uniqueness check when ref_id is being changed
    new_ref = (payload.fields_changed.get("ref_id") or {}).get("new")
    if new_ref:
        await _assert_ref_id_unique(session, company_id, new_ref, exclude_entity_id=entity_id)
    # Validate issue/due date ordering against the merged resulting state
    patch_flat = {k: v.get("new") for k, v in payload.fields_changed.items() if v.get("new") is not None}
    _assert_date_order(patch_flat, row.state)

    # Validate patched line items when present
    new_line_items = (payload.fields_changed.get("line_items") or {}).get("new")
    if new_line_items is not None and isinstance(new_line_items, list):
        unit_map = await _get_unit_map(session, company_id)
        sell_by_map = await _get_item_sell_by_map(session, company_id)
        for li in new_line_items:
            sku = li.get("sku")
            resolved_sell_by = li.get("sell_by") or (sell_by_map.get(sku) if sku else None)
            validate_line_quantity(
                float(li.get("quantity", 0) or 0),
                resolved_sell_by,
                unit_map,
                label=li.get("name") or sku or "Line item",
            )

        # Fix 3: guard against deleting fulfilled line items via the patch endpoint.
        # Compare the current doc's entity_ids against the incoming list; any entity_id
        # that disappears must not be in a fulfilled state.
        existing_eids = {
            li.get("entity_id") or li.get("item_id") or ""
            for li in (row.state.get("line_items") or [])
        } - {""}
        incoming_eids = {
            li.get("entity_id") or li.get("item_id") or ""
            for li in new_line_items
        } - {""}
        removed_eids = existing_eids - incoming_eids
        for eid in removed_eids:
            item_proj = await session.get(Projection, {"company_id": company_id, "entity_id": eid})
            if item_proj and item_proj.state.get("status") in FULFILLED_ITEM_STATUSES:
                raise HTTPException(
                    status_code=409,
                    detail=f"Cannot delete fulfilled line item {eid!r}. Revert fulfillment first.",
                )

    entry = await emit_event(
        session, company_id=company_id, entity_id=entity_id, entity_type="doc", event_type="doc.updated",
        data={"fields_changed": {
            k: {"old": (change.get("old") if change.get("old") is not None else row.state.get(k)), "new": change.get("new")}
            for k, change in payload.fields_changed.items()
        }, "idempotency_key": payload.idempotency_key},
        actor_id=user.id, location_id=None, source="api",
        idempotency_key=payload.idempotency_key or str(uuid.uuid4()), metadata_={},
    )
    await session.commit()
    return {"event_id": entry.id}


@router.post("/{entity_id}/send")
async def send_doc(entity_id: str, payload: DocSendBody, company_id: str = Depends(get_current_company_id), user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    row = await _get_doc(session, company_id, entity_id)
    if row.state.get("status") == "void":
        raise HTTPException(status_code=409, detail="Cannot send void document")
    from celerp_docs.doc_constants import NO_SEND_DOC_TYPES
    doc_type = row.state.get("doc_type", "")
    if doc_type in NO_SEND_DOC_TYPES:
        raise HTTPException(status_code=409, detail=f"Document type '{doc_type}' cannot be sent")
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
        asyncio.create_task(send_email(
            sent_to, subject, body_html, body_text=body_text,
            cc=payload.cc or "", bcc=payload.bcc or "",
        ))

    return {"event_id": entry.id}


@router.post("/{entity_id}/finalize")
async def finalize_doc(entity_id: str, company_id: str = Depends(get_current_company_id), _: None = Depends(require_operator), user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    row = await _get_doc(session, company_id, entity_id)
    if row.state.get("status") == "void":
        raise HTTPException(status_code=409, detail="Cannot finalize void document")

    # Snapshot scalar values early — avoids ORM lazy-load issues after multiple flush() calls.
    _initial_doc_state = dict(row.state)
    _user_id = user.id  # Capture before flushes may expire session objects
    doc_type = _initial_doc_state.get("doc_type", "")
    finalize_data: dict = {}
    event_type = "doc.finalized"

    # Load company once for base currency (used for validation and JE conversion).
    _company = await session.get(Company, company_id)
    _base_currency = (_company.settings.get("currency", "USD") if _company else "USD")

    # Validate: foreign-currency docs require a conversion rate before finalization.
    _doc_currency = _initial_doc_state.get("currency", _base_currency)
    if _doc_currency != _base_currency:
        _rate = _initial_doc_state.get("conversion_rate")
        if not _rate or float(_rate) <= 0:
            raise HTTPException(
                status_code=422,
                detail="A conversion rate is required for foreign-currency documents. Set the exchange rate before finalizing.",
            )

    # Invoices: assign real INV number on finalize, preserving PF ref.
    # On re-finalize (after revert-to-draft) the doc already holds the INV ref
    # and the original source_proforma_ref — reuse both so no counter slot is wasted
    # and the proforma link stays intact.
    if doc_type == "invoice":
        existing_inv_ref = _initial_doc_state.get("ref_id", "")
        is_re_finalize = bool(_initial_doc_state.get("revert_count", 0))
        if is_re_finalize and existing_inv_ref and not existing_inv_ref.startswith("PF-"):
            # Reuse existing INV ref; keep existing source_proforma_ref untouched.
            finalize_data["ref_id"] = existing_inv_ref
        else:
            inv_ref = next_doc_ref(_company, "invoice")
            finalize_data["ref_id"] = inv_ref
            finalize_data["source_proforma_ref"] = existing_inv_ref
            await session.flush()

    # Purchase Orders: "Convert to Bill" - assign BILL number, change doc_type
    elif doc_type == "purchase_order":
        bill_ref = next_doc_ref(_company, "bill")
        finalize_data["ref_id"] = bill_ref
        finalize_data["source_po_ref"] = _initial_doc_state.get("ref_id", "")
        finalize_data["doc_type"] = "bill"
        event_type = "doc.converted_to_bill"
        await session.flush()

    # Bills with only non-stock line items skip receiving and go straight to awaiting_payment.
    if doc_type == "bill":
        _line_items = _initial_doc_state.get("line_items") or []
        _has_stock = any((li.get("receive_as") or "stock") == "stock" for li in _line_items)
        if not _has_stock:
            finalize_data["skip_receiving"] = True

    entry = await emit_event(
        session, company_id=company_id, entity_id=entity_id, entity_type="doc", event_type=event_type,
        data=finalize_data,
        actor_id=_user_id, location_id=None, source="api", idempotency_key=str(uuid.uuid4()), metadata_={},
    )
    # Auto-JE on finalize (invoices, direct bills, or convert to bill (POs))
    if doc_type == "invoice":
        await auto_je.create_for_doc_finalized(session, company_id=company_id, user_id=_user_id, doc_id=entity_id, doc=_initial_doc_state, base_currency=_base_currency)
        # Promote memo_out items to sold: memo→invoice conversion leaves items in memo_out.
        # Finalizing the invoice is the point at which the sale is confirmed.
        _cid = uuid.UUID(str(company_id))
        for _li in _initial_doc_state.get("line_items", []):
            _eid = _li.get("entity_id") or _li.get("item_id") or ""
            if not _eid:
                continue
            _iproj = await session.get(Projection, {"company_id": company_id, "entity_id": _eid})
            if _iproj and _iproj.state.get("status") == "memo_out":
                await emit_event(
                    session, company_id=_cid, entity_id=_eid, entity_type="item",
                    event_type="item.status.set",
                    data={"new_status": "sold", "source_doc_id": entity_id},
                    actor_id=_user_id, location_id=None, source="invoice_finalize",
                    idempotency_key=str(uuid.uuid4()), metadata_={"doc_id": entity_id},
                )
    elif doc_type in ("purchase_order", "bill"):
        # Bill conversion JE: debit expense/inventory accounts, credit AP (2110)
        # Covers both PO->bill conversion and directly-created bills finalized directly.
        # Pass revert_count so cycle-aware idempotency keys are used on re-finalize.
        _revert_count = int(_initial_doc_state.get("revert_count", 0))
        await auto_je.create_for_bill_conversion(session, company_id=company_id, user_id=_user_id, doc_id=entity_id, doc=_initial_doc_state, base_currency=_base_currency, revert_count=_revert_count)
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
async def void_doc(entity_id: str, payload: DocVoidBody, company_id: str = Depends(get_current_company_id), _: None = Depends(require_operator), user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    row = await _get_doc(session, company_id, entity_id)
    current_status = row.state.get("status")
    if current_status in ("paid", "partial"):
        raise HTTPException(status_code=409, detail="Cannot void a document with payments; void the payments first")

    event_data = payload.model_dump(exclude_none=True)
    event_data["pre_void_status"] = current_status
    entry = await emit_event(
        session, company_id=company_id, entity_id=entity_id, entity_type="doc", event_type="doc.voided",
        data=event_data, actor_id=user.id, location_id=None, source="api",
        idempotency_key=payload.idempotency_key or str(uuid.uuid4()), metadata_={},
    )
    await session.commit()
    return {"event_id": entry.id}


@router.post("/{entity_id}/revert-to-draft")
async def revert_doc_to_draft(entity_id: str, payload: DocRevertBody, company_id: str = Depends(get_current_company_id), _: None = Depends(require_operator), user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    row = await _get_doc(session, company_id, entity_id)
    state = row.state
    previous_status = state.get("status")
    # Inbound docs (bill, consignment_in) can also revert from received/fulfilled statuses.
    _doc_type = state.get("doc_type", "")
    _is_inbound = _doc_type in INBOUND_DOC_TYPES
    _REVERTABLE = (
        {"final", "sent", "awaiting_payment", "received", "partially_received", "fulfilled"}
        if _is_inbound
        else {"final", "sent", "awaiting_payment"}
    )
    if previous_status not in _REVERTABLE:
        raise HTTPException(status_code=409, detail="Can only revert documents in 'final', 'sent', or 'awaiting_payment' status")
    if float(state.get("amount_paid", 0) or 0) != 0:
        raise HTTPException(status_code=409, detail="Cannot revert document with existing payments")
    if state.get("received_items"):
        raise HTTPException(status_code=409, detail="Cannot revert document with received items - return goods first")

    # Fix 1: block revert when any line item has been fulfilled.
    # Fulfilled items are tracked in state["fulfilled_items"]; each entry with a non-null item_id
    # corresponds to an inventory item that is now in a terminal fulfilled state.
    # The user must revert fulfillment line-by-line first, then revert the document.
    fulfilled_items = [
        fi for fi in (state.get("fulfilled_items") or [])
        if fi.get("item_id") is not None
    ]
    if fulfilled_items:
        raise HTTPException(
            status_code=409,
            detail="Cannot revert to draft: line items have been fulfilled. "
                   "Revert fulfillment on each fulfilled line first, then revert the document.",
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
    # Void the finalize JE - pass current revert_count (before this revert increments it)
    current_revert_count = int(state.get("revert_count", 0))
    await auto_je.void_for_doc_finalized(session, company_id=company_id, user_id=user.id, doc_id=entity_id, revert_count=current_revert_count)
    await session.commit()
    return {"event_id": entry.id}


class DocRenumberBody(BaseModel):
    ref_id: str


@router.post("/{entity_id}/renumber")
async def renumber_doc(
    entity_id: str,
    payload: DocRenumberBody,
    company_id: str = Depends(get_current_company_id),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Change the display number (ref_id / doc_number) of any non-void document.

    The internal entity_id (storage key) is never changed. Only the ref_id field
    in the document state is updated. Uniqueness is enforced across all doc
    ref_ids in the company (state scan, not entity_id lookup).

    Voided documents are immutable records and cannot be renumbered.
    """
    row = await _get_doc(session, company_id, entity_id)
    state = row.state
    if state.get("status") == "void":
        raise HTTPException(status_code=409, detail="Voided documents cannot be renumbered")

    new_ref = payload.ref_id.strip()
    if not new_ref:
        raise HTTPException(status_code=422, detail="ref_id must not be empty")

    old_ref = state.get("ref_id") or ""
    if new_ref == old_ref:
        # No-op: return current state without emitting an event
        return row.state | {"id": entity_id}

    await _assert_ref_id_unique(session, company_id, new_ref, exclude_entity_id=entity_id)

    entry = await emit_event(
        session,
        company_id=company_id,
        entity_id=entity_id,
        entity_type="doc",
        event_type="doc.renumbered",
        data={"fields_changed": {
            "ref_id": {"old": old_ref, "new": new_ref},
            "doc_number": {"old": old_ref, "new": new_ref},
        }},
        actor_id=user.id,
        location_id=None,
        source="api",
        idempotency_key=f"renumber:{entity_id}:{new_ref}",
        metadata_={},
    )
    await session.commit()

    updated = await _get_doc(session, company_id, entity_id)
    return updated.state | {"id": entity_id}


@router.post("/{entity_id}/unvoid")
async def unvoid_doc(entity_id: str, payload: DocUnvoidBody, company_id: str = Depends(get_current_company_id), _: None = Depends(require_operator), user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
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
    _unvoid_company = await session.get(Company, company_id)
    _unvoid_base_currency = (_unvoid_company.settings.get("currency", "USD") if _unvoid_company else "USD")
    await auto_je.create_for_doc_unvoided(session, company_id=company_id, user_id=user.id, doc_id=entity_id, doc=state, base_currency=_unvoid_base_currency)

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


@router.delete("/bulk-draft")
async def bulk_delete_drafts(
    doc_ids: str,
    company_id: str = Depends(get_current_company_id),
    _: None = Depends(require_manager),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Delete multiple draft documents in one request. Non-draft docs are skipped (not an error)."""
    ids = [x.strip() for x in doc_ids.split(",") if x.strip()]
    if not ids:
        raise HTTPException(status_code=422, detail="No document IDs specified")
    from celerp.models.ledger import LedgerEntry
    import sqlalchemy as _sa
    deleted = []
    for eid in ids:
        row = await session.get(Projection, {"company_id": company_id, "entity_id": eid})
        if row is None or row.state.get("status") != "draft":
            continue
        await session.execute(_sa.delete(Projection).where(Projection.company_id == company_id, Projection.entity_id == eid))
        await session.execute(_sa.delete(LedgerEntry).where(LedgerEntry.company_id == company_id, LedgerEntry.entity_id == eid))
        deleted.append(eid)
    await session.commit()
    return {"deleted": deleted, "count": len(deleted)}


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
async def record_payment(entity_id: str, payload: DocPaymentBody, company_id: str = Depends(get_current_company_id), _: None = Depends(require_operator), user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    row = await _get_doc(session, company_id, entity_id)
    # Snapshot scalar values before any flush() to avoid SQLAlchemy lazy-load expiry
    # (emit_event -> flush() -> ORM expires row -> MissingGreenlet on subsequent row.state access).
    _doc_state = dict(row.state)
    _user_id = user.id
    if _doc_state.get("doc_type") in NON_FINANCIAL_DOC_TYPES:
        raise HTTPException(status_code=409, detail="This document type carries no money and cannot take a payment")
    if _doc_state.get("status") not in {"sent", "final", "partial", "paid", "received", "partially_received", "awaiting_payment"}:
        raise HTTPException(status_code=409, detail="Cannot record payment in current status")
    outstanding = float(_doc_state.get("amount_outstanding", _doc_state.get("total", 0)) or 0)
    if outstanding <= 0:
        raise HTTPException(status_code=409, detail="Invoice already fully paid")
    # Use Decimal comparison: 1-cent tolerance covers display rounding on the final payment.
    # New docs will have exact rounded totals so this tolerance only applies to legacy data.
    from celerp.services.money import to_decimal as _to_d
    if _to_d(payload.amount) - _to_d(outstanding) > _to_d("0.01"):
        raise HTTPException(status_code=409, detail=f"Payment {payload.amount} exceeds amount outstanding {outstanding}")

    body = payload.model_dump(exclude_none=True)
    body.setdefault("currency", _doc_state.get("currency", "USD"))
    body["remaining_balance"] = max(0.0, outstanding - payload.amount)
    if not payload.bank_account:
        raise HTTPException(status_code=422, detail="bank_account is required")
    bank_code = payload.bank_account
    # Compute payment_index BEFORE appending (len of current active+voided list = next index)
    payment_index = len(_doc_state.get("payments", []))
    entry = await emit_event(
        session, company_id=company_id, entity_id=entity_id, entity_type="doc", event_type="doc.payment.received",
        data=body, actor_id=_user_id, location_id=None, source="api",
        idempotency_key=payload.idempotency_key or str(uuid.uuid4()), metadata_={},
    )
    doc_type = _doc_state.get("doc_type", "invoice")
    _pay_company = await session.get(Company, company_id)
    _pay_base_currency = (_pay_company.settings.get("currency", "USD") if _pay_company else "USD")
    await auto_je.create_for_doc_payment(
        session, company_id=company_id, user_id=_user_id, doc_id=entity_id,
        amount=payload.amount, payment_index=payment_index,
        bank_account_code=bank_code, doc_type=doc_type,
        payment_date=payload.payment_date,
        base_currency=_pay_base_currency,
        conversion_rate=float(_doc_state.get("conversion_rate") or 1),
    )
    # Lifecycle hook for modules (e.g. multicurrency FX gain/loss)
    from celerp.modules.slots import fire_lifecycle
    await fire_lifecycle(
        "on_doc_payment",
        session=session,
        company_id=company_id,
        user_id=_user_id,
        doc_id=entity_id,
        doc=_doc_state,
        amount=payload.amount,
        bank_account_code=bank_code,
    )
    await session.commit()
    return {"event_id": entry.id}


@router.post("/{entity_id}/refund")
async def refund_payment(entity_id: str, payload: DocPaymentBody, company_id: str = Depends(get_current_company_id), _: None = Depends(require_operator), user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
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
    refund_date: str | None = None  # ISO date for the reversal JE (defaults to today)
    idempotency_key: str | None = None


@router.post("/{entity_id}/void-payment")
async def void_payment(entity_id: str, payload: VoidPaymentBody, company_id: str = Depends(get_current_company_id), _: None = Depends(require_operator), user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
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
        data={"payment_index": payload.payment_index, "void_reason": payload.void_reason, "refund_date": payload.refund_date},
        actor_id=user.id, location_id=None, source="api",
        idempotency_key=payload.idempotency_key or str(uuid.uuid4()), metadata_={},
    )
    # Reverse the payment JE - use stored bank_account; fall back to "1111" (default account that always exists)
    # for historical payments recorded before bank_account was required.
    doc_type = row.state.get("doc_type", "invoice")
    bank_code = payment.get("bank_account") or "1111"
    _void_company = await session.get(Company, company_id)
    _void_base_currency = (_void_company.settings.get("currency", "USD") if _void_company else "USD")
    await auto_je.void_for_doc_payment(
        session, company_id=company_id, user_id=user.id, doc_id=entity_id,
        payment_index=payload.payment_index, amount=payment["amount"],
        bank_account_code=bank_code, doc_type=doc_type,
        refund_date=payload.refund_date,
        base_currency=_void_base_currency,
        conversion_rate=float(row.state.get("conversion_rate") or 1),
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
# Delete individual payment (data-entry error correction)
# ---------------------------------------------------------------------------

class DeletePaymentBody(BaseModel):
    delete_reason: str | None = None
    idempotency_key: str | None = None


@router.delete("/{entity_id}/payments/{payment_index}")
async def delete_payment(
    entity_id: str,
    payment_index: int,
    payload: DeletePaymentBody = DeletePaymentBody(),
    company_id: str = Depends(get_current_company_id),
    _: None = Depends(require_operator),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Delete a payment entirely (data-entry error correction).

    Unlike void-payment (which creates a reversal JE visible in the bank ledger as a
    refund), delete removes the payment from the doc projection and voids the original
    JE so it disappears from all reports. Use only for payments that were never real.

    Blocked if the payment JE has been reconciled in a closed reconciliation session.
    If reconciled in an open session, the JE is automatically un-matched first.
    """
    from celerp_accounting.models import ReconciliationSession, BankStatementLine  # noqa: PLC0415

    row = await _get_doc(session, company_id, entity_id)
    payments = row.state.get("payments", [])
    if payment_index < 0 or payment_index >= len(payments):
        raise HTTPException(status_code=422, detail="Invalid payment index")
    payment = payments[payment_index]
    if payment.get("status") != "active":
        raise HTTPException(status_code=409, detail="Only active payments can be deleted")

    # Determine the JE id for this payment
    je_id = f"je:auto:{entity_id}:pay:{payment_index}"

    # Check reconciliation status - query all sessions that include this JE
    recon_result = await session.execute(
        _sa.select(ReconciliationSession).where(
            ReconciliationSession.company_id == company_id,
        )
    )
    recon_sessions = recon_result.scalars().all()

    for recon in recon_sessions:
        if je_id in (recon.reconciled_je_ids or []):
            if recon.status == "closed":
                raise HTTPException(
                    status_code=409,
                    detail="Payment has been reconciled in a closed period. Unreconcile to delete.",
                )
            # Open session - auto-unmatch
            updated_ids = [j for j in (recon.reconciled_je_ids or []) if j != je_id]
            recon.reconciled_je_ids = updated_ids
            # Clear matched_je_id on any statement line pointing to this JE
            sl_result = await session.execute(
                _sa.select(BankStatementLine).where(
                    BankStatementLine.reconciliation_session_id == recon.id,
                    BankStatementLine.matched_je_id == je_id,
                )
            )
            for sl in sl_result.scalars().all():
                sl.matched_je_id = None
                sl.status = "unmatched"

    # Emit doc.payment.deleted - projection removes the row and re-indexes
    entry = await emit_event(
        session, company_id=company_id, entity_id=entity_id, entity_type="doc",
        event_type="doc.payment.deleted",
        data={"payment_index": payment_index, "delete_reason": payload.delete_reason},
        actor_id=user.id, location_id=None, source="api",
        idempotency_key=payload.idempotency_key or str(uuid.uuid4()), metadata_={},
    )

    # Void the original payment JE so it disappears from bank ledger + reports
    je_row = await session.get(Projection, {"company_id": company_id, "entity_id": je_id})
    if je_row is not None and je_row.state.get("status") == "posted":
        from celerp.services.je_keys import je_idempotency_key as _je_key  # noqa: PLC0415
        await emit_event(
            session, company_id=company_id, entity_id=je_id, entity_type="journal_entry",
            event_type="acc.journal_entry.voided",
            data={"reason": f"Deleted: payment {payment_index} on {entity_id}. {payload.delete_reason or ''}".strip()},
            actor_id=user.id, location_id=None, source="auto_je",
            idempotency_key=_je_key(entity_id, f"payment.deleted:{payment_index}", "void"),
            metadata_={"trigger": "doc.payment.deleted", "doc_id": entity_id},
        )

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
async def apply_cn_to_invoice(entity_id: str, payload: ApplyToInvoiceBody, company_id: str = Depends(get_current_company_id), _: None = Depends(require_operator), user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
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
    # JE: AR-to-AR transfer. Index by current payment count to get a unique key per application.
    payment_idx = len(cn_row.state.get("payments", []))
    _cn_company = await session.get(Company, company_id)
    _cn_base_currency = (_cn_company.settings.get("currency", "USD") if _cn_company else "USD")
    await auto_je.create_for_cn_application(
        session, company_id=company_id, user_id=user.id,
        doc_id=payload.target_doc_id, cn_id=entity_id, amount=payload.amount,
        payment_index=payment_idx,
        base_currency=_cn_base_currency,
        conversion_rate=float(cn_row.state.get("conversion_rate") or 1),
    )
    await session.commit()
    return {"event_id": entry.id}


# ---------------------------------------------------------------------------
# Credit note: refund to customer
# ---------------------------------------------------------------------------


class CnRefundBody(BaseModel):
    amount: float
    date: str  # ISO date (YYYY-MM-DD), always required
    method: str | None = None
    bank_account: str | None = None
    reference: str | None = None
    idempotency_key: str | None = None


@router.post("/{entity_id}/cn-refund")
async def refund_cn(entity_id: str, payload: CnRefundBody, company_id: str = Depends(get_current_company_id), _: None = Depends(require_operator), user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    row = await _get_doc(session, company_id, entity_id)
    cn = row.state
    if cn.get("doc_type") != "credit_note":
        raise HTTPException(status_code=409, detail="Only credit notes can be refunded")
    if cn.get("status") in ("draft", "void"):
        raise HTTPException(status_code=409, detail="Credit note must be issued before refunding")
    cn_outstanding = float(cn.get("amount_outstanding", cn.get("total", 0)) or 0)
    if payload.amount > cn_outstanding + 1e-9:
        raise HTTPException(status_code=409, detail="Refund amount exceeds credit note balance")

    payment_date = payload.date
    if not payload.bank_account:
        raise HTTPException(status_code=422, detail="bank_account is required")
    bank_code = payload.bank_account

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
    payment_index = len(cn.get("payments", []))
    _refund_company = await session.get(Company, company_id)
    _refund_base_currency = (_refund_company.settings.get("currency", "USD") if _refund_company else "USD")
    await auto_je.create_for_doc_payment(
        session, company_id=company_id, user_id=user.id, doc_id=entity_id,
        amount=payload.amount, payment_index=payment_index,
        bank_account_code=bank_code, doc_type="invoice",
        payment_date=payment_date,
        base_currency=_refund_base_currency,
        conversion_rate=float(cn.get("conversion_rate") or 1),
    )
    await session.commit()
    return {"event_id": entry.id}


# ---------------------------------------------------------------------------
# Bulk payment
# ---------------------------------------------------------------------------


class BulkPaymentBody(BaseModel):
    doc_ids: list[str]
    amount: float
    payment_date: str  # ISO date (YYYY-MM-DD), always required
    method: str | None = None
    bank_account: str | None = None
    reference: str | None = None
    idempotency_key: str | None = None


@router.post("/bulk-payment")
async def bulk_payment(payload: BulkPaymentBody, company_id: str = Depends(get_current_company_id), _: None = Depends(require_operator), user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
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
    payment_date = payload.payment_date
    if not payload.bank_account:
        raise HTTPException(status_code=422, detail="bank_account is required")
    bank_code = payload.bank_account
    _bulk_company = await session.get(Company, company_id)
    _bulk_base_currency = (_bulk_company.settings.get("currency", "USD") if _bulk_company else "USD")

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
        payment_index = len(state.get("payments", []))
        doc_type = state.get("doc_type", "invoice")
        await auto_je.create_for_doc_payment(
            session, company_id=company_id, user_id=user.id, doc_id=doc_id,
            amount=alloc, payment_index=payment_index,
            bank_account_code=bank_code, doc_type=doc_type,
            payment_date=payment_date,
            base_currency=_bulk_base_currency,
            conversion_rate=float(state.get("conversion_rate") or 1),
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
    # Inbound docs always create new parcels - never adjust an existing item's qty.
    # bill and consignment_in are both inbound: goods arrive and become new catalog entries.
    # purchase_order is outbound-style: it adjusts qty on the canonical item record.
    is_inbound = doc_type in ("bill", "consignment_in")
    created_item_ids: list[str] = []

    # Build sell_by lookup: item projections are authoritative; doc line items as fallback
    sell_by_map = await _get_item_sell_by_map(session, company_id)
    doc_line_sell_by: dict[str, str] = {
        li.get("sku", ""): li.get("sell_by") or ""
        for li in (row.state.get("line_items") or [])
        if li.get("sku")
    }
    unit_map = await _get_unit_map(session, company_id)
    # Build purchase_conversion_factor lookups: item_id → factor, sku → factor (default 1)
    all_item_rows = (
        await session.execute(
            select(Projection).where(
                Projection.company_id == company_id,
                Projection.entity_type == "item",
            )
        )
    ).scalars().all()
    item_conversion_map: dict[str, float] = {
        r.entity_id: float(r.state.get("purchase_conversion_factor") or 1)
        for r in all_item_rows
    }
    sku_conversion_map: dict[str, float] = {
        str(r.state.get("sku") or "").strip(): float(r.state.get("purchase_conversion_factor") or 1)
        for r in all_item_rows
        if r.state.get("sku")
    }
    # doc line conversion factors override sku_conversion_map (user may have set them on the PO)
    doc_line_conversion: dict[str, float] = {
        str(li.get("sku") or "").strip(): float(li.get("purchase_conversion_factor") or 1)
        for li in (row.state.get("line_items") or [])
        if li.get("sku")
    }
    for it in payload.received_items:
        sell_by = sell_by_map.get(it.sku or "") or doc_line_sell_by.get(it.sku or "", "") or None
        validate_line_quantity(it.quantity_received, sell_by, unit_map, label=it.name or it.sku or "Received item")

    for it in payload.received_items:
        if it.item_id and not is_inbound:
            # PO (outbound-style): adjust quantity on the canonical catalog item.
            item = await session.get(Projection, {"company_id": company_id, "entity_id": it.item_id})
            if item is None:
                raise HTTPException(status_code=404, detail=f"Item not found: {it.item_id}")
            conversion = item_conversion_map.get(it.item_id, 1)
            stock_qty_received = float(it.quantity_received) * conversion
            new_qty = float(item.state.get("quantity", 0) or 0) + stock_qty_received
            await emit_event(
                session, company_id=company_id, entity_id=it.item_id, entity_type="item", event_type="item.quantity.adjusted",
                data={"new_qty": new_qty},
                actor_id=user.id, location_id=None, source="api",
                idempotency_key=str(uuid.uuid4()), metadata_={"source_doc": entity_id},
            )
        else:
            # Inbound doc (bill, consignment_in): always create a new parcel.
            # If item_id is set it refers to a catalog template - use it for attribute inheritance only.
            if it.receive_as != "stock":
                continue
            if not it.name:
                raise HTTPException(status_code=422, detail="name is required when creating received item")
            # Auto-generate SKU from name if not provided (e.g. custom ad-hoc items)
            _sku = it.sku or it.name.strip().upper().replace(" ", "-")[:40]

            # Template: prefer existing item by item_id, fall back to SKU match.
            template_state: dict = {}
            if it.item_id:
                tmpl_row = await session.get(Projection, {"company_id": company_id, "entity_id": it.item_id})
                if tmpl_row:
                    template_state = tmpl_row.state
            if not template_state:
                template_state = next(
                    (r.state for r in all_item_rows
                     if str(r.state.get("sku") or "").strip() == _sku.strip()),
                    {},
                )
            # sku_ref is an alias kept for clarity below
            sku_ref: dict = template_state

            # Bill line item: explicit user-set fields take highest priority over sku_ref
            doc_line: dict = next(
                (li for li in row.state.get("line_items", [])
                 if str(li.get("sku") or "").strip() == _sku.strip()),
                {},
            )

            # Fields to inherit from existing item; barcode excluded (unique per physical item)
            _INHERIT = (
                "category", "unit", "sell_by", "description",
                "cost_price", "wholesale_price", "retail_price",
                "tax_codes", "hs_code", "weight", "weight_unit",
                "dimensions", "dimensions_unit", "purchase_sku",
                "purchase_name", "purchase_unit", "purchase_conversion_factor",
            )
            item_data: dict = {k: sku_ref[k] for k in _INHERIT if k in sku_ref and sku_ref[k] is not None}
            # Copy dynamic category-specific attributes (measurements, shape/cut, etc.)
            if sku_ref.get("attributes"):
                item_data["attributes"] = dict(sku_ref["attributes"])
            # Bill line item fields override sku_ref (user explicitly set these on the bill)
            for _f in ("category", "attributes"):
                _doc_val = doc_line.get(_f)
                _payload_val = getattr(it, _f, None)
                _v = _doc_val or _payload_val
                if _v:
                    item_data[_f] = _v
            # Resolve conversion factor: doc line overrides sku_ref
            conversion = doc_line_conversion.get(_sku.strip()) or sku_conversion_map.get(_sku.strip()) or 1
            # Payload values always take precedence for the fields below
            item_data.update({
                "sku": _sku,
                "name": it.name,
                "quantity": float(it.quantity_received) * conversion,
                "location_id": payload.location_id,
            })
            if it.cost_price is not None:
                # Emit cost_total when quantity is known (cost_total is the primitive)
                _recv_qty = float(it.quantity_received) * conversion
                item_data["cost_total"] = it.cost_price * _recv_qty if _recv_qty else it.cost_price
            if is_consignment:
                item_data["consignment_flag"] = "in"
            new_eid = f"item:{uuid.uuid4()}"
            created_item_ids.append(new_eid)
            await emit_event(
                session,
                company_id=company_id,
                entity_id=new_eid,
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
            "created_item_ids": created_item_ids,
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
        _rcv_company = await session.get(Company, company_id)
        _rcv_base_currency = (_rcv_company.settings.get("currency", "USD") if _rcv_company else "USD")
        await auto_je.create_for_po_received(
            session,
            company_id=company_id,
            user_id=user.id,
            po_id=entity_id,
            doc=row.state,
            total=po_total,
            unique_suffix=str(uuid.uuid4()),
            base_currency=_rcv_base_currency,
            receive_date=datetime.now(UTC).date().isoformat(),
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

        # Filter line_items: only carry over items with status=memo_out
        original_line_items = state.get("line_items", [])
        qualifying_line_items = []
        for li in original_line_items:
            li_eid = li.get("entity_id") or li.get("item_id") or ""
            if not li_eid:
                continue
            item_proj = await session.get(
                Projection, {"company_id": company_id, "entity_id": li_eid}
            )
            if item_proj and item_proj.state.get("status") == "memo_out":
                qualifying_line_items.append(li)

        if not qualifying_line_items:
            raise HTTPException(
                status_code=422,
                detail="Cannot convert: no line items are currently On Memo (memo_out). Fulfill at least one item before converting.",
            )

        filtered_state = {**state, "line_items": qualifying_line_items}
        # Strip monetary totals: invoice may have fewer items than memo, so memo totals are stale.
        # The invoice will recompute totals from its own line items.
        _MEMO_TOTAL_FIELDS = frozenset({"total", "outstanding", "tax_total", "discount_total", "subtotal", "amount_due"})
        new_data = {k: v for k, v in filtered_state.items() if k not in {"status", "entity_type"} | _MEMO_TOTAL_FIELDS}
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
        _consign_company = await session.get(Company, company_id)
        _consign_base_currency = (_consign_company.settings.get("currency", "USD") if _consign_company else "USD")
        await auto_je.create_for_bill_conversion(
            session, company_id=company_id, user_id=user.id, doc_id=new_doc_id, doc=state, base_currency=_consign_base_currency,
        )
        entry = await emit_event(
            session, company_id=company_id, entity_id=entity_id, entity_type="doc", event_type="doc.converted",
            data={"target_doc_id": new_doc_id, "target_doc_type": "bill"}, actor_id=user.id, location_id=None,
            source="api", idempotency_key=str(uuid.uuid4()), metadata_={},
        )
        await session.commit()
        return {"event_id": entry.id, "target_doc_id": new_doc_id}

    raise HTTPException(status_code=409, detail="Unsupported document conversion")


class NoteCreate(BaseModel):
    note: str
    idempotency_key: str | None = None


class NoteUpdate(BaseModel):
    note: str
    idempotency_key: str | None = None


def _note_list_for(session_exec_result) -> list[dict]:
    """Filter active (non-deleted) note projections from a scalars result."""
    return [
        r.state | {"id": r.entity_id}
        for r in session_exec_result
        if not r.state.get("deleted")
    ]


@router.get("/{entity_id}/notes")
async def list_doc_notes(
    entity_id: str,
    company_id: str = Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    await _get_doc(session, company_id, entity_id)
    rows = (
        await session.execute(
            select(Projection).where(
                Projection.company_id == company_id,
                Projection.entity_type == "doc_note",
            )
        )
    ).scalars().all()
    notes = [r.state | {"id": r.entity_id} for r in rows if r.state.get("doc_id") == entity_id and not r.state.get("deleted")]
    notes.sort(key=lambda n: n.get("created_at") or "", reverse=True)
    return notes


@router.post("/{entity_id}/notes")
async def add_doc_note(
    entity_id: str,
    payload: NoteCreate,
    company_id: str = Depends(get_current_company_id),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    await _get_doc(session, company_id, entity_id)
    if not payload.note.strip():
        raise HTTPException(status_code=422, detail="Note text cannot be empty")
    note_id = f"note:{uuid.uuid4()}"
    entry = await emit_event(
        session, company_id=company_id, entity_id=note_id, entity_type="doc_note",
        event_type="doc.note_added",
        data={
            "doc_id": entity_id,
            "note_id": note_id,
            "note": payload.note.strip(),
            "author_id": str(user.id),
            "author_name": getattr(user, "name", None) or user.email,
            "created_at": datetime.now(UTC).isoformat(),
        },
        actor_id=user.id, location_id=None, source="api",
        idempotency_key=payload.idempotency_key or str(uuid.uuid4()), metadata_={},
    )
    await session.commit()
    return {"event_id": entry.id, "id": note_id}


@router.patch("/{entity_id}/notes/{note_id}")
async def update_doc_note(
    entity_id: str,
    note_id: str,
    payload: NoteUpdate,
    company_id: str = Depends(get_current_company_id),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    await _get_doc(session, company_id, entity_id)
    row = await session.get(Projection, {"company_id": company_id, "entity_id": note_id})
    if row is None or row.entity_type != "doc_note" or row.state.get("doc_id") != entity_id:
        raise HTTPException(status_code=404, detail="Note not found")
    entry = await emit_event(
        session, company_id=company_id, entity_id=note_id, entity_type="doc_note",
        event_type="doc.note_updated",
        data={"doc_id": entity_id, "note_id": note_id, "note": payload.note.strip(), "updated_at": datetime.now(UTC).isoformat()},
        actor_id=user.id, location_id=None, source="api",
        idempotency_key=payload.idempotency_key or str(uuid.uuid4()), metadata_={},
    )
    await session.commit()
    return {"event_id": entry.id}


@router.delete("/{entity_id}/notes/{note_id}")
async def delete_doc_note(
    entity_id: str,
    note_id: str,
    company_id: str = Depends(get_current_company_id),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    await _get_doc(session, company_id, entity_id)
    row = await session.get(Projection, {"company_id": company_id, "entity_id": note_id})
    if row is None or row.entity_type != "doc_note" or row.state.get("doc_id") != entity_id:
        raise HTTPException(status_code=404, detail="Note not found")
    entry = await emit_event(
        session, company_id=company_id, entity_id=note_id, entity_type="doc_note",
        event_type="doc.note_removed",
        data={"doc_id": entity_id, "note_id": note_id},
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
        _imp_company = await session.get(Company, company_id)
        _imp_base_currency = (_imp_company.settings.get("currency", "USD") if _imp_company else "USD")
        await _import_auto_je(session, company_id, user.id, body.entity_id, body.data, base_currency=_imp_base_currency)

    await session.commit()
    return {"event_id": entry.id, "id": body.entity_id, "idempotency_hit": False}


async def _import_auto_je(session: AsyncSession, company_id, user_id, entity_id: str, data: dict, base_currency: str = "USD") -> None:
    """Create finalization JEs for imported docs that arrive in a non-draft state.

    IMPORTANT: We never synthesize payment JEs from snapshot imports.
    - The finalization JE (Dr AR / Cr Revenue) is correct to create from a snapshot:
      it records historical revenue and the accounts receivable balance accurately.
    - A payment JE requires a real payment_date and bank_account. Importers who have
      payment history must emit explicit doc.payment.received events (Option A import).
    - The doc projection reflects amount_paid / amount_outstanding from the snapshot
      payload directly, so the UI shows correct paid/partial/unpaid status without
      requiring a synthetic accounting entry.

    Uses doc-scoped idempotency keys - safe to call multiple times.
    """
    doc_type = data.get("doc_type", "")
    status = data.get("status", "draft")
    total = float(data.get("total", 0) or 0)

    if status in ("void", "draft", "converted", "expired") or total <= 0:
        return

    if doc_type == "invoice" and status in ("sent", "final", "partial", "paid", "awaiting_payment"):
        await auto_je.create_for_doc_finalized(
            session, company_id=company_id, user_id=user_id, doc_id=entity_id, doc=data, base_currency=base_currency,
        )

    elif doc_type == "purchase_order" and status in ("received", "partially_received", "final"):
        await auto_je.create_for_po_received(
            session, company_id=company_id, user_id=user_id, po_id=entity_id, doc=data, total=total, base_currency=base_currency,
            receive_date=data.get("issue_date"),
        )

    elif doc_type == "bill" and status in ("awaiting_payment", "partial", "paid", "final"):
        await auto_je.create_for_bill_conversion(
            session, company_id=company_id, user_id=user_id, doc_id=entity_id, doc=data, base_currency=base_currency,
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
        _select(LedgerEntry.idempotency_key).where(
            LedgerEntry.company_id == company_id,
            LedgerEntry.idempotency_key.in_(keys),
        )
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
    _batch_company = await session.get(Company, company_id)
    _batch_base_currency = (_batch_company.settings.get("currency", "USD") if _batch_company else "USD")
    for rec in body.records:
        if rec.idempotency_key in existing_keys:
            if body.upsert:
                upsert_idem = f"{rec.idempotency_key}:upsert"
                upsert_already = set(
                    (await session.execute(
                        _select(LedgerEntry.idempotency_key).where(
                            LedgerEntry.company_id == company_id,
                            LedgerEntry.idempotency_key == upsert_idem,
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
                await _import_auto_je(session, company_id, user.id, rec.entity_id, rec.data, base_currency=_batch_base_currency)
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
    all_issued: bool = False,
    converted_to_type: str | None = None,
    company_id: str = Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    rows = (await session.execute(
        select(Projection).where(Projection.company_id == company_id, Projection.entity_type == "list")
    )).scalars().all()
    out = [r.state | {"id": r.entity_id} for r in rows]
    if list_type:
        out = [x for x in out if x.get("list_type") == list_type]
    if all_issued:
        out = [x for x in out if x.get("status") not in ("draft", "void")]
    elif status:
        out = [x for x in out if x.get("status") == status]
    if exclude_status:
        out = [x for x in out if x.get("status") != exclude_status]
    if converted_to_type:
        out = [x for x in out if x.get("converted_to_type") == converted_to_type]
    if date_from:
        out = [x for x in out if (x.get("issue_date") or x.get("created_at") or x.get("date") or "")[:10] >= date_from]
    if date_to:
        out = [x for x in out if (x.get("issue_date") or x.get("created_at") or x.get("date") or "")[:10] <= date_to]
    if q:
        ql = q.lower()
        out = [x for x in out if ql in str(x.get("ref_id") or "").lower()
               or ql in str(x.get("customer_name") or x.get("customer_id") or "").lower()]
    out.sort(key=lambda x: x.get("issue_date") or x.get("created_at") or x.get("date") or "", reverse=True)
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
    converted_to_memo_count = 0
    converted_to_invoice_count = 0
    for row in rows:
        st = row.state.get("status", "")
        count_by_status[st] = count_by_status.get(st, 0) + 1
        if st != "void":
            total_value += float(row.state.get("total", 0) or 0)
        if st == "converted":
            ctt = row.state.get("converted_to_type") or ""
            if ctt == "memo":
                converted_to_memo_count += 1
            elif ctt == "invoice":
                converted_to_invoice_count += 1
    draft_count = count_by_status.get("draft", 0)
    void_count = count_by_status.get("void", 0)
    all_issued_count = sum(v for k, v in count_by_status.items() if k not in ("draft", "void"))
    return {
        "total_count": len(rows),
        "draft_count": draft_count,
        "all_issued_count": all_issued_count,
        "total_value": total_value,
        "converted_to_memo_count": converted_to_memo_count,
        "converted_to_invoice_count": converted_to_invoice_count,
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


@lists_router.post("/{entity_id}/revert-to-draft")
async def revert_list_to_draft(
    entity_id: str,
    payload: DocRevertBody,
    company_id: str = Depends(get_current_company_id),
    _: None = Depends(require_operator),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    row = await _get_list(session, company_id, entity_id)
    if row.state.get("status") != "sent":
        raise HTTPException(status_code=409, detail="Only sent lists can be reverted to draft")
    event_data: dict = {"reverted_by": str(user.id), "previous_status": "sent"}
    if payload.reason:
        event_data["reason"] = payload.reason
    entry = await _emit_list(session, company_id, entity_id, "list.patched",
                             {"status": "draft", **event_data}, user)
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
    if state.get("list_type") == "audit":
        raise HTTPException(status_code=409, detail="Audit lists cannot be converted to a sales document")
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


@lists_router.get("/{entity_id}/notes")
async def list_list_notes(
    entity_id: str,
    company_id: str = Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    await _get_list(session, company_id, entity_id)
    rows = (
        await session.execute(
            select(Projection).where(
                Projection.company_id == company_id,
                Projection.entity_type == "list_note",
            )
        )
    ).scalars().all()
    notes = [r.state | {"id": r.entity_id} for r in rows if r.state.get("list_id") == entity_id and not r.state.get("deleted")]
    notes.sort(key=lambda n: n.get("created_at") or "", reverse=True)
    return notes


@lists_router.post("/{entity_id}/notes")
async def add_list_note(
    entity_id: str,
    payload: NoteCreate,
    company_id: str = Depends(get_current_company_id),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    await _get_list(session, company_id, entity_id)
    if not payload.note.strip():
        raise HTTPException(status_code=422, detail="Note text cannot be empty")
    note_id = f"note:{uuid.uuid4()}"
    entry = await emit_event(
        session, company_id=company_id, entity_id=note_id, entity_type="list_note",
        event_type="list.note_added",
        data={
            "list_id": entity_id,
            "note_id": note_id,
            "note": payload.note.strip(),
            "author_id": str(user.id),
            "author_name": getattr(user, "name", None) or user.email,
            "created_at": datetime.now(UTC).isoformat(),
        },
        actor_id=user.id, location_id=None, source="api",
        idempotency_key=payload.idempotency_key or str(uuid.uuid4()), metadata_={},
    )
    await session.commit()
    return {"event_id": entry.id, "id": note_id}


@lists_router.patch("/{entity_id}/notes/{note_id}")
async def update_list_note(
    entity_id: str,
    note_id: str,
    payload: NoteUpdate,
    company_id: str = Depends(get_current_company_id),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    await _get_list(session, company_id, entity_id)
    row = await session.get(Projection, {"company_id": company_id, "entity_id": note_id})
    if row is None or row.entity_type != "list_note" or row.state.get("list_id") != entity_id:
        raise HTTPException(status_code=404, detail="Note not found")
    entry = await emit_event(
        session, company_id=company_id, entity_id=note_id, entity_type="list_note",
        event_type="list.note_updated",
        data={"list_id": entity_id, "note_id": note_id, "note": payload.note.strip(), "updated_at": datetime.now(UTC).isoformat()},
        actor_id=user.id, location_id=None, source="api",
        idempotency_key=payload.idempotency_key or str(uuid.uuid4()), metadata_={},
    )
    await session.commit()
    return {"event_id": entry.id}


@lists_router.delete("/{entity_id}/notes/{note_id}")
async def delete_list_note(
    entity_id: str,
    note_id: str,
    company_id: str = Depends(get_current_company_id),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    await _get_list(session, company_id, entity_id)
    row = await session.get(Projection, {"company_id": company_id, "entity_id": note_id})
    if row is None or row.entity_type != "list_note" or row.state.get("list_id") != entity_id:
        raise HTTPException(status_code=404, detail="Note not found")
    entry = await emit_event(
        session, company_id=company_id, entity_id=note_id, entity_type="list_note",
        event_type="list.note_removed",
        data={"list_id": entity_id, "note_id": note_id},
        actor_id=user.id, location_id=None, source="api",
        idempotency_key=str(uuid.uuid4()), metadata_={},
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
        _select(LedgerEntry.idempotency_key).where(
            LedgerEntry.company_id == company_id,
            LedgerEntry.idempotency_key.in_(keys),
        )
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
                            LedgerEntry.company_id == company_id,
                            LedgerEntry.idempotency_key == upsert_idem,
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
def _validate_line_entity_ids_subset(line_entity_ids: list[str], doc_state: dict) -> None:
    """Guard: every item entity_id must belong to this document's line_items."""
    doc_eids: set[str] = {
        li.get("entity_id") or li.get("item_id") or ""
        for li in doc_state.get("line_items", [])
    } - {""}
    foreign = set(line_entity_ids) - doc_eids
    if foreign:
        raise HTTPException(
            status_code=422,
            detail=f"Item IDs not linked to this document: {', '.join(sorted(foreign))}",
        )


@router.post("/{entity_id}/fulfill-lines")
async def fulfill_lines(
    entity_id: str,
    body: FulfillLinesRequest,
    company_id: str = Depends(get_current_company_id),
    _: None = Depends(require_operator),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Fulfill specific line items by entity_id. Valid for memo and invoice docs only.

    Inbound doc types (bill, consignment_in) must use POST /receive instead.
    """
    row = await _get_doc(session, company_id, entity_id)
    state = row.state
    doc_type = state.get("doc_type", "")

    allowed_statuses = FULFILLABLE_STATUSES.get(doc_type)
    if allowed_statuses is None:
        raise HTTPException(status_code=422, detail=f"fulfill-lines is not supported for doc type: {doc_type}")
    if state.get("status") not in allowed_statuses:
        raise HTTPException(status_code=409, detail=f"Cannot fulfill a {doc_type} in status '{state.get('status')}'")

    _validate_line_entity_ids_subset(body.line_entity_ids, state)

    if not body.line_entity_ids:
        raise HTTPException(status_code=422, detail="line_entity_ids must not be empty")

    # Each line keyed by the item parcel it references (for qty + split measures).
    line_qty_by_eid: dict[str, float] = {}
    line_by_eid: dict[str, dict] = {}
    for li in state.get("line_items", []):
        _eid = li.get("entity_id") or li.get("item_id") or ""
        if _eid:
            line_qty_by_eid[_eid] = float(li.get("quantity") or 0)
            line_by_eid[_eid] = li

    _unit_map = await _get_unit_map(session, company_id)

    errors: list[str] = []          # 422: item not found / not available
    blocked: list[str] = []         # 409: stock shortage / non-splittable partial
    to_fulfill: list[str] = []
    service_eids: set[str] = set()  # service lines: rendered, not picked from stock
    split_plan: dict[str, dict] = {}  # parent_eid -> child measures (partial draws)
    fetched: dict[str, Projection] = {}
    for item_eid in body.line_entity_ids:
        item_proj = await session.get(Projection, {"company_id": company_id, "entity_id": item_eid})
        if item_proj is None:
            errors.append(f"{item_eid}: item not found")
            continue
        # Service lines have no physical stock: mark them done without any stock/availability guard.
        if ((item_proj.state.get("inventory_type") or "stocked") == "service"
                or (item_proj.state.get("sell_by") or "") in SERVICE_SELL_BY):
            service_eids.add(item_eid)
            continue
        item_status = item_proj.state.get("status", "")
        if item_status != "available":
            errors.append(
                f"{item_eid} ({item_proj.state.get('sku', '')}): must be 'available', is '{item_status}'"
            )
            continue
        # Stock guard: the invoiced quantity must not exceed the parcel's stock,
        # and a partial draw is only allowed when the item permits splitting.
        sku = item_proj.state.get("sku", "")
        line_qty = line_qty_by_eid.get(item_eid, 0.0)
        available = float(item_proj.state.get("quantity", 0))
        if line_qty > available + 1e-9:
            blocked.append(
                f"{sku}: insufficient stock — invoiced {line_qty:g}, available {available:g}"
            )
            continue
        if line_qty + 1e-9 < available and not item_proj.state.get("allow_splitting", True):
            blocked.append(
                f"{sku}: invoiced {line_qty:g} of {available:g} but 'Allow Splitting' is off — "
                f"enable splitting or invoice the full quantity"
            )
            continue
        _line = line_by_eid.get(item_eid, {})
        _sb = item_proj.state.get("sell_by")
        # Whole draw: taking all the quantity takes the whole parcel, so the secondary
        # measures (not the sell-by one) must match the parcel exactly.
        if abs(line_qty - available) <= 1e-9:
            _checks = []
            if not is_pieces_unit(_sb, _unit_map):
                _checks.append(("pcs", (item_proj.state.get("attributes") or {}).get("pieces"), _line.get("pieces")))
            if not is_weight_unit(_sb, _unit_map):
                _checks.append(("weight", item_proj.state.get("weight"), _line.get("weight")))
            _bad = next(((n, p, v) for n, p, v in _checks
                         if p is not None and v is not None and abs(float(v) - float(p)) > 1e-9), None)
            if _bad:
                _n, _p, _v = _bad
                blocked.append(
                    f"{sku}: invoicing the whole quantity must take all {float(_p):g} {_n} (got {float(_v):g})"
                )
                continue
        fetched[item_eid] = item_proj
        to_fulfill.append(item_eid)
        if line_qty + 1e-9 < available:
            # Partial draw of a splittable parcel: split off the invoiced amount as a
            # child and fulfill that; the mother keeps the remainder. For a parcel sold
            # BY pieces/weight the quantity IS that measure, so derive it (the line
            # doesn't carry it separately); otherwise take the line's entered value
            # (defaults to the parcel's, so by default the child takes all).
            split_plan[item_eid] = {
                "child_qty": line_qty,
                "child_weight": line_qty if is_weight_unit(_sb, _unit_map) else _line.get("weight"),
                "child_pieces": line_qty if is_pieces_unit(_sb, _unit_map) else _line.get("pieces"),
            }

    # A shortage or a non-splittable partial prohibits the whole fulfill.
    if blocked:
        raise HTTPException(status_code=409, detail="Cannot fulfill: " + "; ".join(blocked))

    if errors and not to_fulfill and not service_eids:
        raise HTTPException(status_code=422, detail={"errors": errors})

    if not to_fulfill and not service_eids:
        raise HTTPException(status_code=422, detail="No fulfillable items in the provided line_entity_ids")

    now = datetime.now(UTC).isoformat()
    cid = uuid.UUID(str(company_id))
    uid = user.id

    # Split partial draws: carve the invoiced amount off each parcel as a child,
    # retarget fulfillment to the child, and rewrite the doc line to reference it.
    if split_plan:
        from celerp_inventory.routes import split_off_child
        new_line_items = [dict(li) for li in state.get("line_items", [])]
        for parent_eid, plan in split_plan.items():
            try:
                child_eid, child_sku = await split_off_child(
                    session, company_id=cid, user_id=uid, parent_proj=fetched[parent_eid],
                    child_qty=plan["child_qty"], child_weight=plan.get("child_weight"),
                    child_pieces=plan.get("child_pieces"),
                )
            except ValueError as exc:
                raise HTTPException(
                    status_code=409,
                    detail=f"Cannot split {fetched[parent_eid].state.get('sku', '')}: {exc}",
                )
            to_fulfill[to_fulfill.index(parent_eid)] = child_eid
            fetched[child_eid] = await session.get(Projection, {"company_id": company_id, "entity_id": child_eid})
            del fetched[parent_eid]
            for nli in new_line_items:
                if (nli.get("entity_id") or nli.get("item_id")) == parent_eid:
                    nli["entity_id"] = child_eid
                    nli["item_id"] = child_eid
                    nli["sku"] = child_sku
                    break
        await emit_event(
            session, company_id=cid, entity_id=entity_id, entity_type="doc",
            event_type="doc.updated",
            data={"fields_changed": {"line_items": {"old": state.get("line_items"), "new": new_line_items}}},
            actor_id=uid, location_id=None, source="fulfillment",
            idempotency_key=str(uuid.uuid4()), metadata_={},
        )
        state["line_items"] = new_line_items

    total_cogs = 0.0
    for item_eid in to_fulfill:
        item_proj = fetched[item_eid]
        qty = float(item_proj.state.get("quantity", 0))
        cost_total = item_proj.state.get("cost_total")
        if cost_total is not None:
            total_cogs += float(cost_total)
        else:
            total_cogs += float(item_proj.state.get("cost_price") or 0) * float(item_proj.state.get("quantity") or 0)
        await emit_event(
            session,
            company_id=cid,
            entity_id=item_eid,
            entity_type="item",
            event_type="item.fulfilled",
            data={
                "source_doc_id": entity_id,
                "quantity_fulfilled": qty,
                "fulfilled_by": str(uid),
                "doc_type": doc_type,
            },
            actor_id=uid,
            location_id=None,
            source="fulfillment",
            idempotency_key=str(uuid.uuid4()),
            metadata_={"doc_id": entity_id},
        )

    # Optimistically compute doc fulfillment_status. Service lines count as fulfilled (they are
    # rendered, not drawn from stock) so a service-only or mixed doc can reach "fulfilled".
    fulfilled_eids = set(to_fulfill) | service_eids
    line_items = state.get("line_items", [])
    all_statuses: list[str] = []
    for li in line_items:
        li_eid = li.get("entity_id") or li.get("item_id") or ""
        if not li_eid:
            continue
        if li_eid in fulfilled_eids:
            all_statuses.append("memo_out")
        else:
            li_proj = await session.get(Projection, {"company_id": company_id, "entity_id": li_eid})
            all_statuses.append(li_proj.state.get("status", "available") if li_proj else "available")

    if all_statuses and all(s in ("memo_out", "sold") for s in all_statuses):
        doc_fulfillment_status = "fulfilled"
        doc_event_type = "doc.fulfilled"
        doc_event_data: dict = {
            "fulfilled_items": [{"item_id": e} for e in to_fulfill],
            "fulfilled_by": str(uid),
            "fulfilled_at": now,
            "strategy": "per_line",
            "total_cogs": total_cogs,
        }
    else:
        doc_fulfillment_status = "partial"
        doc_event_type = "doc.partially_fulfilled"
        doc_event_data = {
            "fulfilled_items": [{"item_id": e} for e in to_fulfill],
            "unfulfilled_items": [],
            "fulfilled_by": str(uid),
            "fulfilled_at": now,
            "strategy": "per_line",
        }

    await emit_event(
        session,
        company_id=cid,
        entity_id=entity_id,
        entity_type="doc",
        event_type=doc_event_type,
        data=doc_event_data,
        actor_id=uid,
        location_id=None,
        source="fulfillment",
        idempotency_key=str(uuid.uuid4()),
        metadata_={},
    )

    # Create COGS JE only for invoices that reach fully-fulfilled status.
    # Memos don't get a COGS JE here; that happens when the invoice is finalized.
    if doc_type == "invoice" and doc_fulfillment_status == "fulfilled":
        await auto_je.create_for_doc_fulfilled(
            session, company_id=cid, user_id=uid, doc_id=entity_id, total_cogs=total_cogs,
            cycle=state.get("fulfill_cycle", 0),
        )

    await session.commit()
    return {"fulfillment_status": doc_fulfillment_status, "fulfilled": to_fulfill}


@router.post("/{entity_id}/revert-lines")
async def revert_lines(
    entity_id: str,
    body: FulfillLinesRequest,
    company_id: str = Depends(get_current_company_id),
    _: None = Depends(require_operator),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Revert fulfillment for specific line items. Valid for memo and invoice docs only.

    Inbound doc types (bill, consignment_in) must use DELETE /receive instead.
    """
    row = await _get_doc(session, company_id, entity_id)
    state = row.state
    doc_type = state.get("doc_type", "")

    if FULFILLABLE_STATUSES.get(doc_type) is None:
        raise HTTPException(status_code=422, detail=f"revert-lines is not supported for doc type: {doc_type}")

    _validate_line_entity_ids_subset(body.line_entity_ids, state)

    if not body.line_entity_ids:
        raise HTTPException(status_code=422, detail="line_entity_ids must not be empty")

    errors: list[str] = []
    to_revert: list[str] = []
    fetched: dict[str, Projection] = {}
    for item_eid in body.line_entity_ids:
        item_proj = await session.get(Projection, {"company_id": company_id, "entity_id": item_eid})
        if item_proj is None:
            errors.append(f"{item_eid}: item not found")
            continue
        item_status = item_proj.state.get("status", "")
        if item_status not in ("memo_out", "sold"):
            errors.append(
                f"{item_eid} ({item_proj.state.get('sku', '')}): must be 'memo_out' or 'sold' to revert, is '{item_status}'"
            )
            continue
        fetched[item_eid] = item_proj
        to_revert.append(item_eid)

    if errors and not to_revert:
        raise HTTPException(status_code=422, detail={"errors": errors})

    if not to_revert:
        raise HTTPException(status_code=422, detail="No revertible items in the provided line_entity_ids")

    now = datetime.now(UTC).isoformat()
    cid = uuid.UUID(str(company_id))
    uid = user.id

    for item_eid in to_revert:
        item_proj = fetched[item_eid]
        qty = float(item_proj.state.get("quantity", 0))
        await emit_event(
            session,
            company_id=cid,
            entity_id=item_eid,
            entity_type="item",
            event_type="item.fulfillment_reversed",
            data={
                "source_doc_id": entity_id,
                "quantity_restored": qty,
                "reversed_by": str(uid),
                "reason": "per_line_revert",
                "doc_type": doc_type,
            },
            actor_id=uid,
            location_id=None,
            source="fulfillment",
            idempotency_key=str(uuid.uuid4()),
            metadata_={"doc_id": entity_id},
        )

    # Optimistically compute doc fulfillment_status
    newly_available = set(to_revert)
    line_items = state.get("line_items", [])
    all_statuses: list[str] = []
    for li in line_items:
        li_eid = li.get("entity_id") or li.get("item_id") or ""
        if not li_eid:
            continue
        if li_eid in newly_available:
            all_statuses.append("available")
        else:
            li_proj = await session.get(Projection, {"company_id": company_id, "entity_id": li_eid})
            all_statuses.append(li_proj.state.get("status", "available") if li_proj else "available")

    if not all_statuses or all(s == "available" for s in all_statuses):
        doc_fulfillment_status = "unfulfilled"
        doc_event_type = "doc.fulfillment_reversed"
        doc_event_data = {
            "reversed_items": [{"item_id": e} for e in to_revert],
            "reversed_by": str(uid),
            "reason": "per_line_revert",
        }
    elif any(s in ("memo_out", "sold") for s in all_statuses):
        doc_fulfillment_status = "partial"
        doc_event_type = "doc.partially_fulfilled"
        doc_event_data = {
            "fulfilled_items": [],
            "unfulfilled_items": [],
            "fulfilled_by": str(uid),
            "fulfilled_at": now,
            "strategy": "per_line",
        }
    else:
        doc_fulfillment_status = "unfulfilled"
        doc_event_type = "doc.fulfillment_reversed"
        doc_event_data = {
            "reversed_items": [{"item_id": e} for e in to_revert],
            "reversed_by": str(uid),
            "reason": "per_line_revert",
        }

    await emit_event(
        session,
        company_id=cid,
        entity_id=entity_id,
        entity_type="doc",
        event_type=doc_event_type,
        data=doc_event_data,
        actor_id=uid,
        location_id=None,
        source="fulfillment",
        idempotency_key=str(uuid.uuid4()),
        metadata_={},
    )

    # Void COGS JE for invoices when fulfillment is fully reversed.
    # void_for_doc_fulfilled is a no-op if no JE exists (safe to call unconditionally).
    if doc_type == "invoice" and doc_fulfillment_status == "unfulfilled":
        await auto_je.void_for_doc_fulfilled(
            session, company_id=cid, user_id=uid, doc_id=entity_id,
            cycle=state.get("fulfill_cycle", 0),
        )

    await session.commit()
    return {"fulfillment_status": doc_fulfillment_status, "reverted": to_revert}


class ReturnReceivedItem(BaseModel):
    sku: str
    quantity: float


class ReceiveReturnPayload(BaseModel):
    items: list[ReturnReceivedItem]
    notes: str | None = None
    idempotency_key: str | None = None


@router.post("/{entity_id}/receive-return")
async def receive_return(
    entity_id: str,
    payload: ReceiveReturnPayload,
    company_id: str = Depends(get_current_company_id),
    _: None = Depends(require_operator),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Receive returned goods on a credit note.

    Item values are resolved server-side (not trusted from the UI):
    - Case 1: CN has original_doc_id -> fetch original invoice, match by SKU, use its values.
    - Case 2: No original_doc_id -> query sold inventory by SKU (LIFO), use those values.
    Creates new inventory items (status=available) and a reversing COGS JE.
    """
    from celerp_inventory.routes import _flatten_item

    row = await _get_doc(session, company_id, entity_id)
    state = row.state
    if state.get("doc_type") != "credit_note":
        raise HTTPException(status_code=409, detail="receive-return is only valid for credit notes")
    if state.get("status") in ("draft", "void"):
        raise HTTPException(status_code=409, detail="Cannot receive return on a draft or voided credit note")
    if not payload.items:
        raise HTTPException(status_code=422, detail="At least one item is required")

    # Validate return quantities against CN line item sell_by precision
    sell_by_map = await _get_item_sell_by_map(session, company_id)
    cn_line_sell_by: dict[str, str] = {
        li.get("sku", ""): li.get("sell_by") or ""
        for li in (state.get("line_items") or [])
        if li.get("sku")
    }
    unit_map = await _get_unit_map(session, company_id)
    for it in payload.items:
        sell_by = sell_by_map.get(it.sku or "") or cn_line_sell_by.get(it.sku or "") or None
        validate_line_quantity(it.quantity, sell_by, unit_map, label=it.sku)

    # --- Resolve item metadata ---
    # Priority: sold inventory records (most authoritative - have cost_price + full attributes).
    # Fallback for descriptive fields only: original invoice line items.
    original_doc_id = state.get("original_doc_id")
    original_line_map: dict[str, dict] = {}
    if original_doc_id:
        try:
            orig_row = await _get_doc(session, company_id, original_doc_id)
            for li in (orig_row.state.get("line_items") or []):
                sku = li.get("sku") or ""
                if sku and sku not in original_line_map:
                    original_line_map[sku] = li
        except HTTPException:
            pass  # original doc inaccessible - descriptive fallback unavailable

    # Load all sold inventory rows upfront
    all_skus = {it.sku for it in payload.items}
    sold_map: dict[str, list[dict]] = {}
    item_rows = (await session.execute(
        select(Projection).where(
            Projection.company_id == company_id,
            Projection.entity_type == "item",
        )
    )).scalars().all()
    for r in item_rows:
        flat = _flatten_item(r.state, r.entity_id)
        if str(flat.get("status") or "").lower() == "sold" and flat.get("sku") in all_skus:
            sold_map.setdefault(flat["sku"], []).append(flat)
    # LIFO: most recently created first
    for sku in sold_map:
        sold_map[sku].sort(key=lambda x: x.get("created_at") or "", reverse=True)

    # --- Validate quantities before touching anything ---
    for it in payload.items:
        if it.quantity <= 0:
            raise HTTPException(status_code=422, detail=f"Quantity must be positive for SKU '{it.sku}'")
        # Sold inventory is best-effort enrichment; no hard gate on its existence.
        # If sold records exist, validate available quantity.
        if it.sku in sold_map:
            available = sum(float(s.get("quantity") or 0) for s in sold_map[it.sku])
            if available < it.quantity:
                raise HTTPException(
                    status_code=422,
                    detail=f"Only {available:g} sold unit(s) of SKU '{it.sku}' found in inventory; {it.quantity:g} requested.",
                )

    # --- Create returned inventory items ---
    now = datetime.now(UTC).isoformat()
    total_cogs = 0.0
    received_items = []

    _CORE_KEYS = frozenset({
        "id", "entity_id", "status", "quantity", "created_at", "updated_at",
        "location_id", "location_name", "source_doc_id",
    })

    for it in payload.items:
        # Sold inventory record is the preferred source (has cost_price + all attributes).
        # Fall back to invoice line item data when no sold record exists (e.g. pre-fulfillment items).
        ref = sold_map[it.sku][0] if it.sku in sold_map else {}
        li_fallback = original_line_map.get(it.sku, {})

        # Collect any extra dynamic price keys from ref (e.g. wholesale_price, retail_price, vip_price, ...)
        extra_prices = {k: v for k, v in ref.items() if k not in _CORE_KEYS and k.endswith("_price") and k not in (
            "cost_price", "unit_price",
        )}

        resolved_name = ref.get("name") or li_fallback.get("name") or ""
        resolved_sell_by = ref.get("sell_by") or li_fallback.get("sell_by") or ""

        # Hard stop: if we cannot resolve the minimum required fields from any source, refuse loudly.
        missing = []
        if not resolved_name:
            missing.append("name")
        if not resolved_sell_by:
            missing.append("sell_by")
        if missing:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Cannot receive return for SKU '{it.sku}': missing required field(s) {missing}. "
                    f"No sold inventory record and no matching line item in the original invoice were found. "
                    f"Ensure the credit note is linked to an invoice or that the item was sold through this system."
                ),
            )

        item_data = {
            "sku": it.sku,
            "name": resolved_name,
            "quantity": it.quantity,
            "sell_by": resolved_sell_by,
            "cost_price": float(ref.get("cost_price") or 0),
            "unit_price": float(ref.get("unit_price") or ref.get("sell_price") or li_fallback.get("unit_price") or 0),
            "wholesale_price": float(ref.get("wholesale_price") or li_fallback.get("wholesale_price") or 0) or None,
            "retail_price": float(ref.get("retail_price") or li_fallback.get("retail_price") or 0) or None,
            "barcode": ref.get("barcode") or li_fallback.get("barcode") or None,
            "description": ref.get("description") or li_fallback.get("description") or "",
            "category": ref.get("category") or li_fallback.get("category") or "",
            "attributes": ref.get("attributes") or li_fallback.get("attributes") or {},
            **extra_prices,
            "status": "available",
            "source_doc_id": entity_id,
            "created_at": now,
        }

        item_id = f"item:{uuid.uuid4()}"
        await emit_event(
            session,
            company_id=company_id,
            entity_id=item_id,
            entity_type="item",
            event_type="item.created",
            data=item_data,
            actor_id=user.id,
            location_id=None,
            source="return",
            idempotency_key=str(uuid.uuid4()),
            metadata_={"source_return_cn": entity_id},
        )
        cost_price = item_data["cost_price"]
        total_cogs += cost_price * it.quantity
        received_items.append({
            "item_id": item_id,
            "sku": it.sku,
            "name": item_data["name"],
            "quantity": it.quantity,
            "cost_price": cost_price,
            "received_at": now,
        })

    await emit_event(
        session,
        company_id=company_id,
        entity_id=entity_id,
        entity_type="doc",
        event_type="doc.return_received",
        data={
            "items": received_items,
            "received_by": str(user.id),
            "notes": payload.notes,
            "received_at": now,
        },
        actor_id=user.id,
        location_id=None,
        source="api",
        idempotency_key=payload.idempotency_key or str(uuid.uuid4()),
        metadata_={},
    )

    await auto_je.create_for_return_received(
        session,
        company_id=company_id,
        user_id=user.id,
        cn_id=entity_id,
        total_cogs=total_cogs,
        je_suffix=received_items[0]["item_id"].split(":")[-1] if received_items else str(uuid.uuid4()),
    )

    await session.commit()
    return {"received_items": received_items, "total_cogs_reversed": total_cogs}


@router.delete("/{entity_id}/receive-return")
async def undo_receive_return(
    entity_id: str,
    company_id: str = Depends(get_current_company_id),
    _: None = Depends(require_operator),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Undo a receive-return on a credit note.

    Deletes all inventory items created by the return and reverses the COGS JE.
    Clears return_received_items on the CN projection.
    """
    row = await _get_doc(session, company_id, entity_id)
    state = row.state
    if state.get("doc_type") != "credit_note":
        raise HTTPException(status_code=409, detail="undo-receive-return is only valid for credit notes")
    received_items = state.get("return_received_items") or []
    if not received_items:
        raise HTTPException(status_code=409, detail="No received return to undo")

    now = datetime.now(UTC).isoformat()
    item_ids = [r["item_id"] for r in received_items if r.get("item_id")]
    total_cogs = sum(
        float(r.get("cost_total") or 0) or (float(r.get("cost_price") or 0) * float(r.get("quantity") or 0))
        for r in received_items
    )

    # Pre-flight: verify every returned item is still "available" before archiving.
    # If an item was re-sold or already archived, we cannot silently remove it.
    if item_ids:
        rows = await session.execute(
            select(Projection).where(
                Projection.company_id == company_id,
                Projection.entity_type == "item",
                Projection.entity_id.in_(item_ids),
            )
        )
        item_rows = {r.entity_id: r.state for r in rows.scalars().all()}
        blocked: list[str] = []
        for iid in item_ids:
            item_state = item_rows.get(iid)
            if item_state is None:
                blocked.append(f"{iid} (not found - may have already been removed)")
            elif item_state.get("status") != "available":
                sku = item_state.get("sku") or iid
                status = item_state.get("status") or "unknown"
                blocked.append(f"SKU '{sku}' is '{status}' - cannot archive")
        if blocked:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Cannot revert return stock: one or more returned items are no longer available. "
                    f"Blocked items: {'; '.join(blocked)}. "
                    "You may need to manually correct the inventory before reverting."
                ),
            )

    # Unique suffix ensures each undo gets its own JE - prevents idempotency collision on repeated attempts
    undo_suffix = str(uuid.uuid4())

    # Archive each returned inventory item (sets status=archived, preserving audit trail)
    for iid in item_ids:
        await emit_event(
            session,
            company_id=company_id,
            entity_id=iid,
            entity_type="item",
            event_type="item.status.set",
            data={"new_status": "archived", "reason": f"undo receive-return on {entity_id}"},
            actor_id=user.id,
            location_id=None,
            source="return_undo",
            idempotency_key=str(uuid.uuid4()),
            metadata_={"source_return_cn": entity_id},
        )

    # Emit doc.return_undone to clear projection
    await emit_event(
        session,
        company_id=company_id,
        entity_id=entity_id,
        entity_type="doc",
        event_type="doc.return_undone",
        data={"undone_by": str(user.id), "undone_at": now, "item_ids": item_ids},
        actor_id=user.id,
        location_id=None,
        source="api",
        idempotency_key=str(uuid.uuid4()),
        metadata_={},
    )

    # Reverse the COGS JE (unique key per undo prevents idempotency collision if attempted twice)
    if total_cogs > 0:
        await auto_je.create_for_return_undone(
            session,
            company_id=company_id,
            user_id=user.id,
            cn_id=entity_id,
            total_cogs=total_cogs,
            unique_suffix=undo_suffix,
        )

    await session.commit()
    return {"undone": True, "item_ids": item_ids}


@router.delete("/{entity_id}/receive")
async def undo_receive(
    entity_id: str,
    company_id: str = Depends(get_current_company_id),
    _: None = Depends(require_operator),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Undo a goods-received on a bill.

    Disposes all inventory items created by the receive and reverses the AP/Inventory JE.
    Clears received_items and received_item_ids on the bill projection.
    """
    row = await _get_doc(session, company_id, entity_id)
    state = row.state
    if state.get("doc_type") != "bill":
        raise HTTPException(status_code=409, detail="undo-receive is only valid for bills")
    received_item_ids = state.get("received_item_ids") or []
    if not received_item_ids:
        raise HTTPException(status_code=409, detail="No received goods to revert")

    now = datetime.now(UTC).isoformat()

    # Pre-flight: verify every created item is still "available" before disposing
    rows_result = await session.execute(
        select(Projection).where(
            Projection.company_id == company_id,
            Projection.entity_type == "item",
            Projection.entity_id.in_(received_item_ids),
        )
    )
    item_rows = {r.entity_id: r.state for r in rows_result.scalars().all()}
    blocked: list[str] = []
    for iid in received_item_ids:
        item_state = item_rows.get(iid)
        if item_state is None:
            blocked.append(f"{iid} (not found - may have already been removed)")
        elif item_state.get("status") != "available":
            sku = item_state.get("sku") or iid
            status = item_state.get("status") or "unknown"
            blocked.append(f"SKU '{sku}' is '{status}' - cannot archive")
    if blocked:
        raise HTTPException(
            status_code=409,
            detail=(
                "Cannot revert goods received: one or more items are no longer available. "
                f"Blocked items: {'; '.join(blocked)}. "
                "You may need to manually correct the inventory before reverting."
            ),
        )

    undo_suffix = str(uuid.uuid4())

    for iid in received_item_ids:
        await emit_event(
            session,
            company_id=company_id,
            entity_id=iid,
            entity_type="item",
            event_type="item.status.set",
            data={"new_status": "archived", "reason": f"undo receive on {entity_id}"},
            actor_id=user.id,
            location_id=None,
            source="receive_undo",
            idempotency_key=str(uuid.uuid4()),
            metadata_={"source_doc": entity_id},
        )

    await emit_event(
        session,
        company_id=company_id,
        entity_id=entity_id,
        entity_type="doc",
        event_type="doc.receive_undone",
        data={"undone_by": str(user.id), "undone_at": now, "item_ids": received_item_ids},
        actor_id=user.id,
        location_id=None,
        source="api",
        idempotency_key=str(uuid.uuid4()),
        metadata_={},
    )

    po_total = float(state.get("total", 0) or 0)
    if po_total == 0:
        po_total = sum(
            float(li.get("quantity", 0) or 0) * float(li.get("unit_price", 0) or 0)
            for li in state.get("line_items", [])
        )
    await auto_je.create_for_receive_undone(
        session,
        company_id=company_id,
        user_id=user.id,
        bill_id=entity_id,
        doc=state,
        total_cost=po_total,
        unique_suffix=undo_suffix,
    )

    await session.commit()
    return {"undone": True, "item_ids": received_item_ids}


# ── Doc File Attachments ───────────────────────────────────────────────────────


def _get_doc_file(files: list[dict], file_id: str) -> dict:
    match = next((f for f in files if f.get("id") == file_id), None)
    if match is None:
        raise HTTPException(status_code=404, detail="File not found")
    return match


@router.post("/{entity_id}/files")
async def upload_doc_file(
    entity_id: str,
    file: UploadFile,
    company_id: str = Depends(get_current_company_id),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    row = await _get_doc(session, company_id, entity_id)
    try:
        meta = await store_upload(company_id, file)
    except ValueError as exc:
        raise HTTPException(status_code=413, detail=str(exc))

    entry = await emit_event(
        session,
        company_id=company_id,
        entity_id=entity_id,
        entity_type="doc",
        event_type="doc.file_attached",
        data={
            "entity_id": entity_id,
            "entity_type": "doc",
            "file_id": meta["id"],
            "filename": meta["filename"],
            "mime": meta["mime"],
            "size": meta["size"],
            "url": meta["url"],
            "document_tag": None,
            "description": None,
            "uploaded_at": datetime.now(UTC).isoformat(),
        },
        actor_id=user.id,
        location_id=None,
        source="api",
        idempotency_key=str(uuid.uuid4()),
        metadata_={},
    )
    await session.commit()
    return {"event_id": entry.id, **meta}


@router.get("/{entity_id}/files/{file_id}")
async def download_doc_file(
    entity_id: str,
    file_id: str,
    company_id: str = Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
):
    """Download a file attached to a doc (invoice, bill, etc.)."""
    from fastapi.responses import FileResponse, RedirectResponse
    from pathlib import Path
    from celerp.config import settings

    row = await _get_doc(session, company_id, entity_id)
    match = _get_doc_file(row.state.get("files", []), file_id)

    url = match.get("url", "")
    # Cloud/S3: redirect to the signed URL directly
    if url.startswith("http://") or url.startswith("https://"):
        return RedirectResponse(url)

    # Legacy records (pre-fix) have no url stored; reconstruct from file_id + filename.
    if not url:
        ext = Path(match.get("filename", "")).suffix
        url = f"/static/attachments/{company_id}/{file_id}{ext}"

    dest = settings.data_dir / url.lstrip("/")
    if not dest.exists():
        raise HTTPException(status_code=404, detail="File missing from disk")

    return FileResponse(
        path=str(dest),
        filename=match["filename"],
        media_type=match.get("mime", "application/octet-stream"),
    )


@router.patch("/{entity_id}/files/{file_id}/tag")
async def tag_doc_file(
    entity_id: str,
    file_id: str,
    document_tag: str = Form(""),
    company_id: str = Depends(get_current_company_id),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    row = await _get_doc(session, company_id, entity_id)
    _get_doc_file(row.state.get("files", []), file_id)
    await emit_event(
        session,
        company_id=company_id,
        entity_id=entity_id,
        entity_type="doc",
        event_type="doc.file_tagged",
        data={"entity_id": entity_id, "entity_type": "doc", "file_id": file_id, "document_tag": document_tag},
        actor_id=user.id,
        location_id=None,
        source="api",
        idempotency_key=str(uuid.uuid4()),
        metadata_={},
    )
    await session.commit()
    updated = await session.get(Projection, {"company_id": company_id, "entity_id": entity_id})
    return (updated.state if updated else {}) | {"id": entity_id}


@router.patch("/{entity_id}/files/{file_id}/description")
async def update_doc_file_description(
    entity_id: str,
    file_id: str,
    description: str = Form(""),
    company_id: str = Depends(get_current_company_id),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    row = await _get_doc(session, company_id, entity_id)
    _get_doc_file(row.state.get("files", []), file_id)
    await emit_event(
        session,
        company_id=company_id,
        entity_id=entity_id,
        entity_type="doc",
        event_type="doc.file_description_updated",
        data={"entity_id": entity_id, "entity_type": "doc", "file_id": file_id, "description": description},
        actor_id=user.id,
        location_id=None,
        source="api",
        idempotency_key=str(uuid.uuid4()),
        metadata_={},
    )
    await session.commit()
    updated = await session.get(Projection, {"company_id": company_id, "entity_id": entity_id})
    return (updated.state if updated else {}) | {"id": entity_id}


@router.delete("/{entity_id}/files/{file_id}")
async def delete_doc_file(
    entity_id: str,
    file_id: str,
    company_id: str = Depends(get_current_company_id),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    row = await _get_doc(session, company_id, entity_id)
    _get_doc_file(row.state.get("files", []), file_id)
    entry = await emit_event(
        session,
        company_id=company_id,
        entity_id=entity_id,
        entity_type="doc",
        event_type="doc.file_deleted",
        data={"entity_id": entity_id, "entity_type": "doc", "file_id": file_id},
        actor_id=user.id,
        location_id=None,
        source="api",
        idempotency_key=str(uuid.uuid4()),
        metadata_={},
    )
    await session.commit()
    updated = await session.get(Projection, {"company_id": company_id, "entity_id": entity_id})
    return (updated.state if updated else {}) | {"id": entity_id}


# ---------------------------------------------------------------------------
# Inventory Audit (a location-bound list_type="audit": scan to confirm presence,
# optionally re-count, then adjust stock - all reversible). See
# context/2026-0614-inventory-audit-scanner-plan.md.
# ---------------------------------------------------------------------------

audits_router = APIRouter(dependencies=[Depends(get_current_user)])

# Physical item types an audit counts (services / non-stocked have no stock to count).
_AUDIT_STOCK_TYPES = frozenset({"stocked", "component"})


class AuditCreateBody(BaseModel):
    location_id: str
    idempotency_key: str | None = None


class AuditScanBody(BaseModel):
    barcode: str


class AuditCountBody(BaseModel):
    counted_qty: float | None = None  # None clears the pending adjustment (no change)


def _audit_unit_cost(state: dict) -> float:
    cp = state.get("cost_price")
    try:
        if cp is not None:
            return float(cp)
        total = state.get("cost_total")
        qty = float(state.get("quantity") or 0)
        if total is not None and qty > 0:
            return float(total) / qty
    except (TypeError, ValueError):
        return 0.0
    return 0.0


async def _get_audit(session: AsyncSession, company_id, entity_id: str) -> Projection:
    row = await _get_list(session, company_id, entity_id)
    if row.state.get("list_type") != "audit":
        raise HTTPException(status_code=404, detail="Audit not found")
    return row


async def _audit_update(session, company_id, entity_id, user, fields: dict):
    """Emit list.updated carrying the given top-level field changes."""
    fc = {k: {"new": v} for k, v in fields.items()}
    return await _emit_list(session, company_id, entity_id, "list.updated", {"fields_changed": fc}, user)


async def _resolve_barcode(session, company_id, code: str):
    """Resolve a scanned code to an item: exact barcode first, then exact SKU. None if no match."""
    rows = (await session.execute(select(Projection).where(
        Projection.company_id == company_id, Projection.entity_type == "item"))).scalars().all()
    hit = next((r for r in rows if str(r.state.get("barcode") or "") == code), None)
    return hit or next((r for r in rows if str(r.state.get("sku") or "") == code), None)


@audits_router.post("")
async def create_audit(
    payload: AuditCreateBody,
    company_id=Depends(get_current_company_id),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Create a location-bound audit pre-populated with the physical items at that location."""
    company = await session.get(Company, company_id)
    rows = (await session.execute(select(Projection).where(
        Projection.company_id == company_id, Projection.entity_type == "item"))).scalars().all()
    lines: list[dict] = []
    for r in rows:
        st = r.state
        if str(r.location_id or "") != payload.location_id:
            continue
        if (st.get("status") or "available") != "available":
            continue
        if (st.get("inventory_type") or "stocked") not in _AUDIT_STOCK_TYPES:
            continue
        lines.append({"item_id": r.entity_id, "sku": st.get("sku"), "name": st.get("name"),
                      "barcode": st.get("barcode"), "quantity": float(st.get("quantity") or 0),
                      "audited_at": None, "counted_qty": None})
    ref_id = next_doc_ref(company, "audit")
    entity_id = f"list:{ref_id}"
    if await session.get(Projection, {"company_id": company_id, "entity_id": entity_id}) is not None:
        raise HTTPException(status_code=409, detail=f"Audit number '{ref_id}' already exists")
    data = {"list_type": "audit", "location_id": payload.location_id, "status": "unaudited",
            "ref_id": ref_id, "line_items": lines, "adjust_count": 0,
            "currency": company.settings.get("currency", "USD")}
    entry = await _emit_list(session, company_id, entity_id, "list.created", data, user, payload.idempotency_key)
    await session.commit()
    return {"event_id": entry.id, "id": entity_id, "ref_id": ref_id, "line_count": len(lines)}


@audits_router.get("/{entity_id}")
async def get_audit(entity_id: str, company_id=Depends(get_current_company_id),
                    session: AsyncSession = Depends(get_session)) -> dict:
    row = await _get_audit(session, company_id, entity_id)
    return row.state | {"id": row.entity_id}


@audits_router.post("/{entity_id}/scan")
async def scan_audit(
    entity_id: str, payload: AuditScanBody,
    company_id=Depends(get_current_company_id), user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Uniform scan rule: not on the list -> add + audit (top-insert); on the list but un-audited ->
    audit (move to top); already audited -> reject. Unknown barcode -> reject."""
    row = await _get_audit(session, company_id, entity_id)
    if row.state.get("status") == "stock_adjusted":
        raise HTTPException(status_code=409, detail="This audit's stock has been adjusted; reopen to scan again")
    code = (payload.barcode or "").strip()
    if not code:
        raise HTTPException(status_code=422, detail="Empty scan")
    item = await _resolve_barcode(session, company_id, code)
    if item is None:
        raise HTTPException(status_code=404, detail=f"Unknown barcode or SKU: {code}")
    now = datetime.now(UTC).isoformat()
    lines = [dict(l) for l in (row.state.get("line_items") or [])]
    idx = next((i for i, l in enumerate(lines) if l.get("item_id") == item.entity_id), None)
    if idx is None:
        lines.insert(0, {"item_id": item.entity_id, "sku": item.state.get("sku"), "name": item.state.get("name"),
                         "barcode": item.state.get("barcode"), "quantity": float(item.state.get("quantity") or 0),
                         "audited_at": now, "counted_qty": None})
        result_state = "added"
    elif lines[idx].get("audited_at") is None:
        ln = lines.pop(idx)
        ln["audited_at"] = now
        lines.insert(0, ln)
        result_state = "audited"
    else:
        raise HTTPException(status_code=409, detail=f"Already scanned: {item.state.get('sku') or code}")
    await _audit_update(session, company_id, entity_id, user, {"line_items": lines})
    await session.commit()
    return {"state": result_state, "item_id": item.entity_id, "sku": item.state.get("sku")}


@audits_router.patch("/{entity_id}/line/{item_id}")
async def set_audit_count(
    entity_id: str, item_id: str, payload: AuditCountBody,
    company_id=Depends(get_current_company_id), user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    row = await _get_audit(session, company_id, entity_id)
    if row.state.get("status") == "stock_adjusted":
        raise HTTPException(status_code=409, detail="Cannot edit an adjusted audit; reopen it first")
    lines = [dict(l) for l in (row.state.get("line_items") or [])]
    idx = next((i for i, l in enumerate(lines) if l.get("item_id") == item_id), None)
    if idx is None:
        raise HTTPException(status_code=404, detail="Item is not on this audit")
    cq = payload.counted_qty
    lines[idx]["counted_qty"] = float(cq) if cq is not None else None
    await _audit_update(session, company_id, entity_id, user, {"line_items": lines})
    await session.commit()
    return {"ok": True}


@audits_router.post("/{entity_id}/done")
async def mark_audit_done(entity_id: str, company_id=Depends(get_current_company_id),
                          user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    row = await _get_audit(session, company_id, entity_id)
    if row.state.get("status") != "unaudited":
        raise HTTPException(status_code=409, detail="Audit is not in the unaudited state")
    await _audit_update(session, company_id, entity_id, user, {"status": "audited"})
    await session.commit()
    return {"ok": True}


@audits_router.post("/{entity_id}/reopen")
async def reopen_audit(entity_id: str, company_id=Depends(get_current_company_id),
                       user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    row = await _get_audit(session, company_id, entity_id)
    if row.state.get("status") != "audited":
        raise HTTPException(status_code=409, detail="Only an audited (not yet adjusted) audit can be reopened")
    await _audit_update(session, company_id, entity_id, user, {"status": "unaudited"})
    await session.commit()
    return {"ok": True}


@audits_router.post("/{entity_id}/adjust")
async def adjust_audit(
    entity_id: str, company_id=Depends(get_current_company_id),
    _: None = Depends(require_manager), user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Apply each line's counted_qty to its item (manager/owner). Lines with counted_qty=None are
    skipped. Posts a Shrinkage/Overage JE and records prior_qty so the action is undoable."""
    row = await _get_audit(session, company_id, entity_id)
    if row.state.get("status") != "audited":
        raise HTTPException(status_code=409, detail="Mark the audit done before adjusting stock")
    cycle = int(row.state.get("adjust_count") or 0)
    lines = [dict(l) for l in (row.state.get("line_items") or [])]
    shrink_val = 0.0
    over_val = 0.0
    adjusted = 0
    for l in lines:
        cq = l.get("counted_qty")
        if cq is None:
            continue  # report-and-skip
        item = await session.get(Projection, {"company_id": company_id, "entity_id": l.get("item_id")})
        if item is None or item.entity_type != "item":
            continue
        prior = float(item.state.get("quantity") or 0)
        cqf = float(cq)
        if abs(cqf - prior) < 1e-9:
            continue  # no change
        unit_cost = _audit_unit_cost(item.state)
        if cqf < prior:
            shrink_val += (prior - cqf) * unit_cost
        else:
            over_val += (cqf - prior) * unit_cost
        await emit_event(
            session, company_id=company_id, entity_id=l["item_id"], entity_type="item",
            event_type="item.quantity.adjusted",
            data={"new_qty": cqf, "reason": "audit", "source_list_id": entity_id, "prior_qty": prior},
            actor_id=user.id, location_id=None, source="audit", idempotency_key=str(uuid.uuid4()),
            metadata_={"audit_id": entity_id},
        )
        l["prior_qty"] = prior
        l["adjusted"] = True
        adjusted += 1
    await auto_je.create_for_audit_adjustment(
        session, company_id=company_id, user_id=user.id, list_id=entity_id,
        shrinkage_value=shrink_val, overage_value=over_val, cycle=cycle,
    )
    await _audit_update(session, company_id, entity_id, user,
                        {"status": "stock_adjusted", "line_items": lines, "adjust_count": cycle + 1})
    await session.commit()
    return {"adjusted": adjusted, "shrinkage_value": round(shrink_val, 2), "overage_value": round(over_val, 2)}


@audits_router.post("/{entity_id}/undo-adjust")
async def undo_audit_adjust(
    entity_id: str, company_id=Depends(get_current_company_id),
    _: None = Depends(require_manager), user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Reverse the last stock adjustment (manager/owner): restore each item's prior quantity and void
    the audit JE. Returns the audit to the 'audited' state."""
    row = await _get_audit(session, company_id, entity_id)
    if row.state.get("status") != "stock_adjusted":
        raise HTTPException(status_code=409, detail="This audit has no adjustment to undo")
    cycle = int(row.state.get("adjust_count") or 1) - 1
    lines = [dict(l) for l in (row.state.get("line_items") or [])]
    for l in lines:
        if l.get("adjusted") and l.get("prior_qty") is not None:
            await emit_event(
                session, company_id=company_id, entity_id=l["item_id"], entity_type="item",
                event_type="item.quantity.adjusted",
                data={"new_qty": float(l["prior_qty"]), "reason": "audit_undo", "source_list_id": entity_id},
                actor_id=user.id, location_id=None, source="audit", idempotency_key=str(uuid.uuid4()),
                metadata_={"audit_id": entity_id},
            )
            l.pop("adjusted", None)
            l.pop("prior_qty", None)
    await auto_je.void_for_audit_adjustment(session, company_id=company_id, user_id=user.id,
                                            list_id=entity_id, cycle=cycle)
    await _audit_update(session, company_id, entity_id, user, {"status": "audited", "line_items": lines})
    await session.commit()
    return {"ok": True}
