# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT

from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import json
import uuid
from datetime import datetime, timezone, date as _date
from typing import Literal

from fastapi import APIRouter, Depends, Form, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse

from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import select, func as _func, text
import sqlalchemy as _sa
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.db import get_session
from celerp.events.engine import emit_event
from celerp.models.company import Company
from celerp.modules.slots import fire_lifecycle
from celerp.models.projections import Projection
from celerp.inventory_codes import MAX_SCAN_CODE_LEN
from celerp_docs.taxes import TaxApplication, compute_tax_amounts
from celerp.services import auto_je
from celerp.services.landed_cost import compute_bill_landed_allocation
from celerp.services.line_measures import line_label, splitting_allowed
from celerp.services.attachments import store_upload
from ui.components.currency import CURRENCY_CODES
from celerp.services.auth import get_current_company_id, get_current_role, get_current_user
from celerp.services.permissions import assert_role_permission, get_current_company_settings, require_permission, role_has_permission
from celerp_docs.sequences import next_doc_ref, get_all_sequences, update_sequence, validate_pattern, list_sequence_key
from celerp.services.units import DEFAULT_UNITS, build_unit_map, is_non_stock_line, is_pieces_unit, is_weight_unit, validate_line_quantity
from celerp.services.money import checked_exchange_rate, round_money, to_decimal, to_stored_float
from celerp.services.pricing import DEFAULT_PRICE_LIST_NAME, coerce_price, get_price_config, resolve_price
from celerp_docs.doc_constants import INBOUND_DOC_TYPES, FULFILLABLE_STATUSES, FULFILLED_ITEM_STATUSES, NON_FINANCIAL_DOC_TYPES, RESERVABLE_DOC_STATUSES
from celerp.services.list_behavior import (
    DRAFT, FINALIZED, CLOSED, VOID, DEFAULT_LIST_TYPE, LIST_TYPES, behavior, terminal_action, is_money_list,
)
from celerp.services.shipping import INCOTERMS_2020, REASONS_FOR_EXPORT

router = APIRouter(dependencies=[Depends(get_current_user)])

# Bulk payment bounds each per-doc row-lock wait so a contended doc skips rather than
# blocking the worker. 3s sits above a normal per-doc lock hold (no false skip under
# ordinary load) and well under typical client/proxy request timeouts. Applied
# transaction-local before each acquisition and reapplied every iteration, since
# apply_doc_payment commits per doc and the prior commit resets the setting.
_BULK_DOC_LOCK_TIMEOUT_MS = 3000

# Closed-set shipment fields: unknown values never reach the event log ('' clears).
_SHIPMENT_ENUM_FIELDS: dict[str, frozenset[str]] = {
    "incoterms": frozenset(INCOTERMS_2020),
    "reason_for_export": frozenset(REASONS_FOR_EXPORT),
}


def _validate_shipment_values(values: dict) -> None:
    for field, allowed in _SHIPMENT_ENUM_FIELDS.items():
        v = values.get(field)
        if v and v not in allowed:
            raise ValueError(f"Invalid {field}: {v!r}")


class LineItem(BaseModel):
    item_id: str | None = None
    entity_id: str | None = None  # alias sent by the frontend; resolved to item_id below
    sku: str | None = None
    # Stamped from the catalog item when the line is added; the identifier a
    # finalized document keeps showing even if the item is later re-barcoded.
    barcode: str | None = None
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


def _stored_conversion_rate(v: float | None) -> float | None:
    """The rate as a document stores it, or ValueError naming what is wrong.

    Shared by every door that sets a rate on a document or on one of its
    payments, so a rate is one stored fact at one precision however it arrived.
    checked_exchange_rate holds the rule; this only adds that a document may
    carry no rate at all, which is what a base-currency document does.

    Validation belongs here, at the request boundary, and never in the
    projection: the projection replays stored events, so rejecting or rounding
    there would restate documents that are already posted.
    """
    if v is None:
        return v
    try:
        return to_stored_float(checked_exchange_rate(v))
    except ValueError as exc:
        raise ValueError(f"conversion_rate {exc}") from exc


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
    # Declared rather than left to extra="allow" so the rate is validated on the
    # way in. Foreign-currency documents require one before they can finalize.
    conversion_rate: float | None = None
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

    @field_validator("conversion_rate")
    @classmethod
    def _rate_is_usable(cls, v: float | None) -> float | None:
        return _stored_conversion_rate(v)


class DocPatch(BaseModel):
    fields_changed: dict[str, dict] = Field(default_factory=dict)
    idempotency_key: str | None = None


class DocSendBody(BaseModel):
    sent_via: str | None = None
    sent_to: str | None = None
    cc: str | None = None
    bcc: str | None = None
    subject: str | None = None
    message: str | None = None
    idempotency_key: str | None = None


class DocVoidBody(BaseModel):
    reason: str | None = None
    idempotency_key: str | None = None


class DocCloseBody(BaseModel):
    reason: str | None = None
    idempotency_key: str | None = None


class DocReopenBody(BaseModel):
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
    conversion_rate: float | None = None
    source_doc_id: str | None = None
    target_doc_id: str | None = None
    idempotency_key: str | None = None

    @field_validator("conversion_rate")
    @classmethod
    def _rate_is_usable(cls, v: float | None) -> float | None:
        return _stored_conversion_rate(v)


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
    records: list[DocImportRecord] = Field(..., max_length=500)
    upsert: bool = False


class FulfillLinesRequest(BaseModel):
    line_entity_ids: list[str]

    @field_validator("line_entity_ids")
    @classmethod
    def strip_empty(cls, v: list[str]) -> list[str]:
        """Drop empty strings and de-duplicate — JS bulk selects may include value="" rows."""
        return list(dict.fromkeys(eid for eid in v if eid))


class RevertLinesRequest(FulfillLinesRequest):
    """Revert whole lines, or return part of one.

    ``quantities`` maps an item entity_id to the quantity coming back. Omit it, or give
    the item's whole quantity, to take the whole lot back. A smaller quantity is a partial
    return: that much is split off and comes back into stock, and the remainder stays out
    with the customer, so goods still in their hands are never written off as returned.

    ``weights`` / ``pieces`` carry the returned lot's measures for parcels tracked by a
    measure the quantity does not imply (a piece-sold parcel that also carries a weight).
    The measure of a part-returned parcel cannot be inferred, so it must be stated.
    """
    quantities: dict[str, float] | None = None
    weights: dict[str, float] | None = None
    pieces: dict[str, int] | None = None


class ReserveLinesRequest(FulfillLinesRequest):
    """Set selected lines to a ledger-neutral stock status.

    ``new_status`` is the target: ``reserved`` marks lines held for this document (stamps it as
    owner); ``available`` releases lines this document reserved back to the pool. Neither draws
    stock nor posts COGS - that is Set-as-shipped (fulfill-lines). Inherits the strip-empty /
    de-duplicate validator from FulfillLinesRequest.
    """
    new_status: Literal["reserved", "available"]


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


async def _get_doc(session: AsyncSession, company_id, entity_id: str, *, for_update: bool = False) -> Projection:
    # for_update takes the doc row under SELECT ... FOR UPDATE so the lifecycle
    # writers that share it (close, fulfill, send) are mutually exclusive: the
    # loser blocks until the winner commits, then populate_existing forces a fresh
    # read of the just-committed state to validate against. Mirrors
    # _get_list_for_update's row-lock idiom.
    if for_update:
        row = await session.get(Projection, {"company_id": company_id, "entity_id": entity_id},
                                with_for_update=True, populate_existing=True)
    else:
        row = await session.get(Projection, {"company_id": company_id, "entity_id": entity_id})
    if row is None or row.entity_type != "doc":
        raise HTTPException(status_code=404, detail="Document not found")
    return row


async def _get_docs_for_update(session: AsyncSession, company_id, entity_ids) -> dict[str, Projection]:
    """Lock a set of doc rows FOR UPDATE in one deterministic (entity_id-sorted) batch,
    the multi-doc analogue of _get_doc(for_update=True). A handler that reads several
    docs (create_shipment) must take them all under the lock in sorted order so it can
    never both pass a status check against a stale read and commit after a concurrent
    lifecycle writer changes one - and so two such handlers sharing docs acquire them in
    the same order (no deadlock). populate_existing overwrites any stale identity-map
    copy. Doc rows are locked before any item rows a caller goes on to lock, the global
    doc-before-item ordering. Missing or non-doc ids are the caller's to reject."""
    want = sorted({e for e in entity_ids if e})
    if not want:
        return {}
    rows = (await session.execute(
        select(Projection).where(
            Projection.company_id == company_id,
            Projection.entity_id.in_(want),
        ).order_by(Projection.entity_id).with_for_update().execution_options(populate_existing=True)
    )).scalars().all()
    return {r.entity_id: r for r in rows if r.entity_type == "doc"}


def _reject_if_closed(state: dict, action: str) -> None:
    """Guard a mutation against a closed memo. A closed memo is settled paperwork:
    payment reversals (refund / void / delete) recompute its status through their
    reducers, so applying one to a closed memo silently un-closes it. The user must
    Reopen it first, which is the way back the message names. Called under the doc-row
    lock so the status read here is the committed one, not a stale pre-lock value."""
    if state.get("status") == "closed":
        raise HTTPException(status_code=409, detail=f"Reopen this closed memo before you {action}.")


def _line_item_brief(line_items: list[dict], eids) -> list[dict]:
    """Brief [{item_id, sku, quantity}] for the given line entity_ids, sourced from the doc
    lines, so fulfillment/reversal events name the items (sku x qty) for the activity feed."""
    by_id: dict[str, dict] = {}
    for li in line_items:
        lid = li.get("entity_id") or li.get("item_id")
        if lid:
            by_id[lid] = li
    out: list[dict] = []
    for e in eids:
        li = by_id.get(e, {})
        out.append({"item_id": e, "sku": line_label(li), "quantity": li.get("quantity")})
    return out


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


async def _catalog_unit_price(session: AsyncSession, company_id, line: dict, price_config) -> float:
    """The base-list catalog price for a document line's item, or 0.0 if unknown.

    Resolved server side the same way a scanned line is priced (flatten then
    resolve against the base price list), so the price a line "should" carry is
    computed identically to how it was stamped when added.
    """
    from celerp_inventory.routes import flatten_item

    item_id = line.get("item_id") or line.get("entity_id")
    proj = None
    if item_id:
        proj = await session.get(Projection, {"company_id": company_id, "entity_id": item_id})
    if proj is None and line.get("sku"):
        proj = (
            await session.execute(
                select(Projection).where(
                    Projection.company_id == company_id,
                    Projection.entity_type == "item",
                    Projection.state["sku"].astext == line["sku"],
                )
            )
        ).scalars().first()
    if proj is None or not proj.state:
        return 0.0
    _lists, base_name, _currency = price_config
    return resolve_price(flatten_item(proj.state, proj.entity_id, price_config=price_config), base_name)


async def _assert_doc_price_permission(
    session: AsyncSession,
    company_id,
    settings: dict,
    role: str,
    incoming_lines: list[dict],
    stored_by_idx: dict[int, dict] | None,
) -> None:
    """Reject a sales-document price override when the caller lacks set_sales_doc_prices.

    A line's unit_price is an override when it differs from its reference price: the
    stored line at the same index when editing an existing document, otherwise the
    item's catalog price. A line with no catalog reference (no item, or an unknown
    one) treats any non-zero unit_price as an override. Lines that leave the price at
    its reference save regardless of the permission, so quantity-only edits are never
    blocked. Settings are read once per request, so a stale page is still denied at
    save time.
    """
    if role_has_permission(settings, role, "set_sales_doc_prices"):
        return
    price_config = None
    for idx, line in enumerate(incoming_lines):
        incoming = coerce_price(line.get("unit_price"))
        if incoming is None:
            continue
        stored = (stored_by_idx or {}).get(idx)
        if stored is not None:
            reference = coerce_price(stored.get("unit_price")) or 0.0
        else:
            if price_config is None:
                price_config = await get_price_config(session, company_id)
            reference = await _catalog_unit_price(session, company_id, line, price_config)
        if abs(incoming - reference) > 1e-6:
            assert_role_permission(settings, role, "set_sales_doc_prices")


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
        # Comma is an OR operand (matches inventory search + the search box's Enter-appends-comma
        # behaviour). Split on commas so "2816," / "2816, 2817" match, instead of LIKE-ing the
        # whole string (which fails on the trailing comma the search box inserts).
        terms = [t.strip().lower() for t in q.split(",") if t.strip()]
        if terms:
            _fields = ("doc_number", "contact_name", "contact_id", "ref")
            term_clauses = [
                _sa.or_(*[
                    _sa.func.lower(Projection.state[f].as_string()).like(f"%{term}%")
                    for f in _fields
                ])
                for term in terms
            ]
            base_where.append(_sa.or_(*term_clauses))

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
            out = [x for x in out if x.get("status") not in ("draft", "void", "closed") and x.get("fulfillment_status") != "fulfilled"]
        if not_restocked:
            out = [x for x in out if x.get("status") not in ("draft", "void") and not (x.get("return_received_items") or [])]
        if not_stocked:
            out = [x for x in out if x.get("status") not in ("draft", "void") and not (x.get("received_items") or [])]
        if converted_to_type:
            out = [x for x in out if x.get("converted_to_type") == converted_to_type]
        # Tiebreak on the unique id so equal-date rows have a deterministic order (same
        # reason as the SQL path: otherwise OFFSET pagination can skip/duplicate a row).
        out.sort(key=lambda x: (x.get("issue_date") or x.get("created_at") or x.get("date") or "", x.get("id") or ""), reverse=True)
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
            # Unique tiebreaker so the sort is a TOTAL order. Without it, rows sharing an
            # issue_date come back in an arbitrary order that differs between the per-page
            # queries, so a row on a page boundary can be skipped (or duplicated) by OFFSET.
            Projection.entity_id.desc(),
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
async def patch_sequence(doc_type: str, payload: SequencePatch, company_id: str = Depends(get_current_company_id), _: None = require_permission("manage_module_settings"), session: AsyncSession = Depends(get_session)) -> dict:
    company = await session.get(Company, company_id)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    try:
        result = update_sequence(company, doc_type, prefix=payload.prefix, pattern=payload.pattern, next_num=payload.next)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    await session.commit()
    return result


async def _memo_allocation_items(session: AsyncSession, company_id, entity_id: str) -> list[Projection]:
    """Every item projection this memo currently owns as its status document.

    A memo line whose quantity exceeds its bound lot draws cross-lot siblings from
    other lots of the same SKU; fulfill stamps each drawn lot status_doc_id==this memo
    but the sibling never appears in the memo's line_items. This is the true, complete
    allocation set (bound lines plus cross-lot siblings), read from the durable stamp so
    close-eligibility and per-line labels both see the sibling a line_items-only scan
    misses."""
    rows = (
        await session.execute(
            select(Projection).where(
                Projection.company_id == company_id,
                Projection.entity_type == "item",
                Projection.state["status_doc_id"].as_string() == entity_id,
            )
        )
    ).scalars().all()
    return list(rows)


async def _derive_shipped_labels(session: AsyncSession, company_id, entity_id: str, line_items: list) -> dict:
    """Map each of this memo's line-item entity ids to a shipped-state label,
    folded last-event-wins from the append-only ledger plus the item's live status.

    "Returned" - the item's latest fulfillment event on this memo is a reversal.
    "On Memo" - shipped and not since reversed, item still out at the customer
    (item status memo_out).
    "Sold" - shipped and not since reversed, item since invoiced/finalized (item
    status sold).
    "Not shipped" - no fulfillment event for this memo (item never left stock).

    The event log is the source of truth for the ship/reverse timeline; the item
    projection's live status distinguishes On Memo from Sold. Nothing is persisted.
    """
    from celerp.models.ledger import LedgerEntry

    item_eids = {li.get("entity_id") or li.get("item_id") for li in line_items or []}
    item_eids.discard(None)
    item_eids.discard("")
    if not item_eids:
        return {}
    rows = (
        await session.execute(
            select(LedgerEntry.id, LedgerEntry.entity_id, LedgerEntry.event_type, LedgerEntry.data)
            .where(
                LedgerEntry.company_id == company_id,
                LedgerEntry.entity_id.in_(item_eids),
                LedgerEntry.event_type.in_(("item.fulfilled", "item.fulfillment_reversed")),
            )
            .order_by(LedgerEntry.id.desc())
        )
    ).all()
    last_event: dict = {}
    # Ledger id of the applicable item.fulfilled event for this memo, per eid: a "sold"
    # event only promotes a line to "Sold" when it FOLLOWS this memo's fulfillment (below).
    fulfilled_id: dict = {}
    for ledger_id, item_eid, event_type, data in rows:
        if item_eid in last_event:
            continue  # id-desc order means the first row seen is the latest
        if (data or {}).get("source_doc_id") != entity_id:
            continue
        last_event[item_eid] = event_type
        if event_type == "item.fulfilled":
            fulfilled_id[item_eid] = ledger_id
    # Sold is read from the durable item.status.set(sold) event in history, not the
    # item's live projection status: an item sold then archived reads "archived" live,
    # and reading the live status would revert a genuinely-sold line to "On Memo". The
    # ledger event that promoted it to sold is never erased by a later archive.
    fulfilled_eids = [eid for eid, ev in last_event.items() if ev != "item.fulfillment_reversed"]
    sold_eids: set[str] = set()
    if fulfilled_eids:
        sold_rows = (
            await session.execute(
                select(LedgerEntry.id, LedgerEntry.entity_id, LedgerEntry.data)
                .where(
                    LedgerEntry.company_id == company_id,
                    LedgerEntry.entity_id.in_(fulfilled_eids),
                    LedgerEntry.event_type == "item.status.set",
                )
            )
        ).all()
        for _sold_id, _eid, _data in sold_rows:
            # Only a sale AFTER this memo's own fulfillment marks the line Sold. A sold
            # event that predates it belongs to an earlier cycle (sold, returned to stock,
            # then re-consigned on this memo) and must read "On Memo", not "Sold".
            if (_data or {}).get("new_status") == "sold" and _sold_id > fulfilled_id.get(_eid, 0):
                sold_eids.add(_eid)

    # Per-SKU rollup over the memo's full allocation set (cross-lot siblings included):
    # a line whose bound lot was returned still reads "On Memo" when a sibling of the
    # same SKU that fulfill drew from another lot is still out at the customer. Without
    # this, a returned bound lot reads "Returned" while the customer still holds a sibling.
    skus_still_out: set[str] = set()
    for item_proj in await _memo_allocation_items(session, company_id, entity_id):
        if item_proj.state.get("status") == "memo_out":
            _sku = item_proj.state.get("sku")
            if _sku:
                skus_still_out.add(_sku)
    sku_by_eid = {(li.get("entity_id") or li.get("item_id")): li.get("sku")
                  for li in line_items or []}

    def _label(eid: str) -> str:
        if sku_by_eid.get(eid) in skus_still_out:
            return "On Memo"
        ev = last_event.get(eid)
        if ev is None:
            return "Not shipped"
        if ev == "item.fulfillment_reversed":
            return "Returned"
        return "Sold" if eid in sold_eids else "On Memo"

    return {eid: _label(eid) for eid in item_eids}


@router.get("/{entity_id}")
async def get_doc(entity_id: str, company_id: str = Depends(get_current_company_id), session: AsyncSession = Depends(get_session)) -> dict:
    row = await _get_doc(session, company_id, entity_id)
    doc = row.state | {"id": row.entity_id}
    if doc.get("doc_type") == "memo":
        try:
            labels = await _derive_shipped_labels(session, company_id, entity_id, doc.get("line_items") or [])
            for li in doc.get("line_items") or []:
                eid = li.get("entity_id") or li.get("item_id")
                if eid in labels:
                    li["shipped_label"] = labels[eid]
        except Exception:
            # Degrade to the raw status badge; never 500 the whole doc payload,
            # never fabricate a label.
            for li in doc.get("line_items") or []:
                li.pop("shipped_label", None)
    return doc


@router.get("/{entity_id}/pdf")
async def get_doc_pdf(
    entity_id: str,
    company_id: str = Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
):
    """Return a PDF of the document with 'Powered by Celerp' footer branding."""
    from fastapi.responses import Response as _Resp
    from celerp.output.pdf import generate_document_pdf

    row = await _get_doc(session, company_id, entity_id)
    doc = row.state | {"entity_id": row.entity_id}

    company_row = await session.get(Company, company_id)
    company = ({"name": company_row.name} | (company_row.settings or {}) if company_row else {}) | {"id": company_id}

    # When the company shows barcodes on lines, backfill lines saved before
    # barcode stamping from their catalog items (mirror of the share view).
    from celerp.services.line_measures import LINE_IDENTIFIER_MODES, identifier_backfill
    if company.get("line_item_identifier") in LINE_IDENTIFIER_MODES and company["line_item_identifier"] != "sku":
        for li in doc.get("line_items") or []:
            eid = li.get("entity_id") or li.get("item_id")
            if li.get("barcode") or not eid:
                continue
            irow = await session.get(Projection, (company_id, eid))
            if irow is not None:
                identifier_backfill(li, irow.state or {})

    # Footer import link only while the share link is live, so saved PDFs
    # never carry a URL that 404s.
    from celerp_docs.routes_share import _find_share_row, _share_active, _share_url
    from celerp.output.doc_print import IMPORTABLE_DOC_TYPES
    import_url = None
    if doc.get("doc_type") in IMPORTABLE_DOC_TYPES:
        share_row = await _find_share_row(session, company_id, entity_id)
        if share_row is not None and _share_active(share_row):
            import_url = _share_url(share_row.token)

    # reportlab layout is CPU-bound Python; a worker thread keeps a large
    # document's render from stalling every concurrent request on the loop.
    pdf_bytes = await asyncio.to_thread(generate_document_pdf, doc, company, import_url=import_url)
    doc_ref = doc.get("ref_id") or doc.get("doc_number") or entity_id
    filename = f"{doc_ref}.pdf".replace("/", "-").replace(" ", "_")
    return _Resp(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


async def _scan_reserved_lines(
    session: AsyncSession, company_id, entity_id: str | None, eids,
) -> tuple[list[str], list[dict]]:
    """Partition the reserved items among ``eids``: entity_ids reserved by THIS
    document (``entity_id``) vs conflict records for items reserved elsewhere,
    each naming the owning document."""
    own: list[str] = []
    conflicts: list[dict] = []
    for eid in sorted({e for e in eids if e}):
        proj = await session.get(Projection, {"company_id": company_id, "entity_id": eid})
        if not proj:
            continue
        st = proj.state
        if st.get("status") != "reserved":
            continue
        if st.get("status_doc_id") == entity_id:
            own.append(eid)
        else:
            owner = st.get("status_doc_number") or st.get("status_doc_id") or "another document"
            conflicts.append({
                "entity_id": eid,
                "sku": st.get("sku") or eid,
                "doc_id": st.get("status_doc_id"),
                "doc_number": st.get("status_doc_number"),
                "message": f"{st.get('sku') or eid}: reserved on {owner} - release it there first",
            })
    return own, conflicts


async def _assert_no_foreign_reserved(
    session: AsyncSession, company_id, doc_type: str, entity_id: str | None, eids,
) -> None:
    """Reject putting items reserved by ANOTHER document onto an invoice or memo,
    naming the owning document. Only invoices and memos claim stock, so quotations
    and shipping lists keep listing reserved items freely. Function-level validation:
    the picker never hides these items, the add is what fails with the reason."""
    if doc_type not in RESERVABLE_DOC_STATUSES:
        return
    _, conflicts = await _scan_reserved_lines(session, company_id, entity_id, eids)
    if conflicts:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "; ".join(c["message"] for c in conflicts),
                "conflicts": conflicts,
            },
        )


async def _assert_no_draft_items(session: AsyncSession, company_id, eids) -> None:
    """A draft item is not stock yet: it cannot be put on any document or list.
    Applies to EVERY doc type (a quotation listing a draft would quote phantom
    stock). Function-level validation: an id can still arrive via scan, import,
    or a stale form, so the add is what fails, with the reason."""
    conflicts: list[dict] = []
    for eid in sorted({e for e in eids if e}):
        proj = await session.get(Projection, {"company_id": company_id, "entity_id": eid})
        if proj is None:
            continue
        st = proj.state or {}
        if str(st.get("status") or "").lower() == "draft":
            sku = st.get("sku") or eid
            conflicts.append({
                "entity_id": eid,
                "sku": sku,
                "message": f"{sku}: item is a draft - make it available first",
            })
    if conflicts:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "; ".join(c["message"] for c in conflicts),
                "conflicts": conflicts,
            },
        )


@router.post("")
async def create_doc(
    payload: DocCreatePayload,
    company_id: str = Depends(get_current_company_id),
    _: None = require_permission("edit_documents"),
    role: str = Depends(get_current_role),
    settings: dict = Depends(get_current_company_settings),
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

        # A brand-new doc cannot own a reservation yet, so any reserved line is foreign.
        await _assert_no_foreign_reserved(
            session, company_id, payload.doc_type, None,
            (li.entity_id or li.item_id for li in payload.line_items),
        )
        await _assert_no_draft_items(
            session, company_id,
            (li.entity_id or li.item_id for li in payload.line_items),
        )

        # Price-override gate: a new line whose unit_price deviates from the item's
        # catalog price is a price override, rejected when the caller lacks
        # set_sales_doc_prices. This closes the create path so the gate cannot be
        # bypassed by making a new draft with overridden prices.
        await _assert_doc_price_permission(
            session, company_id, settings, role,
            [li.model_dump() for li in payload.line_items], None,
        )

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
        # Persist the EFFECTIVE tax (doc-level + line-level) into `tax`. Previously this was computed
        # for `total` but discarded, leaving `tax`=0 for line-level taxes — so the finalize JE booked
        # the tax-inclusive total entirely to revenue (4100) and recorded zero output VAT (2120),
        # overstating revenue and understating the VAT liability. total = subtotal + tax + shipping.
        data["tax"] = to_stored_float(round_money(effective_tax_d, currency))

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
async def patch_doc(entity_id: str, payload: DocPatch, company_id: str = Depends(get_current_company_id), _: None = require_permission("edit_documents"), role: str = Depends(get_current_role), settings: dict = Depends(get_current_company_settings), user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
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
    # Price-override gate: reject unit_price changes when the caller lacks
    # set_sales_doc_prices, comparing incoming lines against the stored lines by
    # index. Runs for drafts and finalized documents alike, before the draft branch.
    _incoming_lines = (payload.fields_changed.get("line_items") or {}).get("new")
    if isinstance(_incoming_lines, list):
        _stored_by_idx = {i: li for i, li in enumerate(row.state.get("line_items") or [])}
        await _assert_doc_price_permission(session, company_id, settings, role, _incoming_lines, _stored_by_idx)
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

        # Newly added lines must not be reserved by another document; lines already on
        # the doc (including ones this doc reserved) pass untouched.
        await _assert_no_foreign_reserved(
            session, company_id, row.state.get("doc_type") or "", entity_id,
            incoming_eids - existing_eids,
        )
        await _assert_no_draft_items(session, company_id, incoming_eids - existing_eids)

    # Money fields are stored at currency precision. The client computes subtotal/tax/total as
    # raw JS floats and legacy values may already carry IEEE-754 tails, so round both old and new
    # here - the single chokepoint for every doc edit - so the ledger and history never record
    # values like "346.50000000000006".
    _currency = row.state.get("currency")
    _MONEY_FIELDS = {"subtotal", "tax", "total", "discount_amount"}
    def _round_field(field: str, value, *, incoming: bool = False):
        if field in _MONEY_FIELDS and value is not None:
            return to_stored_float(round_money(value, _currency))
        if field == "conversion_rate" and incoming:
            # The inline rate field posts a form value, so an edit arrives as a
            # string: normalised here to the same number create stores, at the
            # same ceiling. An empty value clears the rate, which is the remedy
            # for one that should not be on the document at all.
            #
            # Only the incoming value is checked. A rate stored before this guard
            # existed can then still be corrected, rather than the bad value
            # blocking the very edit that fixes it.
            if value in (None, ""):
                return None
            try:
                return to_stored_float(checked_exchange_rate(value))
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=f"The conversion rate {exc}.") from exc
        return value
    # Keep only fields that actually changed (old != new). A re-select/blur that resets a
    # field to its current value must emit no event - otherwise it records an empty
    # `doc.updated` that renders as a ghost activity row (#155).
    effective = {}
    for k, change in payload.fields_changed.items():
        old = _round_field(k, change.get("old") if change.get("old") is not None else row.state.get(k))
        new = _round_field(k, change.get("new"), incoming=True)
        if old != new:
            effective[k] = {"old": old, "new": new}
    if not effective:
        return {"event_id": None}
    entry = await emit_event(
        session, company_id=company_id, entity_id=entity_id, entity_type="doc", event_type="doc.updated",
        data={"fields_changed": effective, "idempotency_key": payload.idempotency_key},
        actor_id=user.id, location_id=None, source="api",
        idempotency_key=payload.idempotency_key or str(uuid.uuid4()), metadata_={},
    )
    await session.commit()
    return {"event_id": entry.id}


async def _sender_reply_to(session: AsyncSession, company_id, user) -> str:
    """Reply-To for outgoing document emails: the company's business address
    (self-contact) so recipients can actually reply, falling back to company
    settings and then the sending user. Mail is sent from noreply@, so without
    this a customer's reply would bounce."""
    company_row = await session.get(Company, company_id)
    cfg = (company_row.settings or {}) if company_row else {}
    self_id = cfg.get("self_contact_id")
    if self_id:
        crow = await session.get(Projection, {"company_id": company_id, "entity_id": self_id})
        email = (crow.state or {}).get("email") if crow else None
        if email:
            return email
    return cfg.get("email") or getattr(user, "email", "") or ""


async def _payments_tip_suffix(session, company_id) -> str:
    """One-time payments pitch that piggybacks on the first delivery receipt
    for a payable doc (no separate nag notification). Empty string once shown,
    once payments are on, or when the instance cannot take payments anyway."""
    from celerp.models.company import Company
    from celerp.services.payments import payments_enabled
    if payments_enabled():
        return ""
    company = await session.get(Company, company_id)
    if company is None or (company.settings or {}).get("pay_tip_shown"):
        return ""
    settings = dict(company.settings or {})
    settings["pay_tip_shown"] = True
    company.settings = settings
    session.add(company)
    return (" Tip: connect a Stripe account under Web Access, Payments and "
            "emailed invoices include a Pay button so customers can pay you by card.")


def _email_with_receipt(company_id, doc_label: str, sent_to: str, action_url: str,
                        payable: bool = False, **send_kwargs) -> None:
    """Send in the background, then drop a bell notification with the outcome.

    The route's session is gone by the time the send resolves, so the receipt
    is written in its own session. Delivery is verified: send_email only
    reports ok when a transport actually accepted the message. For payable
    docs, the very first receipt carries the one-time online-payments tip."""
    import uuid as _uuid_mod

    async def _run() -> None:
        from celerp.db import SessionLocal
        from celerp.notifications import service as notif_service
        from celerp.services.email import send_email
        ok, detail = await send_email(**send_kwargs)
        cid = company_id if isinstance(company_id, _uuid_mod.UUID) else _uuid_mod.UUID(str(company_id))
        try:
            async with SessionLocal() as s:
                if ok:
                    tip = await _payments_tip_suffix(s, cid) if payable else ""
                    await notif_service.create(
                        s, cid, "email", f"Email delivered to {sent_to}",
                        f"{doc_label} was emailed to {sent_to}.{tip}",
                        action_url=action_url, priority="low")
                else:
                    await notif_service.create(
                        s, cid, "email", f"Email to {sent_to} failed",
                        f"{doc_label} could not be delivered: {detail}",
                        action_url=action_url, priority="high")
                await s.commit()
        except Exception:
            import logging
            logging.getLogger(__name__).exception("Email receipt notification failed for %s", sent_to)

    asyncio.create_task(_run())


@router.post("/{entity_id}/send")
async def send_doc(entity_id: str, payload: DocSendBody, company_id: str = Depends(get_current_company_id), _: None = require_permission("edit_documents"), user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    # Lock the doc row so the closed-check below is race-safe against a concurrent
    # close: without it, send and close both read a non-closed memo unlocked and
    # both commit, letting doc.sent silently un-close the memo the close settled.
    row = await _get_doc(session, company_id, entity_id, for_update=True)
    if row.state.get("status") == "void":
        raise HTTPException(status_code=409, detail="Cannot send void document")
    if row.state.get("status") == "closed":
        raise HTTPException(status_code=409, detail="Cannot send a closed memo; reopen it first.")
    if not (row.state.get("line_items") or []):
        raise HTTPException(status_code=422, detail="Add at least one line item before sending this document.")
    from celerp_docs.doc_constants import NO_SEND_DOC_TYPES
    doc_type = row.state.get("doc_type", "")
    if doc_type in NO_SEND_DOC_TYPES:
        raise HTTPException(status_code=409, detail=f"Document type '{doc_type}' cannot be sent")
    entry = await emit_event(
        session, company_id=company_id, entity_id=entity_id, entity_type="doc", event_type="doc.sent",
        data=payload.model_dump(exclude_none=True), actor_id=user.id, location_id=None, source="api",
        idempotency_key=payload.idempotency_key or str(uuid.uuid4()), metadata_={},
    )

    sent_to = payload.sent_to
    view_url = pay_url = None
    amount_due = float(row.state.get("amount_outstanding", row.state.get("total", 0)) or 0)
    if sent_to:
        # Emailing a document always shares it (an email with no viewable
        # document is pointless); send_view_url returns None only when no link
        # can be minted (a self-hosted instance with no relay).
        from celerp_docs.routes_share import send_pay_url, send_view_url
        view_url = await send_view_url(session, company_id, entity_id)
        # Pay button only while something is owed: it charges the remaining
        # balance (net of applied payments/credits), never the face total.
        if doc_type in ("invoice", "proforma") and amount_due > 0:
            pay_url = await send_pay_url(session, company_id, entity_id)
    await session.commit()

    # Fire-and-forget email notification if a recipient was supplied
    if sent_to:
        from celerp_docs.doc_email import compose_doc_email
        doc_number = row.state.get("ref_id") or entity_id.split(":")[-1][:8].upper()
        doc_type = row.state.get("doc_type", "document").replace("_", " ").title()
        contact_name = row.state.get("contact_name") or "there"
        company_row = await session.get(Company, company_id)
        sender_name = company_row.name if company_row else "Your supplier"
        subject = (payload.subject or "").strip() or f"{doc_type} #{doc_number} from {sender_name}"
        body_html, body_text = compose_doc_email(
            doc_type_label=doc_type, doc_number=doc_number, sender_name=sender_name,
            contact_name=contact_name, total=row.state.get("total", 0),
            currency=row.state.get("currency", "USD"),
            message=payload.message, view_url=view_url, pay_url=pay_url,
            amount_due=amount_due,
        )
        reply_to = await _sender_reply_to(session, company_id, user)
        _email_with_receipt(
            company_id, f"{doc_type} #{doc_number}", sent_to, f"/docs/{entity_id}",
            payable=row.state.get("doc_type") in ("invoice", "proforma"),
            to=sent_to, subject=subject, body_html=body_html, body_text=body_text,
            reply_to=reply_to, from_name=sender_name, cc=payload.cc or "", bcc=payload.bcc or "",
        )

    return {"event_id": entry.id}


@router.post("/{entity_id}/finalize")
async def finalize_doc(entity_id: str, company_id: str = Depends(get_current_company_id), _: None = require_permission("finalize_documents"), user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    row = await _get_doc(session, company_id, entity_id, for_update=True)
    # A closed memo is terminal, settled paperwork; finalize maps closed->final in the
    # reducer, silently stripping the terminal status and its close metadata. Refuse under
    # the row lock, same as the other post-close mutations; the user reopens first.
    _reject_if_closed(row.state, "finalize it")
    if row.state.get("status") == "void":
        raise HTTPException(status_code=409, detail="Cannot finalize void document")
    if not (row.state.get("line_items") or []):
        raise HTTPException(status_code=422, detail="Add at least one line item before finalizing this document.")

    # Snapshot scalar values early — avoids ORM lazy-load issues after multiple flush() calls.
    _initial_doc_state = dict(row.state)
    _user_id = user.id  # Capture before flushes may expire session objects
    doc_type = _initial_doc_state.get("doc_type", "")
    finalize_data: dict = {}
    event_type = "doc.finalized"

    # Load company once for base currency (used for validation and JE conversion).
    _company = await session.get(Company, company_id)
    _base_currency = (_company.settings.get("currency", "USD") if _company else "USD")

    # The stored currency and the stored rate have to agree before anything posts.
    # Checked here rather than on entry: this is the last point before the journal
    # entry is minted, both values are final, and the rate is immutable afterwards.
    _doc_currency = _initial_doc_state.get("currency", _base_currency)
    _stored_rate = _initial_doc_state.get("conversion_rate")
    _rate = None
    if _stored_rate not in (None, ""):
        try:
            _rate = checked_exchange_rate(_stored_rate)
        except ValueError as exc:
            raise HTTPException(
                status_code=422,
                detail=f"This document's conversion rate {exc}. Correct it before finalizing.",
            ) from exc
    if _doc_currency != _base_currency and _rate is None:
        raise HTTPException(
            status_code=422,
            detail="A conversion rate is required for foreign-currency documents. Set the exchange rate before finalizing.",
        )
    # A document in the company's own currency converts at 1 by definition, so any
    # other rate silently restates it: 100 USD posted as 3500 in USD books. The
    # manual journal door refuses the same mismatch on a journal line.
    if _doc_currency == _base_currency and _rate is not None and _rate != 1:
        raise HTTPException(
            status_code=422,
            detail=(
                f"{_doc_currency} is this company's own currency, so its conversion rate is 1, "
                f"not {_stored_rate}. Clear the rate before finalizing."
            ),
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
        # span_lots: the interactive finalize recognizes a line exceeding its bound
        # lot at the sibling lots that will actually be drawn; import/repair paths
        # stay on exact bound-lot pricing.
        await auto_je.create_for_doc_finalized(session, company_id=company_id, user_id=_user_id, doc_id=entity_id, doc=_initial_doc_state, base_currency=_base_currency, span_lots=True)
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
                    data={"new_status": "sold", "source_doc_id": entity_id,
                          "doc_number": _initial_doc_state.get("doc_number") or _initial_doc_state.get("ref_id") or ""},
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
async def void_doc(entity_id: str, payload: DocVoidBody, company_id: str = Depends(get_current_company_id), _: None = require_permission("finalize_documents"), user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    row = await _get_doc(session, company_id, entity_id, for_update=True)
    _reject_if_closed(row.state, "void it")
    current_status = row.state.get("status")
    if current_status in ("paid", "partial"):
        raise HTTPException(status_code=409, detail="Cannot void a document with payments; void the payments first")
    # Goods must be back before the paper can go void, or the stock records point
    # at a dead document with no UI path to bring the items home (the fulfillment
    # toolbar only renders on live statuses). Mirrors the revert-to-draft guards.
    if row.state.get("fulfillment_status") in ("fulfilled", "partial"):
        raise HTTPException(
            status_code=409,
            detail="Cannot void a document with fulfilled items; revert fulfillment (receive the goods back) first")
    if row.state.get("received_items"):
        raise HTTPException(
            status_code=409,
            detail="Cannot void a document with received items; return the goods first")

    event_data = payload.model_dump(exclude_none=True)
    event_data["pre_void_status"] = current_status
    # Reverse the recognition JEs before the doc goes void, symmetric with unvoid's
    # batch restore: leaving them posted would double-count once unvoid re-posts.
    # Voiding first also surfaces a locked-period refusal before anything else
    # mutates, mirroring the revert-to-draft ordering.
    await auto_je.void_for_doc_voided(session, company_id=company_id, user_id=user.id, doc_id=entity_id)
    entry = await emit_event(
        session, company_id=company_id, entity_id=entity_id, entity_type="doc", event_type="doc.voided",
        data=event_data, actor_id=user.id, location_id=None, source="api",
        idempotency_key=payload.idempotency_key or str(uuid.uuid4()), metadata_={},
    )
    await session.commit()
    return {"event_id": entry.id}


@router.post("/{entity_id}/close")
async def close_doc(entity_id: str, payload: DocCloseBody, company_id: str = Depends(get_current_company_id), _: None = require_permission("finalize_documents"), user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    """Terminal Close for a resolved memo: the paper leaves the "partially
    fulfilled" limbo once every stone has been sold, kept, or returned to stock.
    Reversible via /reopen. Memo-only, live-status-only, and refused with a
    product count while any line is still out at the customer (memo_out)."""
    row = await _get_doc(session, company_id, entity_id, for_update=True)
    state = row.state
    if state.get("doc_type") != "memo":
        raise HTTPException(status_code=422, detail="Only memos can be closed")
    # Denylist, not an issued-only allowlist: a memo can reach "partial" from a
    # deposit payment and stays resolvable, so only draft/void/converted/already-
    # closed are excluded.
    if state.get("status") in ("draft", "void", "converted", "closed"):
        raise HTTPException(status_code=409, detail="Only a live, issued memo can be closed")
    # Resolution is per-item memo_out, NOT fulfillment_status (a "fulfilled" memo
    # is all-memo_out = all still at the customer = maximally unresolved). Mirror
    # convert's per-line read.
    # Resolution reads the memo's true allocation set (every item stamped
    # status_doc_id==this memo), not just line_items: a cross-lot sibling fulfill drew
    # from another lot of the same SKU carries the stamp but never appears in line_items,
    # and leaving it out silently closes a memo with stock still out at the customer.
    pending = 0
    for item_proj in await _memo_allocation_items(session, company_id, entity_id):
        item_status = item_proj.state.get("status")
        # Still out at the customer, or held reserved-from THIS memo: both are
        # unresolved allocations of this paper and must be settled before Close.
        if item_status == "memo_out" or item_status == "reserved":
            pending += 1
    if pending:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot close: {pending} product(s) still awaiting resolution. Sell or return them first.",
        )
    # A fully paid memo has already settled: bulk payment brought it to "paid" and there is
    # nothing left to resolve by closing. Read status from the locked fresh row above, so a
    # payment that committed after any earlier unlocked read is seen here; refuse rather than
    # letting Close write a doc.closed onto settled paper.
    if state.get("status") == "paid":
        raise HTTPException(
            status_code=409,
            detail="This memo is fully paid and already settled; it cannot be closed.",
        )
    event_data = payload.model_dump(exclude_none=True)
    event_data["pre_close_status"] = state.get("status")
    entry = await emit_event(
        session, company_id=company_id, entity_id=entity_id, entity_type="doc", event_type="doc.closed",
        data=event_data, actor_id=user.id, location_id=None, source="api",
        idempotency_key=payload.idempotency_key or str(uuid.uuid4()), metadata_={},
    )
    await session.commit()
    return {"event_id": entry.id}


@router.post("/{entity_id}/reopen")
async def reopen_doc(entity_id: str, payload: DocReopenBody, company_id: str = Depends(get_current_company_id), _: None = require_permission("finalize_documents"), user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    """Undo a Close: restore the memo to the status it held before closing."""
    row = await _get_doc(session, company_id, entity_id, for_update=True)
    if row.state.get("status") != "closed":
        raise HTTPException(status_code=409, detail="Only a closed memo can be reopened")
    restored = row.state.get("pre_close_status") or "final"
    event_data = payload.model_dump(exclude_none=True)
    event_data["restored_status"] = restored
    entry = await emit_event(
        session, company_id=company_id, entity_id=entity_id, entity_type="doc", event_type="doc.reopened",
        data=event_data, actor_id=user.id, location_id=None, source="api",
        idempotency_key=payload.idempotency_key or str(uuid.uuid4()), metadata_={},
    )
    await session.commit()
    return {"event_id": entry.id}


@router.post("/{entity_id}/revert-to-draft")
async def revert_doc_to_draft(entity_id: str, payload: DocRevertBody, company_id: str = Depends(get_current_company_id), _: None = require_permission("finalize_documents"), user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    row = await _get_doc(session, company_id, entity_id, for_update=True)
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
    # The revert restates the document's own period, so the lock is evaluated
    # against that date even when no posted finalize JE exists to void.
    _doc_date = state.get("finalized_at") or state.get("issue_date")
    if _doc_date:
        event_data["ts"] = str(_doc_date)[:10]

    # PO->bill revert: restore doc_type and ref_id
    extra_data: dict = {}
    doc_type = state.get("doc_type")
    if doc_type == "bill" and state.get("source_po_ref"):
        extra_data["doc_type"] = "purchase_order"
        extra_data["ref_id"] = state["source_po_ref"]

    # Void the finalize JE first: it raises when the entry's date sits in a
    # locked period, and nothing may mutate before that check passes.
    # Pass current revert_count (before this revert increments it).
    current_revert_count = int(state.get("revert_count", 0))
    await auto_je.void_for_doc_finalized(session, company_id=company_id, user_id=user.id, doc_id=entity_id, revert_count=current_revert_count)
    entry = await emit_event(
        session, company_id=company_id, entity_id=entity_id, entity_type="doc",
        event_type="doc.reverted_to_draft",
        data={**event_data, **extra_data},
        actor_id=user.id, location_id=None, source="api",
        idempotency_key=payload.idempotency_key or str(uuid.uuid4()), metadata_={},
    )
    await session.commit()
    return {"event_id": entry.id}


class DocRenumberBody(BaseModel):
    ref_id: str


@router.post("/{entity_id}/renumber")
async def renumber_doc(
    entity_id: str,
    payload: DocRenumberBody,
    company_id: str = Depends(get_current_company_id),
    _: None = require_permission("edit_documents"),
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
async def unvoid_doc(entity_id: str, payload: DocUnvoidBody, company_id: str = Depends(get_current_company_id), _: None = require_permission("finalize_documents"), user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    row = await _get_doc(session, company_id, entity_id, for_update=True)
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
    # Restore the JEs the void reversed (idempotent - uses doc-scoped keys)
    await auto_je.create_for_doc_unvoided(session, company_id=company_id, user_id=user.id, doc_id=entity_id)

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


async def _posted_journal_entries(session: AsyncSession, company_id: str, entity_id: str) -> list[str]:
    """The journal entries posted for a document, void ones included.

    Void ones count. Reverting a finalized document to draft reverses its
    postings but keeps both the entry and its reversal, because the books keep
    their history; the document is then deletable by status while the journal
    still names it, and deleting it leaves entries pointing at a document nothing
    can resolve.

    Entries are found by the id every automatic posting is minted with,
    `je:auto:{doc_id}:{op}` (`celerp/services/auto_je.py`), the same prefix match
    payment recording uses below. `_` and `%` are escaped so a document id
    carrying either cannot widen the match. This finds entries posted FOR the
    document, which is not the same as every entry that mentions it: a credit
    note applied to an invoice posts under the invoice's id, and is unreachable
    here by design, because applying a credit note requires finalising it and a
    finalised document is refused a line earlier.
    """
    prefix = entity_id.replace("\\", "\\\\").replace("_", r"\_").replace("%", r"\%")
    rows = await session.execute(
        select(Projection.entity_id)
        .where(
            Projection.company_id == company_id,
            Projection.entity_type == "journal_entry",
            Projection.entity_id.like(f"je:auto:{prefix}:%", escape="\\"),
        )
        .order_by(Projection.entity_id)
    )
    return list(rows.scalars().all())


def _first_few(names: list[str], limit: int = 5) -> str:
    """The names, capped, so a refusal about fifty documents is still readable."""
    if len(names) <= limit:
        return ", ".join(names)
    return f"{', '.join(names[:limit])} and {len(names) - limit} more"


@router.delete("/bulk-draft")
async def bulk_delete_drafts(
    doc_ids: str,
    company_id: str = Depends(get_current_company_id),
    _: None = require_permission("delete_documents"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Delete multiple draft documents in one request. Non-draft docs are skipped (not an error).

    A document that has posted to the books refuses the whole batch and nothing is
    deleted. A successful bulk delete reloads the page, so a partly done one has
    nowhere left to say what it skipped, and the reader would be left believing
    every ticked document is gone.
    """
    ids = [x.strip() for x in doc_ids.split(",") if x.strip()]
    if not ids:
        raise HTTPException(status_code=422, detail="No document IDs specified")
    from celerp.models.ledger import LedgerEntry
    import sqlalchemy as _sa
    drafts = []
    for eid in ids:
        row = await session.get(Projection, {"company_id": company_id, "entity_id": eid})
        if row is None or row.state.get("status") != "draft":
            continue
        drafts.append(row)

    posted = [row.state.get("ref_id") or row.state.get("doc_number") or row.entity_id
              for row in drafts
              if await _posted_journal_entries(session, company_id, row.entity_id)]
    if posted:
        raise HTTPException(
            status_code=422,
            detail=f"Nothing was deleted. These documents have journal entries in the books, "
                   f"so deleting them would leave the entries unattributable: {_first_few(posted)}. "
                   f"Untick them and try again.",
        )

    deleted = []
    for row in drafts:
        eid = row.entity_id
        await session.execute(_sa.delete(Projection).where(Projection.company_id == company_id, Projection.entity_id == eid))
        await session.execute(_sa.delete(LedgerEntry).where(LedgerEntry.company_id == company_id, LedgerEntry.entity_id == eid))
        deleted.append(eid)
    await session.commit()
    return {"deleted": deleted, "count": len(deleted)}


@router.delete("/{entity_id}")
async def delete_doc(entity_id: str, company_id: str = Depends(get_current_company_id), _: None = require_permission("delete_documents"), user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    row = await _get_doc(session, company_id, entity_id)
    if row.state.get("status") != "draft":
        raise HTTPException(status_code=409, detail="Only draft documents can be deleted")
    entries = await _posted_journal_entries(session, company_id, entity_id)
    if entries:
        raise HTTPException(
            status_code=409,
            detail=f"This document has journal entries in the books, so deleting it would leave "
                   f"them unattributable: {_first_few(entries)}. It can stay void instead.",
        )
    from celerp.models.ledger import LedgerEntry
    import sqlalchemy as _sa
    await session.execute(_sa.delete(Projection).where(Projection.company_id == company_id, Projection.entity_id == entity_id))
    await session.execute(_sa.delete(LedgerEntry).where(LedgerEntry.company_id == company_id, LedgerEntry.entity_id == entity_id))
    await session.commit()
    return {"deleted": entity_id}


async def _alloc_payment_index(session, company_id, payments: list,
                               key_doc_id: str | None = None,
                               key_type: str | None = None) -> int:
    """Next payment index: past the list, past any index field already in use,
    and past any index a journal entry was ever minted with under key_type.

    A payment's index field is its identity (journal-entry ids and idempotency
    keys embed it). Deletions used to compact the list, so on docs compacted
    before tombstoning, len() alone can land on an index whose JE key already
    exists - the new payment's JE would silently dedupe and post nothing.
    """
    idx = max(len(payments),
              1 + max((int(p.get("index") or 0) for p in payments), default=-1))
    if key_doc_id and key_type:
        from celerp.models.ledger import LedgerEntry as _LE
        from celerp.services.je_keys import je_idempotency_key as _je_k
        while (await session.execute(select(_LE.id).where(
                _LE.company_id == company_id,
                _LE.idempotency_key == _je_k(key_doc_id, f"{key_type}:{idx}", "c"),
        ).limit(1))).first() is not None:
            idx += 1
    return idx


async def apply_doc_payment(session, company_id, entity_id: str, body: dict,
                            *, source: str, actor_id, idempotency_key: str,
                            clamp_overshoot: bool = False):
    """Record a payment against a doc: guard, emit doc.payment.received, post the cash
    JE, fire the payment lifecycle hook. Shared by the manual route and online payment
    so a Stripe payment lands identically to a hand-entered one. Commits per success and
    returns (event, applied_amount) - the applied amount is the value that actually
    landed under the row lock (post-clamp), which bulk uses to report and decrement.

    Takes the doc row under SELECT ... FOR UPDATE and validates against that fresh,
    committed read: the doc-row lock is the single serializer across every payment
    path and across connections, so two recorders on one doc are ordered at the row
    and cannot compute a duplicate or colliding payment_index."""
    row = await _get_doc(session, company_id, entity_id, for_update=True)
    doc_state = dict(row.state)
    if doc_state.get("doc_type") in NON_FINANCIAL_DOC_TYPES:
        raise HTTPException(status_code=409, detail="This document type carries no money and cannot take a payment")
    if doc_state.get("status") not in {"sent", "final", "partial", "paid", "received", "partially_received", "awaiting_payment"}:
        raise HTTPException(status_code=409, detail="Cannot record payment in current status")
    # Replay guard for referenced (online) payments: the same Stripe intent
    # arriving twice (return leg + webhook push) records exactly once.
    reference = body.get("reference")
    # Deleted tombstones do not hold the reference: deleting a mistaken
    # payment frees its charge to be re-recorded, as removal always did.
    if reference and any(p.get("reference") == reference and p.get("status") != "deleted"
                         for p in doc_state.get("payments", [])):
        raise HTTPException(status_code=409, detail="Payment already recorded")
    outstanding = float(doc_state.get("amount_outstanding", doc_state.get("total", 0)) or 0)
    if outstanding <= 0:
        raise HTTPException(status_code=409, detail="Invoice already fully paid")
    from celerp.services.money import to_decimal as _to_d
    amount = float(body["amount"])
    if source == "stripe" and _to_d(amount) - _to_d(outstanding) > _to_d("0.01"):
        # An online charge already happened at Stripe; if a manual payment
        # landed while the customer was checking out, apply what the invoice
        # can still absorb and keep the real charge on record - the surplus
        # is a genuine overpayment for the merchant to refund or credit.
        body["charged_amount"] = amount
        amount = float(outstanding)
        body["amount"] = amount
    elif clamp_overshoot and _to_d(amount) - _to_d(outstanding) > _to_d("0.01"):
        # The bulk waterfall allocates off an unlocked pre-read and asks explicitly to
        # clamp (clamp_overshoot=True); when the doc shrank before this locked apply, pay
        # what it can still absorb and let the caller report the applied amount. No
        # charged_amount: this is not an online overpayment on record, just a stale
        # allocation clamped to the fresh balance. A manual single-doc payment keys the
        # default clamp_overshoot=False, so its overshoot 409s below as it always has.
        amount = float(outstanding)
        body["amount"] = amount
    # 1-cent tolerance covers display rounding on the final payment. Without an explicit
    # clamp request a payment is a specific instrument for a specific amount, so an
    # overshoot is a 409 (a manual caller sees it; the bulk caller skips it) rather than
    # a silent partial.
    elif _to_d(amount) - _to_d(outstanding) > _to_d("0.01"):
        raise HTTPException(status_code=409, detail=f"Payment {amount} exceeds amount outstanding {outstanding}")
    bank_code = body.get("bank_account")
    if not bank_code:
        raise HTTPException(status_code=422, detail="bank_account is required")
    body.setdefault("currency", doc_state.get("currency", "USD"))
    # A payment carries its own rate because the rate moves between issuing a
    # foreign-currency document and being paid for it. On a document in the
    # company's own currency there is nothing to convert, so any rate other
    # than 1 restates the receipt: 100 banked as 3500. Refused before the
    # event is written, the same way finalization refuses it on the document.
    _company = await session.get(Company, company_id)
    _base_currency = (_company.settings.get("currency", "USD") if _company else "USD")
    if body.get("currency") == _base_currency and body.get("conversion_rate") not in (None, "") \
            and to_decimal(body["conversion_rate"]) != 1:
        raise HTTPException(
            status_code=422,
            detail=(
                f"{_base_currency} is this company's own currency, so a payment on this "
                f"document converts at 1, not {body['conversion_rate']}."
            ),
        )
    body["remaining_balance"] = max(0.0, outstanding - amount)
    payment_index = await _alloc_payment_index(
        session, company_id, doc_state.get("payments", []),
        key_doc_id=entity_id, key_type="invoice.paid")
    body["index"] = payment_index
    entry = await emit_event(
        session, company_id=company_id, entity_id=entity_id, entity_type="doc", event_type="doc.payment.received",
        data=body, actor_id=actor_id, location_id=None, source=source,
        idempotency_key=idempotency_key, metadata_={},
    )
    await auto_je.create_for_doc_payment(
        session, company_id=company_id, user_id=actor_id, doc_id=entity_id,
        amount=amount, payment_index=payment_index,
        bank_account_code=bank_code, doc_type=doc_state.get("doc_type", "invoice"),
        payment_date=body["payment_date"],
        base_currency=_base_currency,
        # The receivable was raised at the document's rate and can only be
        # cleared at that rate; the bank moves at the rate the cash actually
        # converted at. A payer who records no rate of their own settled at
        # the document's rate, so the two agree and no difference arises.
        doc_rate=float(doc_state.get("conversion_rate") or 1),
        settlement_rate=float(body.get("conversion_rate") or doc_state.get("conversion_rate") or 1),
    )
    from celerp.modules.slots import fire_lifecycle
    await fire_lifecycle(
        "on_doc_payment", session=session, company_id=company_id, user_id=actor_id,
        doc_id=entity_id, doc=doc_state, amount=amount, bank_account_code=bank_code,
    )
    # Commit so this recorder's write is visible to the next holder of the row lock.
    await session.commit()
    # Return the applied amount (post-clamp) alongside the event so callers report
    # and decrement off what actually landed under the row lock, not a stale pre-read.
    return entry, amount


@router.post("/{entity_id}/payment")
async def record_payment(entity_id: str, payload: DocPaymentBody, company_id: str = Depends(get_current_company_id), _: None = require_permission("record_payments"), user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    # apply_doc_payment takes the doc row FOR UPDATE and validates against that fresh
    # read, rejecting a closed memo via its status allowlist; the row lock serializes
    # this payment against a concurrent close or a second recorder on the same doc.
    entry, _amount = await apply_doc_payment(
        session, company_id, entity_id, payload.model_dump(exclude_none=True),
        source="api", actor_id=user.id, idempotency_key=payload.idempotency_key or str(uuid.uuid4()),
    )
    await session.commit()
    return {"event_id": entry.id}


@router.post("/{entity_id}/refund")
async def refund_payment(entity_id: str, payload: DocPaymentBody, company_id: str = Depends(get_current_company_id), _: None = require_permission("record_payments"), user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    row = await _get_doc(session, company_id, entity_id, for_update=True)
    _reject_if_closed(row.state, "refund a payment")
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
async def void_payment(entity_id: str, payload: VoidPaymentBody, company_id: str = Depends(get_current_company_id), _: None = require_permission("record_payments"), user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    row = await _get_doc(session, company_id, entity_id, for_update=True)
    _reject_if_closed(row.state, "void a payment")
    payments = row.state.get("payments", [])
    # Payments are identified by their index FIELD, not list position.
    payment = next((p for p in payments if p.get("index") == payload.payment_index), None)
    if payment is None:
        raise HTTPException(status_code=422, detail="Invalid payment index")
    if payment.get("status") != "active":
        raise HTTPException(status_code=409, detail="Payment is already voided")

    entry = await emit_event(
        session, company_id=company_id, entity_id=entity_id, entity_type="doc",
        event_type="doc.payment.voided",
        data={"payment_index": payload.payment_index, "void_reason": payload.void_reason,
              "refund_date": payload.refund_date, "amount": payment.get("amount"), "method": payment.get("method")},
        actor_id=user.id, location_id=None, source="api",
        idempotency_key=payload.idempotency_key or str(uuid.uuid4()), metadata_={},
    )
    doc_type = row.state.get("doc_type", "invoice")
    if payment.get("method") not in ("credit_note", "applied"):
        # Reverse the payment JE - use stored bank_account; fall back to "1111"
        # (default account that always exists) for historical payments recorded
        # before bank_account was required.
        bank_code = payment.get("bank_account") or "1111"
        _void_company = await session.get(Company, company_id)
        _void_base_currency = (_void_company.settings.get("currency", "USD") if _void_company else "USD")
        await auto_je.void_for_doc_payment(
            session, company_id=company_id, user_id=user.id, doc_id=entity_id,
            payment_index=payload.payment_index, amount=payment["amount"],
            bank_account_code=bank_code, doc_type=doc_type,
            refund_date=payload.refund_date,
            base_currency=_void_base_currency,
            # The same two rates the payment posted at, resolved the same way, so
            # the reversal is its mirror. Reversing at the document's rate alone
            # would not undo the numbers this posting made: it would leave the
            # difference in the receivable and in the bank.
            doc_rate=float(row.state.get("conversion_rate") or 1),
            settlement_rate=float(payment.get("conversion_rate") or row.state.get("conversion_rate") or 1),
        )
    else:
        # Credit-note settlement: void the paired payment on the other doc,
        # then void this application's AR transfer entry. No bank reversal is
        # ever posted - no cash moved.
        _cn_id = payment.get("source_doc_id") if payment.get("method") == "credit_note" else entity_id
        _inv_id = entity_id if payment.get("method") == "credit_note" else payment.get("target_doc_id")
        # The application's identity is the CN-side payment index (the value
        # create_for_cn_application was keyed with).
        _app_idx = payload.payment_index if payment.get("method") == "applied" else None
        _remaining_active = 0
        paired_doc_id = payment.get("source_doc_id") or payment.get("target_doc_id")
        if paired_doc_id:
            paired_row = await session.get(
                Projection, {"company_id": company_id, "entity_id": paired_doc_id}
            )
            if paired_row and paired_row.entity_type == "doc":
                paired_payments = paired_row.state.get("payments", [])
                # Find the matching payment on the other doc; its index FIELD
                # is its identity (list position can differ on skip-allocated
                # docs).
                # Correlate the exact counterpart: the same credit note can be
                # applied to the same invoice several times, so match the
                # paired index recorded at apply time, then fall back to the
                # same amount and date before settling for the first match.
                _linked = [
                    (pi, pp) for pi, pp in enumerate(paired_payments)
                    if pp.get("status") == "active" and (
                        (pp.get("source_doc_id") == entity_id) or (pp.get("target_doc_id") == entity_id)
                    )
                ]
                _exact = [c for c in _linked if c[1].get("paired_index") == payload.payment_index]
                if not _exact:
                    _exact = [
                        c for c in _linked
                        if abs(float(c[1].get("amount") or 0) - float(payment.get("amount") or 0)) < 0.005
                        and str(c[1].get("payment_date") or "")[:10] == str(payment.get("payment_date") or "")[:10]
                    ]
                for pi, pp in (_exact or _linked)[:1]:
                    if _app_idx is None and pp.get("method") == "applied":
                        _app_idx = pp.get("index", pi)
                    await emit_event(
                        session, company_id=company_id, entity_id=paired_doc_id, entity_type="doc",
                        event_type="doc.payment.voided",
                        data={"payment_index": pp.get("index", pi), "void_reason": payload.void_reason or "Paired void",
                              "amount": pp.get("amount"), "method": pp.get("method")},
                        actor_id=user.id, location_id=None, source="api",
                        idempotency_key=str(uuid.uuid4()), metadata_={},
                    )
                # Applications of this CN to this invoice still active after
                # this void (governs whether the legacy shared entry may be
                # voided).
                _cn_side = paired_payments if payment.get("method") == "credit_note" else payments
                _remaining_active = sum(
                    1 for p in _cn_side
                    if p.get("status") == "active" and p.get("method") == "applied"
                    and (p.get("target_doc_id") == _inv_id)
                    and p.get("index") != _app_idx
                )

        # Per-application entry first; fall back to the legacy shared entity
        # (written before ids carried the index), which is only safe to void
        # when no other application of this pair remains active.
        from celerp.services.je_keys import je_void_data as _je_void  # noqa: PLC0415
        _cnapply_row = None
        _cnapply_id = None
        if _app_idx is not None:
            _cand = f"je:auto:{_inv_id}:cnapply:{_cn_id}:{_app_idx}"
            _row_c = await session.get(Projection, {"company_id": company_id, "entity_id": _cand})
            if _row_c is not None and _row_c.state.get("status") == "posted":
                _cnapply_row, _cnapply_id = _row_c, _cand
        if _cnapply_row is None and _remaining_active == 0:
            _cand = f"je:auto:{_inv_id}:cnapply:{_cn_id}"
            _row_c = await session.get(Projection, {"company_id": company_id, "entity_id": _cand})
            if _row_c is not None and _row_c.state.get("status") == "posted":
                _cnapply_row, _cnapply_id = _row_c, _cand
        if _cnapply_row is not None:
            await emit_event(
                session, company_id=company_id, entity_id=_cnapply_id, entity_type="journal_entry",
                event_type="acc.journal_entry.voided",
                data=_je_void(f"Credit note application voided on {_inv_id}", _cnapply_row.state),
                actor_id=user.id, location_id=None, source="auto_je",
                idempotency_key=f"{_cnapply_id}:void:{payload.payment_index}",
                metadata_={"trigger": "cn.application.voided", "doc_id": _inv_id, "cn_id": _cn_id},
            )

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
    _: None = require_permission("record_payments"),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Delete a payment entirely (data-entry error correction).

    Unlike void-payment (which creates a reversal JE visible in the bank ledger as a
    refund), delete tombstones the payment on the doc projection and voids the original
    JE so it disappears from all reports. Use only for payments that were never real.

    Blocked if the payment JE has been reconciled in a closed reconciliation session.
    If reconciled in an open session, the JE is automatically un-matched first.
    """
    from celerp_accounting.models import ReconciliationSession, BankStatementLine  # noqa: PLC0415

    row = await _get_doc(session, company_id, entity_id, for_update=True)
    _reject_if_closed(row.state, "delete a payment")
    payments = row.state.get("payments", [])
    # Payments are identified by their index FIELD, not list position.
    payment = next((p for p in payments if p.get("index") == payment_index), None)
    if payment is None:
        raise HTTPException(status_code=422, detail="Invalid payment index")
    if payment.get("status") != "active":
        raise HTTPException(status_code=409, detail="Only active payments can be deleted")
    if payment.get("method") in ("credit_note", "applied"):
        # A credit-note settlement is a pair with an AR transfer entry, not a
        # cash payment; deleting one side would strand the other and its
        # entry. Voiding unwinds the whole application cleanly.
        raise HTTPException(
            status_code=422,
            detail="Credit note applications cannot be deleted. Void the payment instead.",
        )

    # Determine the JE for this payment. The exact-index id covers every
    # payment recorded since indices became stable, but on docs compacted by
    # pre-tombstone deletions the stored index was rewritten, so the resolved
    # entry must also LOOK like this payment (same date, an entry line of the
    # same magnitude); otherwise the void could hit a different payment's
    # entry. If no unambiguous owner is found, nothing is voided - leaving a
    # posted entry beats voiding the wrong one.
    _p_date = str(payment.get("payment_date") or "")[:10]
    _p_rate = float(payment.get("conversion_rate") or row.state.get("conversion_rate") or 1)
    _p_base = round(float(payment.get("amount") or 0) * _p_rate, 2)

    def _owns(state: dict) -> bool:
        if state.get("status") != "posted":
            return False
        if _p_date and str(state.get("ts") or "")[:10] != _p_date:
            return False
        magnitudes = [round(float(e.get("debit") or 0), 2) for e in state.get("entries", [])] \
            + [round(float(e.get("credit") or 0), 2) for e in state.get("entries", [])]
        return any(abs(m - _p_base) < 0.02 for m in magnitudes)

    je_id = f"je:auto:{entity_id}:pay:{payment_index}"
    je_row = await session.get(Projection, {"company_id": company_id, "entity_id": je_id})
    if je_row is None or not _owns(je_row.state):
        _pay_rows = (await session.execute(
            _sa.select(Projection).where(
                Projection.company_id == company_id,
                Projection.entity_id.like(f"je:auto:{entity_id}:pay:%"),
            )
        )).scalars().all()
        _owners = [c for c in _pay_rows if _owns(c.state)]
        je_row = _owners[0] if len(_owners) == 1 else None
        je_id = je_row.entity_id if je_row is not None else je_id

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

    # Void the original payment JE first so it disappears from the bank ledger
    # and reports. The void carries the entry's own date, so a payment inside a
    # locked period is rejected here before anything mutates.
    if je_row is not None and je_row.state.get("status") == "posted":
        from celerp.services.je_keys import je_void_data as _je_void  # noqa: PLC0415
        await emit_event(
            session, company_id=company_id, entity_id=je_id, entity_type="journal_entry",
            event_type="acc.journal_entry.voided",
            data=_je_void(f"Deleted: payment {payment_index} on {entity_id}. {payload.delete_reason or ''}".strip(),
                          je_row.state),
            actor_id=user.id, location_id=None, source="auto_je",
            # Keyed on the resolved JE id: the old doc-scoped positional shape
            # could collide with a compaction-era deletion's key and silently
            # swallow this void.
            idempotency_key=f"{je_id}:void:del:{payment_index}",
            metadata_={"trigger": "doc.payment.deleted", "doc_id": entity_id},
        )

    # Emit doc.payment.deleted - projection tombstones the row in place.
    # tombstone marks the new reducer semantics (pre-flag events compacted, and
    # replaying them must keep doing so); ts is the payment's own date, so the
    # period lock rejects the deletion even when no posted JE exists to void.
    entry = await emit_event(
        session, company_id=company_id, entity_id=entity_id, entity_type="doc",
        event_type="doc.payment.deleted",
        data={"payment_index": payment_index, "delete_reason": payload.delete_reason,
              "amount": payment.get("amount"), "method": payment.get("method"),
              "tombstone": True, "ts": payment.get("payment_date")},
        actor_id=user.id, location_id=None, source="api",
        idempotency_key=payload.idempotency_key or str(uuid.uuid4()), metadata_={},
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
async def apply_cn_to_invoice(entity_id: str, payload: ApplyToInvoiceBody, company_id: str = Depends(get_current_company_id), _: None = require_permission("record_payments"), user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    # Lock both docs FOR UPDATE in one ordered batch: the doc-row lock is the single
    # serializer, so two concurrent applications sharing docs acquire them in the same
    # order (no deadlock) and each re-reads the other's committed state before allocating
    # an index. A stale list would allocate a colliding index.
    locked = await _get_docs_for_update(session, company_id, {entity_id, payload.target_doc_id})
    cn_row = locked.get(entity_id)
    inv_row = locked.get(payload.target_doc_id)
    if cn_row is None or inv_row is None:
        raise HTTPException(status_code=404, detail="Document not found")
    cn = cn_row.state
    if cn.get("doc_type") != "credit_note":
        raise HTTPException(status_code=409, detail="Only credit notes can be applied to invoices")
    if cn.get("status") in ("draft", "void"):
        raise HTTPException(status_code=409, detail="Credit note must be issued before applying")

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

    payment_date = payload.date or datetime.now(timezone.utc).date().isoformat()

    # Both sides get allocated indices so their identity fields never
    # collide with skip-allocated payments on either doc.
    inv_pay_index = await _alloc_payment_index(session, company_id, inv.get("payments", []))
    payment_idx = await _alloc_payment_index(
        session, company_id, cn_row.state.get("payments", []),
        key_doc_id=payload.target_doc_id, key_type=f"cn.applied:cn_apply_{entity_id}")

    # Emit paired events: payment on invoice (credit_note method), payment on CN (applied method)
    await emit_event(
        session, company_id=company_id, entity_id=payload.target_doc_id, entity_type="doc",
        event_type="doc.payment.received",
        data={
            "amount": payload.amount, "method": "credit_note",
            "source_doc_id": entity_id, "payment_date": payment_date,
            "currency": cn.get("currency", "USD"),
            "index": inv_pay_index,
            # The counterpart's index on the other doc: voiding one side
            # must release exactly its pair, even when the same credit
            # note is applied to the same invoice more than once.
            "paired_index": payment_idx,
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
            "index": payment_idx,
            "paired_index": inv_pay_index,
        },
        actor_id=user.id, location_id=None, source="api",
        idempotency_key=payload.idempotency_key or str(uuid.uuid4()), metadata_={},
    )
    _cn_company = await session.get(Company, company_id)
    _cn_base_currency = (_cn_company.settings.get("currency", "USD") if _cn_company else "USD")
    await auto_je.create_for_cn_application(
        session, company_id=company_id, user_id=user.id,
        doc_id=payload.target_doc_id, cn_id=entity_id, amount=payload.amount,
        payment_index=payment_idx, payment_date=payment_date,
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
async def refund_cn(entity_id: str, payload: CnRefundBody, company_id: str = Depends(get_current_company_id), _: None = require_permission("record_payments"), user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    # Lock the credit note row FOR UPDATE and validate against that fresh read: the
    # doc-row lock is the single serializer, so a concurrent application/refund on this
    # credit note is ordered at the row and cannot mint a colliding index.
    row = await _get_doc(session, company_id, entity_id, for_update=True)
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

    payment_index = await _alloc_payment_index(session, company_id, cn.get("payments", []),
                                               key_doc_id=entity_id, key_type="invoice.paid")
    entry = await emit_event(
        session, company_id=company_id, entity_id=entity_id, entity_type="doc",
        event_type="doc.payment.received",
        data={
            "amount": payload.amount, "method": "refund",
            "bank_account": bank_code, "reference": payload.reference,
            "payment_date": payment_date, "currency": cn.get("currency", "USD"),
            "index": payment_index,
        },
        actor_id=user.id, location_id=None, source="api",
        idempotency_key=payload.idempotency_key or str(uuid.uuid4()), metadata_={},
    )
    # JE: debit AR, credit bank
    _refund_company = await session.get(Company, company_id)
    _refund_base_currency = (_refund_company.settings.get("currency", "USD") if _refund_company else "USD")
    await auto_je.create_for_doc_payment(
        session, company_id=company_id, user_id=user.id, doc_id=entity_id,
        amount=payload.amount, payment_index=payment_index,
        bank_account_code=bank_code, doc_type="credit_note",
        payment_date=payment_date,
        base_currency=_refund_base_currency,
        # A refund is issued at the rate the credit note itself carries, and
        # there is no second rate to record: the form does not ask for one.
        doc_rate=float(cn.get("conversion_rate") or 1),
        settlement_rate=float(cn.get("conversion_rate") or 1),
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
async def bulk_payment(payload: BulkPaymentBody, company_id: str = Depends(get_current_company_id), _: None = require_permission("record_payments"), user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
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

    # Sort by due_date asc (oldest first), then issue_date, then doc_id. The doc_id
    # tie-breaker gives every request one canonical acquisition order over docs that
    # share due/issue dates, so two concurrent bulk runs over an overlapping set take
    # the shared rows in the same order and cannot form a cross-request lock cycle.
    payable.sort(key=lambda x: (x[1].get("due_date") or x[1].get("issue_date") or "9999", x[1].get("issue_date") or "9999", x[0]))

    # Allocate amount oldest-first. The unlocked pre-read above only orders and caps the
    # tender; apply_doc_payment re-reads each doc under its row lock and re-validates, so
    # a doc concurrently closed, paid, or shrunk - or one whose plain reference overshoots
    # its fresh outstanding - is skipped honestly (its 409 is caught, the lock released by
    # rollback, the doc recorded in skipped with its reason) rather than paid off a stale
    # read. remaining and total_allocated track the amount apply_doc_payment reports as
    # actually applied under the lock, so a doc that shrank is reported at its real share.
    from sqlalchemy.exc import DBAPIError
    remaining = payload.amount
    allocations = []
    skipped: list[dict] = []
    payment_date = payload.payment_date
    if not payload.bank_account:
        raise HTTPException(status_code=422, detail="bank_account is required")
    bank_code = payload.bank_account

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
        # Bound this doc's row-lock wait transaction-locally. apply_doc_payment commits
        # per doc, so the prior iteration's commit reset the timeout; reapply every loop so
        # a contended doc raises 55P03 and is skipped below rather than blocking the worker.
        await session.execute(
            text(f"SET LOCAL lock_timeout = '{_BULK_DOC_LOCK_TIMEOUT_MS}ms'"))
        try:
            _entry, applied = await apply_doc_payment(
                session, company_id, doc_id, body,
                source="api", actor_id=user.id, idempotency_key=str(uuid.uuid4()),
                clamp_overshoot=True)
        except HTTPException as e:
            # A doc concurrently closed/paid/shrunk under the row lock: skip it and record
            # why, so the caller can surface which docs were paid and which were not. Roll
            # back first so the skipped doc's FOR UPDATE lock is released at once (no write
            # occurs before the raise; prior per-doc commits are already durable), leaving
            # no lock held across a skip to seed a cross-request deadlock.
            await session.rollback()
            skipped.append({"doc_id": doc_id, "reason": e.detail})
            continue
        except DBAPIError as e:
            # A contended doc held past lock_timeout raises SQLSTATE 55P03 (asyncpg surfaces
            # it as a DBAPIError, not an HTTPException). Treat that ONE state as an honest
            # skip - roll back and record the doc - so a timeout surfaces as a skip reason,
            # never a 500 for the whole bulk. Any other DB error is a real fault, re-raised
            # so it is never dishonestly masked.
            if getattr(getattr(e, "orig", None), "sqlstate", None) != "55P03":
                raise
            await session.rollback()
            skipped.append({"doc_id": doc_id, "reason": "Document was locked by another operation; try again"})
            continue
        allocations.append({"doc_id": doc_id, "amount": applied})
        remaining -= applied

    await session.commit()
    return {"allocations": allocations, "skipped": skipped,
            "total_allocated": payload.amount - remaining, "remaining": remaining}


@router.post("/{entity_id}/receive")
async def receive_po(entity_id: str, payload: ReceiveBody, company_id: str = Depends(get_current_company_id), _: None = require_permission("edit_documents"), user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
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
    # Landed-cost allocation: spread this bill's freight/duty/insurance/non-recoverable-VAT charges
    # across its stocked goods lines (by value). Received parcels carry their per-unit share so each
    # item's cost reflects landed cost; per-unit storage prorates naturally to the received quantity.
    bill_alloc: dict[str, dict[str, float]] = {}
    landed_drawdown: dict[str, float] = {}   # per-kind landed cost capitalised by this receipt
    if doc_type == "bill":
        bill_alloc = await compute_bill_landed_allocation(session, company_id, row.state)

    for it in payload.received_items:
        sell_by = sell_by_map.get(it.sku or "") or doc_line_sell_by.get(it.sku or "", "") or None
        validate_line_quantity(it.quantity_received, sell_by, unit_map, label=it.name or it.sku or "Received item")

    # Received parcels each get a fresh sequential barcode so every physical lot is
    # scannable and barcode uniqueness (the physical-lot key now that SKU may repeat)
    # actually holds. Allocate the whole batch in one locked call AFTER validation so
    # concurrent receipts mint distinct barcodes; the lock is held until this request
    # commits. The DB unique index is the backstop, not the normal mechanism.
    from celerp_inventory.services import allocate_internal_codes

    def _creates_parcel(it) -> bool:
        return not (it.item_id and not is_inbound) and it.receive_as == "stock"

    _new_parcel_count = sum(1 for it in payload.received_items if _creates_parcel(it))
    _recv_barcodes = await allocate_internal_codes(session, company_id, _new_parcel_count) if _new_parcel_count else []
    _recv_barcode_idx = 0

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
                "allow_splitting", "pick_method",
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
            # Resolve conversion factor: prefer the item_id-keyed factor (specific
            # lot/template) when the line carries item_id, then doc-line override,
            # then sku factor, then 1.
            conversion = (
                (item_conversion_map.get(it.item_id) if it.item_id else None)
                or doc_line_conversion.get(_sku.strip())
                or sku_conversion_map.get(_sku.strip())
                or 1
            )
            # Payload values always take precedence for the fields below
            item_data.update({
                "sku": _sku,
                "name": it.name,
                "quantity": float(it.quantity_received) * conversion,
                "location_id": payload.location_id,
            })
            # Fresh sequential barcode per physical lot (unique + scannable), taken
            # from the batch allocated under the code-namespace lock above.
            item_data["barcode"] = _recv_barcodes[_recv_barcode_idx]
            _recv_barcode_idx += 1
            if it.cost_price is not None:
                # Emit cost_total when quantity is known (cost_total is the primitive)
                _recv_qty = float(it.quantity_received) * conversion
                item_data["cost_total"] = it.cost_price * _recv_qty if _recv_qty else it.cost_price
            # Attach the per-unit landed cost allocated to this goods line: the projection derives
            # cost_total = cost_base + Σ(unit × quantity), so the parcel carries its landed share.
            _landed = bill_alloc.get(_sku.strip(), {})
            if _landed:
                item_data["landed_contributions"] = {f"{entity_id}::{k}": u for k, u in _landed.items()}
                _recv_qty_total = float(it.quantity_received) * conversion
                for _k, _u in _landed.items():
                    landed_drawdown[_k] = round(landed_drawdown.get(_k, 0.0) + _u * _recv_qty_total, 2)
            if is_consignment:
                item_data["consignment_flag"] = "in"
                # Pair the new parcel with the consignment doc: inventory renders the
                # number in the status cell and q-search matches it.
                item_data["status_doc_id"] = entity_id
                item_data["status_doc_number"] = row.state.get("doc_number") or row.state.get("ref_id") or ""
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

    if doc_type == "purchase_order":
        # A purchase order recognises goods + AP at receipt (its accounting point): Dr inventory / Cr AP.
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
            receive_date=datetime.now(timezone.utc).date().isoformat(),
        )
    elif doc_type == "bill" and landed_drawdown:
        # A bill already recognised goods + AP at finalize (create_for_bill_conversion); receiving must
        # NOT re-post that JE (it would double-count inventory and AP). Receipt only capitalises the
        # received landed cost from the clearing accounts into 1130-P (Dr 1130-P / Cr clearing).
        await auto_je.create_for_landed_capitalisation(
            session, company_id=company_id, user_id=user.id, doc_id=entity_id,
            landed_by_kind=landed_drawdown, receive_suffix=str(uuid.uuid4()),
            receive_date=datetime.now(timezone.utc).date().isoformat(),
        )
    # consignment_in: no JE (goods not owned).
    await session.commit()
    return {"event_id": entry.id}


# Item statuses meaning the goods are not on our shelf, so they cannot be handed back to a
# supplier: they are at a customer, gone, or no longer a live parcel.
_NOT_ON_HAND_STATUSES: frozenset[str] = frozenset({"memo_out", "sold", "archived", "merged", "disposed"})


class ReturnItem(BaseModel):
    item_id: str
    quantity_returned: float


class ReturnBody(BaseModel):
    items: list[ReturnItem]
    notes: str | None = None
    idempotency_key: str | None = None


@router.post("/{entity_id}/return-items")
async def return_consignment_items(entity_id: str, payload: ReturnBody, company_id: str = Depends(get_current_company_id), _: None = require_permission("edit_documents"), user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
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
        # Only goods actually on the shelf can go back to a supplier. Anything out on memo
        # is at a customer's site and anything sold has left; shrinking those here would
        # quietly write off stock that is still owed back to us.
        _item_status = str(item.state.get("status") or "").lower()
        if _item_status in _NOT_ON_HAND_STATUSES:
            raise HTTPException(
                status_code=409,
                detail=(f"Cannot return {item.state.get('sku', it.item_id)}: it is "
                        f"'{_item_status}', not on hand. Bring it back into stock first."),
            )
        current_qty = float(item.state.get("quantity", 0) or 0)
        if it.quantity_returned > current_qty + 1e-9:
            raise HTTPException(status_code=409, detail=f"Cannot return more than on-hand quantity for {it.item_id}")
        new_qty = max(0.0, current_qty - it.quantity_returned)
        adjustment: dict = {"new_qty": new_qty, "consignment_flag": None if new_qty == 0 else "in"}
        # The returned units physically leave, so the lot's goods cost leaves with them:
        # scale cost_base to what is still on hand. Without this the remaining balance
        # would keep carrying the whole lot's cost (a partial return is an in-place
        # quantity reduction, not a split, so nothing else rescales it).
        _base = item.state.get("cost_base")
        if _base is None:
            _base = item.state.get("cost_total")
        if _base is not None and current_qty > 0:
            adjustment["cost_base"] = round(float(_base) * (new_qty / current_qty), 2)
        await emit_event(
            session, company_id=company_id, entity_id=it.item_id, entity_type="item",
            event_type="item.quantity.adjusted",
            data=adjustment,
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


class ShipmentFromDocsBody(BaseModel):
    doc_ids: list[str]


@router.post("/shipment")
async def create_shipment_from_docs(
    payload: ShipmentFromDocsBody,
    company_id: str = Depends(get_current_company_id),
    _: None = require_permission("edit_documents"),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Create ONE draft shipping document (list_type="shipping_doc") from one or
    more issued invoices / consignment-out memos - the "now I have to ship it"
    step, single order or consolidated box alike.

    Copies contact, ship-to, currency, and every document's lines in selection
    order. Quantities and unit values carry over (the unit value doubles as the
    customs value on the Commercial Invoice); discounts and taxes do not -
    shipment paperwork is not an accounting document. A shipment goes to one
    consignee in one currency, so mixed customers or currencies are rejected.
    The shipment posts no journal entry and moves no stock; fulfillment stays on
    the source documents, referenced via source_docs.
    """
    doc_ids = [i.strip() for i in payload.doc_ids if i.strip()]
    if not doc_ids:
        raise HTTPException(status_code=422, detail="Select at least one document to ship")

    # Lock every source doc FOR UPDATE in one sorted batch before validating, so a
    # concurrent close cannot slip a status change between this read and the shipment
    # commit (TOCTOU). Validate in the caller's selection order off the locked rows.
    locked = await _get_docs_for_update(session, company_id, doc_ids)
    states = []
    for eid in doc_ids:
        row = locked.get(eid)
        if row is None:
            raise HTTPException(status_code=404, detail="Document not found")
        state = row.state
        if state.get("doc_type") not in ("invoice", "memo"):
            raise HTTPException(status_code=422,
                                detail="Only invoices and consignment-out memos can be shipped")
        if state.get("status") in ("draft", "void"):
            raise HTTPException(status_code=409,
                                detail="Issue every document before creating its shipping paperwork")
        if state.get("status") == "closed":
            raise HTTPException(status_code=409,
                                detail="Reopen a closed memo before creating its shipping paperwork")
        states.append(state)

    # One consignee prefills the shipment. Several consignees is a real flow too
    # (a freight consolidator receives one box for many end customers), so it is
    # NOT an error - the shipment is created with a blank consignee and the user
    # enters the consolidator's details on the draft.
    consignees = {(s.get("contact_id") or "",
                   s.get("contact_name") or s.get("customer_name") or "") for s in states}
    single_consignee = len(consignees) == 1
    # Currencies stay a hard stop: a commercial invoice declares one total in one
    # currency, so mixed-currency lines would produce a wrong declared value.
    currencies = {s.get("currency") or "" for s in states}
    if len(currencies) > 1:
        raise HTTPException(status_code=422,
                            detail="Selected documents use different currencies - a shipping document declares one")

    lines = []
    for state in states:
        for li in state.get("line_items") or []:
            qty = float(li.get("quantity") or 0)
            price = float(li.get("unit_price") or 0)
            lines.append({k: v for k, v in {
                "item_id": li.get("item_id") or li.get("entity_id"),
                "sku": li.get("sku"),
                "name": li.get("name"),
                "description": li.get("description") or li.get("name"),
                "unit": li.get("unit"),
                "quantity": qty,
                "unit_price": price,
                "line_total": qty * price,
                "hs_code": li.get("hs_code"),
                "country_of_origin": li.get("country_of_origin"),
                "pieces": li.get("pieces"),
                "weight": li.get("weight"),
            }.items() if v is not None})

    first = states[0]
    _ship_to = next((s.get("contact_shipping_address") for s in states
                     if s.get("contact_shipping_address")), None)
    _attn = next((s.get("shipping_attn") for s in states if s.get("shipping_attn")), None)
    company = await session.get(Company, company_id)
    ref_id = next_doc_ref(company, list_sequence_key("shipping_doc"))
    new_entity_id = f"list:{ref_id}"
    data = {k: v for k, v in {
        "list_type": "shipping_doc",
        "status": "draft",
        "ref_id": ref_id,
        "source_docs": doc_ids,
        "line_items": lines,
        "currency": first.get("currency"),
        "contact_id": first.get("contact_id") if single_consignee else None,
        "contact_name": first.get("contact_name") if single_consignee else None,
        "customer_name": (first.get("contact_name") or first.get("customer_name"))
                         if single_consignee else None,
        "contact_shipping_address": _ship_to if single_consignee else None,
        "shipping_attn": _attn if single_consignee else None,
        # Consigned goods are still the sender's property: customs-wise "not for
        # resale" - but one sold item in the box makes the shipment a sale.
        "reason_for_export": "sale" if any(s.get("doc_type") == "invoice" for s in states)
                             else "not_for_resale",
    }.items() if v is not None}
    entry = await _emit_list(session, company_id, new_entity_id, "list.created", data, user)
    await session.commit()
    return {"event_id": entry.id, "id": new_entity_id}


@router.post("/{entity_id}/convert")
async def convert_doc(entity_id: str, company_id: str = Depends(get_current_company_id), _: None = require_permission("edit_documents"), user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    row = await _get_doc(session, company_id, entity_id, for_update=True)
    state = row.state
    if state.get("doc_type") == "quotation":
        if state.get("status") in {"void", "converted"}:
            raise HTTPException(status_code=409, detail="Cannot convert quotation in current status")
        valid_until = state.get("valid_until")
        if valid_until and valid_until < datetime.now(timezone.utc).date().isoformat():
            raise HTTPException(status_code=409, detail="Cannot convert expired quotation")
        # Quotation docs never own reservations, so any reserved line belongs to
        # another document and must be released there before invoicing.
        await _assert_no_foreign_reserved(
            session, company_id, "invoice", None,
            (li.get("entity_id") or li.get("item_id") or "" for li in state.get("line_items") or []),
        )
        await _assert_no_draft_items(
            session, company_id,
            (li.get("entity_id") or li.get("item_id") or "" for li in state.get("line_items") or []),
        )
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
        # An already-converted memo falls through to the allocation-set guard below,
        # which refuses with the clearer "nothing On Memo" 422: convert settles every
        # backing lot, so a converted memo has an empty allocation set and can never
        # re-bill. Only a genuinely-not-issued memo (draft/void) is rejected here.
        if state.get("status") not in ("final", "sent", "received", "partially_received", "converted"):
            raise HTTPException(status_code=409, detail="Memo must be issued before converting to invoice")
        company = await session.get(Company, company_id)
        ref = next_doc_ref(company, "invoice")
        new_doc_id = f"doc:{ref}"

        # The memo's settlement unit is its allocation set (every lot stamped
        # status_doc_id==this memo), not line_items: a memo line whose quantity exceeds
        # its bound lot draws cross-lot siblings from other lots of the same SKU, and each
        # sibling carries the memo stamp but never appears in line_items. Bill each line
        # for the full still-out quantity of its SKU summed across the allocation set, at
        # the line's own unit_price (revenue is per-SKU and identical across lots; lot cost
        # is COGS, recognized per lot at fulfill and untouched here).
        allocation_items = await _memo_allocation_items(session, company_id, entity_id)
        memo_out_backing: list[Projection] = [
            item_proj for item_proj in allocation_items
            if item_proj.state.get("status") == "memo_out"
        ]

        if not memo_out_backing:
            raise HTTPException(
                status_code=422,
                detail="Cannot convert: no items are currently On Memo (memo_out). Fulfill at least one item before converting.",
            )

        # Bill each still-out lot against the original memo line it is bound to, at THAT
        # line's own price/discount/tax. A memo line binds to exactly one lot by its item
        # key (the LineItem validator folds a frontend entity_id into item_id and clears
        # entity_id), the same key fulfill_lines uses; a lot's only line pointer is the
        # identity item_proj.entity_id == that key. A cross-lot sibling (drawn from another
        # same-SKU lot to cover a line whose quantity exceeded its bound lot) carries the
        # memo stamp but has no bound line, so it is billed once onto the first same-SKU
        # original line at that line's unit_price - the sibling's revenue is per-SKU and
        # its lot cost was already recognized at fulfill.
        _currency = state.get("currency")
        original_line_items = state.get("line_items", [])
        line_by_bound_eid: dict[str, dict] = {}
        for li in original_line_items:
            _key = li.get("entity_id") or li.get("item_id")
            if _key:
                line_by_bound_eid[_key] = li

        billed_by_bound_eid: dict[str, dict] = {}
        unbound_qty_by_sku: dict[str, float] = {}
        for item_proj in memo_out_backing:
            _lot_eid = item_proj.entity_id
            _still_out = float(item_proj.state.get("quantity") or 0)
            if _still_out <= 1e-9:
                continue
            _line = line_by_bound_eid.get(_lot_eid)
            if _line is None:
                # Genuinely unbound cross-lot sibling: fold its still-out quantity into the
                # first same-SKU original line below, priced at that line's unit_price.
                _sku = item_proj.state.get("sku")
                unbound_qty_by_sku[_sku] = unbound_qty_by_sku.get(_sku, 0.0) + _still_out
                continue
            # Bill the bound lot's own still-out quantity to its own line. Scale line_total
            # and each tax amount by the kept fraction so an explicit per-line discount
            # (a line_total below quantity*unit_price) and per-line tax both survive at the
            # billed quantity; taxes are linear in the base, so one fraction is exact.
            _line_qty = float(_line.get("quantity") or 0)
            billed = {**_line, "quantity": _still_out}
            if _line_qty and abs(_still_out - _line_qty) > 1e-9:
                _kept = to_decimal(_still_out) / to_decimal(_line_qty)
                if _line.get("line_total") is not None:
                    billed["line_total"] = to_stored_float(round_money(
                        to_decimal(_line["line_total"]) * _kept, _currency))
                if _line.get("taxes"):
                    billed["taxes"] = [
                        {**tx, "amount": to_stored_float(round_money(
                            to_decimal(tx.get("amount") or 0) * _kept, _currency))}
                        if tx.get("amount") is not None else {**tx}
                        for tx in _line["taxes"]
                    ]
            billed_by_bound_eid[_lot_eid] = billed

        # Fold each unbound sibling's quantity onto the first same-SKU original line, priced
        # fresh at that line's unit_price so the added units never disturb the bound
        # portion's discount or tax. Fail loud if a sibling SKU has no original line at all
        # (structurally unreachable: span draws are always same-SKU) rather than invent a
        # price and fabricate a billed amount.
        for _sku, _sib_qty in unbound_qty_by_sku.items():
            if _sib_qty <= 1e-9:
                continue
            _target_eid = next(
                (li.get("entity_id") or li.get("item_id")
                 for li in original_line_items if li.get("sku") == _sku),
                None,
            )
            if _target_eid is None or _target_eid not in billed_by_bound_eid:
                raise HTTPException(
                    status_code=500,
                    detail=f"convert: sibling SKU {_sku} has no matching original line",
                )
            billed = billed_by_bound_eid[_target_eid]
            _unit = to_decimal(billed.get("unit_price") or 0)
            billed["quantity"] = float(billed.get("quantity") or 0) + _sib_qty
            if billed.get("line_total") is not None:
                billed["line_total"] = to_stored_float(round_money(
                    to_decimal(billed["line_total"]) + _unit * to_decimal(_sib_qty), _currency))

        # Preserve original line order among the lines actually billed.
        qualifying_line_items = [
            billed_by_bound_eid[li.get("entity_id") or li.get("item_id")]
            for li in original_line_items
            if (li.get("entity_id") or li.get("item_id")) in billed_by_bound_eid
        ]

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
        # Settle every backing lot, not just the line_items rows: a cross-lot sibling is
        # still out at the customer and must leave memo_out here or it would keep the memo
        # un-closeable with no settlement path. The invoice bills its SKU at the parent
        # line; the sibling's revenue is already carried there. Invoice-finalize's own
        # promotion loop then skips these (they are no longer memo_out), so nothing is
        # promoted twice.
        _memo_number = state.get("doc_number") or state.get("ref_id") or ""
        for item_proj in memo_out_backing:
            await emit_event(
                session, company_id=company_id, entity_id=item_proj.entity_id, entity_type="item",
                event_type="item.status.set",
                data={"new_status": "sold", "source_doc_id": entity_id, "doc_number": _memo_number},
                actor_id=user.id, location_id=None, source="memo_convert",
                idempotency_key=str(uuid.uuid4()), metadata_={"doc_id": entity_id},
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
    _: None = require_permission("edit_documents"),
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
            "created_at": datetime.now(timezone.utc).isoformat(),
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
    _: None = require_permission("edit_documents"),
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
        data={"doc_id": entity_id, "note_id": note_id, "note": payload.note.strip(), "updated_at": datetime.now(timezone.utc).isoformat()},
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
    _: None = require_permission("edit_documents"),
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
    _: None = require_permission("edit_documents"),
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
    _: None = require_permission("edit_documents"),
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
    contact_id: str | None = None
    contact_name: str | None = None
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
    # Shipment fields (list_type="shipping_doc"): one shipment record feeds both the
    # Delivery Note and Commercial Invoice printouts. Declared - not extra="allow" -
    # so the shipment contract is visible and the closed-set fields validate.
    contact_shipping_address: str | None = None
    shipping_attn: str | None = None
    carrier: str | None = None
    tracking: str | None = None
    incoterms: str | None = None
    package_count: int | None = None
    gross_weight: str | None = None
    reason_for_export: str | None = None
    country_of_export: str | None = None
    country_of_destination: str | None = None
    importer: str | None = None
    source_docs: list[str] | None = None  # invoices/memos this shipment ships
    idempotency_key: str | None = None
    model_config = {"extra": "allow"}

    @model_validator(mode="after")
    def _validate_shipment_enums(self) -> "ListCreatePayload":
        _validate_shipment_values({"incoterms": self.incoterms,
                                   "reason_for_export": self.reason_for_export})
        return self


class ListPatch(DocPatch):
    # Optimistic concurrency: when set, the patch applies only if it equals the list's current
    # version (its latest ledger-entry id). A mismatch means another editor moved the list on since
    # this client last read it, so the write is a stale clobber and is rejected 409. Omitted = no check.
    expected_version: int | None = None


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


async def _get_list_for_update(session: AsyncSession, company_id, entity_id: str) -> Projection:
    """Load a list row under a write lock (SELECT ... FOR UPDATE) so a read-modify-write over its
    line_items is serialized against concurrent writers. Scan check-off and counting read the whole
    array, mutate it in Python, and write it back; two writers off the same read would lose one
    update. The lock makes the second writer block until the first commits, then re-read the
    committed array. populate_existing forces the locking SELECT even if the row is already in the
    session's identity map."""
    row = await session.get(Projection, {"company_id": company_id, "entity_id": entity_id},
                            with_for_update=True, populate_existing=True)
    if row is None or row.entity_type != "list":
        raise HTTPException(status_code=404, detail="List not found")
    return row


async def _emit_list(session, company_id, entity_id, event_type, data, user, idem_key=None, meta=None):
    return await emit_event(
        session, company_id=company_id, entity_id=entity_id, entity_type="list",
        event_type=event_type, data=data, actor_id=user.id, location_id=None,
        source="api", idempotency_key=idem_key or str(uuid.uuid4()), metadata_=meta or {},
    )


def _list_base_where(company_id) -> list:
    """The company-scoped list projection selector shared by every list read."""
    return [Projection.company_id == company_id, Projection.entity_type == "list"]


def _list_sort_date():
    """The list's effective sort/window date, matching the Python order issue_date > created_at
    column > date with the historic [:10] truncation. A list's state carries no date of its own,
    so the projection's created_at column is the fallback. Written once and reused by the index
    ORDER BY, the index date_from/date_to bounds, and the page/export ordering."""
    issue = _func.nullif(_func.substr(Projection.state["issue_date"].as_string(), 1, 10), "")
    created = _func.substr(_sa.cast(Projection.created_at, _sa.Text), 1, 10)
    date = _func.nullif(_func.substr(Projection.state["date"].as_string(), 1, 10), "")
    return _func.coalesce(issue, created, date)


def _list_search_where(q: str, *, include_customer_id: bool):
    """SQL predicate for the free-text list search over ref_id / customer_name (and customer_id for
    the index, matching its wider Python match; export deliberately omits customer_id)."""
    ql = f"%{q.lower()}%"
    clauses = [
        _func.lower(Projection.state["ref_id"].as_string()).like(ql),
        _func.lower(Projection.state["customer_name"].as_string()).like(ql),
    ]
    if include_customer_id:
        clauses.append(_func.lower(Projection.state["customer_id"].as_string()).like(ql))
    return _sa.or_(*clauses)


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
    base_where = _list_base_where(company_id)
    if list_type:
        base_where.append(Projection.state["list_type"].as_string() == list_type)
    if all_issued:
        base_where.append(Projection.state["status"].as_string().notin_((DRAFT, VOID)))
    elif status:
        base_where.append(Projection.state["status"].as_string() == status)
    if exclude_status:
        base_where.append(Projection.state["status"].as_string() != exclude_status)
    if converted_to_type:
        base_where.append(Projection.state["converted_to_type"].as_string() == converted_to_type)
    sort_date = _list_sort_date()
    if date_from:
        base_where.append(sort_date >= date_from)
    if date_to:
        base_where.append(sort_date <= date_to)
    if q:
        base_where.append(_list_search_where(q, include_customer_id=True))

    total = (await session.execute(
        select(_func.count()).select_from(Projection).where(*base_where))).scalar_one()
    # The index table needs only the header fields plus a line count and total weight; it never shows
    # individual lines. Push the count and weight sum into SQL and select the header columns alone, so
    # a list with thousands of lines never drags its whole line_items array back into Python here.
    item_count = _func.coalesce(_func.json_array_length(Projection.state["line_items"]), 0)
    weight_sum = _sa.literal_column(
        "(SELECT COALESCE(SUM(COALESCE("
        "NULLIF(elem ->> 'weight_ct', '')::numeric, NULLIF(elem ->> 'weight', '')::numeric, 0)), 0) "
        "FROM json_array_elements(projections.state -> 'line_items') AS elem)"
    )
    list_q = (
        select(
            Projection.entity_id,
            Projection.created_at,
            Projection.state["ref_id"].as_string().label("ref_id"),
            Projection.state["list_type"].as_string().label("list_type"),
            Projection.state["customer_name"].as_string().label("customer_name"),
            Projection.state["receiver"].as_string().label("receiver"),
            Projection.state["customer_id"].as_string().label("customer_id"),
            Projection.state["date"].as_string().label("date"),
            Projection.state["total"].as_string().label("total"),
            Projection.state["status"].as_string().label("status"),
            Projection.state["created_at"].as_string().label("state_created_at"),
            item_count.label("item_count"),
            weight_sum.label("total_weight"),
        )
        .where(*base_where)
        # Descending newest-first with the unique entity_id tiebreak so equal-date rows have a
        # total order and OFFSET pagination never skips or duplicates a boundary row.
        .order_by(sort_date.desc(), Projection.entity_id.desc())
        .offset(offset)
    )
    if limit is not None:
        list_q = list_q.limit(limit)
    rows = (await session.execute(list_q)).all()
    out = [{
        "id": r.entity_id,
        "created_at": (r.created_at.isoformat() if r.created_at is not None
                       else r.state_created_at or ""),
        "ref_id": r.ref_id,
        "list_type": r.list_type,
        "customer_name": r.customer_name,
        "receiver": r.receiver,
        "customer_id": r.customer_id,
        "date": r.date,
        "total": r.total,
        "status": r.status,
        "item_count": r.item_count,
        "total_weight": float(r.total_weight or 0),
    } for r in rows]
    return {"items": out, "total": total}


@lists_router.get("/summary")
async def get_list_summary(
    company_id: str = Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    base_where = _list_base_where(company_id)
    status_expr = _func.coalesce(Projection.state["status"].as_string(), "")
    # One grouped pass over the projection: a bounded histogram (one row per status), with the
    # value sum carried per group so total_value is derived without a second scan. total is text
    # in the json state, so cast the ->> output to numeric - never a jsonb cast.
    total_num = _sa.cast(
        _func.nullif(Projection.state["total"].as_string(), ""), _sa.Numeric)
    grouped = (await session.execute(
        select(status_expr,
               _func.count(),
               _func.coalesce(_func.sum(total_num), 0))
        .where(*base_where)
        .group_by(status_expr)
    )).all()
    count_by_status: dict[str, int] = {}
    total_count = 0
    total_value = 0.0
    for st, n, value_sum in grouped:
        count_by_status[st] = n
        total_count += n
        if st != VOID:
            total_value += float(value_sum or 0)
    draft_count = count_by_status.get(DRAFT, 0)
    all_issued_count = sum(v for k, v in count_by_status.items() if k not in (DRAFT, VOID))

    # Converted outcomes: closed lists whose result is a conversion, split by target type.
    converted_where = base_where + [
        status_expr == CLOSED,
        Projection.state["result"].as_string() == "converted",
    ]
    ctt_expr = Projection.state["converted_to_type"].as_string()
    converted_rows = (await session.execute(
        select(ctt_expr, _func.count())
        .where(*converted_where)
        .group_by(ctt_expr)
    )).all()
    converted_by_type = {ctt: n for ctt, n in converted_rows}
    return {
        "total_count": total_count,
        "draft_count": draft_count,
        "all_issued_count": all_issued_count,
        "total_value": total_value,
        "converted_to_memo_count": converted_by_type.get("memo", 0),
        "converted_to_invoice_count": converted_by_type.get("invoice", 0),
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
    base_where = _list_base_where(company_id)
    if q:
        # Export matches ref_id / customer_name only (never customer_id), matching its own historic
        # behaviour rather than the wider index search.
        base_where.append(_list_search_where(q, include_customer_id=False))
    if list_type:
        base_where.append(Projection.state["list_type"].as_string() == list_type)
    if status:
        base_where.append(Projection.state["status"].as_string() == status)
    _COLS = ["id", "ref_id", "list_type", "customer_name", "date", "total", "status"]

    async def _rows():
        # Read the projection in bounded SQL batches so the whole set is never buffered in Python.
        header = io.StringIO()
        writer = csv.DictWriter(header, fieldnames=_COLS, extrasaction="ignore")
        writer.writeheader()
        yield header.getvalue()
        batch = 500
        offset = 0
        # Select only the seven columns the CSV emits, straight from the json state, so a list's whole
        # line_items array is never deserialized just to write its header row.
        col_exprs = [Projection.entity_id.label("id")] + [
            Projection.state[c].as_string().label(c) for c in _COLS if c != "id"
        ]
        while True:
            rows = (await session.execute(
                select(*col_exprs)
                .where(*base_where)
                .order_by(Projection.entity_id.desc())
                .offset(offset)
                .limit(batch)
            )).all()
            if not rows:
                break
            buf = io.StringIO()
            w = csv.DictWriter(buf, fieldnames=_COLS, extrasaction="ignore")
            for r in rows:
                w.writerow({c: (getattr(r, c) or "") for c in _COLS})
            yield buf.getvalue()
            if len(rows) < batch:
                break
            offset += batch

    return StreamingResponse(
        _rows(),
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
    return row.state | {"id": row.entity_id, "version": row.version}


_PAGE_LIMIT_MAX = 100


def _page_bounds(offset: str, limit: str) -> tuple[int, int]:
    """Validate the page window at the function level (never by hiding controls): offset must be a
    non-negative integer, limit a positive integer hard-capped at 100. Returns the effective values."""
    try:
        off = int(offset)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="offset must be an integer")
    if off < 0:
        raise HTTPException(status_code=400, detail="offset must be zero or greater")
    try:
        lim = int(limit)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="limit must be an integer")
    if lim <= 0:
        raise HTTPException(status_code=400, detail="limit must be greater than zero")
    return off, min(lim, _PAGE_LIMIT_MAX)


@lists_router.get("/{entity_id}/page")
async def get_list_page(
    entity_id: str,
    offset: str = "0",
    limit: str = str(_PAGE_LIMIT_MAX),
    company_id: str = Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """The list header (stored state without line_items), one bounded page of line_items, the total,
    and enriched item metadata for exactly the page's ids. Everything is read in bounded SQL: the
    header is `state - line_items` (never the whole document row), the total is json_array_length, the
    window is a positional json subscript over generate_series, and item_meta joins the page's catalog
    items only. The detail view renders the page and enriches it from this one call, so a large list
    is never expanded into Python and no follow-up metadata round-trip is needed."""
    off, lim = _page_bounds(offset, limit)
    # Header, version and total in ONE bounded read: the header is the stored state with line_items
    # stripped in SQL, so the whole line array is never dragged back into Python on a page request.
    head = (await session.execute(
        text("SELECT state::jsonb - 'line_items' AS header, version, "
             "COALESCE(json_array_length(state -> 'line_items'), 0) AS total "
             "FROM projections "
             "WHERE company_id = :cid AND entity_id = :eid AND entity_type = 'list'"),
        {"cid": str(company_id), "eid": entity_id},
    )).first()
    if head is None:
        raise HTTPException(status_code=404, detail="List not found")
    total = head.total
    window = (await session.execute(
        text("""
            SELECT (state -> 'line_items') -> gs AS item
            FROM projections, generate_series(CAST(:lo AS integer), CAST(:hi AS integer)) AS gs
            WHERE company_id = :cid AND entity_id = :eid
            ORDER BY gs
        """),
        {"lo": off, "hi": off + lim - 1, "cid": str(company_id), "eid": entity_id},
    )).all()
    items = [r.item for r in window if r.item is not None]
    header = dict(head.header or {})
    header["id"] = entity_id
    header["version"] = head.version
    item_meta = await _page_item_meta(session, company_id, items)
    return {"list": header, "items": items, "total": total, "version": head.version,
            "item_meta": item_meta}


async def _page_item_meta(session: AsyncSession, company_id, page_items: list[dict]) -> dict:
    """Catalog metadata for exactly the ids on this page, keyed by entity_id: the flattened item state
    (attributes lifted to the top level, matching the bulk item-metadata shape) the detail view enriches
    each line with (measures, on-hand, item status). Off-page lines are never touched. A page with no
    catalog-backed lines returns an empty map, and the UI degrades to each line's stored values."""
    ids = list(dict.fromkeys(
        (li.get("item_id") or li.get("entity_id"))
        for li in page_items
        if (li.get("item_id") or li.get("entity_id"))
    ))
    if not ids:
        return {}
    rows = (await session.execute(
        select(Projection.entity_id, Projection.state)
        .where(Projection.company_id == company_id,
               Projection.entity_type == "item",
               Projection.entity_id.in_(ids))
    )).all()
    meta: dict = {}
    for eid, state in rows:
        flat = dict(state or {})
        # Lift attributes.* to the top level so item_measure_meta reads pieces and friends directly,
        # exactly as the bulk item-metadata read does; never overwrite a core field.
        for k, v in (flat.pop("attributes", None) or {}).items():
            flat.setdefault(k, v)
        flat["id"] = eid
        meta[eid] = flat
    return meta


@lists_router.post("")
async def create_list(
    payload: ListCreatePayload,
    company_id: str = Depends(get_current_company_id),
    _: None = require_permission("edit_documents"),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    company = await session.get(Company, company_id)
    ref_id = payload.ref_id or next_doc_ref(company, list_sequence_key(payload.list_type))
    entity_id = f"list:{ref_id}"

    # Uniqueness check
    existing = await session.get(Projection, {"company_id": company_id, "entity_id": entity_id})
    if existing is not None:
        raise HTTPException(status_code=409, detail=f"List number '{ref_id}' already exists")

    data = payload.model_dump(exclude_none=True)
    data["ref_id"] = ref_id
    data.setdefault("currency", company.settings.get("currency", "USD"))
    await _assert_no_draft_items(
        session, company_id,
        (li.get("item_id") or li.get("entity_id") for li in data.get("line_items") or []),
    )
    entry = await _emit_list(session, company_id, entity_id, "list.created", data, user, payload.idempotency_key)
    await session.commit()
    return {"event_id": entry.id, "id": entity_id}


@lists_router.patch("/{entity_id}")
async def patch_list(
    entity_id: str,
    payload: ListPatch,
    company_id: str = Depends(get_current_company_id),
    _: None = require_permission("edit_documents"),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    # Locked load so the version check and the emit are one atomic compare-and-set: two concurrent
    # patches cannot both read version N, both pass the check, and both write (the second clobbering
    # the first). The second waits, re-reads the advanced version, and its stale expected_version fails.
    row = await _get_list_for_update(session, company_id, entity_id)
    if row.state.get("status") != "draft":
        raise HTTPException(status_code=409, detail="Cannot edit non-draft list")
    # Replacing line_items is a read-modify-write over the whole array: two concurrent editors (or a
    # scan and a line edit) would each save their own full array and the second would silently drop the
    # first's lines. Require expected_version for it so the stale writer is rejected; scalar-only patches
    # touch independent fields and stay backward compatible without a version.
    _new_lines = (payload.fields_changed.get("line_items") or {}).get("new")
    if isinstance(_new_lines, list) and payload.expected_version is None:
        raise HTTPException(status_code=409, detail="Reload the list to get its latest version before saving line changes")
    if payload.expected_version is not None and row.version != payload.expected_version:
        raise HTTPException(status_code=409, detail="This list was changed by someone else; reload to get the latest before saving")
    try:
        _validate_shipment_values({f: (c or {}).get("new")
                                   for f, c in payload.fields_changed.items()})
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    if isinstance(_new_lines, list):
        _normalize_line_item_ids(_new_lines)  # keep the item link the editable UI sends as entity_id
        _existing = {li.get("item_id") for li in row.state.get("line_items") or []}
        await _assert_no_draft_items(
            session, company_id,
            {li.get("item_id") for li in _new_lines} - _existing,
        )
    # expected_version is a concurrency guard, not list data - it never enters the event payload.
    entry = await _emit_list(session, company_id, entity_id, "list.updated",
                             payload.model_dump(exclude_none=True, exclude={"expected_version"}),
                             user, payload.idempotency_key)
    await session.commit()
    # entry.id is the list's new version (the projection version tracks the latest entry id), so the
    # client refreshes its cached version from here and its next save pins the value it just wrote.
    return {"event_id": entry.id, "version": entry.id}


class ListLinePagePatch(BaseModel):
    line_items: list[dict] = Field(default_factory=list)
    offset: int = 0
    original_count: int | None = None
    expected_version: int | None = None


def _line_identity(li: dict) -> str | None:
    """The stable identity of a catalog-backed line, or None for a free-text line that carries none."""
    return li.get("item_id") or li.get("entity_id")


@lists_router.patch("/{entity_id}/line-page")
async def patch_list_line_page(
    entity_id: str,
    payload: ListLinePagePatch,
    company_id: str = Depends(get_current_company_id),
    _: None = require_permission("edit_documents"),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Save one page of a list's lines without scraping or re-sending the whole array. Under the same
    row lock and optimistic-version guard as a full save, the submitted page REPLACES the stored
    window [offset:offset+original_count] the client originally loaded. Relative to that window a
    shorter page truncates (deletes) tail rows and a longer one inserts; off-window rows are left
    byte-identical and totals are recomputed from the full merged array. Omitting original_count means
    the window is exactly the submitted page's own length (a pure in-place replace that never drops
    off-window rows), so a delete or insert must carry the loaded window length, which the editor
    always sends."""
    row = await _get_list_for_update(session, company_id, entity_id)
    if row.state.get("status") != "draft":
        raise HTTPException(status_code=409, detail="Cannot edit non-draft list")
    if payload.expected_version is None:
        raise HTTPException(status_code=409, detail="Reload the list to get its latest version before saving line changes")
    if row.version != payload.expected_version:
        raise HTTPException(status_code=409, detail="This list was changed by someone else; reload to get the latest before saving")

    page = payload.line_items
    if len(page) > _PAGE_LIMIT_MAX:
        raise HTTPException(status_code=400, detail=f"A saved page cannot exceed {_PAGE_LIMIT_MAX} lines")
    _normalize_line_item_ids(page)
    stored = list(row.state.get("line_items") or [])
    offset = payload.offset
    if offset < 0:
        raise HTTPException(status_code=400, detail="offset must be zero or greater")
    # The window the client loaded is [offset:offset+original_count]; the page replaces exactly it.
    # When original_count is omitted the window is the submitted page's own length, so a bare
    # {page, offset} save is a pure in-place replace that leaves every off-window row intact. A
    # delete or insert changes the window size and so must send original_count (the editor does); the
    # UI proxy applies the same len(page) default, keeping the two layers in lockstep.
    original_count = payload.original_count
    if original_count is None:
        original_count = len(page)
    elif original_count < 0:
        raise HTTPException(status_code=400, detail="original_count must be zero or greater")
    # A submitted row carrying an id must land on the position holding that same id: this catches a
    # page saved against a shifted array. Free-text rows carry no id and overwrite by position.
    for i, incoming in enumerate(page):
        pos = offset + i
        if pos < len(stored):
            incoming_id = _line_identity(incoming)
            stored_id = _line_identity(stored[pos])
            if incoming_id is not None and stored_id is not None and incoming_id != stored_id:
                raise HTTPException(
                    status_code=409,
                    detail="This list was changed by someone else; reload to get the latest before saving")

    # A draft item is not stock and must never reach a list, on this path as on the full save.
    _existing = {_line_identity(li) for li in stored}
    await _assert_no_draft_items(
        session, company_id,
        {_line_identity(li) for li in page} - _existing,
    )

    # Slice-splice: replace exactly the originally-loaded window. A shorter page truncates, a longer
    # one inserts; positional overwrite/append could never delete a tail row.
    merged = stored[:offset] + list(page) + stored[offset + original_count:]

    entry = await _emit_list(
        session, company_id, entity_id, "list.updated",
        {"fields_changed": {"line_items": {"new": merged}}}, user)
    await session.commit()
    return {"event_id": entry.id, "version": entry.id}


@lists_router.post("/{entity_id}/finalize")
async def finalize_list(
    entity_id: str,
    company_id: str = Depends(get_current_company_id),
    _: None = require_permission("edit_documents"),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Lock a draft list (draft -> finalized). The single draft->open transition for every type.

    Type-specific on-finalize (from the behaviour registry): a quotation records `sent_at`, a
    transfer `issued_at`, an audit freezes each line's on-hand snapshot (for variance + a stable
    On-hand column while counting). Counting / terminal actions happen in the finalized stage.
    """
    # Lock the projection: freeze_onhand rebuilds line_items (read-modify-write), and the status guard
    # must be a compare-and-set against a concurrent scan/patch so only one draft->finalized wins.
    row = await _get_list_for_update(session, company_id, entity_id)
    state = row.state
    if state.get("status") != DRAFT:
        raise HTTPException(status_code=409, detail="Only a draft list can be finalized")
    lt = state.get("list_type") or DEFAULT_LIST_TYPE
    milestone = behavior(lt).finalize_milestone
    now = datetime.now(timezone.utc).isoformat()
    data: dict = {"status": FINALIZED, "finalized_at": now}
    if milestone == "freeze_onhand":
        # An audit counts each inventory item once, so its manifest is a set keyed by item_id. Collapse
        # duplicate item_id lines at the gateway into counting: a single item record has one physical
        # count, so two lines would leave the second unreachable when checking off (scan always matches
        # the first) and double-count at Adjust. Lines without an item_id are kept as-is.
        src = [dict(l) for l in (state.get("line_items") or [])]
        _normalize_line_item_ids(src)  # heal legacy entity_id-only lines so dedup + freeze find the item
        seen: set[str] = set()
        lines = []
        for l in src:
            key = l.get("item_id")
            if key:
                if key in seen:
                    continue
                seen.add(key)
            lines.append(l)
        for l in lines:
            item = await session.get(Projection, {"company_id": company_id, "entity_id": l.get("item_id")})
            l["on_hand"] = float(item.state.get("quantity") or 0) if (item and item.entity_type == "item") else 0.0
        data["line_items"] = lines
    elif milestone:
        data[milestone] = now
    entry = await _emit_list(session, company_id, entity_id, "list.finalized", data, user)
    await session.commit()
    return {"event_id": entry.id, "status": FINALIZED}


@lists_router.post("/{entity_id}/reserve-lines")
async def reserve_list_lines(
    entity_id: str,
    body: ReserveLinesRequest,
    company_id: str = Depends(get_current_company_id),
    _: None = require_permission("fulfill_documents"),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Set selected lines reserved/available on a draft or finalized list of any type (ledger-neutral)."""
    row = await _get_list(session, company_id, entity_id)
    if row.state.get("status") not in (DRAFT, FINALIZED):
        raise HTTPException(status_code=409, detail=f"Cannot reserve on a list in status '{row.state.get('status')}'")
    return await _reserve_lines_impl(row, entity_id, body.new_status, body.line_entity_ids, user, session)


@lists_router.post("/{entity_id}/revert-to-draft")
async def revert_list_to_draft(
    entity_id: str,
    payload: DocRevertBody = DocRevertBody(),
    company_id: str = Depends(get_current_company_id),
    _: None = require_permission("finalize_documents"),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Go back from finalized to draft, allowed only before a terminal action has run (GDR 2c)."""
    row = await _get_list(session, company_id, entity_id)
    if row.state.get("status") != FINALIZED:
        raise HTTPException(status_code=409,
                            detail="Only a finalized list (before its terminal action) can be reverted to draft")
    event_data: dict = {"status": DRAFT, "reverted_by": str(user.id)}
    if payload.reason:
        event_data["reason"] = payload.reason
    entry = await _emit_list(session, company_id, entity_id, "list.reverted", event_data, user)
    await session.commit()
    return {"event_id": entry.id}


@lists_router.post("/{entity_id}/void")
async def void_list(
    entity_id: str,
    payload: ListVoidBody = ListVoidBody(),
    company_id: str = Depends(get_current_company_id),
    _: None = require_permission("edit_documents"),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    row = await _get_list(session, company_id, entity_id)
    status = row.state.get("status")
    if status == VOID:
        raise HTTPException(status_code=409, detail="Already voided")
    if status == CLOSED:
        raise HTTPException(status_code=409, detail="Cannot void a closed list; undo its terminal action first")
    entry = await _emit_list(session, company_id, entity_id, "list.voided",
                             payload.model_dump(exclude_none=True), user, payload.idempotency_key)
    await session.commit()
    return {"event_id": entry.id}


@lists_router.delete("/{entity_id}")
async def delete_list(
    entity_id: str,
    company_id: str = Depends(get_current_company_id),
    _: None = require_permission("delete_documents"),
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
    _: None = require_permission("edit_documents"),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    row = await _get_list(session, company_id, entity_id)
    state = row.state
    lt = state.get("list_type") or DEFAULT_LIST_TYPE
    if payload.target_type not in ("invoice", "memo"):
        raise HTTPException(status_code=422, detail="target_type must be 'invoice' or 'memo'")
    if terminal_action(lt, f"convert-{payload.target_type}") is None:
        raise HTTPException(status_code=409,
                            detail=f"{behavior(lt).label} lists cannot be converted to a sales document")
    if state.get("status") != FINALIZED:
        raise HTTPException(status_code=409, detail="Finalize the quotation before converting it")

    # Reservation ownership moves with the conversion: lines this list reserved are
    # re-stamped to the new document below; lines reserved elsewhere block it here.
    to_transfer, _conflicts = await _scan_reserved_lines(
        session, company_id, entity_id,
        [li.get("item_id") or li.get("entity_id") or "" for li in state.get("line_items") or []],
    )
    if _conflicts:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "; ".join(c["message"] for c in _conflicts),
                "conflicts": _conflicts,
            },
        )

    company = await session.get(Company, company_id)
    ref = next_doc_ref(company, payload.target_type)
    new_doc_id = f"doc:{ref}"
    new_data = {k: v for k, v in state.items()
                if k not in {"status", "result", "entity_type", "list_type", "finalized_at", "sent_at", "accepted_at"}}
    new_data.update({"doc_type": payload.target_type, "ref_id": ref, "source_list_id": entity_id, "status": "draft"})
    await emit_event(
        session, company_id=company_id, entity_id=new_doc_id, entity_type="doc",
        event_type="doc.created", data=new_data, actor_id=user.id, location_id=None,
        source="api", idempotency_key=str(uuid.uuid4()), metadata_={},
    )
    for li_eid in to_transfer:
        await emit_event(
            session, company_id=company_id, entity_id=li_eid, entity_type="item",
            event_type="item.status.set",
            data={"new_status": "reserved", "source_doc_id": new_doc_id, "doc_number": ref},
            actor_id=user.id, location_id=None, source="reservation",
            idempotency_key=str(uuid.uuid4()), metadata_={"doc_id": new_doc_id},
        )
    entry = await _emit_list(session, company_id, entity_id, "list.closed",
                             {"result": "converted", "converted_to": new_doc_id,
                              "converted_to_type": payload.target_type}, user)
    await session.commit()
    return {"event_id": entry.id, "target_doc_id": new_doc_id}


@lists_router.post("/{entity_id}/duplicate")
async def duplicate_list(
    entity_id: str,
    company_id: str = Depends(get_current_company_id),
    _: None = require_permission("edit_documents"),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    row = await _get_list(session, company_id, entity_id)
    state = row.state
    company = await session.get(Company, company_id)
    ref_id = next_doc_ref(company, list_sequence_key(state.get("list_type")))
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
    _: None = require_permission("edit_documents"),
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
            "created_at": datetime.now(timezone.utc).isoformat(),
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
    _: None = require_permission("edit_documents"),
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
        data={"list_id": entity_id, "note_id": note_id, "note": payload.note.strip(), "updated_at": datetime.now(timezone.utc).isoformat()},
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
    _: None = require_permission("edit_documents"),
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
    _: None = require_permission("edit_documents"),
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
    _: None = require_permission("edit_documents"),
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


async def _validate_revert_entity_ids_subset(
    session: AsyncSession, company_id, entity_id: str, doc_state: dict, line_entity_ids: list[str],
) -> None:
    """Revert-only guard: an id is owned by this doc if it is a line_items row OR, for a
    memo, a cross-lot sibling this memo currently owns as its status document.

    A memo line whose quantity exceeds its bound lot draws siblings from other lots of the
    same SKU; fulfill stamps each drawn lot status_doc_id==this memo but the sibling is not
    a line_items row. Revert is the memo's own settlement workflow, so it must accept those
    siblings to return them to stock. The union arm applies ONLY to memos; every other doc
    type keeps the line_items-only universe, so an invoice/PO revert is unchanged. The
    shared _validate_line_entity_ids_subset (fulfill, reserve) is deliberately left as-is."""
    doc_eids: set[str] = {
        li.get("entity_id") or li.get("item_id") or ""
        for li in doc_state.get("line_items", [])
    } - {""}
    if doc_state.get("doc_type") == "memo":
        doc_eids |= {p.entity_id for p in await _memo_allocation_items(session, company_id, entity_id)}
    foreign = set(line_entity_ids) - doc_eids
    if foreign:
        raise HTTPException(
            status_code=422,
            detail=f"Item IDs not linked to this document: {', '.join(sorted(foreign))}",
        )


async def _plan_span_draws(session, company_id, primary_proj, needed: float, exclude: set, owner_entity_id: str = ""):
    """Plan a cross-lot draw of ``needed`` units for a splittable SKU.

    Consumes the line's bound (primary) lot first - so the doc line's own parcel is
    always marked fulfilled - then draws the shortfall from the SKU's other available
    lots in the effective pick order (FIFO/FEFO/LIFO, resolved from the item's
    pick_method / company inventory_method). COGS is each drawn lot's own cost
    (specific identification by lot), so the order yields FIFO-cost / LIFO-cost.

    A lot reserved BY ``owner_entity_id`` is a candidate too: this document's own hold is
    being converted to a real draw ("set as shipped" on a reserved line). Lots reserved by
    another document stay excluded - that is the fulfil-time exclusivity point.

    Returns ``[(lot_proj, take_qty, is_full)]`` covering ``needed``, or None when the
    SKU's total available stock (minus already-committed lots) is still short.
    """
    from celerp.services.pick import plan_lot_draws, resolve_pick_method
    from celerp.models.company import Company
    sku = str(primary_proj.state.get("sku") or "").strip()
    company = await session.get(Company, company_id)
    company_settings = (company.settings or {}) if company else {}
    method = resolve_pick_method(primary_proj.state, company_settings)
    rows = (await session.execute(select(Projection).where(
        Projection.company_id == company_id, Projection.entity_type == "item"))).scalars().all()
    lots = [r for r in rows
            if str(r.state.get("sku") or "").strip() == sku
            and ((r.state.get("status") or "available") == "available"
                 or (r.state.get("status") == "reserved" and r.state.get("status_doc_id") == owner_entity_id))
            and float(r.state.get("quantity") or 0) > 1e-9
            and r.entity_id not in exclude]
    by_id = {l.entity_id: l for l in lots}
    if primary_proj.entity_id not in by_id:
        return None

    def _d(p):
        return {"entity_id": p.entity_id,
                "quantity": float(p.state.get("quantity") or 0),
                "created_at": p.created_at.isoformat() if p.created_at else "",
                "expires_at": p.state.get("expires_at")}
    others = [_d(l) for l in lots if l.entity_id != primary_proj.entity_id]
    draws, short_qty = plan_lot_draws(_d(primary_proj), needed, others, method)
    if short_qty > 1e-9:
        return None  # truly short across all lots
    return [(by_id[lot["entity_id"]], take, is_full) for lot, take, is_full in draws]


def _plan_line_carve(line: dict, parcel_state: dict, unit_map: dict) -> dict | None:
    """Plan how to carve the invoiced portion off a parent parcel for an on-page split.

    Returns the child's split measures (quantity plus the weight/pieces the parcel is
    sold by), or None when the line takes the whole parcel (a full or over-invoiced
    line needs no split). Pure computation shared by fulfill and reserve so the carve
    rule lives once.
    """
    line_qty = float(line.get("quantity") or 0)
    available = float(parcel_state.get("quantity", 0))
    if not (line_qty + 1e-9 < available):
        return None
    sell_by = parcel_state.get("sell_by")
    # An explicit line measure is authoritative and is validated by split_off_child (which
    # rejects a weight/pieces that disagrees with child_qty). Only when the line omits it
    # do we derive it: for a parcel sold BY that measure the quantity IS the measure.
    line_weight = line.get("weight")
    line_pieces = line.get("pieces")
    return {
        "child_qty": line_qty,
        "child_weight": line_weight if line_weight is not None
        else (line_qty if is_weight_unit(sell_by, unit_map) else None),
        "child_pieces": line_pieces if line_pieces is not None
        else (line_qty if is_pieces_unit(sell_by, unit_map) else None),
    }


async def _apply_split_plan(
    session, *, company_id, cid, uid, entity_id: str, state: dict,
    split_plan: dict[str, dict], fetched: dict[str, Projection], source: str,
) -> dict[str, tuple[str, str]]:
    """Carve each planned child off its parent, retarget the doc lines to the child, and
    emit the doc.updated line-items change. Returns ``{parent_eid: (child_eid, child_sku)}``.

    Emits no status/transition event - each caller applies its own transition afterward
    (fulfill emits item.fulfilled; reserve retargets its item.status.set), so fulfill's
    semantics never leak into reserve. The parent-projection FOR UPDATE lock that keeps
    two concurrent carves of one parcel from losing an update lives in split_off_child,
    the single carve primitive both paths share.
    """
    from celerp_inventory.routes import split_off_child

    remap: dict[str, tuple[str, str]] = {}
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
        remap[parent_eid] = (child_eid, child_sku)
        fetched[child_eid] = await session.get(
            Projection, {"company_id": company_id, "entity_id": child_eid})
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
        actor_id=uid, location_id=None, source=source,
        idempotency_key=str(uuid.uuid4()), metadata_={},
    )
    state["line_items"] = new_line_items
    return remap


@router.post("/{entity_id}/fulfill-lines")
async def fulfill_lines(
    entity_id: str,
    body: FulfillLinesRequest,
    company_id: str = Depends(get_current_company_id),
    _: None = require_permission("fulfill_documents"),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Fulfill specific line items by entity_id. Valid for memo and invoice docs only.

    Inbound doc types (bill, consignment_in) must use POST /receive instead.
    """
    row = await _get_doc(session, company_id, entity_id, for_update=True)
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
    # Line indices are captured before any split remap: _apply_split_plan rewrites
    # lines in place, so the index is the stable key the finalize JE's per-line
    # COGS allocation snapshot is also keyed by.
    line_qty_by_eid: dict[str, float] = {}
    line_by_eid: dict[str, dict] = {}
    line_index_by_eid: dict[str, int] = {}
    for _idx, li in enumerate(state.get("line_items", [])):
        _eid = li.get("entity_id") or li.get("item_id") or ""
        if _eid:
            line_qty_by_eid[_eid] = float(li.get("quantity") or 0)
            line_by_eid[_eid] = li
            line_index_by_eid[_eid] = _idx

    _unit_map = await _get_unit_map(session, company_id)

    errors: list[str] = []          # 422: item not found / not available
    blocked: list[str] = []         # 409: stock shortage / non-splittable partial
    to_fulfill: list[str] = []
    service_eids: set[str] = set()  # service lines: rendered, not picked from stock
    split_plan: dict[str, dict] = {}  # parent_eid -> child measures (partial draws)
    fetched: dict[str, Projection] = {}
    span_consumed: set[str] = set()  # extra lots pulled in by cross-lot spanning
    fulfilled_line_eids: set[str] = set()  # accepted stock lines, by pre-remap line eid
    for item_eid in body.line_entity_ids:
        item_proj = await session.get(Projection, {"company_id": company_id, "entity_id": item_eid})
        if item_proj is None:
            errors.append(f"{item_eid}: item not found")
            continue
        # Non-stock lines (service or freight charge) have no physical stock: mark them done
        # without any stock/availability guard.
        if is_non_stock_line(item_proj.state.get("inventory_type"), item_proj.state.get("sell_by")):
            service_eids.add(item_eid)
            continue
        item_status = item_proj.state.get("status", "")
        if item_status != "available":
            # A line reserved BY THIS document may be set as shipped directly - the reservation was
            # this doc's own hold, now converted to a real stock draw. A line reserved by ANOTHER
            # document is the exclusivity point and cannot be sent from here.
            if not (item_status == "reserved" and item_proj.state.get("status_doc_id") == entity_id):
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
            # Cross-lot spanning: a splittable product can draw the shortfall from other
            # lots of the same SKU (bound lot first, then FIFO/FEFO/LIFO). Each drawn lot
            # is fulfilled at its own cost (specific identification by lot).
            if splitting_allowed(item_proj.state):
                _draws = await _plan_span_draws(
                    session, company_id, item_proj, line_qty,
                    exclude=set(to_fulfill) | span_consumed,
                    owner_entity_id=entity_id,
                )
                if _draws is not None:
                    fulfilled_line_eids.add(item_eid)
                    for _lot, _take, _full in _draws:
                        _leid = _lot.entity_id
                        fetched[_leid] = _lot
                        to_fulfill.append(_leid)
                        span_consumed.add(_leid)
                        if not _full:
                            _lsb = _lot.state.get("sell_by")
                            split_plan[_leid] = {
                                "child_qty": _take,
                                "child_weight": _take if is_weight_unit(_lsb, _unit_map) else None,
                                "child_pieces": _take if is_pieces_unit(_lsb, _unit_map) else None,
                            }
                    continue
            blocked.append(
                f"{sku}: insufficient stock — invoiced {line_qty:g}, available {available:g}"
            )
            continue
        # Split-on-fulfill is blocked only when splitting is explicitly disabled.
        # A missing/None allow_splitting (e.g. older imports) is treated as splittable.
        if line_qty + 1e-9 < available and not splitting_allowed(item_proj.state):
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
        fulfilled_line_eids.add(item_eid)
        # Partial draw of a splittable parcel: split off the invoiced amount as a child
        # and fulfill that; the mother keeps the remainder. A full or over-invoiced line
        # takes the whole parcel and plans no carve.
        _carve = _plan_line_carve(_line, item_proj.state, _unit_map)
        if _carve is not None:
            split_plan[item_eid] = _carve

    # A shortage or a non-splittable partial prohibits the whole fulfill.
    if blocked:
        raise HTTPException(status_code=409, detail="Cannot fulfill: " + "; ".join(blocked))

    if errors and not to_fulfill and not service_eids:
        raise HTTPException(status_code=422, detail={"errors": errors})

    if not to_fulfill and not service_eids:
        raise HTTPException(status_code=422, detail="No fulfillable items in the provided line_entity_ids")

    now = datetime.now(timezone.utc).isoformat()
    cid = uuid.UUID(str(company_id))
    uid = user.id

    # Split partial draws: carve the invoiced amount off each parcel as a child,
    # retarget fulfillment to the child, and rewrite the doc line to reference it.
    if split_plan:
        remap = await _apply_split_plan(
            session, company_id=company_id, cid=cid, uid=uid, entity_id=entity_id,
            state=state, split_plan=split_plan, fetched=fetched, source="fulfillment",
        )
        for parent_eid, (child_eid, _child_sku) in remap.items():
            to_fulfill[to_fulfill.index(parent_eid)] = child_eid
            del fetched[parent_eid]

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
                "doc_number": state.get("doc_number") or state.get("ref_id") or "",
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

    # True up recognized COGS against reality: the finalize JE recognized each line at
    # the lots it expected to draw; the lots actually drawn just now can cost more or
    # less (a sibling lot was sold in between, costs changed). The difference for the
    # lines fulfilled here posts as one adjustment JE dated to this fulfillment. Docs
    # whose finalize JE carries no allocation snapshot have no recognized basis and
    # get no adjustment.
    if doc_type == "invoice" and to_fulfill:
        _recognized_allocs = await auto_je.recognized_cogs_allocations(session, company_id, entity_id)
        if _recognized_allocs is not None:
            _batch_indices = sorted({
                line_index_by_eid[_eid] for _eid in fulfilled_line_eids if _eid in line_index_by_eid
            })
            _recognized = sum(
                float((_recognized_allocs.get(str(_idx)) or {}).get("amount") or 0)
                for _idx in _batch_indices
            )
            _delta = round(total_cogs - _recognized, 2)
            if abs(_delta) > 0.005:
                # The JE identity carries the batch's line indexes: lines of one
                # cycle can be fulfilled in separate calls, and each batch's delta
                # must post on its own JE. Retrying the same batch dedups on the
                # idempotency key; a different batch is a different key.
                _batch_tag = "l" + "-".join(str(_idx) for _idx in _batch_indices)
                await auto_je.create_for_doc_cogs_adjustment(
                    session,
                    company_id=cid,
                    user_id=uid,
                    doc_id=entity_id,
                    delta=_delta,
                    cycle_tag=f"fulfill-{int(state.get('revert_count') or 0)}:{_batch_tag}",
                    doc_number=state.get("doc_number") or state.get("ref_id") or entity_id,
                    ts=now,
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

    fulfilled_brief = _line_item_brief(line_items, to_fulfill)
    if all_statuses and all(s in ("memo_out", "sold") for s in all_statuses):
        doc_fulfillment_status = "fulfilled"
        doc_event_type = "doc.fulfilled"
        doc_event_data: dict = {
            "fulfilled_items": fulfilled_brief,
            "fulfilled_by": str(uid),
            "fulfilled_at": now,
            "strategy": "per_line",
            "total_cogs": total_cogs,
        }
    else:
        doc_fulfillment_status = "partial"
        doc_event_type = "doc.partially_fulfilled"
        # Lines on the doc not in this fulfill batch are still pending.
        unfulfilled_brief = _line_item_brief(
            line_items, [li.get("entity_id") or li.get("item_id") for li in line_items
                         if (li.get("entity_id") or li.get("item_id")) and (li.get("entity_id") or li.get("item_id")) not in fulfilled_eids])
        doc_event_data = {
            "fulfilled_items": fulfilled_brief,
            "unfulfilled_items": unfulfilled_brief,
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

    await session.commit()
    return {"fulfillment_status": doc_fulfillment_status, "fulfilled": to_fulfill}


async def _reverse_whole_lines(
    session,
    *,
    company_id,
    cid,
    uid,
    entity_id: str,
    state: dict,
    doc_type: str,
    to_revert: list[str],
    fetched: dict[str, Projection],
    returned_brief: list[dict] | None = None,
) -> str:
    """Reverse whole-line fulfillment: restore each lot to stock, recompute the doc's
    fulfillment status, log the doc-level revert, and void the COGS JE when an invoice
    lands fully unfulfilled. Emits events only - the caller owns the commit.

    Shared by revert-lines (Set as available on sold/memo lines) and reserve-lines
    (Set as reserved on a line this doc already shipped: reverse, then reserve).
    """
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
                "doc_number": state.get("doc_number") or state.get("ref_id") or "",
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

    # A revert is logged as a revert (naming the reverted items), regardless of whether the
    # doc lands fully unfulfilled or stays partially fulfilled. The doc status reflects what
    # remains; the activity entry reflects the action taken.
    reverted_brief = _line_item_brief(line_items, to_revert)
    doc_event_data = {
        "reversed_items": reverted_brief,
        "reversed_by": str(uid),
        "reason": "per_line_revert",
    }
    if returned_brief:
        # Part-returned lots: named separately from whole-line reverts, because the line
        # itself is still out (for the balance the customer kept).
        doc_event_data["partially_returned_items"] = returned_brief
    if any(s in ("memo_out", "sold") for s in all_statuses):
        doc_fulfillment_status = "partial"
        doc_event_type = "doc.partially_reverted"
    else:
        doc_fulfillment_status = "unfulfilled"
        doc_event_type = "doc.fulfillment_reversed"

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

    return doc_fulfillment_status


@router.post("/{entity_id}/revert-lines")
async def revert_lines(
    entity_id: str,
    body: RevertLinesRequest,
    company_id: str = Depends(get_current_company_id),
    _: None = require_permission("fulfill_documents"),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Revert fulfillment for specific line items. Valid for memo and invoice docs only.

    Inbound doc types (bill, consignment_in) must use DELETE /receive instead.
    """
    row = await _get_doc(session, company_id, entity_id, for_update=True)
    state = row.state
    if state.get("status") == "closed":
        raise HTTPException(status_code=409, detail="Cannot revert a closed memo; reopen it first.")
    doc_type = state.get("doc_type", "")

    if FULFILLABLE_STATUSES.get(doc_type) is None:
        raise HTTPException(status_code=422, detail=f"revert-lines is not supported for doc type: {doc_type}")

    await _validate_revert_entity_ids_subset(session, company_id, entity_id, state, body.line_entity_ids)

    if not body.line_entity_ids:
        raise HTTPException(status_code=422, detail="line_entity_ids must not be empty")

    _returned_qty = body.quantities or {}
    _unknown = [eid for eid in _returned_qty if eid not in body.line_entity_ids]
    if _unknown:
        raise HTTPException(
            status_code=422,
            detail=f"quantities names items that are not in line_entity_ids: {', '.join(sorted(_unknown))}",
        )

    errors: list[str] = []
    to_revert: list[str] = []
    # item_eid -> quantity coming back, for lines where only part of the lot returned.
    partial_plan: dict[str, float] = {}
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
        _sku = item_proj.state.get("sku", "")
        _qty_back = _returned_qty.get(item_eid)
        if _qty_back is not None:
            _on_hand = float(item_proj.state.get("quantity") or 0)
            if _qty_back <= 0:
                errors.append(f"{item_eid} ({_sku}): returned quantity must be greater than zero")
                continue
            if _qty_back > _on_hand + 1e-9:
                errors.append(
                    f"{item_eid} ({_sku}): cannot return {_qty_back:g} of {_on_hand:g} that went out"
                )
                continue
            if abs(_qty_back - _on_hand) > 1e-9:
                # Part of the lot is coming back; the rest stays with the customer.
                if item_status != "memo_out":
                    errors.append(
                        f"{item_eid} ({_sku}): only goods out on memo can be part-returned, "
                        f"this one is '{item_status}'"
                    )
                    continue
                fetched[item_eid] = item_proj
                partial_plan[item_eid] = float(_qty_back)
                continue
        fetched[item_eid] = item_proj
        to_revert.append(item_eid)

    if errors and not to_revert and not partial_plan:
        raise HTTPException(status_code=422, detail={"errors": errors})

    if not to_revert and not partial_plan:
        raise HTTPException(status_code=422, detail="No revertible items in the provided line_entity_ids")

    now = datetime.now(timezone.utc).isoformat()
    cid = uuid.UUID(str(company_id))
    uid = user.id

    # Partial returns first: split the returned amount off the lot that is out. The child
    # carries the returned goods and starts available (back in stock); the mother keeps the
    # remainder and stays memo_out, so what the customer still holds is never lost.
    returned_brief: list[dict] = []
    if partial_plan:
        from celerp_inventory.routes import split_off_child
        _unit_map = await _get_unit_map(session, company_id)
        for parent_eid, qty_back in partial_plan.items():
            parent_proj = fetched[parent_eid]
            _sb = parent_proj.state.get("sell_by") or ""
            _sku = parent_proj.state.get("sku", "")
            child_weight = qty_back if is_weight_unit(_sb, _unit_map) else (body.weights or {}).get(parent_eid)
            child_pieces = qty_back if is_pieces_unit(_sb, _unit_map) else (body.pieces or {}).get(parent_eid)
            try:
                child_eid, _child_sku = await split_off_child(
                    session, company_id=cid, user_id=uid, parent_proj=parent_proj,
                    child_qty=qty_back, child_weight=child_weight, child_pieces=child_pieces,
                )
            except ValueError as exc:
                raise HTTPException(
                    status_code=409,
                    detail=f"Cannot return part of {_sku}: {exc}",
                )
            returned_brief.append({"item_id": child_eid, "sku": _sku, "quantity": qty_back})

    doc_fulfillment_status = await _reverse_whole_lines(
        session, company_id=company_id, cid=cid, uid=uid, entity_id=entity_id, state=state,
        doc_type=doc_type, to_revert=to_revert, fetched=fetched, returned_brief=returned_brief,
    )

    await session.commit()
    return {
        "fulfillment_status": doc_fulfillment_status,
        "reverted": to_revert,
        # Lots that came back in part: the new in-stock parcel per part-returned line.
        "partially_returned": returned_brief,
    }


async def _reserve_lines_impl(row, entity_id, new_status, line_entity_ids, user, session) -> dict:
    """Set selected lines to ``reserved`` or ``available`` (ledger-neutral for available lines).

    All-or-nothing: every selected line is pre-validated first; if any line fails its guard the
    request commits nothing and returns 422 with the full per-line error list (GDR 2e). A
    ``reserved`` target requires the line ``available`` - or ``sold`` by THIS document, in which
    case the sale is reversed first (stock restored, COGS voided when the invoice lands fully
    unfulfilled - the same path Set as available uses) and the line is then reserved, atomically
    in one request. An ``available`` target requires the line ``reserved`` and owned by this
    document. Reserve stamps this document as owner; release clears the ownership stamp (emit
    without source_doc_id).

    Shared by the docs-router and lists-router wrappers - both bind their lines by item_id, so
    line resolution is uniform across surfaces. A list can never own a sold line (only docs
    fulfil), so the reverse-then-reserve branch is unreachable from the lists wrapper.
    """
    state = row.state
    _validate_line_entity_ids_subset(line_entity_ids, state)
    if not line_entity_ids:
        raise HTTPException(status_code=422, detail="line_entity_ids must not be empty")

    # Each line keyed by the item parcel it references, so a partial reserve can carve
    # only the invoiced portion (the same measures the fulfill split uses).
    line_qty_by_eid: dict[str, float] = {}
    line_by_eid: dict[str, dict] = {}
    for li in state.get("line_items", []):
        _eid = li.get("entity_id") or li.get("item_id") or ""
        if _eid:
            line_qty_by_eid[_eid] = float(li.get("quantity") or 0)
            line_by_eid[_eid] = li
    unit_map = await _get_unit_map(session, row.company_id)

    errors: list[str] = []
    blocked: list[str] = []  # 409: partial reserve of a non-splittable parcel
    split_plan: dict[str, dict] = {}  # parent_eid -> child measures (partial reserves)
    projs: dict[str, Projection] = {}
    to_unship: dict[str, Projection] = {}  # sold by THIS doc: reverse the sale, then reserve
    for eid in line_entity_ids:
        proj = await session.get(Projection, {"company_id": row.company_id, "entity_id": eid})
        if proj is None:
            errors.append(f"{eid}: item not found")
            continue
        if is_non_stock_line(proj.state.get("inventory_type"), proj.state.get("sell_by")):
            errors.append(f"{eid} ({proj.state.get('sku', '')}): a non-stock line cannot be reserved")
            continue
        sku = proj.state.get("sku", "")
        item_status = proj.state.get("status", "")
        if new_status == "reserved":
            if item_status == "sold" and proj.state.get("status_doc_id") == entity_id:
                to_unship[eid] = proj
            elif item_status == "memo_out":
                errors.append(f"{eid} ({sku}): out on memo - use 'Set as available' to take it back first")
                continue
            elif item_status != "available":
                _owner = proj.state.get("status_doc_number")
                _where = f" by {_owner}" if _owner and proj.state.get("status_doc_id") != entity_id else ""
                errors.append(f"{eid} ({sku}): must be 'available' to reserve, is '{item_status}'{_where}")
                continue
        else:  # available (release)
            if item_status != "reserved":
                errors.append(f"{eid} ({sku}): only a reserved line can be set available, is '{item_status}'")
                continue
            if proj.state.get("status_doc_id") != entity_id:
                errors.append(f"{eid} ({sku}): reserved by another document")
                continue
        # A partially-invoiced line reserves only the invoiced portion: carve a child and
        # reserve that. A partial reserve of an explicitly non-splittable parcel is gated
        # exactly as partial fulfillment is. A full or over-invoiced line reserves whole.
        if new_status == "reserved" and eid not in to_unship:
            _carve = _plan_line_carve(line_by_eid.get(eid, {}), proj.state, unit_map)
            if _carve is not None:
                if not splitting_allowed(proj.state):
                    blocked.append(
                        f"{sku}: invoiced {line_qty_by_eid.get(eid, 0):g} of "
                        f"{float(proj.state.get('quantity', 0)):g} but 'Allow Splitting' is off; "
                        f"enable splitting or reserve the full quantity")
                    continue
                split_plan[eid] = _carve
        projs[eid] = proj

    if blocked:
        raise HTTPException(status_code=409, detail="Cannot reserve: " + "; ".join(blocked))
    if errors:
        raise HTTPException(status_code=422, detail={"errors": errors})

    cid = uuid.UUID(str(row.company_id))
    if to_unship:
        # Take the shipped goods back into stock before reserving them - the reversal and the
        # reserve share this transaction, so a failure commits neither.
        await _reverse_whole_lines(
            session, company_id=row.company_id, cid=cid, uid=user.id, entity_id=entity_id,
            state=state, doc_type=state.get("doc_type", ""), to_revert=list(to_unship),
            fetched=to_unship,
        )
    # Carve the invoiced portion off each partial parcel and reserve the child instead of
    # the mother; the mother keeps its remainder available.
    remap: dict[str, tuple[str, str]] = {}
    if split_plan:
        remap = await _apply_split_plan(
            session, company_id=row.company_id, cid=cid, uid=user.id, entity_id=entity_id,
            state=state, split_plan=split_plan, fetched=projs, source="reservation",
        )
    doc_number = state.get("doc_number") or state.get("ref_id") or ""
    reserved_eids: list[str] = []
    for eid in line_entity_ids:
        target_eid = remap[eid][0] if eid in remap else eid
        reserved_eids.append(target_eid)
        # Reserve stamps this doc as owner (source_doc_id present); release omits it so
        # _stamp_status_doc clears the ownership stamp - a true handoff back to the pool.
        data: dict = {"new_status": new_status}
        if new_status == "reserved":
            data["source_doc_id"] = entity_id
            data["doc_number"] = doc_number
        await emit_event(
            session, company_id=cid, entity_id=target_eid, entity_type="item",
            event_type="item.status.set", data=data,
            actor_id=user.id, location_id=None, source="reservation",
            idempotency_key=str(uuid.uuid4()), metadata_={"doc_id": entity_id},
        )

    await session.commit()
    return {"new_status": new_status, "reserved": reserved_eids}


@router.post("/{entity_id}/reserve-lines")
async def reserve_lines(
    entity_id: str,
    body: ReserveLinesRequest,
    company_id: str = Depends(get_current_company_id),
    _: None = require_permission("fulfill_documents"),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Set selected lines reserved/available on an invoice or memo (ledger-neutral)."""
    row = await _get_doc(session, company_id, entity_id, for_update=True)
    doc_type = row.state.get("doc_type", "")
    allowed = RESERVABLE_DOC_STATUSES.get(doc_type)
    if allowed is None:
        raise HTTPException(status_code=422, detail=f"reserve-lines is not supported for doc type: {doc_type}")
    if row.state.get("status") not in allowed:
        raise HTTPException(status_code=409, detail=f"Cannot reserve on a {doc_type} in status '{row.state.get('status')}'")
    return await _reserve_lines_impl(row, entity_id, body.new_status, body.line_entity_ids, user, session)


class ReturnReceivedItem(BaseModel):
    sku: str
    quantity: float
    # Optional: bind the return to a specific physical lot. Under non-unique SKU a
    # credit-note line may carry item_id; when present it is authoritative (else the
    # documented LIFO-by-sku tiebreak applies).
    item_id: str | None = None


class ReceiveReturnPayload(BaseModel):
    items: list[ReturnReceivedItem]
    notes: str | None = None
    idempotency_key: str | None = None


@router.post("/{entity_id}/receive-return")
async def receive_return(
    entity_id: str,
    payload: ReceiveReturnPayload,
    company_id: str = Depends(get_current_company_id),
    _: None = require_permission("fulfill_documents"),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Receive returned goods on a credit note.

    Item values are resolved server-side (not trusted from the UI):
    - Case 1: CN has original_doc_id -> fetch original invoice, match by SKU, use its values.
    - Case 2: No original_doc_id -> query sold inventory by SKU (LIFO), use those values.
    Creates new inventory items (status=available) and a reversing COGS JE.
    """
    from celerp_inventory.routes import flatten_item

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
    item_by_id: dict[str, dict] = {}
    for r in item_rows:
        flat = flatten_item(r.state, r.entity_id)
        item_by_id[r.entity_id] = flat
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

    # --- Allocate a fresh barcode per returned parcel ---
    # A returned item is a NEW physical lot and must never inherit the sold lot's or
    # the invoice line's barcode - that barcode still belongs to the sold item and
    # reusing it would collide. Allocate the whole batch under the code-namespace lock
    # after validation and before any item.created event, so concurrent returns mint
    # distinct barcodes; the lock is held until this request commits.
    from celerp_inventory.services import allocate_internal_codes
    _return_barcodes = await allocate_internal_codes(session, company_id, len(payload.items))

    # --- Create returned inventory items ---
    now = datetime.now(timezone.utc).isoformat()
    total_cogs = 0.0
    received_items = []

    _CORE_KEYS = frozenset({
        "id", "entity_id", "status", "quantity", "created_at", "updated_at",
        "location_id", "location_name", "source_doc_id",
    })

    for _ridx, it in enumerate(payload.items):
        # Prefer the exact physical lot when the credit-note line carries an item_id
        # (SKUs can repeat across lots, so item_id/barcode is the authoritative bind).
        # Else fall back to the documented LIFO-by-sku tiebreak (most-recent sold lot),
        # then to the invoice line item data when no sold record exists.
        if it.item_id and it.item_id in item_by_id:
            ref = item_by_id[it.item_id]
        elif it.sku in sold_map:
            ref = sold_map[it.sku][0]
        else:
            ref = {}
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
            "barcode": _return_barcodes[_ridx],
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
    _: None = require_permission("fulfill_documents"),
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

    now = datetime.now(timezone.utc).isoformat()
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
    _: None = require_permission("fulfill_documents"),
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

    now = datetime.now(timezone.utc).isoformat()

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
    _: None = require_permission("edit_documents"),
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
            "uploaded_at": datetime.now(timezone.utc).isoformat(),
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
    _: None = require_permission("edit_documents"),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    row = await _get_doc(session, company_id, entity_id)
    f = _get_doc_file(row.state.get("files", []), file_id)
    await emit_event(
        session,
        company_id=company_id,
        entity_id=entity_id,
        entity_type="doc",
        event_type="doc.file_tagged",
        data={"entity_id": entity_id, "entity_type": "doc", "file_id": file_id, "document_tag": document_tag, "filename": f.get("filename")},
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
    _: None = require_permission("edit_documents"),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    row = await _get_doc(session, company_id, entity_id)
    f = _get_doc_file(row.state.get("files", []), file_id)
    await emit_event(
        session,
        company_id=company_id,
        entity_id=entity_id,
        entity_type="doc",
        event_type="doc.file_description_updated",
        data={"entity_id": entity_id, "entity_type": "doc", "file_id": file_id, "description": description, "filename": f.get("filename")},
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
    _: None = require_permission("edit_documents"),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    row = await _get_doc(session, company_id, entity_id)
    f = _get_doc_file(row.state.get("files", []), file_id)
    entry = await emit_event(
        session,
        company_id=company_id,
        entity_id=entity_id,
        entity_type="doc",
        event_type="doc.file_deleted",
        data={"entity_id": entity_id, "entity_type": "doc", "file_id": file_id, "filename": f.get("filename")},
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
# Type-specific list activities, all on lists_router (one lifecycle, type strategies):
#   - audit creation pre-seeds a draft manifest from a location;
#   - /scan dispatches on (list_type, status) — draft always ADDS a line; finalized is
#     type-specific (audit records presence, transfer receives, quotation is locked);
#   - terminal actions close a finalized list: convert (quotation, above), adjust (audit),
#     receive (transfer). Adjust + undo route stock through inventory events + auto_je.
# See context/2026-0617-unified-lists-lifecycle-plan.md.
# ---------------------------------------------------------------------------

# Physical item types an audit counts (services / non-stocked have no stock to count).
_AUDIT_STOCK_TYPES = frozenset({"stocked", "component"})


class AuditCreateBody(BaseModel):
    location_id: str
    idempotency_key: str | None = None


# A single scan run submits its whole accumulated comma list at once. These bounds cap one
# submission by count AND by byte size, so a pathological paste can neither build an unbounded
# in-memory line set behind one write nor bloat the persisted replay data (below).
MAX_SCAN_BATCH = 200            # max codes in one submitted run
MAX_CODE_LEN = MAX_SCAN_CODE_LEN  # max chars in a single scanned code: the inventory scan-code ceiling (max sku/barcode length)
MAX_RUN_KEY_LEN = 64          # max chars in a client-supplied idempotency run key
_MAX_SCAN_BODY_LEN = MAX_SCAN_BATCH * MAX_CODE_LEN + (MAX_SCAN_BATCH - 1) * len(", ")  # comma-space-joined upper bound
# Recent scan runs retained on the list for retry dedup (post-commit refresh failure).
_MAX_SCAN_RUNS = 50


class ListScanBody(BaseModel):
    barcode: str = Field(max_length=_MAX_SCAN_BODY_LEN)
    price_list: str | None = None  # money lists price the added line from this list (default Retail)
    # one Add click; a retry reuses it so a re-submit never re-adds (bounded so replay data stays small)
    run_key: str | None = Field(default=None, max_length=MAX_RUN_KEY_LEN)


class ListCountBody(BaseModel):
    counted_qty: float | None = None  # None clears the count (line skipped on adjust)


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


async def _get_audit(session: AsyncSession, company_id, entity_id: str, *, for_update: bool = False) -> Projection:
    row = await (_get_list_for_update if for_update else _get_list)(session, company_id, entity_id)
    if row.state.get("list_type") != "audit":
        raise HTTPException(status_code=404, detail="Audit not found")
    return row


async def _set_list_fields(session, company_id, entity_id, user, fields: dict):
    """Emit list.updated carrying the given top-level field changes (used by every type)."""
    fc = {k: {"new": v} for k, v in fields.items()}
    return await _emit_list(session, company_id, entity_id, "list.updated", {"fields_changed": fc}, user)


def _ambiguous_sku_detail(code: str) -> str:
    """The one message for an ambiguous SKU: never silently pick a lot - the operator disambiguates."""
    return f"Multiple items share SKU '{code}'. Scan its barcode or pick a specific lot."


def _normalize_line_item_ids(lines: list) -> None:
    """In-place: ensure every line carries `item_id`. The editable list UI sends the item's id in
    `entity_id` (what celerpFillRow + the hidden field carry), but scan check-off, finalize dedup and
    the on-hand freeze all key on `item_id` (as `_scan_line_from_item` and the LineItem model set it).
    Without this, any autosave on an editable draft strips the item link and those operations break."""
    for l in lines:
        if isinstance(l, dict) and l.get("entity_id") and not l.get("item_id"):
            l["item_id"] = l["entity_id"]


def _scan_line_from_item(item: Projection, list_type: str, price_list: str | None,
                         price_config: tuple[list[dict], str, str] | None = None) -> dict:
    """Build a new line for a scanned item. Money lists carry a unit_price resolved from the chosen
    price list via the shared resolver, on flattened state so derived lists price correctly."""
    st = item.state
    line = {"item_id": item.entity_id, "sku": st.get("sku"), "name": st.get("name"),
            "description": st.get("name"), "barcode": st.get("barcode")}
    if list_type == "audit":
        # System qty snapshot for the Qty column; on_hand is frozen separately at finalize.
        line["quantity"] = float(st.get("quantity") or 0)
    else:
        line["quantity"] = 1
        if is_money_list(list_type):
            from celerp_inventory.routes import flatten_item
            flat = flatten_item(st, item.entity_id, price_config=price_config)
            line["unit_price"] = resolve_price(flat, price_list or DEFAULT_PRICE_LIST_NAME)
    return line


@lists_router.post("/audit")
async def create_audit_list(
    payload: AuditCreateBody,
    company_id=Depends(get_current_company_id),
    _: None = require_permission("edit_documents"),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Create a location-bound audit as a DRAFT manifest pre-seeded with the location's physical
    items. The manifest is reviewed/extended in draft (scan adds more); Finalize freezes each line's
    on-hand snapshot, then counting happens in the finalized stage."""
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
                      "barcode": st.get("barcode"), "quantity": float(st.get("quantity") or 0)})
    ref_id = next_doc_ref(company, "audit")
    entity_id = f"list:{ref_id}"
    if await session.get(Projection, {"company_id": company_id, "entity_id": entity_id}) is not None:
        raise HTTPException(status_code=409, detail=f"Audit number '{ref_id}' already exists")
    data = {"list_type": "audit", "location_id": payload.location_id, "status": DRAFT,
            "ref_id": ref_id, "line_items": lines, "adjust_count": 0,
            "currency": company.settings.get("currency", "USD")}
    entry = await _emit_list(session, company_id, entity_id, "list.created", data, user, payload.idempotency_key)
    await session.commit()
    return {"event_id": entry.id, "id": entity_id, "ref_id": ref_id, "line_count": len(lines)}


def _scan_run_fingerprint(codes: list[str], price_list: str | None) -> str:
    """A fixed-size digest identifying one scan batch: its ordered normalized codes and the price
    list they price against. Binds a run_key to the exact batch it acknowledged, so the same key
    reused for a different batch is caught rather than mis-replayed."""
    payload = json.dumps({"codes": codes, "price_list": price_list}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@lists_router.post("/{entity_id}/scan")
async def scan_list(
    entity_id: str, payload: ListScanBody,
    company_id=Depends(get_current_company_id), _: None = require_permission("edit_documents"), user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """One scan endpoint for every list type, dispatching on (list_type, status):

    - DRAFT (all types): always ADD a line (audit dedups its manifest + moves to top; money lists
      price the new line). No status change (GDR 2d) — building the list never finalizes it.
    - FINALIZED audit: record presence (audited_at), add + audit if not yet on the manifest.
    - FINALIZED transfer: scan-to-receive (records receipt on the line; the stock move is the
      Phase 4 seam).
    - FINALIZED quotation / any closed|void list: scanning is disabled (clear 409).
    """
    row = await _get_list_for_update(session, company_id, entity_id)
    state = row.state
    status = state.get("status")
    lt = state.get("list_type") or DEFAULT_LIST_TYPE
    # The scan bar accumulates codes client-side (Enter appends a comma, never a per-scan request) and
    # submits the whole run at once, so `barcode` is a comma-separated batch. Resolve and apply every
    # code against ONE in-memory copy of line_items and persist a single time; a code that fails to
    # resolve is collected in `failed` and reported, never aborting the codes that did resolve.
    codes = [c.strip() for c in (payload.barcode or "").split(",") if c.strip()]
    if not codes:
        raise HTTPException(status_code=422, detail="Empty scan")
    if len(codes) > MAX_SCAN_BATCH:
        raise HTTPException(status_code=422,
                            detail=f"Too many codes in one scan ({len(codes)}); the limit is {MAX_SCAN_BATCH}")
    if any(len(c) > MAX_CODE_LEN for c in codes):
        raise HTTPException(status_code=422,
                            detail=f"A scanned code exceeds {MAX_CODE_LEN} characters")
    if status not in (DRAFT, FINALIZED):
        raise HTTPException(status_code=409, detail="Cannot scan a closed or void list")
    scan_mode = behavior(lt).scan_finalized if status == FINALIZED else None
    if status == FINALIZED and scan_mode != "count":
        raise HTTPException(status_code=409, detail="This list is finalized; scanning is disabled for this type")

    # A retry of one Add click carries the same run_key AND the same batch still sitting in the field.
    # The key alone is not enough: if the response is lost, the user could edit the field to a new
    # batch and resubmit under the same key - replaying the old run would then silently drop the new
    # one. So each recorded run stores a fingerprint of its normalized codes + price_list; a matching
    # key with a matching fingerprint replays the recorded outcome (never re-adding the lines), while a
    # matching key with a DIFFERENT batch is a client error, rejected with 409 rather than mis-replayed.
    run_fp = _scan_run_fingerprint(codes, payload.price_list) if payload.run_key else None
    if payload.run_key:
        for rec in (state.get("scan_runs") or []):
            if rec.get("key") == payload.run_key:
                if rec.get("fp") != run_fp:
                    # Same key, different batch: a client bug (edited the field then resubmitted under
                    # a spent key). Reply with a code so the scan bar branches on it, not on message text.
                    from fastapi.responses import JSONResponse
                    return JSONResponse(status_code=409, content={
                        "code": "scan_run_conflict",
                        "detail": "This scan key was already used for a different batch"})
                return {"scanned": rec.get("scanned", 0), "results": [],
                        "failed": rec.get("failed", []), "duplicate": True}

    lines = [dict(l) for l in (state.get("line_items") or [])]
    _normalize_line_item_ids(lines)  # heal any legacy lines stored with only entity_id so matching works
    now = datetime.now(timezone.utc).isoformat()
    price_config = None  # money lists price new lines; fetched once for the whole batch, lazily
    results: list[dict] = []
    failed: list[dict] = []
    changed = False

    # Resolve the whole run against ONE inventory load (vs one load per code).
    from celerp_inventory.routes import duplicate_barcode_detail, resolve_items_by_codes
    resolved = await resolve_items_by_codes(session, company_id, codes)

    for code in codes:
        res = resolved.get(code)
        reason = detail = None
        if res is not None and res.duplicate_barcode:
            item = None
            reason, detail = "duplicate_barcode", duplicate_barcode_detail(code)
        elif res is not None and res.ambiguous:
            item = None
            reason, detail = "ambiguous_sku", _ambiguous_sku_detail(code)
        else:
            item = res.one if res is not None else None
            if item is None:
                reason, detail = "unknown_code", f"Unknown barcode or SKU: {code}"
            elif str((item.state or {}).get("status") or "").lower() == "draft":
                reason, detail = "draft_item", f"{(item.state or {}).get('sku') or code}: item is a draft - make it available first"
        if detail is not None:
            failed.append({"code": code, "reason": reason, "label": detail})
            results.append({"code": code, "state": "error", "reason": reason, "label": detail})
            continue

        idx = next((i for i, l in enumerate(lines) if l.get("item_id") == item.entity_id), None)
        if status == DRAFT:
            if lt == "audit":
                # Manifest is a set: move an existing line to top, else add it (no count yet).
                if idx is not None:
                    lines.insert(0, lines.pop(idx))
                    result_state = "present"
                else:
                    lines.insert(0, _scan_line_from_item(item, lt, None))
                    result_state = "added"
            else:
                # A list holds one line per physical lot: the same item scanned again (already
                # added earlier in this batch, or already on the list from a prior submit) is
                # reported and skipped, not duplicated. `idx` is recomputed each iteration
                # against the live `lines`, so this dedups both within-batch and against
                # persisted lines - matching the audit branch's set semantics above.
                if idx is not None:
                    detail = f"{item.state.get('sku') or code}: already on the list"
                    failed.append({"code": code, "reason": "duplicate_scan", "label": detail})
                    results.append({"code": code, "state": "error", "reason": "duplicate_scan", "label": detail})
                    continue
                if price_config is None:
                    price_config = await get_price_config(session, company_id)
                lines.append(_scan_line_from_item(item, lt, payload.price_list, price_config=price_config))
                result_state = "added"
        else:
            # FINALIZED audit: the manifest is LOCKED - scanning only checks off items already on the
            # list and never adds. An item not on the list is reported (add it while still a draft).
            if idx is None:
                detail = f"{item.state.get('sku') or code} is not on this audit"
                failed.append({"code": code, "reason": "not_on_audit", "label": detail})
                results.append({"code": code, "state": "error", "reason": "not_on_audit", "label": detail})
                continue
            ln = lines.pop(idx)
            ln["audited_at"] = now        # confirm presence -> the row highlights as accounted for
            lines.insert(0, ln)           # newest scan to the top (GDR 2n)
            result_state = "audited"
        changed = True
        results.append({"code": code, "state": result_state,
                        "item_id": item.entity_id, "sku": item.state.get("sku")})

    scanned = sum(1 for r in results if r["state"] != "error")
    if changed:
        fields = {"line_items": lines}
        if payload.run_key:
            # Record this run so a retry replays instead of re-adding (recent runs only). The stored
            # failed list is already bounded: each code is <= MAX_CODE_LEN and the batch <=
            # MAX_SCAN_BATCH, so the retained replay data can never bloat the projection.
            runs = [dict(r) for r in (state.get("scan_runs") or [])]
            runs.append({"key": payload.run_key, "fp": run_fp, "scanned": scanned, "failed": failed})
            fields["scan_runs"] = runs[-_MAX_SCAN_RUNS:]
        await _set_list_fields(session, company_id, entity_id, user, fields)
        await session.commit()
    return {"scanned": scanned, "results": results, "failed": failed}


@lists_router.patch("/{entity_id}/line/{item_id}")
async def set_audit_count(
    entity_id: str, item_id: str, payload: ListCountBody,
    company_id=Depends(get_current_company_id), _: None = require_permission("edit_documents"), user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Set a line's physical count. Editable only while the audit is finalized (counting stage)."""
    row = await _get_audit(session, company_id, entity_id, for_update=True)
    if row.state.get("status") != FINALIZED:
        raise HTTPException(status_code=409, detail="Counts can only be entered on a finalized audit")
    lines = [dict(l) for l in (row.state.get("line_items") or [])]
    idx = next((i for i, l in enumerate(lines) if l.get("item_id") == item_id), None)
    if idx is None:
        raise HTTPException(status_code=404, detail="Item is not on this audit")
    cq = payload.counted_qty
    lines[idx]["counted_qty"] = float(cq) if cq is not None else None
    await _set_list_fields(session, company_id, entity_id, user, {"line_items": lines})
    await session.commit()
    return {"ok": True}


class SetScannedBody(BaseModel):
    item_ids: list[str] = Field(default_factory=list)
    scanned: bool = True


@lists_router.post("/{entity_id}/set-scanned")
async def set_scanned(
    entity_id: str, payload: SetScannedBody = SetScannedBody(),
    company_id=Depends(get_current_company_id), _: None = require_permission("edit_documents"), user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Toggle the scanned/accounted-for highlight (audited_at) on audit lines. scanned=True marks the
    rows as scanned (stamps audited_at), scanned=False clears it. Pass item_ids to target specific
    rows, or none for every line. The highlight otherwise persists indefinitely."""
    row = await _get_audit(session, company_id, entity_id, for_update=True)
    if row.state.get("status") != FINALIZED:
        raise HTTPException(status_code=409, detail="Counting happens on a finalized audit")
    targets = set(payload.item_ids)
    stamp = datetime.now(timezone.utc).isoformat() if payload.scanned else None
    lines = [dict(l) for l in (row.state.get("line_items") or [])]
    changed = 0
    for l in lines:
        if targets and l.get("item_id") not in targets:
            continue
        already = l.get("audited_at") is not None
        if payload.scanned and not already:
            l["audited_at"] = stamp
            changed += 1
        elif not payload.scanned and already:
            l["audited_at"] = None
            changed += 1
    if changed:
        await _set_list_fields(session, company_id, entity_id, user, {"line_items": lines})
        await session.commit()
    return {"changed": changed}


@lists_router.post("/{entity_id}/adjust")
async def adjust_audit(
    entity_id: str, company_id=Depends(get_current_company_id),
    _: None = require_permission("adjust_inventory"), user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Audit terminal action: overwrite each counted line's item qty to its count (finalized ->
    closed). The magnitude and the shrinkage/overage JE are computed against the LIVE item qty at
    adjust time (decision 5.2) — `new_qty = counted`, `delta = counted - live`. Uncounted (blank)
    lines are skipped and reported. Reversible via undo-adjust."""
    # Lock the projection: the terminal action reads every counted line, adjusts stock, and writes the
    # line_items array back (read-modify-write), so it must serialize against a concurrent count/scan.
    row = await _get_audit(session, company_id, entity_id, for_update=True)
    if row.state.get("status") != FINALIZED:
        raise HTTPException(status_code=409, detail="Finalize the count before adjusting stock")
    cycle = int(row.state.get("adjust_count") or 0)
    lines = [dict(l) for l in (row.state.get("line_items") or [])]
    shrink_val = 0.0
    over_val = 0.0
    adjusted = 0
    skipped = 0
    for l in lines:
        cq = l.get("counted_qty")
        if cq is None:
            skipped += 1  # report-and-skip (item qty untouched)
            continue
        item = await session.get(Projection, {"company_id": company_id, "entity_id": l.get("item_id")})
        if item is None or item.entity_type != "item":
            continue
        live = float(item.state.get("quantity") or 0)  # LIVE qty drives the delta + JE (decision 5.2)
        cqf = float(cq)
        if abs(cqf - live) < 1e-9:
            continue  # no change
        unit_cost = _audit_unit_cost(item.state)
        if cqf < live:
            shrink_val += (live - cqf) * unit_cost
        else:
            over_val += (cqf - live) * unit_cost
        await emit_event(
            session, company_id=company_id, entity_id=l["item_id"], entity_type="item",
            event_type="item.quantity.adjusted",
            data={"new_qty": cqf, "reason": "audit", "source_list_id": entity_id, "prior_qty": live},
            actor_id=user.id, location_id=None, source="audit", idempotency_key=str(uuid.uuid4()),
            metadata_={"audit_id": entity_id},
        )
        l["prior_qty"] = live
        l["adjusted"] = True
        adjusted += 1
    if shrink_val > 0:
        await _validate_writeoff_account(session, company_id, auto_je._AUDIT_SHRINKAGE_ACCT)
    await auto_je.create_for_audit_adjustment(
        session, company_id=company_id, user_id=user.id, list_id=entity_id,
        shrinkage_value=shrink_val, overage_value=over_val, cycle=cycle,
    )
    await _emit_list(session, company_id, entity_id, "list.closed",
                     {"result": "stock_adjusted", "line_items": lines, "adjust_count": cycle + 1}, user)
    await session.commit()
    return {"adjusted": adjusted, "skipped": skipped,
            "shrinkage_value": round(shrink_val, 2), "overage_value": round(over_val, 2)}


@lists_router.post("/{entity_id}/undo-adjust")
async def undo_audit_adjust(
    entity_id: str, company_id=Depends(get_current_company_id),
    _: None = require_permission("adjust_inventory"), user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Reverse the last stock adjustment (manager/owner): restore each item's prior quantity and void
    the audit JE. Reopens the audit (closed -> finalized) so it can be re-counted or re-adjusted."""
    # Lock the projection: undo reads each adjusted line, restores its prior qty, and writes the
    # line_items array back (read-modify-write), so it must serialize against a concurrent re-adjust.
    row = await _get_audit(session, company_id, entity_id, for_update=True)
    if row.state.get("status") != CLOSED or row.state.get("result") != "stock_adjusted":
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
    await _emit_list(session, company_id, entity_id, "list.reopened", {"line_items": lines}, user)
    await session.commit()
    return {"ok": True}


# --- Write-off (disposal) list: seed from a selection, remove stock per line, post one JE ----------
# Mirrors the audit trio (create / set-line / terminal / undo) but is EVENT-based: the user enters the
# known quantity leaving stock per line, with a destination expense/cogs/equity account and a comment,
# rather than counting. The terminal carves or disposes each line's stock and posts one balanced JE.

_WRITEOFF_ACCOUNT_TYPES = frozenset({"expense", "cogs", "equity"})


class WriteoffCreateBody(BaseModel):
    entity_ids: list[str] = Field(default_factory=list)
    idempotency_key: str | None = None


class WriteoffLineBody(BaseModel):
    line_id: str | None = None
    item_id: str | None = None
    qty_out: float | None = None
    account: str | None = None
    comment: str | None = None


async def _get_writeoff(session: AsyncSession, company_id, entity_id: str, *, for_update: bool = False) -> Projection:
    row = await (_get_list_for_update if for_update else _get_list)(session, company_id, entity_id)
    if row.state.get("list_type") != "writeoff":
        raise HTTPException(status_code=404, detail="Write-off not found")
    return row


async def _validate_writeoff_account(session, company_id, code: str) -> None:
    """A write-off destination must be a real chart account of an expense/cogs/equity class (spoilage
    and samples -> expense or cogs; owner drawings / family use -> equity). Validated at the function
    level, never only in the picker: a direct API call cannot post to an asset or revenue account."""
    from celerp_accounting.models import Account
    acc = (await session.execute(select(Account).where(
        Account.company_id == company_id, Account.code == code))).scalar_one_or_none()
    if acc is None:
        raise HTTPException(status_code=422, detail=f"Account '{code}' is not in the chart of accounts")
    if acc.account_type not in _WRITEOFF_ACCOUNT_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"Account '{code}' is a {acc.account_type} account; a write-off destination must be "
                   "an expense, cogs, or equity account",
        )


def _writeoff_seed_line(item: Projection) -> dict:
    """One draft write-off line for an item, seeded with its live on-hand and blank entry fields."""
    st = item.state
    return {"line_id": uuid.uuid4().hex, "item_id": item.entity_id, "sku": st.get("sku"),
            "name": st.get("name"), "quantity": float(st.get("quantity") or 0),
            "qty_out": None, "account": "6970", "comment": ""}


@lists_router.post("/writeoff")
async def create_writeoff_list(
    payload: WriteoffCreateBody,
    company_id=Depends(get_current_company_id),
    _: None = require_permission("edit_documents"),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Create a write-off list as a DRAFT seeded from the selected inventory items - the entry point for
    the inventory Write off bulk action. Each seeded line carries the item's on-hand quantity and blank
    qty_out/account/comment for on-page entry; Finalize locks it, then the Write off stock terminal
    removes the stock and posts one JE. Mirrors create_audit_list, but the seed source is the selection,
    not a location scan."""
    if not payload.entity_ids:
        raise HTTPException(status_code=422, detail="Select at least one item to write off")
    company = await session.get(Company, company_id)
    lines: list[dict] = []
    for eid in payload.entity_ids:
        item = await session.get(Projection, {"company_id": company_id, "entity_id": eid})
        if item is None or item.entity_type != "item":
            continue  # non-item ids in the selection are skipped, never seeded as phantom lines
        lines.append(_writeoff_seed_line(item))
    if not lines:
        raise HTTPException(status_code=422, detail="No inventory items in the selection")
    ref_id = next_doc_ref(company, "writeoff")
    entity_id = f"list:{ref_id}"
    if await session.get(Projection, {"company_id": company_id, "entity_id": entity_id}) is not None:
        raise HTTPException(status_code=409, detail=f"Write-off number '{ref_id}' already exists")
    data = {"list_type": "writeoff", "status": DRAFT, "ref_id": ref_id, "line_items": lines,
            "adjust_count": 0, "currency": company.settings.get("currency", "USD")}
    entry = await _emit_list(session, company_id, entity_id, "list.created", data, user, payload.idempotency_key)
    await session.commit()
    return {"event_id": entry.id, "id": entity_id, "ref_id": ref_id, "line_count": len(lines)}


@lists_router.post("/{entity_id}/writeoff-line")
async def set_writeoff_line(
    entity_id: str, payload: WriteoffLineBody,
    company_id=Depends(get_current_company_id),
    _: None = require_permission("edit_documents"),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Set (or append) a write-off line's quantity-out, destination account and comment, on-page while
    the list is a draft. Pass line_id to edit a seeded line, or item_id (no line_id) to add another line
    for an item already selectable on the list - the SAME item can be written off to two accounts
    (spoiled -> wastage, sampled -> marketing). qty_out and account are validated at the function
    level."""
    row = await _get_writeoff(session, company_id, entity_id, for_update=True)
    if row.state.get("status") != DRAFT:
        raise HTTPException(status_code=409, detail="Write-off lines are editable only while it is a draft")
    lines = [dict(l) for l in (row.state.get("line_items") or [])]
    if payload.line_id is not None:
        line = next((l for l in lines if l.get("line_id") == payload.line_id), None)
        if line is None:
            raise HTTPException(status_code=404, detail="Line is not on this write-off")
    elif payload.item_id is not None:
        item = await session.get(Projection, {"company_id": company_id, "entity_id": payload.item_id})
        if item is None or item.entity_type != "item":
            raise HTTPException(status_code=404, detail="Item not found")
        line = _writeoff_seed_line(item)
        lines.insert(0, line)  # newest line to the top (GDR 2n)
    else:
        raise HTTPException(status_code=422, detail="line_id or item_id is required")

    if payload.qty_out is not None:
        q = float(payload.qty_out)
        on_hand = float(line.get("quantity") or 0)
        if q <= 0:
            raise HTTPException(status_code=422, detail="qty_out must be greater than 0")
        if q > on_hand:
            raise HTTPException(status_code=422, detail=f"qty_out {q} exceeds the on-hand quantity {on_hand}")
        line["qty_out"] = q
    if payload.account is not None:
        await _validate_writeoff_account(session, company_id, payload.account)
        line["account"] = payload.account
    if payload.comment is not None:
        line["comment"] = payload.comment
    await _set_list_fields(session, company_id, entity_id, user, {"line_items": lines})
    await session.commit()
    return {"ok": True, "line_id": line["line_id"]}


@lists_router.post("/{entity_id}/write-off")
async def write_off_stock(
    entity_id: str, company_id=Depends(get_current_company_id),
    _: None = require_permission("adjust_inventory"), user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Write-off terminal (manager): in one step, validate every intended line, remove each line's
    qty_out from stock and post one balanced JE (one debit per destination account against a single
    Inventory credit). Runs from a draft (finalized inline after validation) or a finalized list. A
    full-quantity line disposes the whole item row; a partial line carves a child lot via the shared
    split primitive and disposes that. Every disposed portion ends as a hidden `disposed` item - the
    permanent disposal record. Lines with no quantity entered are skipped and reported; a line with a
    quantity but an invalid account/item/quantity rejects the whole action. Reversible via
    undo-write-off."""
    from celerp_inventory.routes import split_off_child
    # Row-lock the list for the whole transaction: this terminal moves ledger value, so a second
    # concurrent run must serialize (a double run would double-carve and post twice). The audit terminal
    # only reads status; the ledger effect here is why the write-off locks and the audit does not.
    row = (await session.execute(select(Projection).where(
        Projection.company_id == company_id, Projection.entity_id == entity_id
    ).with_for_update())).scalar_one_or_none()
    if row is None or row.state.get("list_type") != "writeoff":
        raise HTTPException(status_code=404, detail="Write-off not found")
    status = row.state.get("status")
    if status not in (DRAFT, FINALIZED):
        raise HTTPException(status_code=409, detail="This write-off has already been processed")
    cycle = int(row.state.get("adjust_count") or 0)
    unit_map = await _get_unit_map(session, company_id)
    lines = [dict(l) for l in (row.state.get("line_items") or [])]
    # Pre-flight: an "intended" line is one the user entered a quantity on. Every intended line must be
    # fully valid BEFORE anything is disposed, so a line the user meant to write off but left incomplete
    # rejects the whole action (no silent skip, no false success). Untouched (no-qty) lines are the only
    # legitimately skipped-and-reported lines.
    intended = [l for l in lines if l.get("qty_out") is not None]
    if not intended:
        raise HTTPException(status_code=422,
                            detail="Enter a quantity and a destination account for each item you want to write off")
    prepared: list[tuple[dict, Projection, float]] = []  # (line, item, qty_out)
    # Validate every intended line's account and quantity, and collect the distinct item ids to lock.
    want_ids: set[str] = set()
    for l in intended:
        name = l.get("name") or l.get("sku") or l.get("item_id") or "item"
        account = l.get("account")
        if not account:
            raise HTTPException(status_code=422, detail=f"{name}: choose a destination account")
        await _validate_writeoff_account(session, company_id, account)
        qty_out = float(l.get("qty_out"))
        if qty_out <= 0:
            raise HTTPException(status_code=422, detail=f"{name}: write-off quantity must be greater than 0")
        want_ids.add(l.get("item_id"))
    # Lock every distinct item projection FOR UPDATE in one deterministic (entity_id-sorted) batch: two
    # concurrent runs that share items acquire them in the same order (no deadlock), and each reads the
    # other's committed decrement. populate_existing overwrites any stale identity-map copy, closing the
    # unlocked-read hazard the plain get left open.
    locked: dict[str, Projection] = {
        p.entity_id: p for p in (await session.execute(
            select(Projection).where(
                Projection.company_id == company_id,
                Projection.entity_id.in_(sorted(want_ids)),
            ).order_by(Projection.entity_id).with_for_update().execution_options(populate_existing=True)
        )).scalars().all()
    }
    # From each item's LOCKED fresh state, re-check availability and capture its live quantity and
    # unit_cost once. unit_cost is split-invariant, so capturing it pre-carve keeps every line's
    # valuation independent of carve-order rounding on the mutated parent.
    live_by_item: dict[str, float] = {}
    unit_cost_by_item: dict[str, float] = {}
    for l in intended:
        name = l.get("name") or l.get("sku") or l.get("item_id") or "item"
        item = locked.get(l.get("item_id"))
        if item is None or item.entity_type != "item" or (item.state.get("status") or "available") != "available":
            raise HTTPException(status_code=422, detail=f"{name}: item is no longer available to write off")
        live_by_item[item.entity_id] = float(item.state.get("quantity") or 0)
        unit_cost_by_item.setdefault(item.entity_id, _audit_unit_cost(item.state))
        prepared.append((l, item, float(l.get("qty_out"))))
    # Aggregate availability: the same item can appear on several lines (two destination accounts), so
    # validate the SUM of its write-off quantities against live stock, not each line on its own.
    agg: dict[str, float] = {}
    for l, item, qty_out in prepared:
        agg[item.entity_id] = round(agg.get(item.entity_id, 0.0) + qty_out, 10)
    for item_id, total_out in agg.items():
        live = live_by_item[item_id]
        if total_out > live + 1e-9:
            name = locked[item_id].state.get("sku") or item_id
            raise HTTPException(
                status_code=422,
                detail=f"{name}: total write-off quantity {total_out} exceeds stock {live}",
            )
    skipped = len(lines) - len(intended)  # untouched (no-qty) lines, reported alongside the write-off
    # Single step: a draft write-off is finalized inline here, after validation passes, so the manager
    # removes stock in one click. The writeoff behaviour has no finalize milestone (pure status
    # transition), and this runs under the same row lock/transaction as the disposal below, so the
    # finalize and the disposal are atomic - a later failure rolls both back and the list stays a draft.
    if status == DRAFT:
        await _emit_list(session, company_id, entity_id, "list.finalized",
                         {"status": FINALIZED, "finalized_at": datetime.now(timezone.utc).isoformat()}, user)
    debits: dict[str, float] = {}
    total_value = 0.0
    written_off = 0
    remaining: dict[str, float] = dict(live_by_item)
    for l, item, qty_out in prepared:
        account = l.get("account")
        unit_cost = unit_cost_by_item[item.entity_id]
        value = round(unit_cost * qty_out, 2)
        sku = item.state.get("sku") or ""  # read before any rollback expires the ORM row
        rem = remaining[item.entity_id]
        try:
            if abs(qty_out - rem) < 1e-9:
                disposed_eid = l["item_id"]  # the line consuming the item's remainder disposes the row in place, no split
            else:
                # A piece-unit parcel's discarded pieces equal the discarded count, so the carve is
                # fully determined by qty_out. A weight parcel's discarded weight is not (grams are not
                # the count), and the write-off line has no weight input, so child_weight stays unset and
                # split_off_child raises below -> a partial weight write-off is 409, not a guessed carve.
                sell_by = item.state.get("sell_by") or ""
                child_pieces = qty_out if is_pieces_unit(sell_by, unit_map) else None
                disposed_eid, _sku = await split_off_child(
                    session, company_id=company_id, user_id=user.id, parent_proj=item,
                    child_qty=qty_out, child_pieces=child_pieces,
                )
        except ValueError as exc:
            # A weight/piece-tracked parcel needs the discarded weight/pieces to carve; without them the
            # split cannot proceed. Roll back the whole terminal so nothing is disposed and no JE posts.
            await session.rollback()
            raise HTTPException(status_code=409, detail=f"Cannot write off {sku}: {exc}")
        await emit_event(
            session, company_id=company_id, entity_id=disposed_eid, entity_type="item",
            event_type="item.written_off",
            data={"account": account, "qty": qty_out, "unit_cost": unit_cost, "cost_total": value,
                  "reason": l.get("comment") or None, "source_list_id": entity_id},
            actor_id=user.id, location_id=None, source="writeoff", idempotency_key=str(uuid.uuid4()),
            metadata_={"writeoff_id": entity_id},
        )
        debits[account] = round(debits.get(account, 0.0) + value, 2)
        total_value = round(total_value + value, 2)
        l["disposed_entity_id"] = disposed_eid
        l["disposed_qty"] = qty_out
        l["adjusted"] = True
        written_off += 1
        remaining[item.entity_id] = round(rem - qty_out, 10)
    entries = [{"account": acct, "debit": val, "credit": 0.0} for acct, val in debits.items()]
    if entries:
        entries.append({"account": auto_je._INVENTORY_ACCT, "debit": 0.0, "credit": round(total_value, 2)})
    await auto_je.create_for_line_adjustment(
        session, company_id=company_id, user_id=user.id, list_id=entity_id,
        kind="writeoff", entries=entries, cycle=cycle,
    )
    await _emit_list(session, company_id, entity_id, "list.closed",
                     {"result": "written_off", "line_items": lines, "adjust_count": cycle + 1}, user)
    await session.commit()
    return {"written_off": written_off, "skipped": skipped, "value": round(total_value, 2)}


@lists_router.post("/{entity_id}/undo-write-off")
async def undo_write_off(
    entity_id: str, company_id=Depends(get_current_company_id),
    _: None = require_permission("adjust_inventory"), user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Reverse a write-off (manager): void its JE and restore each disposed lot to `available`. A carved
    child lot is NOT re-merged into its parent - it returns as its own available lot, so quantity is
    conserved (parent remainder + restored child = the original). Reopens the list (closed -> finalized)
    so it can be re-run."""
    # Lock the projection: undo reads each disposed line, restores its lot, and writes the line_items
    # array back (read-modify-write), so it must serialize against a concurrent re-run/undo.
    row = await _get_writeoff(session, company_id, entity_id, for_update=True)
    if row.state.get("status") != CLOSED or row.state.get("result") != "written_off":
        raise HTTPException(status_code=409, detail="This write-off has no removal to undo")
    cycle = int(row.state.get("adjust_count") or 1) - 1
    lines = [dict(l) for l in (row.state.get("line_items") or [])]
    for l in lines:
        if l.get("adjusted") and l.get("disposed_entity_id"):
            await emit_event(
                session, company_id=company_id, entity_id=l["disposed_entity_id"], entity_type="item",
                event_type="item.status.set",
                data={"new_status": "available", "reason": "writeoff_undo"},
                actor_id=user.id, location_id=None, source="writeoff", idempotency_key=str(uuid.uuid4()),
                metadata_={"writeoff_id": entity_id},
            )
            l.pop("adjusted", None)
            l.pop("disposed_entity_id", None)
            l.pop("disposed_qty", None)
    await auto_je.void_for_list_adjustment(session, company_id=company_id, user_id=user.id,
                                           list_id=entity_id, kind="writeoff", cycle=cycle)
    await _emit_list(session, company_id, entity_id, "list.reopened", {"line_items": lines}, user)
    await session.commit()
    return {"ok": True}


class ListChangeTypeBody(BaseModel):
    list_type: str


@lists_router.post("/{entity_id}/change-type")
async def change_list_type(
    entity_id: str, payload: ListChangeTypeBody,
    company_id: str = Depends(get_current_company_id), _: None = require_permission("edit_documents"), user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Change a list's type while it is a draft OR issued (finalized). The change is just another
    event in history, so everything the list did under its old type stays recorded; terminal/
    financial actions are gated by status, so re-typing can't undo or fabricate past work. Type
    fields persist (nothing is zeroed). Switching a FINALIZED list to audit re-freezes the on-hand
    baseline that the audit's own finalize would have captured, so variance/Adjust stay correct.
    Closed/void lists are terminal — duplicate instead."""
    row = await _get_list_for_update(session, company_id, entity_id)
    state = row.state
    new_type = payload.list_type
    if new_type not in LIST_TYPES:
        raise HTTPException(status_code=422, detail=f"Unknown list type: {new_type}")
    status = state.get("status")
    if status not in (DRAFT, FINALIZED):
        raise HTTPException(status_code=409,
                            detail="A list's type can only be changed while it is a draft or issued")
    fields: dict = {"list_type": new_type}
    if new_type == "audit" and status == FINALIZED:
        lines = [dict(l) for l in (state.get("line_items") or [])]
        for l in lines:
            item = await session.get(Projection, {"company_id": company_id, "entity_id": l.get("item_id")})
            l["on_hand"] = (float(item.state.get("quantity") or 0)
                            if (item and item.entity_type == "item") else l.get("on_hand", 0.0))
        fields["line_items"] = lines
    await _set_list_fields(session, company_id, entity_id, user, fields)
    await session.commit()
    return {"ok": True, "list_type": new_type}


@lists_router.post("/{entity_id}/send")
async def send_list(
    entity_id: str, payload: DocSendBody = DocSendBody(),
    company_id: str = Depends(get_current_company_id), _: None = require_permission("edit_documents"), user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Record a finalized list as sent (sets the `sent_at` milestone; status stays finalized) and,
    if a recipient is given, fire the relay email — the same Send / Mark-as-sent mechanism documents
    use. `sent_via="manual"` (no recipient) is the Mark-as-sent path."""
    row = await _get_list(session, company_id, entity_id)
    if row.state.get("status") != FINALIZED:
        raise HTTPException(status_code=409, detail="Issue the list before sending it")
    now = datetime.now(timezone.utc).isoformat()
    await _set_list_fields(session, company_id, entity_id, user,
                           {"sent_at": now, "sent_via": payload.sent_via or "email",
                            "sent_to": payload.sent_to})
    view_url = None
    if payload.sent_to:
        from celerp_docs.routes_share import send_view_url
        view_url = await send_view_url(session, company_id, entity_id)
        await session.commit()
    if payload.sent_to:
        from celerp_docs.doc_email import compose_doc_email
        ref = row.state.get("ref_id") or entity_id.split(":")[-1]
        company_row = await session.get(Company, company_id)
        sender = company_row.name if company_row else "Your supplier"
        contact = row.state.get("customer_name") or "there"
        subject = (payload.subject or "").strip() or f"Quotation #{ref} from {sender}"
        html, text = compose_doc_email(
            doc_type_label="Quotation", doc_number=ref, sender_name=sender,
            contact_name=contact, total=row.state.get("total", 0),
            currency=row.state.get("currency", "USD"),
            message=payload.message, view_url=view_url,
        )
        reply_to = await _sender_reply_to(session, company_id, user)
        _email_with_receipt(
            company_id, f"Quotation #{ref}", payload.sent_to, f"/lists/{entity_id}",
            to=payload.sent_to, subject=subject, body_html=html, body_text=text,
            reply_to=reply_to, from_name=sender, cc=payload.cc or "", bcc=payload.bcc or "")
    return {"ok": True}


@lists_router.post("/{entity_id}/unmark-sent")
async def unmark_list_sent(
    entity_id: str, company_id: str = Depends(get_current_company_id),
    _: None = require_permission("edit_documents"),
    user=Depends(get_current_user), session: AsyncSession = Depends(get_session),
) -> dict:
    """Clear the sent milestone (the list stays finalized)."""
    row = await _get_list(session, company_id, entity_id)
    await _set_list_fields(session, company_id, entity_id, user,
                           {"sent_at": None, "sent_via": None, "sent_to": None})
    await session.commit()
    return {"ok": True}


class ListMoveBody(BaseModel):
    to_location_id: str


@lists_router.post("/{entity_id}/move")
async def move_transfer(
    entity_id: str, payload: ListMoveBody,
    company_id=Depends(get_current_company_id), _: None = require_permission("edit_documents"), user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Transfer action: relocate every item on a finalized transfer to one location, by emitting the
    inventory `item.transferred` event per line (stock is owned by inventory; docs only emits the
    event). Repeatable — the transfer stays finalized so it can be moved again."""
    row = await _get_list(session, company_id, entity_id)
    if (row.state.get("list_type") or "") != "transfer":
        raise HTTPException(status_code=409, detail="Only transfers can move stock")
    if row.state.get("status") != FINALIZED:
        raise HTTPException(status_code=409, detail="Issue the transfer before moving its items")
    if not payload.to_location_id:
        raise HTTPException(status_code=422, detail="A destination location is required")
    moved = 0
    for l in (row.state.get("line_items") or []):
        item_id = l.get("item_id") or l.get("entity_id")
        if not item_id:
            continue
        await emit_event(
            session, company_id=company_id, entity_id=item_id, entity_type="item",
            event_type="item.transferred", data={"to_location_id": str(payload.to_location_id)},
            actor_id=user.id, location_id=payload.to_location_id, source="transfer",
            idempotency_key=str(uuid.uuid4()), metadata_={"transfer_id": entity_id},
        )
        moved += 1
    await _set_list_fields(session, company_id, entity_id, user,
                           {"moved_to_location_id": payload.to_location_id,
                            "moved_at": datetime.now(timezone.utc).isoformat()})
    await session.commit()
    return {"moved": moved, "to_location_id": payload.to_location_id}
