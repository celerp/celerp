# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import uuid
from dataclasses import asdict, dataclass, replace as _dc_replace
from datetime import datetime, timezone, date as _date
from decimal import Decimal
from collections.abc import Awaitable, Callable, Iterable
from typing import Literal, NamedTuple

from fastapi import APIRouter, Depends, Form, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse

from pydantic import BaseModel, Field, field_validator, model_serializer, model_validator
from sqlalchemy import select, func as _func, text
import sqlalchemy as _sa
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.db import get_session
from celerp.events.engine import (emit_event, find_event_by_idempotency, is_stripe_receipt,
                                  refuse_stripe_payment_removal, stripe_payment_indexes, stripe_receipt_references)
from celerp.importers.results import failure_reason
from celerp.models.company import Company, Location
from celerp.modules.slots import fire_lifecycle
from celerp.models.projections import Projection
from celerp.inventory_codes import MAX_SCAN_CODE_LEN, PHYSICAL_CODE_RESOLVE_EXCLUDED_STATUSES
from celerp_docs.doc_money import document_money, refresh_line_taxes
from celerp_docs.doc_projections import received_line_index
from celerp_docs.taxes import TaxApplication, compute_tax_amounts
from celerp.services import auto_je
from celerp.services.field_schema import reject_system_item_fields
from celerp.accounting_roles import LOT_ACCOUNT_FIELD, VALUED_FROM_KEY, AccountRole, refusal
from celerp.services.account_roles import current_settings, lot_account, new_lot_account, role_map
from celerp.services.company_lock import lock_company, lock_projections, locked_company
from celerp.services.goods_cost import negative_cost_error
from celerp.services.journal_accounts import require_destinations, require_line_destinations, require_settlement_account
from celerp.services.lot_origin import is_stock_type
from celerp.services.physical_codes import lock_item_code_namespace
from celerp.services.payments import recorded_unmatched, return_unmatched
from celerp.services.business_time import business_date_at, business_date_of
from celerp.services.landed_cost import compute_bill_landed_allocation
from celerp.services.line_measures import line_label, splitting_allowed
from celerp.services.document_lines import (
    line_id_counts, line_item_id, linked_items, memo_out_refusal, out_on_another_memo, strip_line_ids,
)
from celerp.services.attachments import attach_file, storing
from celerp.services.csv_export import csv_stream, resolve_export_cols
from celerp.services.currencies import CURRENCY_CODES, require_currency_code
from celerp.services.auth import get_current_company_id, get_current_role, get_current_user
from celerp.services.permissions import assert_role_permission, get_current_company_settings, locked_authority, reject_price_change, require_permission, role_has_permission
from celerp_docs.sequences import next_doc_ref, next_draft_ref, require_doc_type, get_all_sequences, update_sequence, list_sequence_key
from celerp_docs.search import doc_q_clause
from celerp.services.units import DEFAULT_UNITS, build_unit_map, is_non_stock_line, is_pieces_unit, is_weight_unit, line_receive_kind, validate_line_quantity
from celerp.services.money import books_currency, checked_exchange_rate, discount_from_inputs, doc_rate, document_line_unit, require_doc_rate, round_basis, round_money, round_rate, to_base, to_decimal, to_stored_float
from celerp.services.pricing import DEFAULT_PRICE_LIST_NAME, coerce_price, get_price_config, is_cost_list_name, price_keys_in, resolve_price
from celerp.services.terms import resolve_document_terms
from celerp.services.payment_terms import company_payment_terms, due_date_for_terms
from celerp_contacts.references import contact_accepts, contact_snapshot, lock_contacts
from celerp.output.document_context import prepare_document_output
from celerp_docs.doc_constants import WRITEOFF_ACCOUNT_TYPES, INBOUND_DOC_TYPES, FULFILLABLE_STATUSES, LEGACY_CONTACT_FIELDS, LIFECYCLE_OWNED_FIELDS, NON_FINANCIAL_DOC_TYPES, RECEIVABLE_STATUSES, RESERVABLE_DOC_STATUSES, REVERTIBLE_STATUSES, SALES_PRICED_DOC_TYPES, SUPPLIER_RETURN_STATUSES, VENDOR_DOC_TYPES
from celerp.services.doc_balance import DOC_FIELD_FALLBACKS, doc_value, is_awaiting_payment, is_overdue_document, is_owed, outstanding_balance, today_iso
from celerp.services.list_behavior import (
    DRAFT, FINALIZED, CLOSED, VOID, DEFAULT_LIST_TYPE, LIST_TYPES, behavior, terminal_action, is_money_list,
)
from celerp.services.shipping import INCOTERMS_2020, REASONS_FOR_EXPORT
from celerp.schemas.numbers import FiniteFloat

router = APIRouter(dependencies=[Depends(get_current_user)])

# Longest explicit id list the list route accepts; a batch of AI drafts is far below it.
MAX_IDS_FILTER = 500

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
    # The line's own stable identity, assigned where lines are written when omitted.
    line_id: str | None = None
    item_id: str | None = None
    entity_id: str | None = None  # alias sent by the frontend; resolved to item_id below
    sku: str | None = None
    # Stamped from the catalog item when the line is added; the identifier a
    # finalized document keeps showing even if the item is later re-barcoded.
    barcode: str | None = None
    name: str | None = None
    description: str | None = None
    quantity: FiniteFloat = 0
    unit: str | None = None
    unit_price: FiniteFloat = 0
    tax_rate: FiniteFloat | None = None  # deprecated: kept for backward compat; prefer taxes list
    taxes: list[TaxApplication] = Field(default_factory=list)
    sell_by: str | None = None
    line_total: FiniteFloat | None = None
    # Purchasing: whether the line's goods are received into stock, as an expense or as an asset.
    receive_as: Literal["stock", "expense", "asset"] | None = None
    # Invoice fulfillment: how much of the linked parcel this line draws, by piece
    # and by weight. Drive the split-on-fulfill (child_pieces / child_weight). Only
    # editable when the parcel actually tracks that measure and splitting is allowed.
    pieces: FiniteFloat | None = None
    weight: FiniteFloat | None = None
    # Purchasing: what the received lot records about the goods.
    attributes: dict | None = None
    # Purchasing: the chart account the line posts to; checked when the bill is finalized.
    account_code: str | None = None

    @model_validator(mode="after")
    def _resolve_entity_id(self) -> "LineItem":
        """Frontend sends entity_id; normalise to item_id so the stored state is consistent.
        A line naming two different items under the two keys is refused, as every line
        writer refuses it (document_lines.linked_items)."""
        if self.entity_id and self.item_id and self.entity_id != self.item_id:
            raise ValueError(f"This line names two different items ({self.item_id} and {self.entity_id}). "
                             "Keep one item per line.")
        if self.entity_id and not self.item_id:
            self.item_id = self.entity_id
        self.entity_id = None  # never persist entity_id; always use item_id
        return self


def _calendar_date(v: str | None) -> str | None:
    """A date money moves on as YYYY-MM-DD, or ValueError when it is not a real date.

    Left out (None) stays None where the date is optional."""
    if v is None:
        return None
    try:
        return _date.fromisoformat(str(v).strip()[:10]).isoformat()
    except ValueError:
        raise ValueError("Enter the date as YYYY-MM-DD.") from None


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


def _require_doc_rate_http(doc: dict, base_currency: str) -> Decimal:
    try:
        return require_doc_rate(doc, base_currency)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=f"This document has no usable exchange rate. {exc}. Base-currency documents use rate 1; foreign-currency documents require a stored rate. Set the exchange rate to continue.",
        ) from exc


def _reject_lifecycle_fields(data):
    """Creation owns no lifecycle or settlement state. Status is checked on its
    own (it may say draft); anything else lifecycle-owned is refused by name."""
    if isinstance(data, dict):
        forged = sorted(k for k in data if k in LIFECYCLE_OWNED_FIELDS and k != "status")
        if forged:
            raise ValueError(
                f"{', '.join(forged)} cannot be set when creating. A new record is always "
                "an unpaid draft; finalize, send, record payments or receive it with those actions."
            )
    return data


def _change_new(change):
    return change.get("new") if isinstance(change, dict) else change


def _canonical_contact(values: dict, new=lambda v: v) -> dict:
    """Name the counterparty by contact_id and contact_name only. An older field name is
    taken as the current one; an older and a current field that disagree are refused."""
    out = dict(values)
    for legacy, canonical in LEGACY_CONTACT_FIELDS.items():
        if legacy not in out:
            continue
        value = out.pop(legacy)
        current = new(out.get(canonical))
        if current in (None, ""):
            out[canonical] = value
        elif new(value) not in (None, "") and new(value) != current:
            raise ValueError(f"{legacy} and {canonical} disagree. Send {canonical} only.")
    return out


def _canonical_contact_payload(data):
    return _canonical_contact(data) if isinstance(data, dict) else data


def _request_digest(kind: str, entity_id: str | None, expected_version: int | None, body: dict) -> str:
    canonical = json.dumps([kind, entity_id, expected_version, body], sort_keys=True,
                           separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _check_replay(replay, *, event_type: str, digest: str, entity_id: str | None = None) -> None:
    if (
        replay.event_type != event_type
        or (entity_id is not None and replay.entity_id != entity_id)
        or (replay.metadata_ or {}).get("request") != digest
    ):
        raise HTTPException(status_code=409, detail="Idempotency key was already used for another operation")


def _replay_result(replay, *, event_type: str, digest: str, entity_id: str | None = None) -> dict:
    _check_replay(replay, event_type=event_type, digest=digest, entity_id=entity_id)
    return {"event_id": replay.id, "id": replay.entity_id, "version": replay.id}


_OPERATION_KEY_MAX = 200
# Records an operation writes are named from its key and the document, so a long key
# is shortened to a digest that names them just as uniquely.
_OPERATION_KEY_KEPT = 64


def _operation(kind: str, scope: str | None, payload: BaseModel) -> tuple[str, str]:
    """The key and request digest of one call of an operation on ``scope``."""
    key = payload.idempotency_key or str(uuid.uuid4())
    if len(key) > _OPERATION_KEY_MAX:
        raise HTTPException(status_code=422, detail=f"idempotency_key is longer than {_OPERATION_KEY_MAX} characters")
    if len(key) > _OPERATION_KEY_KEPT:
        key = f"op:{hashlib.sha256(key.encode()).hexdigest()}"
    return key, _request_digest(kind, scope, None, payload.model_dump(mode="json", exclude={"idempotency_key"}))


def _step_key(key: str, *parts) -> str:
    """The key of one step of the operation ``key``."""
    return ":".join((key, *map(str, parts)))


def _step_id(key: str, *parts) -> uuid.UUID:
    """The id of a record one step of the operation ``key`` creates."""
    return uuid.uuid5(uuid.NAMESPACE_OID, _step_key(key, *parts))


async def _earlier_run(session: AsyncSession, company_id, key: str, *, event_type: str,
                       entity_id: str | None, digest: str, with_event_id: bool = False) -> dict | None:
    """What an earlier call of this same request returned, or None when there was none.
    ``with_event_id``: the call answered with its event's id beside the recorded result."""
    replay = await find_event_by_idempotency(session, company_id, key)
    if replay is None:
        return None
    _check_replay(replay, event_type=event_type, digest=digest, entity_id=entity_id)
    result = (replay.metadata_ or {}).get("result")
    if with_event_id:
        return {"event_id": replay.id, **(result or {})}
    return result or {"event_id": replay.id}


_PROTECTED_FIELDS = frozenset({"status", "entity_type", "company_id", "source_memo_id"})


def _refuse_protected_fields(fields_changed: dict) -> None:
    attempted = _PROTECTED_FIELDS & set(fields_changed)
    if attempted:
        raise HTTPException(
            status_code=422,
            detail=f"Fields {sorted(attempted)} cannot be changed via patch. Use the appropriate lifecycle endpoints.",
        )


def _patch_identity(kind: str, entity_id: str, payload: "DocPatch") -> tuple[str, str | None]:
    digest = _request_digest(kind, entity_id, payload.expected_version, payload.fields_changed)
    if payload.idempotency_key:
        return digest, payload.idempotency_key
    return digest, (f"{kind}:patch:{digest}" if payload.expected_version is not None else None)


def _patch_replay(replay, event_type: str, entity_id: str, digest: str) -> dict:
    done = _replay_result(replay, event_type=event_type, digest=digest, entity_id=entity_id)
    return {"event_id": done["event_id"], "version": done["version"]}


async def _find_patch_replay(session: AsyncSession, company_id, idem_key: str | None,
                             event_type: str, entity_id: str, digest: str) -> dict | None:
    """The result of an earlier save of this same patch, or None when there was none."""
    if not idem_key:
        return None
    replay = await find_event_by_idempotency(session, company_id, idem_key)
    return None if replay is None else _patch_replay(replay, event_type, entity_id, digest)


def _created_as_draft(v):
    if v != "draft":
        raise ValueError(
            "A new record is always created as a draft. "
            "Create it, then finalize, send or receive it."
        )
    return v


class DocCreatePayload(BaseModel):
    doc_type: str
    ref_id: str | None = None
    contact_id: str | None = None
    contact_name: str | None = None
    purchase_kind: str | None = None  # inventory|expense|asset (purchase_order only)
    line_items: list[LineItem] = Field(default_factory=list)
    subtotal: FiniteFloat = 0
    tax: FiniteFloat = 0  # deprecated: kept for backward compat; prefer doc_taxes list
    doc_taxes: list[TaxApplication] = Field(default_factory=list)
    discount: FiniteFloat = 0
    shipping: FiniteFloat = 0
    total: FiniteFloat = 0
    payment_terms: str | None = None
    due_date: str | None = None
    currency: str | None = None
    # Declared rather than left to extra="allow" so the rate is validated on the
    # way in. Foreign-currency documents require one before they can finalize.
    conversion_rate: FiniteFloat | None = None
    notes: str | None = None
    reference: str | None = None
    terms_template: str | None = None
    terms_text: str | None = None
    customer_note: str | None = None
    expected_delivery: str | None = None
    valid_until: str | None = None
    carrier: str | None = None
    tracking: str | None = None
    from_location_id: str | None = None
    to_address: dict | None = None
    original_doc_id: str | None = None
    reason: str | None = None
    # Creation always makes an unpaid draft; issuing and settling it are
    # lifecycle actions that post their entries. Only the import routes create
    # a document already issued.
    status: Literal["draft"] = "draft"
    idempotency_key: str | None = None
    model_config = {"extra": "allow"}

    _contact_fields = model_validator(mode="before")(_canonical_contact_payload)
    _no_lifecycle_state = model_validator(mode="before")(_reject_lifecycle_fields)
    _known_doc_type = field_validator("doc_type")(require_doc_type)
    _draft_only = field_validator("status", mode="before")(_created_as_draft)

    @field_validator("conversion_rate")
    @classmethod
    def _rate_is_usable(cls, v: float | None) -> float | None:
        return _stored_conversion_rate(v)


class DocPatch(BaseModel):
    fields_changed: dict[str, dict] = Field(default_factory=dict)
    idempotency_key: str | None = None
    # Optimistic concurrency: when set, the patch applies only if it equals the record's current
    # version (its latest ledger-entry id). A mismatch means another editor moved the record on since
    # this client last read it, so the write is a stale clobber and is rejected 409. Omitted = no check.
    expected_version: int | None = None

    @model_validator(mode="before")
    @classmethod
    def _canonical_contact_fields(cls, data):
        if isinstance(data, dict) and isinstance(data.get("fields_changed"), dict):
            data = {**data, "fields_changed": _canonical_contact(data["fields_changed"], new=_change_new)}
        return data


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
    amount: FiniteFloat
    payment_date: str  # ISO date (YYYY-MM-DD), always required
    currency: str | None = None
    method: str | None = None
    reference: str | None = None
    bank_account: str | None = None
    conversion_rate: FiniteFloat | None = None
    source_doc_id: str | None = None
    target_doc_id: str | None = None
    idempotency_key: str | None = None

    _real_date = field_validator("payment_date")(_calendar_date)

    @field_validator("conversion_rate")
    @classmethod
    def _rate_is_usable(cls, v: float | None) -> float | None:
        return _stored_conversion_rate(v)


class ReceivedItem(BaseModel):
    po_line_index: int = -1  # optional; -1 means not specified (e.g. one-click bill receive)
    source_line_id: str | None = None  # the document line's id; wins over po_line_index
    item_id: str | None = None
    quantity_received: FiniteFloat = Field(gt=0)
    condition: str = "good"
    sku: str | None = None
    name: str | None = None
    cost_price: FiniteFloat | None = None
    receive_as: str | None = None  # taken from the document line when not given
    category: str | None = None
    attributes: dict | None = None

    @model_serializer(mode="wrap")
    def _omit_unnamed_line(self, handler):
        # A receipt that names no line id serializes as it always has, so a retry of one
        # sent before line ids existed still matches its earlier run.
        data = handler(self)
        if data.get("source_line_id") is None:
            data.pop("source_line_id", None)
        return data


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

    _contact_fields = field_validator("data", mode="before")(_canonical_contact_payload)


class BatchImportResult(BaseModel):
    created: int
    skipped: int
    updated: int = 0
    errors: list[str]


class DocBatchImportRequest(BaseModel):
    records: list[DocImportRecord] = Field(..., max_length=500)
    upsert: bool = False


class FulfillLinesRequest(BaseModel):
    """The lines an action applies to: ``line_ids`` names each line by its line id.

    ``line_entity_ids`` names lines by the item they bind, for clients written before
    lines had ids; each item must be on exactly one line. Send one of the two.
    ``idempotency_key`` names one submission: sending it again returns the first result."""
    line_ids: list[str] = Field(default_factory=list)
    line_entity_ids: list[str] = Field(default_factory=list)
    idempotency_key: str | None = None

    @field_validator("line_ids", "line_entity_ids")
    @classmethod
    def strip_empty(cls, v: list[str]) -> list[str]:
        """Drop empty strings and de-duplicate: bulk selects may include value="" rows."""
        return list(dict.fromkeys(eid for eid in v if eid))


class RevertLinesRequest(FulfillLinesRequest):
    """Revert whole lines, or return part of one.

    ``quantities`` maps a chosen line (its line id, or its item id when lines are chosen
    by item) to the quantity coming back. Omit it, or give the whole quantity out, to take
    it all back. A smaller quantity is a partial
    return: that much is split off and comes back into stock, and the remainder stays out
    with the customer, so goods still in their hands are never written off as returned.

    ``weights`` / ``pieces`` carry the returned lot's measures for parcels tracked by a
    measure the quantity does not imply (a piece-sold parcel that also carries a weight).
    The measure of a part-returned parcel cannot be inferred, so it must be stated.
    """
    quantities: dict[str, FiniteFloat] | None = None
    weights: dict[str, FiniteFloat] | None = None
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
            detail=refusal("documents.due_before_issue", "The due date cannot be earlier than the issue date."),
        )


async def _get_doc(session: AsyncSession, company_id, entity_id: str, *, for_update: bool = False) -> Projection:
    # for_update takes the doc row under SELECT ... FOR UPDATE so the lifecycle
    # writers that share it (close, fulfill, send) are mutually exclusive: the
    # loser blocks until the winner commits, then populate_existing forces a fresh
    # read of the just-committed state to validate against. lock_projections takes
    # the company lock first, so every writer takes company, then document, then item rows.
    if for_update:
        row = (await lock_projections(session, company_id, [entity_id])).get(entity_id)
    else:
        row = await session.get(Projection, {"company_id": company_id, "entity_id": entity_id})
    if row is None or row.entity_type != "doc":
        raise HTTPException(status_code=404, detail="Document not found")
    return row


async def _lock_copied_contacts(session: AsyncSession, company_id, entity_ids) -> dict[str, str]:
    """Lock the contacts that the records about to be copied name, before the records.

    Returns {entity_id: contact_id} as read before the lock; the caller locks the records
    and passes them to _assert_contacts_unchanged.
    """
    named: dict[str, str] = {}
    for eid in entity_ids:
        row = await session.get(Projection, {"company_id": company_id, "entity_id": eid})
        if row is not None:
            named[eid] = (row.state or {}).get("contact_id") or ""
    await lock_contacts(session, company_id, [c for c in named.values() if c])
    return named


def _assert_contacts_unchanged(named: dict[str, str], rows) -> None:
    for row in rows:
        if row is not None and ((row.state or {}).get("contact_id") or "") != named.get(row.entity_id, ""):
            raise HTTPException(
                status_code=409,
                detail="The contact on this record changed while it was being copied. Reload and try again.",
            )


async def _lock_contact_reference(session: AsyncSession, company_id, contact_id: str) -> Projection | None:
    """Lock the local contact a Document or List is about to reference, and refuse a bad one.

    Documents and Lists deliberately support snapshot/external contact identifiers that do
    not have a local CRM projection (imports and historical records rely on it), so absence
    is valid and returns None. An id that *does* resolve locally must name a live contact.
    The row stays locked until commit, so a concurrent merge or delete (which lock the same
    rows first) cannot retire it underneath the new reference.
    """
    contact = (await lock_contacts(session, company_id, [contact_id])).get(contact_id)
    if contact is None:
        if await session.get(Projection, {"company_id": company_id, "entity_id": contact_id}) is not None:
            raise HTTPException(status_code=422, detail="contact_id refers to a non-contact record")
        return None
    if (contact.state or {}).get("deleted"):
        raise HTTPException(
            status_code=422,
            detail="This contact has been deleted and cannot be used on documents.",
        )
    return contact


async def _get_docs_for_update(session: AsyncSession, company_id, entity_ids) -> dict[str, Projection]:
    """Lock a set of doc rows FOR UPDATE in one deterministic (entity_id-sorted) batch,
    the multi-doc analogue of _get_doc(for_update=True). A handler that reads several
    docs (create_shipment) must take them all under the lock in sorted order so it can
    never both pass a status check against a stale read and commit after a concurrent
    lifecycle writer changes one - and so two such handlers sharing docs acquire them in
    the same order (no deadlock). populate_existing overwrites any stale identity-map
    copy. The company lock comes first and doc rows before any item rows a caller goes on
    to lock, the global company-document-item ordering. Missing or non-doc ids are the caller's to reject."""
    rows = await lock_projections(session, company_id, entity_ids)
    return {eid: r for eid, r in rows.items() if r.entity_type == "doc"}


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


async def _line_sell_by_map(
    session: AsyncSession, company_id: str, line_items: list[dict]
) -> dict[str, str | None]:
    """Resolve sell_by for exactly the items the submitted lines link to.

    Keyed by the line's authoritative item id (line_item_id), never by SKU: two
    distinct lots can share a SKU string and a free-text line has none, so a
    SKU key mis-attributes a unit. Fetches only the linked ids in one bounded
    query (mirrors the id-scoped pattern in assert_document_item_uniqueness),
    never the whole inventory.

    Returns every id that RESOLVES to a real item projection (its value may be
    None when the item carries no sell_by), so a caller can distinguish a linked
    id that resolves-but-has-no-unit from one that resolves to no item at all.
    An id absent from the result did not resolve to any item.
    """
    ids = {line_item_id(li) for li in line_items if isinstance(li, dict)}
    ids.discard(None)
    if not ids:
        return {}
    rows = (
        await session.execute(
            select(Projection).where(
                Projection.company_id == company_id,
                Projection.entity_type == "item",
                Projection.entity_id.in_(ids),
            )
        )
    ).scalars().all()
    return {row.entity_id: row.state.get("sell_by") for row in rows}


def _check_line_quantity(
    qty_raw,
    sell_by: str | None,
    unit_map: dict[str, dict],
    *,
    require_positive: bool,
    label: str,
) -> None:
    """The shared per-line quantity gate used by every List and document writer.

    Applies its own type/finiteness gate first, independent of sell_by: a None, a
    bool, a value that does not coerce to float, or a non-finite float (NaN/inf) is
    rejected 422 naming the offending line. This own gate is required because
    validate_line_quantity skips entirely when sell_by is absent/unknown/service,
    and its positive check only rejects qty <= 0, so NaN and True slip past it. Only
    when a sell_by resolves is the positive/decimal check delegated to
    validate_line_quantity. A numeric 0 is a legitimate value and is not rejected by
    the finiteness gate; the per-unit rule decides it (audit lists pass
    require_positive=False so a real zero on-hand count is allowed).
    """
    if qty_raw is None or isinstance(qty_raw, bool):
        raise HTTPException(status_code=422, detail=f"{label}: quantity is required and must be a number")
    try:
        qty = float(qty_raw)
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail=f"{label}: quantity must be a number, got {qty_raw!r}")
    if not math.isfinite(qty):
        raise HTTPException(status_code=422, detail=f"{label}: quantity must be a finite number, got {qty_raw!r}")
    validate_line_quantity(qty, sell_by, unit_map, label=label, require_positive=require_positive)


def _reject_line_attributes(line_items: list) -> None:
    """A line's attributes become the received lot's (see receive), so a line naming a field
    only the app writes is refused when it is written, as every item writer refuses it."""
    for li in line_items:
        attributes = li.get("attributes") if isinstance(li, dict) else getattr(li, "attributes", None)
        if attributes:
            reject_system_item_fields({"attributes": attributes})


async def _validate_document_line_quantities(
    line_items: list[dict], session: AsyncSession, company_id: str, *, require_positive: bool = True
) -> None:
    """Reject malformed document line quantities at the function boundary before a write.

    Documents preserve the unit captured on the line at the time it was added, so the
    submitted sell_by wins and the linked item's stored unit is only a fallback when the
    line carries none. Delegates each line to the shared _check_line_quantity gate. An
    unlinked / free-text line (no id, no line-supplied sell_by) gets only the finiteness
    gate.
    """
    if not line_items:
        return
    unit_map = await _get_unit_map(session, company_id)
    id_sell_by = await _line_sell_by_map(session, company_id, line_items)
    for li in line_items:
        if not isinstance(li, dict):
            continue
        label = li.get("name") or li.get("sku") or "Line item"
        resolved_sell_by = li.get("sell_by") or id_sell_by.get(line_item_id(li))
        _check_line_quantity(
            li.get("quantity"), resolved_sell_by, unit_map,
            require_positive=require_positive, label=label,
        )


async def _validate_list_line_quantities(
    line_items: list[dict], session: AsyncSession, company_id: str, *, require_positive: bool = True,
    stored: list | None = None,
) -> None:
    """Reject malformed List line quantities at the function boundary before a write.

    A List line linked by item_id takes the item's STORED unit, never the submitted one:
    a stocked piece cannot be smuggled past the positive/decimal rule by submitting a
    service unit. A linked item_id that resolves to no real item is rejected 422 by
    linked_items (the rule every line writer shares) rather than silently dropping to the
    free-text finiteness gate. ``stored`` is the stored lines these replace: as many lines
    per id as they held are carried forward even when the item has since gone, and such a
    line, having no stored unit, is checked as submitted.
    Delegates each line to the shared _check_line_quantity gate; an unlinked / free-text
    line (no id) uses its own submitted sell_by.
    """
    if not line_items:
        return
    unit_map = await _get_unit_map(session, company_id)
    items = await linked_items(session, company_id, line_items, known=line_id_counts(stored))
    for li in line_items:
        if not isinstance(li, dict):
            continue
        label = li.get("name") or li.get("sku") or "Line item"
        lid = line_item_id(li)
        if lid in items:
            # Linked line: the stored unit governs; a submitted sell_by is ignored.
            resolved_sell_by = items[lid].state.get("sell_by")
        else:
            resolved_sell_by = li.get("sell_by")
        _check_line_quantity(
            li.get("quantity"), resolved_sell_by, unit_map,
            require_positive=require_positive, label=label,
        )


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


async def _assert_sales_line_price_permission(
    session: AsyncSession,
    company_id,
    settings: dict,
    role: str,
    incoming_lines: list[dict],
    stored_by_idx: dict[int, dict] | None,
) -> None:
    """Reject a sales-document or quotation price change without set_sales_doc_prices.

    Callers apply it only to sales-priced documents (SALES_PRICED_DOC_TYPES) and
    money lists (is_money_list); purchase-side prices are never gated by it.

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


@dataclass
class DocListFilters:
    """Every filter the document list accepts, as one query-parameter dependency, so the list and
    its CSV export read the same filters and can never drift apart."""

    doc_type: str | None = None
    status: str | None = None
    status_in: str | None = None
    exclude_status: str | None = None
    date_from: str | None = None
    date_to: str | None = None
    due_from: str | None = None
    due_to: str | None = None
    q: str | None = None
    contact_id: str | None = None
    overdue_only: bool = False
    all_issued: bool = False
    unfulfilled_only: bool = False
    not_restocked: bool = False
    not_stocked: bool = False
    converted_to_type: str | None = None
    ids: str | None = None
    sort: str | None = None
    dir: str = "desc"


# Sort keys the list accepts, mapped to the state field they order by. "updated" orders by the
# projection's updated_at column, which the rows expose as _updated_at.
_DOC_SORT_FIELDS = {
    "number": "doc_number",
    "type": "doc_type",
    "contact": "contact_name",
    "date": "issue_date",
    "due": "due_date",
    "total": "total",
    "outstanding": "amount_outstanding",
    "status": "status",
    "updated": "_updated_at",
}
_DOC_NUMERIC_SORT_FIELDS = frozenset({"total", "amount_outstanding"})


def _doc_sql_where(company_id: str, f: DocListFilters) -> list:
    """The SQL WHERE for ``f`` (company scope included): every filter the DB can evaluate. The
    multi-field filters (overdue_only, unfulfilled_only, not_restocked, not_stocked) are not
    here; query_docs applies them in Python, and the summary ignores them by design."""
    id_list = [x.strip() for x in f.ids.split(",") if x.strip()] if f.ids else []
    if len(id_list) > MAX_IDS_FILTER:
        raise HTTPException(status_code=422, detail=f"ids accepts at most {MAX_IDS_FILTER} document ids")
    base_where = [
        Projection.company_id == company_id,
        Projection.entity_type == "doc",
    ]
    if f.doc_type:
        base_where.append(Projection.state["doc_type"].as_string() == f.doc_type)
    if f.status:
        base_where.append(Projection.state["status"].as_string() == f.status)
    if f.status_in:
        _allowed = set(f.status_in.split(","))
        base_where.append(Projection.state["status"].as_string().in_(_allowed))
    if f.exclude_status:
        base_where.append(Projection.state["status"].as_string() != f.exclude_status)
    if f.all_issued:
        base_where.append(Projection.state["status"].as_string().notin_((DRAFT, VOID)))
    if f.converted_to_type:
        base_where.append(Projection.state["converted_to_type"].as_string() == f.converted_to_type)
    if f.contact_id:
        base_where.append(Projection.state["contact_id"].as_string() == f.contact_id)
    if id_list:
        base_where.append(Projection.entity_id.in_(id_list))
    if f.date_from:
        base_where.append(Projection.state["issue_date"].as_string() >= f.date_from)
    if f.date_to:
        base_where.append(Projection.state["issue_date"].as_string() <= f.date_to)
    if f.due_from:
        base_where.append(Projection.state["due_date"].as_string() >= f.due_from)
    if f.due_to:
        base_where.append(Projection.state["due_date"].as_string() <= f.due_to)
    _q_clause = doc_q_clause(f.q)
    if _q_clause is not None:
        base_where.append(_q_clause)
    return base_where


def _doc_sort_field(f: DocListFilters) -> str:
    """The state field ``f.sort``/``f.dir`` order by, or a 422 naming the accepted values."""
    if f.dir not in ("asc", "desc"):
        raise HTTPException(status_code=422, detail="dir must be asc or desc")
    if f.sort is None:
        return "issue_date"
    field = _DOC_SORT_FIELDS.get(f.sort)
    if field is None:
        raise HTTPException(status_code=422, detail=f"Unknown sort {f.sort!r}. Choose from: {', '.join(_DOC_SORT_FIELDS)}")
    return field


_SQL_NUMBER_PATTERN = r"^[+-]?([0-9]+[.]?[0-9]*|[.][0-9]+)([eE][+-]?[0-9]+)?$"


def _sql_number(expr):
    """``expr`` (json text) as NUMERIC, or NULL when it is not a number. Imported and hand-edited
    documents can hold text such as "N/A" in an amount field; a plain cast fails the whole query on
    one such row, where this treats that row's amount as missing."""
    return _sa.case((expr.op("~")(_SQL_NUMBER_PATTERN), _sa.cast(expr, _sa.Numeric)), else_=None)


def _doc_sql_order(field: str, descending: bool) -> list:
    """ORDER BY for a sort field, with the unique entity_id tiebreak so the sort is a TOTAL order.
    Without it, rows sharing a value come back in an arbitrary order that differs between the
    per-page queries, so a row on a page boundary can be skipped (or duplicated) by OFFSET."""
    if field == "_updated_at":
        expr = Projection.updated_at
    else:
        values = [
            _func.nullif(Projection.state[k].as_string(), "")
            for k in (field, *DOC_FIELD_FALLBACKS.get(field, ()))
        ]
        expr = _func.coalesce(*values) if len(values) > 1 else values[0]
        if field in _DOC_NUMERIC_SORT_FIELDS:
            expr = _sql_number(expr)
    if descending:
        return [expr.desc().nulls_last(), Projection.entity_id.desc()]
    return [expr.asc().nulls_first(), Projection.entity_id.asc()]


def _doc_display(state: dict) -> dict:
    """``state`` with each displayed field filled from its older keys (``doc_value``). The sort
    orders by the same keys, so the order on the page is the order of what the page shows."""
    return state | {field: doc_value(state, field) for field in DOC_FIELD_FALLBACKS}


def _doc_row(r: Projection) -> dict:
    """The list row for a document: its displayed state (``_doc_display``) with ``id`` and
    ``_updated_at``."""
    return _doc_display(r.state | {"id": r.entity_id, "_updated_at": r.updated_at.isoformat() if r.updated_at else None})


# The state keys the row-by-row filters of ``_doc_filter`` read, before display fallbacks.
_DOC_FILTER_KEYS = ("doc_type", "status", "due_date", "amount_outstanding", "total", "fulfillment_status", "return_received_items", "received_items")


def _doc_filter(f: DocListFilters, today: str):
    """The filters that run row by row over displayed rows (``_doc_display``), as one predicate,
    or None when none is set and every filter is in the SQL WHERE."""
    checks = []
    if f.overdue_only:
        checks.append(lambda x: is_overdue_document(x, today))
    if f.unfulfilled_only:
        checks.append(lambda x: x.get("status") not in ("draft", "void", "closed") and x.get("fulfillment_status") != "fulfilled")
    if f.not_restocked:
        checks.append(lambda x: x.get("status") not in ("draft", "void") and not (x.get("return_received_items") or []))
    if f.not_stocked:
        checks.append(lambda x: x.get("status") not in ("draft", "void") and not (x.get("received_items") or []))
    return (lambda x: all(c(x) for c in checks)) if checks else None


def _doc_base_amounts(state: dict, fields: tuple[str, ...], base_currency: str) -> dict[str, Decimal] | None:
    """``fields`` of a document (``doc_value``; missing is 0, and ``amount_outstanding`` is what
    it still owes, ``outstanding_balance``) in the company currency, each rounded at its
    precision, or None when the document cannot be valued there: its exchange rate is unknown or
    invalid (``doc_rate``), or an amount is not a number. None is never counted as 0 or at a rate
    of 1."""
    try:
        rate = doc_rate(state, base_currency)
        if rate is None:
            return None
        amounts = {
            f: outstanding_balance(state) if f == "amount_outstanding" else to_decimal(doc_value(state, f) or 0)
            for f in fields
        }
    except (ArithmeticError, TypeError, ValueError):
        return None
    if not all(a is not None and a.is_finite() for a in amounts.values()):
        return None
    return {f: round_money(a * rate, base_currency) for f, a in amounts.items()}


# The statuses in which a document takes a payment (apply_doc_payment).
PAYABLE_STATUSES = frozenset({"sent", "final", "partial", "paid", "received", "partially_received",
                              "awaiting_payment"})


def _payable_balance(state: dict) -> Decimal:
    """What a document still owes (``outstanding_balance``) at its currency's precision, for a
    payment, credit or refund to be checked against; 409 when the recorded balance is not a
    number."""
    balance = outstanding_balance(state)
    if balance is None:
        raise HTTPException(status_code=409, detail="The document's outstanding balance is not a number")
    return round_money(balance, str(state.get("currency") or "USD").upper())


async def query_docs(session: AsyncSession, company_id: str, f: DocListFilters, *, limit: int | None, offset: int = 0) -> dict:
    """The filtered, sorted document list (newest first unless ``sort``/``dir`` say otherwise):
    ``{"items", "total"}`` where items carry ``id``. ``limit=None`` returns every matching row;
    the index passes its page."""
    # Every single-field filter is in the SQL WHERE. The multi-field filters (``_doc_filter``) run
    # in Python over the SQL-ordered rows, so both paths share one ORDER BY.
    base_where = _doc_sql_where(company_id, f)
    order_by = _doc_sql_order(_doc_sort_field(f), f.dir == "desc")
    keep = _doc_filter(f, today_iso())

    if keep is not None:
        rows = (await session.execute(select(Projection).where(*base_where).order_by(*order_by))).scalars().all()
        out = [x for x in map(_doc_row, rows) if keep(x)]
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
        .order_by(*order_by)
        .offset(offset)
    )
    if limit is not None:
        list_q = list_q.limit(limit)

    rows = (await session.execute(list_q)).scalars().all()
    return {"items": [_doc_row(r) for r in rows], "total": total}


@router.get("", dependencies=[require_permission("view_documents")], openapi_extra={"x-celerp-agent": True})
async def list_docs(
    filters: DocListFilters = Depends(),
    limit: int | None = None,
    offset: int = 0,
    company_id: str = Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    return await query_docs(session, company_id, filters, limit=limit, offset=offset)


async def _ids_numbered(session: AsyncSession, company_id, kind: Literal["doc", "list"], number: str,
                        doc_type: str | None = None) -> list[str]:
    """Every Document (of doc_type, when given) or List whose number is exactly ``number``,
    ignoring case as the import key does."""
    fields = ("doc_number", "ref_id") if kind == "doc" else ("ref_id",)
    wanted = number.strip().lower()
    query = select(Projection.entity_id).where(
        Projection.company_id == company_id,
        Projection.entity_type == kind,
        _sa.or_(*(_func.lower(Projection.state[f].as_string()) == wanted for f in fields)),
    )
    if doc_type:
        query = query.where(Projection.state["doc_type"].as_string() == doc_type)
    return sorted((await session.execute(query)).scalars().all())


async def _assert_import_number_free(session: AsyncSession, company_id, kind: Literal["doc", "list"], data: dict) -> None:
    """Refuse an imported new Document or List whose number another one of its kind already has."""
    await lock_company(session, company_id)
    fields = ("doc_number", "ref_id") if kind == "doc" else ("ref_id",)
    for number in {str(data[f]).strip() for f in fields if str(data.get(f) or "").strip()}:
        doc_type = data.get("doc_type") if kind == "doc" else None
        if await _ids_numbered(session, company_id, kind, number, doc_type):
            label = "Document" if kind == "doc" else "List"
            raise HTTPException(status_code=409, detail=f"{label} number '{number}' already exists")


@router.get("/numbered", dependencies=[require_permission("view_documents")])
async def docs_numbered(
    number: str = Query(min_length=1),
    doc_type: str | None = None,
    company_id: str = Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """The ids of the documents numbered exactly ``number``."""
    return {"ids": await _ids_numbered(session, company_id, "doc", number, doc_type)}


@router.get("/summary", dependencies=[require_permission("view_documents")], openapi_extra={"x-celerp-agent": True})
async def get_doc_summary(
    filters: DocListFilters = Depends(),
    company_id: str = Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Counts and totals for the document list, over the same filters as list_docs (type, search,
    contact, ids, date window) so the cards over a filtered list count the rows the list shows.
    The status filters are ignored: the cards split the filtered set by status.

    Totals are in the company currency (``_doc_base_amounts``). A document that cannot be valued
    there is left out of every total and counted in ``unvalued_count``."""
    today = today_iso()
    company = await session.get(Company, company_id)
    base_currency = (company.settings or {}).get("currency", "USD") if company else "USD"
    summary_where = _doc_sql_where(company_id, _dc_replace(filters, status=None, status_in=None, exclude_status=None, all_issued=False))
    rows = (await session.execute(select(Projection).where(*summary_where))).scalars().all()
    totals = dict.fromkeys((
        "ar_gross", "ar_paid", "ar_outstanding", "awaiting_payment", "overdue", "paid", "sent",
        "draft", "void", "memo", "unfulfilled",
    ), Decimal(0))
    count_by_status: dict[str, int] = {}
    invoice_count = 0
    awaiting_payment_count = 0
    overdue_count = 0
    paid_count = 0
    unfulfilled_count = 0
    not_restocked_count = 0
    not_stocked_count = 0
    unvalued_count = 0

    def add(key: str, amounts: dict[str, Decimal] | None, field: str) -> None:
        if amounts is not None:
            totals[key] += amounts[field]

    for row in rows:
        state = row.state
        st = state.get("status", "")
        count_by_status[st] = count_by_status.get(st, 0) + 1
        dt = state.get("doc_type")
        if dt == "invoice":
            amounts = _doc_base_amounts(state, ("total", "amount_outstanding", "amount_paid"), base_currency)
            if amounts is None:
                unvalued_count += 1
            if st == "draft":
                add("draft", amounts, "total")
                continue
            if st == "void":
                add("void", amounts, "total")
                continue
            invoice_count += 1
            add("ar_gross", amounts, "total")
            add("ar_paid", amounts, "amount_paid")
            add("ar_outstanding", amounts, "amount_outstanding")
            if state.get("fulfillment_status") != "fulfilled":
                unfulfilled_count += 1
                add("unfulfilled", amounts, "total")
            if is_awaiting_payment(dt, st):
                awaiting_payment_count += 1
                add("awaiting_payment", amounts, "amount_outstanding")
                if is_overdue_document(state, today):
                    overdue_count += 1
                    add("overdue", amounts, "amount_outstanding")
                if st == "sent":
                    add("sent", amounts, "amount_outstanding")
            elif st == "paid":
                paid_count += 1
                add("paid", amounts, "total")
        else:
            if st in ("void", "draft"):
                continue
            if dt == "memo":
                amounts = _doc_base_amounts(state, ("total",), base_currency)
                if amounts is None:
                    unvalued_count += 1
                add("memo", amounts, "total")
            if is_awaiting_payment(dt, st):
                awaiting_payment_count += 1
            if is_overdue_document(state, today):
                overdue_count += 1
            if dt == "credit_note":
                if not (state.get("return_received_items") or []):
                    not_restocked_count += 1
            if dt == "bill":
                if not (state.get("received_items") or []):
                    not_stocked_count += 1
    money = {k: to_stored_float(round_money(v, base_currency)) for k, v in totals.items()}
    draft_count = count_by_status.get("draft", 0)
    total_rows = sum(count_by_status.values())
    live_count = total_rows - draft_count
    all_issued_count = live_count - count_by_status.get("void", 0)
    return {
        "total_count": live_count,
        "draft_count": draft_count,
        "non_void_count": sum(v for k, v in count_by_status.items() if k not in ("void", "draft")),
        "all_issued_count": all_issued_count,
        "all_issued_total": money["ar_gross"],
        "awaiting_payment_count": awaiting_payment_count,
        "awaiting_payment_total": money["awaiting_payment"],
        "overdue_count": overdue_count,
        "overdue_total": money["overdue"],
        "paid_count": paid_count,
        "paid_total": money["paid"],
        "sent_total": money["sent"],
        "draft_total": money["draft"],
        "void_total": money["void"],
        "ar_total": money["ar_gross"],
        "ar_paid": money["ar_paid"],
        "ar_outstanding": money["ar_outstanding"],
        "invoice_count": invoice_count,
        "unfulfilled_count": unfulfilled_count,
        "unfulfilled_total": money["unfulfilled"],
        "not_restocked_count": not_restocked_count,
        "not_stocked_count": not_stocked_count,
        "unvalued_count": unvalued_count,
        "memo_all_total": money["memo"],
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
    company = await locked_company(session, company_id)
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


async def _line_holds(session: AsyncSession, company_id, owner_id: str) -> dict[str, float]:
    """How much each line of a document or List holds, by line id, read from the holds
    stamped with their line. A page shows a line holding less than its quantity as held in
    part, never as fully reserved. Kept apart from the lines so no line write can store it."""
    rows = (await session.execute(select(
        Projection.state["status_line_entity_id"].as_string(), Projection.state["quantity"].as_string(),
    ).where(
        Projection.company_id == company_id,
        Projection.entity_type == "item",
        Projection.state["status_doc_id"].as_string() == owner_id,
        Projection.state["status"].as_string() == "reserved",
    ))).all()
    holds: dict[str, float] = {}
    for line_id, qty in rows:
        if line_id:
            holds[line_id] = holds.get(line_id, 0.0) + float(qty or 0)
    return holds


@router.get("/{entity_id}", dependencies=[require_permission("view_documents")], openapi_extra={"x-celerp-agent": True})
async def get_doc(entity_id: str, company_id: str = Depends(get_current_company_id), session: AsyncSession = Depends(get_session)) -> dict:
    row = await _get_doc(session, company_id, entity_id)
    doc = row.state | {"id": row.entity_id, "version": row.version}
    # Payments Stripe reported are refunded or reversed in Stripe, never here.
    if held := await stripe_payment_indexes(session, company_id, entity_id, doc.get("payments") or []):
        doc["payments"] = [p | {"held_by": "stripe"} if p.get("index") in held else p for p in doc["payments"]]
    # Recorded by a person from the unmatched payments: deleting it puts it back there.
    if recorded := await recorded_unmatched(session, company_id, entity_id):
        doc["payments"] = [p | {"unmatched": True} if p.get("reference") in recorded and p.get("status") == "active"
                           and not p.get("refunded") else p for p in doc.get("payments") or []]
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
        # How much of each line is still out (on memo or sold), the most Set as available
        # can take back on it.
        out, line_of = await _lots_out(session, company_id, entity_id, row.state,
                                       await _memo_allocation_items(session, company_id, entity_id))
        out_by_line: dict[int, float] = {}
        for eid, idx in line_of.items():
            if idx is not None:
                out_by_line[idx] = out_by_line.get(idx, 0.0) + float(out[eid].state.get("quantity") or 0)
        lines = list(doc.get("line_items") or [])
        for i, qty in out_by_line.items():
            if i < len(lines):
                lines[i] = {**lines[i], "out_quantity": qty}
        doc["line_items"] = lines
    doc["line_holds"] = await _line_holds(session, company_id, entity_id)
    returnable = doc.get("status") in SUPPLIER_RETURN_STATUSES
    if doc.get("doc_type") in ("bill", "consignment_in", "purchase_order") and (
            returnable or doc.get("returned_items")):
        # How much of each line can still go back to the supplier, in stock units, what holds
        # the rest of what it brought in (return_held, reason -> units), and whether the line
        # has sent goods back, all or part of it. Lines whose goods cannot be traced to them
        # alone carry none of these and are not offered for return.
        traced = await _line_return_lots(session, company_id, row.state, lock=False)
        if traced is not None:
            by_line, shared, kept, held = traced
            sent_back: dict[str, float] = {}
            for x in doc.get("returned_items") or []:
                sent_back[x["item_id"]] = sent_back.get(x["item_id"], 0.0) + float(x.get("quantity_returned") or 0)
            lines = list(doc.get("line_items") or [])
            for i, lots in by_line.items():
                if i >= len(lines) or any(lot in shared for lot, _ in lots):
                    continue
                line = {**lines[i]}
                if returnable:
                    line["returnable_quantity"] = sum(q for _, q in lots)
                    if held.get(i):
                        line["return_held"] = held[i]
                if sum(sent_back.get(lot, 0.0) for lot, _ in lots) > 1e-9:
                    line["return_status"] = (
                        "returned" if sum(kept.get(lot, 0.0) for lot, _ in lots) <= 1e-9 else "partial_returned")
                lines[i] = line
            doc["line_items"] = lines
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
    self_contact: dict = {}
    if company_row:
        self_id = (company_row.settings or {}).get("self_contact_id")
        if self_id:
            self_row = await session.get(Projection, (company_id, self_id))
            if self_row is not None and self_row.entity_type == "contact":
                self_contact = self_row.state or {}
    contact: dict = {}
    if doc.get("contact_id"):
        contact_row = await session.get(Projection, (company_id, doc["contact_id"]))
        if contact_row is not None and contact_row.entity_type == "contact":
            contact = contact_row.state or {}
    doc = prepare_document_output(
        doc, company=company, self_contact=self_contact, contact=contact,
    )

    # When the company shows barcodes on lines, backfill lines saved before
    # barcode stamping from their catalog items (mirror of the share view).
    from celerp.services.line_measures import LINE_IDENTIFIER_MODES, identifier_backfill
    if company.get("line_item_identifier") in LINE_IDENTIFIER_MODES and company["line_item_identifier"] != "sku":
        for li in doc.get("line_items") or []:
            eid = li.get("entity_id") or li.get("item_id")
            if li.get("barcode") or not eid:
                continue
            irow = await session.get(Projection, (company_id, eid))
            if irow is not None and irow.entity_type == "item":
                identifier_backfill(li, irow.state or {})

    # Footer import link only while the share link is live, so saved PDFs
    # never carry a URL that 404s.
    from celerp_docs.routes_share import _find_share_row, share_import_url
    import_url = await share_import_url(session, await _find_share_row(session, company_id, entity_id))

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


@router.post("", openapi_extra={"x-celerp-agent": True, "x-celerp-agent-idempotent": True})
async def create_doc(
    payload: DocCreatePayload,
    company_id: str = Depends(get_current_company_id),
    _: None = require_permission("edit_documents"),
    role: str = Depends(get_current_role),
    settings: dict = Depends(get_current_company_settings),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    idem_key = payload.idempotency_key or str(uuid.uuid4())
    digest = _request_digest("doc", None, None, payload.model_dump(mode="json", exclude={"idempotency_key"}))
    if payload.idempotency_key:
        replay = await find_event_by_idempotency(session, company_id, idem_key)
        if replay is not None:
            return _replay_result(replay, event_type="doc.created", digest=digest)

    # The contact first, as every contact-reference writer takes it (its lock takes the
    # company lock, then the contact row). The company lock comes before any line check: Revert to Draft and Reserve take it too, so
    # the lines are checked as the last of them left the items, and numbering is serialized.
    contact = await _lock_selected_contact(session, company_id, settings, role, payload.contact_id or "")
    company = await locked_company(session, company_id)

    if payload.doc_type == "credit_note" and payload.original_doc_id:
        # Locked, so the balance reduced below is the one the invoice's last writer left.
        inv = await _get_doc(session, company_id, payload.original_doc_id, for_update=True)
        original_total = float(inv.state.get("total", 0) or 0)
        if payload.total > original_total + 1e-9:
            raise HTTPException(status_code=409, detail="Credit note total cannot exceed original invoice total")
    require_currency_code(payload.currency)

    _assert_date_order(payload.model_dump(exclude_none=True))

    _reject_line_attributes(payload.line_items)
    # Validate line item quantities against sell_by unit precision
    if payload.line_items:
        unit_map = await _get_unit_map(session, company_id)
        sell_by_map = await _get_item_sell_by_map(session, company_id)
        for li in payload.line_items:
            resolved_sell_by = li.sell_by or (sell_by_map.get(li.sku) if li.sku else None)
            validate_line_quantity(li.quantity, resolved_sell_by, unit_map, label=li.name or li.sku or "Line item")

        # Price-override gate: on a sales document, a new line whose unit_price
        # deviates from the item's catalog price is a price override, rejected when
        # the caller lacks set_sales_doc_prices. This closes the create path so the
        # gate cannot be bypassed by making a new draft with overridden prices.
        if payload.doc_type in SALES_PRICED_DOC_TYPES:
            await _assert_sales_line_price_permission(
                session, company_id, settings, role,
                [li.model_dump() for li in payload.line_items], None,
            )

    # Re-check under the company lock, which also owns numbering. A concurrent
    # retry can only reach this point before the first request commits; once it does,
    # the second request observes the original event and returns without consuming a
    # second document number.
    replay = await find_event_by_idempotency(session, company_id, idem_key)
    if replay is not None:
        return _replay_result(replay, event_type="doc.created", digest=digest)
    ref_id = payload.ref_id or next_draft_ref(company, payload.doc_type)
    entity_id = f"doc:{ref_id}"

    # Uniqueness check: reject if a doc with this ref_id already exists
    existing = await session.get(Projection, {"company_id": company_id, "entity_id": entity_id})
    if existing is not None:
        raise HTTPException(status_code=409, detail=f"Document number '{ref_id}' already exists")

    data = payload.model_dump(exclude_none=True)
    chosen = _chosen_terms(data)
    if payload.doc_type == "credit_note" and payload.original_doc_id:
        # A credit note refunds its invoice's amounts, which the contact's prices do not change.
        chosen |= {"currency", "price_list"}
    # Canonicalize the historical `terms` alias at the API boundary so every
    # newly-created document stores one customer-facing terms field.
    data.pop("terms", None)
    data["ref_id"] = ref_id
    data.setdefault("currency", company.settings.get("currency", "USD"))
    data.update(resolve_document_terms(
        payload.model_dump(), company.settings or {}, payload.doc_type,
        explicit_fields=payload.model_fields_set,
    ))

    # Default issue_date to the company's today so date filters and sorting work correctly on new docs
    data.setdefault("issue_date", business_date_of(None, company.settings.get("timezone")))

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
        # the tax-inclusive total entirely to revenue and recorded zero output VAT,
        # overstating revenue and understating the VAT liability. total = subtotal + tax + shipping.
        data["tax"] = to_stored_float(round_money(effective_tax_d, currency))

    if contact is not None:
        data.update(await _contact_selection_values(
            session, company_id, settings, role, data,
            kind="doc", contact_id=payload.contact_id, contact=contact, client_values=data, chosen=chosen,
        ))

    data["amount_paid"] = 0.0
    data["amount_outstanding"] = float(data.get("total", 0))

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
        idempotency_key=idem_key,
        metadata_={"request": digest},
    )

    if getattr(entry, "was_deduped", False):
        return {"event_id": entry.id, "id": entry.entity_id}

    if payload.doc_type == "credit_note" and payload.original_doc_id:
        inv = await _get_doc(session, company_id, payload.original_doc_id)
        outstanding = to_stored_float(max(Decimal(0), _payable_balance(inv.state) - round_money(
            payload.total, str(inv.state.get("currency") or "USD").upper())))
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
            idempotency_key=f"{idem_key}:credit-note-original",
            metadata_={"source_credit_note": entry.entity_id},
        )
    await session.commit()
    return {"event_id": entry.id, "id": entry.entity_id}


@router.patch("/{entity_id}", openapi_extra={"x-celerp-agent": True, "x-celerp-agent-idempotent": True})
async def patch_doc(entity_id: str, payload: DocPatch, company_id: str = Depends(get_current_company_id), _: None = require_permission("edit_documents"), role: str = Depends(get_current_role), settings: dict = Depends(get_current_company_settings), user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    result = await write_doc_patch(session, company_id, role, settings, user, entity_id, payload)
    await session.commit()
    return result


async def write_doc_patch(session: AsyncSession, company_id, role: str, settings: dict, user, entity_id: str, payload: DocPatch) -> dict:
    """Apply a document edit without committing, so an import can make it part of a larger unit."""
    fields_changed = dict(payload.fields_changed)
    require_currency_code((fields_changed.get("currency") or {}).get("new"))
    _refuse_protected_fields(fields_changed)
    selecting = "contact_id" in fields_changed
    new_contact_id = str((fields_changed.get("contact_id") or {}).get("new") or "")
    if selecting and "line_items" in fields_changed:
        raise HTTPException(status_code=422, detail="Change the contact and the line items in separate saves.")
    digest, idem_key = _patch_identity("doc", entity_id, payload)
    if (done := await _find_patch_replay(session, company_id, idem_key, "doc.updated", entity_id, digest)) is not None:
        return done
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
    # The selected contact is locked before the document, the order merge and delete use.
    contact = await _lock_selected_contact(session, company_id, settings, role, new_contact_id) if selecting else None
    # Locked load so the version check and the emit are one compare-and-set, as for lists.
    row = await _get_doc(session, company_id, entity_id, for_update=True)
    if (done := await _find_patch_replay(session, company_id, idem_key, "doc.updated", entity_id, digest)) is not None:
        return done
    if payload.expected_version is not None and row.version != payload.expected_version:
        raise HTTPException(status_code=409, detail="This document was changed by someone else; reload to get the latest before saving")
    if selecting:
        client_values = {k: (v or {}).get("new") for k, v in fields_changed.items()}
        selection = await _contact_selection_values(
            session, company_id, settings, role, row.state,
            kind="doc", contact_id=new_contact_id, contact=contact, client_values=client_values,
        )
        fields_changed.update({f: {"new": v} for f, v in selection.items()})
    # Price-override gate: on a sales document, reject unit_price changes when the
    # caller lacks set_sales_doc_prices, comparing incoming lines against the stored
    # lines by index. Runs for drafts and finalized documents alike, before the draft branch.
    _incoming_lines = (fields_changed.get("line_items") or {}).get("new")
    if isinstance(_incoming_lines, list):
        _reject_line_attributes(_incoming_lines)
    if isinstance(_incoming_lines, list) and row.state.get("doc_type") in SALES_PRICED_DOC_TYPES:
        _stored_by_idx = {i: li for i, li in enumerate(row.state.get("line_items") or [])}
        await _assert_sales_line_price_permission(session, company_id, settings, role, _incoming_lines, _stored_by_idx)
    is_draft = row.state.get("status") == "draft"
    if not is_draft:
        locked_fields = set(fields_changed) - _FINALIZED_EDITABLE_FIELDS
        if locked_fields:
            status = row.state.get("status") or "final"
            # A void document comes back through Unvoid; revert to Draft refuses it.
            if status == "void":
                raise HTTPException(status_code=409, detail=refusal(
                    "docs.edit_void", "This document is void and cannot be edited. To make changes, Unvoid it first."))
            raise HTTPException(status_code=409, detail=refusal(
                "docs.edit_locked",
                f"This document is {status.replace('_', ' ')} and cannot be edited. To make changes, revert it to Draft first.",
                doc_status=status))
        # Guard: line_items patch on finalized doc may only touch _LI_FINALIZED_EDITABLE fields
        if "line_items" in fields_changed:
            incoming_lis = (fields_changed["line_items"].get("new") or [])
            existing_lis = row.state.get("line_items") or []
            existing_by_idx = {i: li for i, li in enumerate(existing_lis)}
            for i, incoming in enumerate(incoming_lis):
                original = existing_by_idx.get(i, {})
                for k, v in incoming.items():
                    if k in _LI_FINALIZED_EDITABLE:
                        continue
                    orig_v = original.get(k)
                    if k == "line_id" and not orig_v:
                        continue  # a legacy line is given its id on its first save
                    if v != orig_v:
                        raise HTTPException(
                            status_code=409,
                            detail=f"Field '{k}' in line item {i} cannot be changed on a finalized document.",
                        )
            if row.state.get("doc_type") in _BILL_POSTED:
                # A line moved to another account posts there if the bill is reverted and finalized again.
                await require_line_destinations(session, company_id, [
                    li for i, li in enumerate(incoming_lis)
                    if isinstance(li, dict) and li.get("account_code") != existing_by_idx.get(i, {}).get("account_code")])
    # Uniqueness check when ref_id is being changed
    new_ref = (fields_changed.get("ref_id") or {}).get("new")
    if new_ref:
        await _assert_ref_id_unique(session, company_id, new_ref, exclude_entity_id=entity_id)
    # Validate issue/due date ordering against the merged resulting state
    patch_flat = {k: v.get("new") for k, v in fields_changed.items() if v.get("new") is not None}
    _assert_date_order(patch_flat, row.state)

    # Validate patched line items when present
    new_line_items = (fields_changed.get("line_items") or {}).get("new")
    if new_line_items is not None and isinstance(new_line_items, list):
        # Documents are never audits: the positive rule always applies. The document validator
        # preserves the unit captured on each line (submitted sell_by wins, stored is the
        # fallback) and applies the own finiteness gate, so a NaN/inf/bool quantity can no
        # longer be persisted onto a document.
        await _validate_document_line_quantities(new_line_items, session, company_id)


    # Money fields are stored at currency precision. The client computes subtotal/tax/total as
    # raw JS floats and legacy values may already carry IEEE-754 tails, so round both old and new
    # here - the single chokepoint for every doc edit - so the ledger and history never record
    # values like "346.50000000000006".
    # Stored values round at the stored currency, incoming ones at the currency this patch leaves.
    _old_currency = row.state.get("currency")
    _new_currency = (fields_changed.get("currency") or {}).get("new") or _old_currency
    if is_draft and isinstance(new_line_items, list):
        # Edited lines carry their old tax amounts; the summary reads them, so they follow the line.
        refresh_line_taxes(new_line_items, _new_currency)
    _MONEY_FIELDS = {"subtotal", "tax", "total", "discount_amount"}
    def _round_field(field: str, value, *, incoming: bool = False):
        if field in _MONEY_FIELDS and value is not None:
            return to_stored_float(round_money(value, _new_currency if incoming else _old_currency))
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
    for k, change in fields_changed.items():
        old = _round_field(k, change.get("old") if change.get("old") is not None else row.state.get(k))
        new = _round_field(k, change.get("new"), incoming=True)
        if old != new:
            effective[k] = {"old": old, "new": new}
    if not effective:
        return {"event_id": None, "version": row.version}
    entry = await emit_event(
        session, company_id=company_id, entity_id=entity_id, entity_type="doc", event_type="doc.updated",
        data={"fields_changed": effective, "idempotency_key": idem_key},
        actor_id=user.id, location_id=None, source="api",
        idempotency_key=idem_key or str(uuid.uuid4()), metadata_={"request": digest},
    )
    if getattr(entry, "was_deduped", False):
        return _patch_replay(entry, "doc.updated", entity_id, digest)
    # entry.id is the document's new version, so the client's next versioned write pins
    # exactly the state this patch produced, as patch_list does.
    return {"event_id": entry.id, "version": entry.id}


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
    company = await locked_company(session, company_id)
    if (company.settings or {}).get("pay_tip_shown"):
        return ""
    settings = dict(company.settings or {})
    settings["pay_tip_shown"] = True
    company.settings = settings
    session.add(company)
    return (" Tip: connect a Stripe account under Web Access, Payments and "
            "emailed invoices include a Pay button so customers can pay you online.")


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
    key, digest = _operation("send", entity_id, payload)
    if (done := await _earlier_run(session, company_id, key, event_type="doc.sent",
                                   entity_id=entity_id, digest=digest)) is not None:
        return done
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
        idempotency_key=key, metadata_={"request": digest},
    )

    sent_to = payload.sent_to
    view_url = pay_url = None
    amount_due = float(outstanding_balance(row.state) or 0)
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


async def _refuse_unsellable_lots(session, company_id, entity_id: str, state: dict) -> None:
    """Finalizing an invoice books the cost of the lots its lines are bound to, so each must be
    stock this invoice can sell: free, reserved to it, out on the memo it was converted from (the
    customer keeps it), or already sold to it or to that memo. A lot out on any other memo is
    refused naming that memo, and a lot sold elsewhere, expired or otherwise not stock by name
    and status (409), before anything is booked. A product made from a recipe is exempt: an
    order for it is met by making it."""
    from celerp_inventory.projections import demand_claim, is_manufacturable
    lines = [li.get("entity_id") or li.get("item_id") for li in state.get("line_items") or []]
    lots = await lock_projections(session, company_id, lines)
    sold_to = {entity_id, state.get("source_memo_id")} - {None, ""}
    for eid in dict.fromkeys(e for e in lines if e):
        if eid not in lots:
            continue
        lot = lots[eid].state
        if is_non_stock_line(lot.get("inventory_type"), lot.get("sell_by")) or is_manufacturable(lot):
            continue
        status = str(lot.get("status") or "").lower()
        sku = lot.get("sku") or eid
        if out_on_another_memo(lot, entity_id, state.get("source_memo_id")):
            raise HTTPException(status_code=409, detail=memo_out_refusal(lot, sku))
        if demand_claim(lot, entity_id) is not None or status == "memo_out" or (
                status == "sold" and lot.get("status_doc_id") in sold_to):
            continue
        raise HTTPException(status_code=409, detail=refusal(
            "item.invoice_not_available", f"{sku} is {status}: only available stock can be invoiced.",
            sku=sku, status=status))


# Documents whose finalize posts the bill entry, each line to its own account when it names one.
_BILL_POSTED = frozenset({"purchase_order", "bill"})


async def finalize_document(
    entity_id: str,
    company_id: str,
    user,
    session: AsyncSession,
    *,
    commit: bool = True,
) -> dict:
    """Finalize with caller-owned transaction support for domain integrations."""
    # Company before the doc row: finalizing may draw the next invoice or bill number, and an
    # invoice's recognized COGS reads lot costs that a cost correction may be rewriting.
    _company = await locked_company(session, company_id)
    row = await _get_doc(session, company_id, entity_id, for_update=True)
    # A closed memo is terminal, settled paperwork; finalize maps closed->final in the
    # reducer, silently stripping the terminal status and its close metadata. Refuse under
    # the row lock, same as the other post-close mutations; the user reopens first.
    _reject_if_closed(row.state, "finalize it")
    if row.state.get("status") == "void":
        raise HTTPException(status_code=409, detail="Cannot finalize void document")
    if row.state.get("finalized"):
        return {"event_id": None, "already_finalized": True}
    if not (row.state.get("line_items") or []):
        raise HTTPException(status_code=422, detail="Add at least one line item before finalizing this document.")
    if row.state.get("doc_type") in _BILL_POSTED:
        await require_line_destinations(session, company_id, row.state["line_items"])
    if row.state.get("doc_type") == "invoice":
        await _refuse_unsellable_lots(session, company_id, entity_id, row.state)

    # Snapshot scalar values early — avoids ORM lazy-load issues after multiple flush() calls.
    _initial_doc_state = dict(row.state)
    _user_id = user.id  # Capture before flushes may expire session objects
    doc_type = _initial_doc_state.get("doc_type", "")
    finalize_data: dict = {}
    event_type = "doc.finalized"

    _base_currency = (_company.settings.get("currency", "USD") if _company else "USD")

    _require_doc_rate_http(_initial_doc_state, _base_currency)

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
        _has_stock = any(auto_je.bill_line_kind(li) == "stock" for li in _line_items)
        if not _has_stock:
            finalize_data["skip_receiving"] = True

    entry = await emit_event(
        session, company_id=company_id, entity_id=entity_id, entity_type="doc", event_type=event_type,
        data=finalize_data,
        actor_id=_user_id, location_id=None, source="api", idempotency_key=str(uuid.uuid4()), metadata_={},
    )
    # Modules react to the finalize before its entry is booked, so stock a module makes
    # for the document (manufacturing's make-on-finalize) is what the invoice is costed
    # and later fulfilled from.
    await fire_lifecycle(
        "doc_finalize_hook",
        session=session,
        entity_id=entity_id,
        doc_state=_initial_doc_state,
        company_id=company_id,
        user_id=_user_id,
        doc_type=doc_type,
    )
    # Auto-JE on finalize (invoices, direct bills, or convert to bill (POs))
    if doc_type == "invoice":
        # span_lots: the interactive finalize recognizes a line exceeding its bound
        # lot at the sibling lots that will actually be drawn; import/repair paths
        # stay on exact bound-lot pricing.
        await auto_je.create_for_doc_finalized(session, company_id=company_id, user_id=_user_id, doc_id=entity_id, doc=_initial_doc_state, base_currency=_base_currency, span_lots=True)
        # Finalizing is where the sale of goods out with the customer is confirmed: every
        # lot the invoice holds out on memo (a converted memo's lots, cross-lot siblings
        # included) and every memo_out lot a line binds is sold on the invoice's own number.
        # _refuse_unsellable_lots has refused any lot out on another memo, so only this
        # invoice's or its source memo's lots reach here.
        _cid = uuid.UUID(str(company_id))
        _bound = [_li.get("entity_id") or _li.get("item_id") for _li in _initial_doc_state.get("line_items", [])]
        _held = [p.entity_id for p in await _memo_allocation_items(session, company_id, entity_id)]
        for _eid in dict.fromkeys(e for e in _bound + _held if e):
            _iproj = await session.get(Projection, {"company_id": company_id, "entity_id": _eid})
            if _iproj and _iproj.state.get("status") == "memo_out":
                await emit_event(
                    session, company_id=_cid, entity_id=_eid, entity_type="item",
                    event_type="item.status.set",
                    data={"new_status": "sold", "source_doc_id": entity_id, "doc_number": finalize_data["ref_id"]},
                    actor_id=_user_id, location_id=None, source="invoice_finalize",
                    idempotency_key=str(uuid.uuid4()), metadata_={"doc_id": entity_id},
                )
    elif doc_type in _BILL_POSTED:
        # Bill conversion JE: debit expense/inventory accounts, credit accounts payable
        # Covers both PO->bill conversion and directly-created bills finalized directly.
        # Pass revert_count so cycle-aware idempotency keys are used on re-finalize.
        _revert_count = int(_initial_doc_state.get("revert_count", 0))
        await auto_je.create_for_bill_conversion(session, company_id=company_id, user_id=_user_id, doc_id=entity_id, doc=_initial_doc_state, base_currency=_base_currency, revert_count=_revert_count)
    if commit:
        await session.commit()
    return {"event_id": entry.id}


@router.post("/{entity_id}/finalize")
async def finalize_doc(
    entity_id: str,
    company_id: str = Depends(get_current_company_id),
    _: None = require_permission("finalize_documents"),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    return await finalize_document(entity_id, company_id, user, session, commit=True)


@router.post("/{entity_id}/void")
async def void_doc(entity_id: str, payload: DocVoidBody, company_id: str = Depends(get_current_company_id), _: None = require_permission("finalize_documents"), user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    row = await _get_doc(session, company_id, entity_id, for_update=True)
    key, digest = _operation("void", entity_id, payload)
    if (done := await _earlier_run(session, company_id, key, event_type="doc.voided",
                                   entity_id=entity_id, digest=digest)) is not None:
        return done
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
    await _release_holds(session, company_id=company_id, uid=user.id, owner=row)
    await _return_to_source_memo(session, company_id=company_id, uid=user.id, owner=row)
    entry = await emit_event(
        session, company_id=company_id, entity_id=entity_id, entity_type="doc", event_type="doc.voided",
        data=event_data, actor_id=user.id, location_id=None, source="api",
        idempotency_key=key, metadata_={"request": digest},
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
    key, digest = _operation("close", entity_id, payload)
    if (done := await _earlier_run(session, company_id, key, event_type="doc.closed",
                                   entity_id=entity_id, digest=digest)) is not None:
        return done
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
        raise HTTPException(status_code=409, detail=refusal(
            "docs.close_goods_out",
            f"This memo cannot be closed while {pending} product(s) are still out or held on it. "
            "Sell or return them first.", count=pending))
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
        idempotency_key=key, metadata_={"request": digest},
    )
    await session.commit()
    return {"event_id": entry.id}


@router.post("/{entity_id}/reopen")
async def reopen_doc(entity_id: str, payload: DocReopenBody, company_id: str = Depends(get_current_company_id), _: None = require_permission("finalize_documents"), user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    """Undo a Close: restore the memo to the status it held before closing."""
    row = await _get_doc(session, company_id, entity_id, for_update=True)
    key, digest = _operation("reopen", entity_id, payload)
    if (done := await _earlier_run(session, company_id, key, event_type="doc.reopened",
                                   entity_id=entity_id, digest=digest)) is not None:
        return done
    if row.state.get("status") != "closed":
        raise HTTPException(status_code=409, detail="Only a closed memo can be reopened")
    restored = row.state.get("pre_close_status") or "final"
    event_data = payload.model_dump(exclude_none=True)
    event_data["restored_status"] = restored
    entry = await emit_event(
        session, company_id=company_id, entity_id=entity_id, entity_type="doc", event_type="doc.reopened",
        data=event_data, actor_id=user.id, location_id=None, source="api",
        idempotency_key=key, metadata_={"request": digest},
    )
    await session.commit()
    return {"event_id": entry.id}


@router.post("/{entity_id}/revert-to-draft")
async def revert_doc_to_draft(entity_id: str, payload: DocRevertBody, company_id: str = Depends(get_current_company_id), _: None = require_permission("finalize_documents"), user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    row = await _get_doc(session, company_id, entity_id, for_update=True)
    key, digest = _operation("revert-to-draft", entity_id, payload)
    if (done := await _earlier_run(session, company_id, key, event_type="doc.reverted_to_draft",
                                   entity_id=entity_id, digest=digest)) is not None:
        return done
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
    # Both blocks below name the button on the document's lines that clears them, so the
    # user is sent to the action rather than left to guess where goods are returned.
    if state.get("received_items"):
        raise HTTPException(
            status_code=409,
            detail="Cannot revert to draft while goods received on this document are still in stock. "
                   "Select those lines and use Return Goods first, then revert.",
        )

    # Block revert when any line item has been fulfilled. Fulfilled items are tracked in
    # state["fulfilled_items"]; each entry with a non-null item_id is an inventory item that is
    # now out on memo or sold.
    fulfilled_items = [
        fi for fi in (state.get("fulfilled_items") or [])
        if fi.get("item_id") is not None
    ]
    if fulfilled_items:
        raise HTTPException(
            status_code=409,
            detail="Cannot revert to draft while lines are still out on memo or sold. "
                   "Select those lines and use Set as available first, then revert.",
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
        idempotency_key=key, metadata_={"request": digest},
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
    row = await _get_doc(session, company_id, entity_id, for_update=True)
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
        idempotency_key=str(uuid.uuid4()),
        metadata_={},
    )
    await session.commit()

    updated = await _get_doc(session, company_id, entity_id)
    return updated.state | {"id": entity_id}


@router.post("/{entity_id}/unvoid")
async def unvoid_doc(entity_id: str, payload: DocUnvoidBody, company_id: str = Depends(get_current_company_id), _: None = require_permission("finalize_documents"), user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    row = await _get_doc(session, company_id, entity_id, for_update=True)
    key, digest = _operation("unvoid", entity_id, payload)
    if (done := await _earlier_run(session, company_id, key, event_type="doc.unvoided",
                                   entity_id=entity_id, digest=digest)) is not None:
        return done
    state = row.state
    if state.get("status") != "void":
        raise HTTPException(status_code=409, detail="Can only unvoid documents in 'void' status")
    restored_status = state.get("pre_void_status")
    if not restored_status:
        raise HTTPException(status_code=409, detail="Cannot unvoid: document was voided before unvoid support was added (no pre_void_status)")
    if (memo := await _source_memo(session, company_id, state)) is not None and \
            memo.state.get("converted_to") != entity_id:
        number = memo.state.get("doc_number") or memo.state.get("ref_id") or memo.entity_id
        raise HTTPException(status_code=409, detail=refusal(
            "documents.unvoid_conversion_undone",
            f"The goods on this invoice went back to memo {number} when it was voided. "
            f"Convert the memo again to bill them.", memo=number))

    entry = await emit_event(
        session, company_id=company_id, entity_id=entity_id, entity_type="doc",
        event_type="doc.unvoided",
        data={"unvoided_by": str(user.id), "restored_status": restored_status},
        actor_id=user.id, location_id=None, source="api",
        idempotency_key=key, metadata_={"request": digest},
    )
    # Restore the JEs the void reversed (idempotent - uses doc-scoped keys)
    await auto_je.create_for_doc_unvoided(session, company_id=company_id, user_id=user.id, doc_id=entity_id)
    if state.get("doc_type") == "invoice":
        # Cost corrections made while the invoice was void apply once it stands again.
        try:
            await auto_je.reconcile_doc_cogs(
                session, company_id=company_id, user_id=user.id, doc_id=entity_id,
                cycle_tag=f"unvoid-{entry.id}", ts=None, trigger="doc.unvoided",
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

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
            idempotency_key=_step_key(key, "fulfillment"), metadata_={"restored_from_unvoid": True},
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
    user=Depends(get_current_user),
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
    # Locked and read fresh, so a finalize that committed while this waited is seen.
    # Only a document is deleted here: a draft item or List sharing the ID space
    # is skipped like any other non-draft-document ID.
    rows = await lock_projections(session, company_id, ids)
    drafts = [row for eid in dict.fromkeys(ids)
              if (row := rows.get(eid)) is not None and row.entity_type == "doc" and row.state.get("status") == "draft"]

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
        await _release_holds(session, company_id=company_id, uid=user.id, owner=row)
        await _return_to_source_memo(session, company_id=company_id, uid=user.id, owner=row)
        await session.execute(_sa.delete(Projection).where(Projection.company_id == company_id, Projection.entity_id == eid))
        await session.execute(_sa.delete(LedgerEntry).where(LedgerEntry.company_id == company_id, LedgerEntry.entity_id == eid))
        deleted.append(eid)
    await session.commit()
    return {"deleted": deleted, "count": len(deleted)}


@router.delete("/{entity_id}")
async def delete_doc(entity_id: str, company_id: str = Depends(get_current_company_id), _: None = require_permission("delete_documents"), user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    row = await _get_doc(session, company_id, entity_id, for_update=True)
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
    await _release_holds(session, company_id=company_id, uid=user.id, owner=row)
    await _return_to_source_memo(session, company_id=company_id, uid=user.id, owner=row)
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
        from celerp.services.je_keys import je_minted
        while await je_minted(session, company_id, key_doc_id, f"{key_type}:{idx}"):
            idx += 1
    return idx


async def books_currency_still(session, company_id, base: str) -> str:
    """*base*, or 422 when the company now keeps its books in another currency. The
    company is read FOR SHARE, so a settings change either committed first and is seen
    here, or waits for the caller's transaction."""
    company = (await session.execute(
        select(Company).where(Company.id == company_id).with_for_update(read=True)
        .execution_options(populate_existing=True))).scalar_one_or_none()
    current = books_currency((company.settings or {}) if company else {})
    if base != current:
        raise HTTPException(status_code=422, detail=f"The payment is on {base} books; the company keeps them in {current}")
    return current


async def _books_still_kept(session, company_id, doc_state: dict, books: tuple[str, Decimal]) -> tuple[str, float]:
    """The (base currency, document rate) a payment's *books* post on, or 422 when they no
    longer describe the ledger: the company now keeps its books in another currency, or
    the document now converts into them at another rate. Called under the document's row
    lock with its locked state."""
    base, rate = books
    current = await books_currency_still(session, company_id, base)
    current_rate = _require_doc_rate_http(doc_state, current)
    if rate != current_rate:
        raise HTTPException(status_code=422, detail=f"The payment is at rate {rate}; the document is now at {current_rate}")
    return base, float(rate)


async def apply_doc_payment(session, company_id, entity_id: str, body: dict,
                            *, source: str, actor_id, idempotency_key: str,
                            request: str | None = None, commit: bool = True, moves_cash: bool = True,
                            books: tuple[str, Decimal] | None = None):
    """Record a payment against a doc: guard, emit doc.payment.received, post the cash
    JE, fire the payment lifecycle hook. Shared by the manual route and online payment
    so a Stripe payment lands identically to a hand-entered one. Commits per success and
    returns (event, applied_amount) - the applied amount is the value that actually
    landed under the row lock (post-clamp), which bulk uses to report and decrement.

    Takes the doc row under SELECT ... FOR UPDATE and validates against that fresh,
    committed read: the doc-row lock is the single serializer across every payment
    path and across connections, so two recorders on one doc are ordered at the row
    and cannot compute a duplicate or colliding payment_index.

    The money moves through ``bank_account``, which must be able to hold it. Only
    an imported debit note passes ``moves_cash=False``: it settles its bill against
    the bill's own payable account, so no money moves.

    *books* is the (base currency, document rate) the payment posts on when the caller
    already holds them - an online payment keeps the books its payment page opened with,
    while they still match the company's and the document's (_books_still_kept); otherwise
    they are the company's and the document's now."""
    row = await _get_doc(session, company_id, entity_id, for_update=True)
    replay = await find_event_by_idempotency(session, company_id, idempotency_key)
    if replay is not None:
        if request is not None:
            _check_replay(replay, event_type="doc.payment.received", digest=request, entity_id=entity_id)
        elif replay.event_type != "doc.payment.received" or replay.entity_id != entity_id:
            raise HTTPException(status_code=409, detail="Idempotency key was already used for another operation")
        # Match emit_event's duplicate-race contract so callers can distinguish a
        # replay from a newly applied payment without changing this function's return shape.
        replay.was_deduped = True
        return replay, float((replay.data or {}).get("amount") or 0)
    doc_state = dict(row.state)
    if doc_state.get("doc_type") in NON_FINANCIAL_DOC_TYPES:
        raise HTTPException(status_code=409, detail="This document type carries no money and cannot take a payment")
    if doc_state.get("status") not in PAYABLE_STATUSES:
        raise HTTPException(status_code=409, detail="Cannot record payment in current status")
    # Replay guard for referenced (online) payments: the same Stripe intent
    # delivered twice records exactly once.
    reference = body.get("reference")
    # Deleted tombstones do not hold the reference: deleting a mistaken
    # payment frees its charge to be re-recorded, as removal always did.
    if reference and any(p.get("reference") == reference and p.get("status") != "deleted"
                         for p in doc_state.get("payments", [])):
        raise HTTPException(status_code=409, detail="Payment already recorded")
    doc_currency = str(doc_state.get("currency") or "USD").upper()
    payment_currency = str(body.get("currency") or doc_currency).upper()
    if payment_currency != doc_currency:
        raise HTTPException(
            status_code=422,
            detail=f"Payment currency {payment_currency} does not match document currency {doc_currency}",
        )
    body["currency"] = doc_currency
    outstanding_d = _payable_balance(doc_state)
    if outstanding_d <= 0:
        raise HTTPException(status_code=409, detail="Invoice already fully paid")
    amount_d = round_money(body["amount"], doc_currency)
    if amount_d <= 0:
        raise HTTPException(status_code=422, detail="Payment amount must be positive")
    if amount_d > outstanding_d:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Payment {to_stored_float(amount_d)} exceeds amount outstanding "
                f"{to_stored_float(outstanding_d)}"
            ),
        )
    amount = to_stored_float(amount_d)
    body["amount"] = amount
    bank_code = body.get("bank_account")
    if not bank_code:
        raise HTTPException(status_code=422, detail="bank_account is required")
    if moves_cash:
        await require_settlement_account(session, company_id, bank_code)
    body["currency"] = doc_currency
    # A payment carries its own rate because the rate moves between issuing a
    # foreign-currency document and being paid for it. On a document in the
    # company's own currency there is nothing to convert, so any rate other
    # than 1 restates the receipt: 100 banked as 3500. Refused before the
    # event is written, the same way finalization refuses it on the document.
    if books is not None:
        from celerp_docs.routes_payments import require_online_deposit_account
        await require_online_deposit_account(session, company_id, bank_code)
        _base_currency, _document_rate = await _books_still_kept(session, company_id, doc_state, books)
    else:
        _company = await session.get(Company, company_id)
        _base_currency = (_company.settings.get("currency", "USD") if _company else "USD")
        _document_rate = float(_require_doc_rate_http(doc_state, _base_currency))
    if body.get("currency") == _base_currency and body.get("conversion_rate") not in (None, "") \
            and to_decimal(body["conversion_rate"]) != 1:
        raise HTTPException(
            status_code=422,
            detail=(
                f"{_base_currency} is this company's own currency, so a payment on this "
                f"document converts at 1, not {body['conversion_rate']}."
            ),
        )
    body["remaining_balance"] = to_stored_float(max(Decimal(0), outstanding_d - amount_d))
    payment_index = await _alloc_payment_index(
        session, company_id, doc_state.get("payments", []),
        key_doc_id=entity_id, key_type="invoice.paid")
    body["index"] = payment_index
    # The receivable was raised at the document's rate and can only be cleared at that
    # rate; the bank moves at the rate the cash actually converted at. A payer who
    # records no rate of their own settled at the document's rate, so the two agree
    # and no difference arises. Kept with the payment, so every refund and void of it
    # reverses exactly this (posted_books).
    books = PaymentBooks(
        bank_account=bank_code, base_currency=_base_currency, doc_rate=_document_rate,
        settlement_rate=(float(checked_exchange_rate(body["conversion_rate"]))
                         if body.get("conversion_rate") not in (None, "") else _document_rate))
    body["books"] = asdict(books)
    entry = await emit_event(
        session, company_id=company_id, entity_id=entity_id, entity_type="doc", event_type="doc.payment.received",
        data=body, actor_id=actor_id, location_id=None, source=source,
        idempotency_key=idempotency_key, metadata_={"request": request} if request else {},
    )
    if getattr(entry, "was_deduped", False):
        return entry, float((entry.data or {}).get("amount") or 0)
    await auto_je.create_for_doc_payment(
        session, company_id=company_id, user_id=actor_id, doc_id=entity_id,
        amount=amount, payment_index=payment_index,
        bank_account_code=bank_code, doc_type=doc_state.get("doc_type", "invoice"),
        payment_date=body["payment_date"],
        base_currency=books.base_currency, doc_rate=books.doc_rate, settlement_rate=books.settlement_rate,
    )
    from celerp.modules.slots import fire_lifecycle
    await fire_lifecycle(
        "on_doc_payment", session=session, company_id=company_id, user_id=actor_id,
        doc_id=entity_id, doc=doc_state, amount=amount, bank_account_code=bank_code,
    )
    # Interactive/online callers commit here; domain integrations may compose this
    # payment atomically with finalize/fulfillment and commit the enclosing transaction.
    if commit:
        await session.commit()
    # Return the applied amount (post-clamp) alongside the event so callers report
    # and decrement off what actually landed under the row lock, not a stale pre-read.
    return entry, amount


@router.post(
    "/{entity_id}/payment",
    summary="Record a payment on a document",
    openapi_extra={"x-celerp-agent": True, "x-celerp-agent-idempotent": True},
)
async def record_payment(entity_id: str, payload: DocPaymentBody, company_id: str = Depends(get_current_company_id), _: None = require_permission("record_payments"), user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    # apply_doc_payment takes the doc row FOR UPDATE and validates against that fresh
    # read, rejecting a closed memo via its status allowlist; the row lock serializes
    # this payment against a concurrent close or a second recorder on the same doc.
    key, digest = _operation("payment", entity_id, payload)
    entry, _amount = await apply_doc_payment(
        session, company_id, entity_id, payload.model_dump(exclude_none=True),
        source="api", actor_id=user.id, idempotency_key=key, request=digest,
    )
    await session.commit()
    return {"event_id": entry.id}


class RefundBody(BaseModel):
    payment_index: int  # the payment the money is given back from
    amount: FiniteFloat
    payment_date: str  # ISO date (YYYY-MM-DD) of the refund
    currency: str | None = None
    method: str | None = None
    reference: str | None = None
    reason: str | None = None
    idempotency_key: str | None = None

    _real_date = field_validator("payment_date")(_calendar_date)


def _refundable(payment: dict, currency: str):
    """What is left of a payment to give back."""
    return round_money(payment.get("amount") or 0, currency) - round_money(payment.get("refunded") or 0, currency)


@dataclass(frozen=True)
class PaymentBooks:
    """The books a payment posted on, which every refund and void of it reverses: the
    bank the payment went to, the company currency and the two rates it posted at.
    ``apply_doc_payment`` records them with the payment."""
    bank_account: str | None
    base_currency: str
    doc_rate: float
    settlement_rate: float


UNREADABLE_STRIPE_BOOKS = ("Celerp cannot tell which books this Stripe payment was recorded on, so it cannot be "
                           "refunded or voided here. Record the refund with a journal entry instead.")


async def posted_books(session, company_id, entity_id: str, row: Projection, payment: dict) -> PaymentBooks:
    """The books *payment* on the document *row* posted on. A payment recorded before
    payments kept their books: one received through Stripe has them read from its
    entry (``_books_from_entry``); any other reverses on the company's currency and the
    document's rate now, as it always did."""
    if payment.get("books"):
        return PaymentBooks(**payment["books"])
    if is_stripe_receipt(payment, await stripe_receipt_references(session, company_id, entity_id)):
        return await _books_from_entry(session, company_id, entity_id, row, payment)
    company = await session.get(Company, company_id)
    return PaymentBooks(
        bank_account=payment.get("bank_account"),
        base_currency=(company.settings.get("currency", "USD") if company else "USD"),
        doc_rate=float(row.state.get("conversion_rate") or 1),
        settlement_rate=float(payment.get("conversion_rate") or row.state.get("conversion_rate") or 1))


async def _books_from_entry(session, company_id, entity_id: str, row: Projection, payment: dict) -> PaymentBooks:
    """The books an older Stripe payment posted on, read from its posted entry, or 422
    (``UNREADABLE_STRIPE_BOOKS``) when the entry does not show them. The ledger keeps
    its entries in the company's currency, so the payment's entry is read in it. A
    Stripe payment posted at one rate on both sides: its own, or the document's when it
    carried none. The books are the ones whose lines are exactly the entry's."""
    company = await session.get(Company, company_id)
    base = books_currency((company.settings or {}) if company else {})
    entry = await session.get(Projection, (company_id, f"je:auto:{entity_id}:pay:{payment.get('index')}"))

    def lines(entries) -> list[tuple]:
        return sorted((e["account"], round_money(e.get("debit") or 0, base), round_money(e.get("credit") or 0, base))
                      for e in entries)

    if entry is not None and entry.state.get("status") == "posted" and payment.get("bank_account"):
        posted = lines(entry.state.get("entries", []))
        # The receivable or payable line is on whatever account the entry recognized it on.
        others = {e["account"] for e in entry.state.get("entries", [])} - {payment["bank_account"]}
        control = next(iter(others)) if len(others) == 1 else None
        for rate in dict.fromkeys(r for r in (payment.get("conversion_rate"), row.state.get("conversion_rate"),
                                              1 if str(row.state.get("currency") or base).upper() == base else None)
                                  if r not in (None, "")):
            books = PaymentBooks(bank_account=payment["bank_account"], base_currency=base,
                                 doc_rate=float(rate), settlement_rate=float(rate))
            try:
                amounts = auto_je.payment_amounts(
                    amount=payment.get("amount") or 0, base_currency=base, doc_rate=books.doc_rate,
                    settlement_rate=books.settlement_rate)
            except ValueError:
                continue
            expected = auto_je.payment_lines(
                row.state.get("doc_type", "invoice"),
                lambda debit=0.0, credit=0.0: {"account": control, "debit": debit, "credit": credit},
                {"account": books.bank_account}, *amounts)
            if control is not None and lines(expected) == posted:
                return books
    raise HTTPException(status_code=422, detail=UNREADABLE_STRIPE_BOOKS)


async def apply_payment_refund(session, company_id, entity_id: str, row: Projection, payment: dict, *,
                               amount, refund_date: str, books: PaymentBooks, data: dict,
                               actor_id, source: str, idempotency_key: str, metadata_: dict | None = None):
    """Give back *amount* of *payment* on the locked document *row*: emit
    doc.payment.refunded and post the entry that reverses the refunded share of the
    payment, on *books*. The one refund implementation, for the refund route and for
    a refund Stripe reports (``payments.receive_refund``). 422/409 when the payment
    cannot give that much back; the caller commits. Returns the event, flagged
    ``was_deduped`` when *idempotency_key* already recorded it."""
    _reject_if_closed(row.state, "refund a payment")
    currency = str(row.state.get("currency") or "USD").upper()
    if payment.get("method") in ("credit_note", "applied"):
        raise HTTPException(
            status_code=422,
            detail="A credit note application moved no money, so it cannot be refunded. Void it instead.",
        )
    amount_d = round_money(amount, currency)
    if amount_d <= 0:
        raise HTTPException(status_code=422, detail="Refund amount must be positive")
    left = min(_refundable(payment, currency), round_money(row.state.get("amount_paid", 0) or 0, currency))
    if amount_d > left:
        raise HTTPException(
            status_code=422,
            detail=f"At most {to_stored_float(max(left, 0))} {currency} of this payment can still be refunded.",
        )
    refund_number = int(payment.get("refund_count", 0))
    given_back = float(payment.get("refunded") or 0)
    refund_data = {**data, "amount": to_stored_float(amount_d), "currency": currency, "refund_date": refund_date,
                   "payment_index": payment.get("index"), "method": data.get("method") or payment.get("method"),
                   "refund_number": refund_number}
    entry = await emit_event(
        session, company_id=company_id, entity_id=entity_id, entity_type="doc", event_type="doc.payment.refunded",
        data=refund_data, actor_id=actor_id, location_id=None, source=source,
        idempotency_key=idempotency_key, metadata_=metadata_ or {},
    )
    if getattr(entry, "was_deduped", False):
        return entry
    # The refund gives back this payment's money: the same bank, at the same two rates
    # the payment posted at, in proportion to the amount refunded.
    await auto_je.void_for_doc_payment(
        session, company_id=company_id, user_id=actor_id, doc_id=entity_id,
        payment_index=payment.get("index"), amount=to_stored_float(amount_d),
        bank_account_code=books.bank_account, doc_type=row.state.get("doc_type", "invoice"),
        refund_date=refund_date, base_currency=books.base_currency,
        doc_rate=books.doc_rate, settlement_rate=books.settlement_rate,
        refund_number=refund_number, already_given_back=given_back,
    )
    return entry


async def reverse_payment_refund(session, company_id, entity_id: str, row: Projection, payment: dict,
                                 refund: dict, *, reversal_date: str, books: PaymentBooks, actor_id,
                                 source: str, idempotency_key: str):
    """Undo *refund*, the data of a doc.payment.refunded event of *payment* on the locked
    document *row*, when the money it gave back came back: emit
    doc.payment.refund_reversed and post the lines that give *refund*'s amount back
    from the payment's refunded total (``auto_je.payment_return_entries``), swapped, on
    *books*. Undoing the latest refund mirrors its entry; undoing an earlier one still
    leaves the books at what the refunds left in place convert to. The caller commits.
    Returns the event, flagged ``was_deduped`` when *idempotency_key* already recorded it."""
    from celerp.services.je_keys import je_idempotency_key, unminted_payment_key
    index, number = payment.get("index"), refund["refund_number"]
    amount = to_decimal(refund["amount"])
    left_given_back = to_stored_float(to_decimal(payment.get("refunded") or 0) - amount)
    entry = await emit_event(
        session, company_id=company_id, entity_id=entity_id, entity_type="doc",
        event_type="doc.payment.refund_reversed",
        data={"payment_index": index, "refund_number": number, "amount": refund["amount"],
              "refund_id": refund.get("refund_id"), "reversal_date": reversal_date},
        actor_id=actor_id, location_id=None, source=source, idempotency_key=idempotency_key,
    )
    if getattr(entry, "was_deduped", False):
        return entry
    key = await unminted_payment_key(session, company_id, entity_id, "payment.refund_reversed",
                                     f"refund_{index}_{number}")
    lines = await auto_je.payment_return_entries(
        session, company_id, doc_id=entity_id, payment_index=index,
        doc_type=row.state.get("doc_type", "invoice"), bank_account_code=books.bank_account,
        amount=to_stored_float(amount),
        already_given_back=left_given_back,
        base_currency=books.base_currency, doc_rate=books.doc_rate, settlement_rate=books.settlement_rate,
    )
    await auto_je._emit_auto_posted_je(
        session, company_id=company_id, user_id=actor_id,
        je_id=f"je:auto:{entity_id}:payrefundrev:{key}",
        idem_create=je_idempotency_key(entity_id, f"payment.refund_reversed:{key}", "c"),
        idem_posted=je_idempotency_key(entity_id, f"payment.refund_reversed:{key}", "p"),
        memo=f"Auto JE for {entity_id} payment refund reversed (index {index})",
        ts=reversal_date, currency=books.base_currency.upper(),
        entries=[{**e, "debit": e.get("credit") or 0.0, "credit": e.get("debit") or 0.0} for e in lines],
        metadata_={"trigger": "doc.payment.refund_reversed", "doc_id": entity_id, "payment_index": index},
    )
    return entry


@router.post("/{entity_id}/refund")
async def refund_payment(entity_id: str, payload: RefundBody, company_id: str = Depends(get_current_company_id), _: None = require_permission("record_payments"), user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    row = await _get_doc(session, company_id, entity_id, for_update=True)
    key, digest = _operation("refund", entity_id, payload)
    if (done := await _earlier_run(session, company_id, key, event_type="doc.payment.refunded",
                                   entity_id=entity_id, digest=digest)) is not None:
        return done
    currency = str(row.state.get("currency") or "USD").upper()
    if payload.currency and str(payload.currency).upper() != currency:
        raise HTTPException(
            status_code=422,
            detail=f"Refund currency {str(payload.currency).upper()} does not match document currency {currency}",
        )
    payment = next((p for p in row.state.get("payments", []) if p.get("index") == payload.payment_index), None)
    if payment is None or payment.get("status") != "active":
        raise HTTPException(status_code=422, detail="Choose a payment on this document to refund.")
    entry = await apply_payment_refund(
        session, company_id, entity_id, row, payment, amount=payload.amount, refund_date=payload.payment_date,
        books=await posted_books(session, company_id, entity_id, row, payment),
        data=payload.model_dump(exclude_none=True, exclude={"payment_date", "idempotency_key", "amount", "currency"}),
        actor_id=user.id, source="api", idempotency_key=key, metadata_={"request": digest},
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

    _real_date = field_validator("refund_date")(_calendar_date)


@router.post("/{entity_id}/void-payment")
async def void_payment(entity_id: str, payload: VoidPaymentBody, company_id: str = Depends(get_current_company_id), _: None = require_permission("record_payments"), user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    row = await _get_doc(session, company_id, entity_id, for_update=True)
    key, digest = _operation("void-payment", entity_id, payload)
    if (done := await _earlier_run(session, company_id, key, event_type="doc.payment.voided",
                                   entity_id=entity_id, digest=digest)) is not None:
        return done
    _reject_if_closed(row.state, "void a payment")
    payments = row.state.get("payments", [])
    # Payments are identified by their index FIELD, not list position.
    payment = next((p for p in payments if p.get("index") == payload.payment_index), None)
    if payment is None:
        raise HTTPException(status_code=422, detail="Invalid payment index")
    if payment.get("status") != "active":
        raise HTTPException(status_code=409, detail="Payment is already voided")
    # A refund already gave back part of the payment; the void reverses the rest.
    remaining = _refundable(payment, str(row.state.get("currency") or "USD").upper())
    if payment.get("refunded") and remaining <= 0:
        raise HTTPException(status_code=409, detail="This payment has been refunded in full, so there is nothing left to void.")
    given_back = float(payment.get("refunded") or 0)
    # The payment's own books, so the reversal is its mirror; read before anything is
    # written, so a payment whose books cannot be told is refused whole.
    books = (await posted_books(session, company_id, entity_id, row, payment)
             if payment.get("method") not in ("credit_note", "applied") else None)

    entry = await emit_event(
        session, company_id=company_id, entity_id=entity_id, entity_type="doc",
        event_type="doc.payment.voided",
        data={"payment_index": payload.payment_index, "void_reason": payload.void_reason,
              "refund_date": payload.refund_date, "amount": to_stored_float(remaining), "method": payment.get("method")},
        actor_id=user.id, location_id=None, source="api",
        idempotency_key=key, metadata_={"request": digest},
    )
    doc_type = row.state.get("doc_type", "invoice")
    if books is not None:
        await auto_je.void_for_doc_payment(
            session, company_id=company_id, user_id=user.id, doc_id=entity_id,
            payment_index=payload.payment_index, amount=to_stored_float(remaining),
            bank_account_code=books.bank_account, doc_type=doc_type,
            refund_date=payload.refund_date, base_currency=books.base_currency,
            doc_rate=books.doc_rate, settlement_rate=books.settlement_rate,
            already_given_back=given_back,
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
                        if round_money(
                            c[1].get("amount") or 0, row.state.get("currency") or "USD"
                        ) == round_money(
                            payment.get("amount") or 0, row.state.get("currency") or "USD"
                        )
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
                        idempotency_key=_step_key(key, "paired"), metadata_={},
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
    key, digest = _operation("delete-payment", f"{entity_id}:{payment_index}", payload)
    if (done := await _earlier_run(session, company_id, key, event_type="doc.payment.deleted",
                                   entity_id=entity_id, digest=digest)) is not None:
        return done
    _reject_if_closed(row.state, "delete a payment")
    payments = row.state.get("payments", [])
    # Payments are identified by their index FIELD, not list position.
    payment = next((p for p in payments if p.get("index") == payment_index), None)
    if payment is None:
        raise HTTPException(status_code=422, detail="Invalid payment index")
    if payment.get("status") != "active":
        raise HTTPException(status_code=409, detail="Only active payments can be deleted")
    # Said before "void it instead": a Stripe payment is voided only in Stripe either.
    await refuse_stripe_payment_removal(session, company_id, entity_id, payments, payment_index,
                                        "doc.payment.deleted")
    if payment.get("refunded"):
        raise HTTPException(
            status_code=409,
            detail="Part of this payment has been refunded, so it was real. Void it instead of deleting it.",
        )
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
    recon_sessions = [r for r in recon_result.scalars().all() if je_id in (r.reconciled_je_ids or [])]
    if any(recon.status == "closed" for recon in recon_sessions):
        raise HTTPException(
            status_code=409,
            detail="Payment has been reconciled in a closed period. Unreconcile to delete.",
        )

    # Emit doc.payment.deleted first - projection tombstones the row in place.
    # Every refusal the event carries lands before anything else is changed.
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
        idempotency_key=key, metadata_={"request": digest},
    )

    for recon in recon_sessions:
        # Open session - auto-unmatch
        recon.reconciled_je_ids = [j for j in (recon.reconciled_je_ids or []) if j != je_id]
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

    # Void the original payment JE so it disappears from the bank ledger
    # and reports. The void carries the entry's own date, so an entry inside a
    # locked period refuses the whole deletion.
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

    await return_unmatched(session, company_id, entity_id, payment.get("reference"))
    await session.commit()
    return {"event_id": entry.id}


# ---------------------------------------------------------------------------
# Credit note: apply to invoice
# ---------------------------------------------------------------------------


class ApplyToInvoiceBody(BaseModel):
    target_doc_id: str
    amount: FiniteFloat
    date: str | None = None
    idempotency_key: str | None = None

    _real_date = field_validator("date")(_calendar_date)


async def apply_credit_note(session, company_id, entity_id: str, target_doc_id: str, amount: float, *,
                            payment_date: str | None, actor_id, source: str, idempotency_key: str,
                            request: str | None = None):
    """Apply an issued credit note to an invoice of the same contact: a paired
    doc.payment.received on each side and the AR-to-AR entry. Shared by the apply
    route and the migration sink; the caller commits.

    A replayed idempotency key returns the recorded application, marked was_deduped,
    without writing anything; with ``request`` the replay must also carry the same
    request digest. The invoice-side event is keyed as a step of the same key."""
    # Lock both docs FOR UPDATE in one ordered batch: the doc-row lock is the single
    # serializer, so two concurrent applications sharing docs acquire them in the same
    # order (no deadlock) and each re-reads the other's committed state before allocating
    # an index. A stale list would allocate a colliding index.
    locked = await _get_docs_for_update(session, company_id, {entity_id, target_doc_id})
    cn_row = locked.get(entity_id)
    inv_row = locked.get(target_doc_id)
    if cn_row is None or inv_row is None:
        raise HTTPException(status_code=404, detail="Document not found")
    replay = await find_event_by_idempotency(session, company_id, idempotency_key)
    if replay is not None:
        if request is not None:
            _check_replay(replay, event_type="doc.payment.received", digest=request, entity_id=entity_id)
        elif replay.event_type != "doc.payment.received" or replay.entity_id != entity_id:
            raise HTTPException(status_code=409, detail="Idempotency key was already used for another operation")
        replay.was_deduped = True
        return replay
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

    cn_currency = str(cn.get("currency") or "USD").upper()
    inv_currency = str(inv.get("currency") or "USD").upper()
    if cn_currency != inv_currency:
        raise HTTPException(
            status_code=422,
            detail="Credit note and invoice must use the same currency",
        )
    amount_d = round_money(amount, cn_currency)
    if amount_d <= 0:
        raise HTTPException(status_code=422, detail="Application amount must be positive")
    cn_outstanding = _payable_balance(cn)
    inv_outstanding = _payable_balance(inv)
    if amount_d > cn_outstanding:
        raise HTTPException(status_code=409, detail="Amount exceeds credit note balance")
    if amount_d > inv_outstanding:
        raise HTTPException(status_code=409, detail="Amount exceeds invoice outstanding")
    amount = to_stored_float(amount_d)

    _cn_company = await session.get(Company, company_id)
    payment_date = payment_date or business_date_of(None, (_cn_company.settings or {}).get("timezone") if _cn_company else None)
    _cn_base_currency = (_cn_company.settings.get("currency", "USD") if _cn_company else "USD")
    _cn_rate = float(_require_doc_rate_http(cn, _cn_base_currency))
    _require_doc_rate_http(inv, _cn_base_currency)

    # Both sides get allocated indices so their identity fields never
    # collide with skip-allocated payments on either doc.
    inv_pay_index = await _alloc_payment_index(session, company_id, inv.get("payments", []))
    payment_idx = await _alloc_payment_index(
        session, company_id, cn_row.state.get("payments", []),
        key_doc_id=target_doc_id, key_type=f"cn.applied:cn_apply_{entity_id}")

    # Emit paired events: payment on invoice (credit_note method), payment on CN (applied method)
    await emit_event(
        session, company_id=company_id, entity_id=target_doc_id, entity_type="doc",
        event_type="doc.payment.received",
        data={
            "amount": amount, "method": "credit_note",
            "source_doc_id": entity_id, "payment_date": payment_date,
            "currency": cn.get("currency", "USD"),
            "index": inv_pay_index,
            # The counterpart's index on the other doc: voiding one side
            # must release exactly its pair, even when the same credit
            # note is applied to the same invoice more than once.
            "paired_index": payment_idx,
        },
        actor_id=actor_id, location_id=None, source=source,
        idempotency_key=_step_key(idempotency_key, "invoice"), metadata_={},
    )
    entry = await emit_event(
        session, company_id=company_id, entity_id=entity_id, entity_type="doc",
        event_type="doc.payment.received",
        data={
            "amount": amount, "method": "applied",
            "target_doc_id": target_doc_id, "payment_date": payment_date,
            "currency": cn.get("currency", "USD"),
            "index": payment_idx,
            "paired_index": inv_pay_index,
        },
        actor_id=actor_id, location_id=None, source=source,
        idempotency_key=idempotency_key, metadata_={"request": request} if request is not None else {},
    )
    await auto_je.create_for_cn_application(
        session, company_id=company_id, user_id=actor_id,
        doc_id=target_doc_id, cn_id=entity_id, amount=amount,
        payment_index=payment_idx, payment_date=payment_date,
        base_currency=_cn_base_currency,
        conversion_rate=_cn_rate,
    )
    return entry


@router.post("/{entity_id}/apply-to-invoice")
async def apply_cn_to_invoice(entity_id: str, payload: ApplyToInvoiceBody, company_id: str = Depends(get_current_company_id), _: None = require_permission("record_payments"), user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    key, digest = _operation("apply-credit-note", entity_id, payload)
    entry = await apply_credit_note(
        session, company_id, entity_id, payload.target_doc_id, payload.amount,
        payment_date=payload.date, actor_id=user.id, source="api", idempotency_key=key, request=digest,
    )
    await session.commit()
    return {"event_id": entry.id}


# ---------------------------------------------------------------------------
# Credit note: refund to customer
# ---------------------------------------------------------------------------


class CnRefundBody(BaseModel):
    amount: FiniteFloat
    date: str  # ISO date (YYYY-MM-DD), always required
    method: str | None = None
    bank_account: str | None = None
    reference: str | None = None
    idempotency_key: str | None = None

    _real_date = field_validator("date")(_calendar_date)


@router.post("/{entity_id}/cn-refund")
async def refund_cn(entity_id: str, payload: CnRefundBody, company_id: str = Depends(get_current_company_id), _: None = require_permission("record_payments"), user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    # Lock the credit note row FOR UPDATE and validate against that fresh read: the
    # doc-row lock is the single serializer, so a concurrent application/refund on this
    # credit note is ordered at the row and cannot mint a colliding index.
    row = await _get_doc(session, company_id, entity_id, for_update=True)
    key, digest = _operation("credit-note-refund", entity_id, payload)
    if (done := await _earlier_run(session, company_id, key, event_type="doc.payment.received",
                                   entity_id=entity_id, digest=digest)) is not None:
        return done
    cn = row.state
    if cn.get("doc_type") != "credit_note":
        raise HTTPException(status_code=409, detail="Only credit notes can be refunded")
    if cn.get("status") in ("draft", "void"):
        raise HTTPException(status_code=409, detail="Credit note must be issued before refunding")
    currency = str(cn.get("currency") or "USD").upper()
    amount_d = round_money(payload.amount, currency)
    if amount_d <= 0:
        raise HTTPException(status_code=422, detail="Refund amount must be positive")
    cn_outstanding = _payable_balance(cn)
    if amount_d > cn_outstanding:
        raise HTTPException(status_code=409, detail="Refund amount exceeds credit note balance")
    amount = to_stored_float(amount_d)

    payment_date = payload.date
    if not payload.bank_account:
        raise HTTPException(status_code=422, detail="bank_account is required")
    bank_code = payload.bank_account
    await require_settlement_account(session, company_id, bank_code)

    _refund_company = await session.get(Company, company_id)
    _refund_base_currency = (_refund_company.settings.get("currency", "USD") if _refund_company else "USD")
    _refund_rate = float(_require_doc_rate_http(cn, _refund_base_currency))
    payment_index = await _alloc_payment_index(session, company_id, cn.get("payments", []),
                                               key_doc_id=entity_id, key_type="invoice.paid")
    entry = await emit_event(
        session, company_id=company_id, entity_id=entity_id, entity_type="doc",
        event_type="doc.payment.received",
        data={
            "amount": amount, "method": "refund",
            "bank_account": bank_code, "reference": payload.reference,
            "payment_date": payment_date, "currency": currency,
            "index": payment_index,
        },
        actor_id=user.id, location_id=None, source="api",
        idempotency_key=key, metadata_={"request": digest},
    )
    # JE: debit AR, credit bank
    await auto_je.create_for_doc_payment(
        session, company_id=company_id, user_id=user.id, doc_id=entity_id,
        amount=amount, payment_index=payment_index,
        bank_account_code=bank_code, doc_type="credit_note",
        payment_date=payment_date,
        base_currency=_refund_base_currency,
        # A refund is issued at the rate the credit note itself carries, and
        # there is no second rate to record: the form does not ask for one.
        doc_rate=_refund_rate,
        settlement_rate=_refund_rate,
    )
    await session.commit()
    return {"event_id": entry.id}


# ---------------------------------------------------------------------------
# Bulk payment
# ---------------------------------------------------------------------------


class BulkPaymentBody(BaseModel):
    doc_ids: list[str]
    amount: FiniteFloat
    payment_date: str  # ISO date (YYYY-MM-DD), always required
    method: str | None = None
    bank_account: str | None = None
    reference: str | None = None
    idempotency_key: str | None = None

    _real_date = field_validator("payment_date")(_calendar_date)


@router.post("/bulk-payment")
async def bulk_payment(payload: BulkPaymentBody, company_id: str = Depends(get_current_company_id), _: None = require_permission("record_payments"), user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    if not payload.doc_ids:
        raise HTTPException(status_code=422, detail="No documents specified")
    key, digest = _operation("bulk-payment", None, payload)
    rows = await _get_docs_for_update(session, company_id, payload.doc_ids)
    if (done := await _earlier_run(session, company_id, key, event_type="payment_batch.recorded",
                                   entity_id=None, digest=digest)) is not None:
        return done

    docs = [(doc_id, dict(rows[doc_id].state)) for doc_id in dict.fromkeys(payload.doc_ids) if doc_id in rows]
    if not docs:
        raise HTTPException(status_code=404, detail="No valid documents found")
    contact_ids = {s.get("contact_id") for _, s in docs if s.get("contact_id")}
    if len(contact_ids) > 1:
        raise HTTPException(status_code=422, detail="All documents must belong to the same contact")

    payable = []
    for doc_id, state in docs:
        if not (is_awaiting_payment(state.get("doc_type"), state.get("status")) and is_owed(state)):
            continue
        currency = str(state.get("currency") or "USD").upper()
        payable.append((doc_id, state, currency, _payable_balance(state)))
    if not payable:
        raise HTTPException(status_code=409, detail="No documents in payable status")

    currencies = {currency for _, _, currency, _ in payable}
    if len(currencies) != 1:
        raise HTTPException(
            status_code=422,
            detail="Bulk payment requires all payable documents to use the same currency",
        )
    currency = next(iter(currencies))
    tender = round_money(payload.amount, currency)
    if tender <= 0:
        raise HTTPException(status_code=422, detail="Payment amount must be positive")
    if not payload.bank_account:
        raise HTTPException(status_code=422, detail="bank_account is required")
    await require_settlement_account(session, company_id, payload.bank_account)

    payable.sort(key=lambda x: (
        x[1].get("due_date") or x[1].get("issue_date") or "9999",
        x[1].get("issue_date") or "9999",
        x[0],
    ))

    remaining = tender
    allocations = []
    skipped: list[dict] = []
    for doc_id, _state, _, outstanding in payable:
        if remaining <= 0:
            break
        alloc = min(remaining, outstanding)
        body = {
            "amount": to_stored_float(alloc),
            "method": payload.method,
            "reference": payload.reference,
            "payment_date": payload.payment_date,
            "bank_account": payload.bank_account,
            "currency": currency,
        }
        # A document that refuses its share is reported and leaves the rest of the batch intact.
        try:
            async with session.begin_nested():
                _entry, applied = await apply_doc_payment(
                    session, company_id, doc_id, body,
                    source="api", actor_id=user.id, idempotency_key=_step_key(key, doc_id),
                    commit=False)
        except HTTPException as exc:
            skipped.append({"doc_id": doc_id, "reason": exc.detail})
            continue
        applied_d = round_money(applied, currency)
        allocations.append({"doc_id": doc_id, "amount": to_stored_float(applied_d)})
        remaining = round_money(max(Decimal(0), remaining - applied_d), currency)

    result = {
        "allocations": allocations,
        "skipped": skipped,
        "total_allocated": to_stored_float(round_money(tender - remaining, currency)),
        "remaining": to_stored_float(remaining),
    }
    await emit_event(
        session, company_id=company_id, entity_id=f"payment_batch:{_step_id(key)}",
        entity_type="payment_batch", event_type="payment_batch.recorded",
        data={"doc_ids": payload.doc_ids, "amount": to_stored_float(tender), "currency": currency,
              "payment_date": payload.payment_date, **result},
        actor_id=user.id, location_id=None, source="api",
        idempotency_key=key, metadata_={"request": digest, "result": result},
    )
    await session.commit()
    return result


_RECEIVING_DOC_LABEL = {"purchase_order": "purchase order", "bill": "bill", "consignment_in": "consignment"}


def _resolve_inbound_line(doc: dict, it: ReceivedItem, item_skus: dict[str, str]) -> None:
    """Tie received goods to the document line they are for, and take what they are from it.

    The receipt names the line by its id, else by its index, else by the one line holding its
    item, SKU or name. Whatever else it says about the goods (item, SKU, stock or expense)
    must agree with that line. Goods no line is for, or that two lines could be for, are
    refused: the line is added or changed first.
    """
    label = _RECEIVING_DOC_LABEL.get(doc.get("doc_type"), "document")
    lines = doc.get("line_items") or []
    what = it.sku or it.name or it.item_id or "Received item"
    if it.source_line_id:
        found = [i for i, li in enumerate(lines) if li.get("line_id") == it.source_line_id]
        if len(found) != 1:
            raise HTTPException(status_code=422, detail=refusal(
                "docs.receive_line_unknown",
                f"{what}: that line is not on this document. Reload the document and pick the line again.",
                what=what))
        index = found[0]
        if it.po_line_index not in (-1, index):
            raise HTTPException(status_code=422, detail=refusal(
                "docs.receive_line_mismatch",
                f"{what}: that line has moved on this document. Reload the document and pick the line again.",
                what=what))
    elif it.po_line_index != -1:
        if not 0 <= it.po_line_index < len(lines):
            raise HTTPException(status_code=422, detail=f"{what}: line {it.po_line_index + 1} is not on this {label}.")
        index = it.po_line_index
    else:
        found = [i for i, li in enumerate(lines)
                 if (it.item_id and li.get("item_id") == it.item_id)
                 or (it.sku and str(li.get("sku") or "").strip() == it.sku.strip())]
        if not found and not (it.item_id or it.sku) and (it.name or "").strip():
            found = [i for i, li in enumerate(lines)
                     if str(li.get("name") or li.get("description") or "").strip() == it.name.strip()]
        if len(found) > 1:
            raise HTTPException(status_code=422, detail=refusal(
                "docs.receive_line_ambiguous",
                f"{what}: more than one line on this document holds it. Pick the line to receive it on.",
                what=what))
        index = found[0] if found else None
    if index is None:
        raise HTTPException(status_code=422, detail=f"{what}: it is not on this {label}. Add it to the {label} first.")
    line = lines[index]
    # The line's item, else the item it was written against; never a parcel a receipt made.
    pre_receipt = line.get("entity_id") if line.get("entity_id") not in set(doc.get("received_item_ids") or []) else None
    line_item = line.get("item_id") or pre_receipt or None
    line_sku = str(line.get("sku") or "").strip() or None
    line_kind = line_receive_kind(line)
    if it.item_id and it.item_id != line_item and not (line_item is None and line_sku
                                                      and item_skus.get(it.item_id) == line_sku):
        raise HTTPException(status_code=422, detail=f"{what}: that item is not the one on line {index + 1} of this {label}.")
    if it.sku and it.sku.strip() != (line_sku or item_skus.get(line_item or "")):
        raise HTTPException(status_code=422, detail=f"{what}: that SKU is not the one on line {index + 1} of this {label}.")
    if it.receive_as and it.receive_as != line_kind:
        raise HTTPException(
            status_code=422,
            detail=f"{what}: line {index + 1} of this {label} is received as {line_kind}, not {it.receive_as}.")
    it.po_line_index = index
    it.source_line_id = line.get("line_id") or None
    it.item_id = it.item_id or line_item
    it.sku = line_sku or it.sku
    it.receive_as = line_kind
    it.name = it.name or line.get("name") or line.get("description") or None


async def _received_goods_cost(session: AsyncSession, company_id, doc: dict, it: ReceivedItem, stock_qty: float) -> float | None:
    """What the received goods cost in the books' currency, or None when no line prices them.

    The document line the receipt was resolved to prices them per purchase unit in the
    document's currency. A unit cost given on the receipt (per stock unit, in the books'
    currency) must agree with that line, since the bill books the line.
    """
    lines = doc.get("line_items") or []
    if not 0 <= it.po_line_index < len(lines):
        return None
    line = lines[it.po_line_index]
    currency = str(doc.get("currency") or "").upper()
    unit = document_line_unit(line, currency)
    if unit is None:
        return None
    company = await session.get(Company, company_id)
    base_currency = (company.settings or {}).get("currency", "USD") if company else "USD"
    try:
        rate = require_doc_rate(doc, base_currency)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    cost = to_base(unit * to_decimal(it.quantity_received), rate, base_currency)
    if it.cost_price is not None:
        given, priced = (round_money(v, base_currency) for v in (float(it.cost_price) * stock_qty, cost))
        if given != priced:
            doc_label = _RECEIVING_DOC_LABEL.get(doc.get("doc_type"), "document")
            raise HTTPException(
                status_code=422,
                detail=(f"{it.sku or it.name or it.item_id}: the receipt cost {to_stored_float(given)} differs "
                        f"from the {doc_label} line ({to_stored_float(priced)}). Receive the goods at the "
                        f"{doc_label} price, then add a landed cost or correct the item's cost."),
            )
    return cost


def _refuse_receipt_when_not_open(state: dict) -> None:
    """Goods are received only in the statuses RECEIVABLE_STATUSES names for the document.

    A bill not yet issued books nothing, so goods received on it would sit on no entry. That
    includes a draft an earlier release already received goods on."""
    if state.get("doc_type") == "bill" and (
            state.get("status") == "draft"
            or (state.get("pre_receipt_status") == "draft" and not state.get("finalized"))):
        raise HTTPException(status_code=409, detail=refusal(
            "docs.receive_draft_bill",
            "This bill is still a draft, so it has not booked these goods. "
            "Finalize the bill first, then receive them."))
    if state.get("status") not in RECEIVABLE_STATUSES.get(state.get("doc_type"), frozenset()):
        raise HTTPException(status_code=409, detail=refusal(
            "docs.receive_not_open",
            "This document is not open for receiving, so no goods can be received on it."))


@router.post("/{entity_id}/receive")
async def receive_po(entity_id: str, payload: ReceiveBody, company_id: str = Depends(get_current_company_id), _: None = require_permission("fulfill_documents"), role: str = Depends(get_current_role), settings: dict = Depends(get_current_company_settings), user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    # A receipt adds to the quantity and cost of the lots it reads, so it waits for any
    # receipt or cost change in flight and reads what that one committed.
    row = await _get_doc(session, company_id, entity_id, for_update=True)
    key, digest = _operation("receive", entity_id, payload)
    if (done := await _earlier_run(session, company_id, key, event_type="doc.received",
                                   entity_id=entity_id, digest=digest, with_event_id=True)) is not None:
        return done
    doc_type = row.state.get("doc_type")
    if doc_type not in ("purchase_order", "bill", "consignment_in"):
        raise HTTPException(status_code=409, detail="receive is only valid for bills, purchase orders, and consignment_in documents")
    _refuse_receipt_when_not_open(row.state)

    location_uuid = None
    if payload.location_id:
        try:
            location_uuid = uuid.UUID(payload.location_id)
        except ValueError:
            location_uuid = None
        if location_uuid is None or (await session.execute(
            select(Location.id).where(Location.id == location_uuid, Location.company_id == company_id)
        )).scalar_one_or_none() is None:
            raise HTTPException(status_code=422, detail="That location does not exist. Choose one of your locations.")

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
    item_skus = {r.entity_id: str(r.state.get("sku") or "").strip() for r in all_item_rows}
    for it in payload.received_items:
        _resolve_inbound_line(row.state, it, item_skus)
    _recv_price_lists = (await get_price_config(session, company_id))[0]
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
    bill_alloc: dict[int, dict[str, float]] = {}
    landed_drawdown: dict[str, float] = {}   # per-kind landed cost capitalised by this receipt
    if doc_type == "bill":
        bill_alloc = await compute_bill_landed_allocation(session, company_id, row.state)

    for it in payload.received_items:
        sell_by = sell_by_map.get(it.sku or "") or doc_line_sell_by.get(it.sku or "", "") or None
        validate_line_quantity(it.quantity_received, sell_by, unit_map, label=it.name or it.sku or "Received item")

    doc_label = _RECEIVING_DOC_LABEL.get(doc_type, "document")
    # A line is received up to what it orders, across every receipt on it. Goods sent back
    # are credited against the line, so replacing them means raising the line first.
    lines = row.state.get("line_items") or []
    held = _line_quantities_received(row.state)
    for it in payload.received_items:
        before = held.get(it.po_line_index, 0.0)
        held[it.po_line_index] = before + float(it.quantity_received)
        ordered = float(lines[it.po_line_index].get("quantity") or 0)
        if held[it.po_line_index] > ordered + 1e-9:
            name, remaining = it.name or it.sku or it.item_id, max(0.0, ordered - before)
            raise HTTPException(status_code=422, detail=refusal(
                "docs.receive_more_than_line",
                f"{name}: this line is for {ordered:g} and {before:g} has been received, so at most "
                f"{remaining:g} more can be received. Change the line first to receive more.",
                name=name, ordered=f"{ordered:g}", received=f"{before:g}", remaining=f"{remaining:g}"))

    # Received parcels each get a fresh sequential barcode so every physical lot is
    # scannable and barcode uniqueness (the physical-lot key now that SKU may repeat)
    # actually holds. Allocate the whole batch in one locked call AFTER validation so
    # concurrent receipts mint distinct barcodes; the lock is held until this request
    # commits. The DB unique index is the backstop, not the normal mechanism.
    from celerp_inventory.services import (
        allocate_internal_codes,
        goods_basis,
        resolve_catalog_anchor_for_item,
    )

    def _creates_parcel(it) -> bool:
        return not (it.item_id and not is_inbound) and it.receive_as == "stock"

    # One pricing for the whole receipt: what each received line cost is both what it adds
    # to its lot and what a purchase order receipt books, so the two cannot disagree.
    priced: list[tuple[float, float, float | None]] = []  # (conversion, stock quantity, cost)
    for it in payload.received_items:
        if it.item_id and not is_inbound:
            conversion = item_conversion_map.get(it.item_id, 1)
        elif it.receive_as == "stock":
            _sku = (it.sku or (it.name or "").strip().upper().replace(" ", "-")[:40]).strip()
            conversion = (
                (item_conversion_map.get(it.item_id) if it.item_id else None)
                or doc_line_conversion.get(_sku)
                or sku_conversion_map.get(_sku)
                or 1
            )
        else:
            conversion = 1
        stock_qty = float(it.quantity_received) * conversion
        cost: float | None = None
        if is_consignment:
            # Consigned goods are costed from their consignment line. A foreign-currency
            # consignment may have no rate until it is invoiced, and its goods carry no cost
            # until then. A different cost on the receipt is a price edit, so only a role that
            # sets prices may give one.
            base_currency = settings.get("currency", "USD")
            try:
                rate_known = doc_rate(row.state, base_currency) is not None
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            if rate_known:
                cost = await _received_goods_cost(session, company_id, row.state, it.model_copy(update={"cost_price": None}), stock_qty)
            if it.cost_price is not None:
                given = float(it.cost_price) * stock_qty
                if cost is None or round_money(given, base_currency) != round_money(cost, base_currency):
                    reject_price_change({"cost_price"}, role, settings)
                    cost = given
        elif doc_type == "purchase_order" or it.receive_as == "stock":
            cost = await _received_goods_cost(session, company_id, row.state, it, stock_qty)
            if cost is None:
                raise HTTPException(
                    status_code=422,
                    detail=(f"{it.sku or it.name or it.item_id}: no line on this {doc_label} prices it, "
                            f"so the received goods cannot be costed. Add it to the {doc_label} first."),
                )
        negative = negative_cost_error(str(it.sku or it.name or it.item_id), cost)
        if negative:
            raise HTTPException(status_code=422, detail=negative)
        priced.append((conversion, stock_qty, cost))

    _new_parcel_count = sum(1 for it in payload.received_items if _creates_parcel(it))
    _recv_barcodes = await allocate_internal_codes(session, company_id, _new_parcel_count) if _new_parcel_count else []
    _recv_barcode_idx = 0
    added_to_lot: dict[int, dict] = {}  # line -> what it added to a lot already on hand
    line_lot_account: dict[int, str] = {}  # line -> inventory account of the lot it added stock to
    landed_by_account: dict[str, float] = {}  # landed cost capitalised, per receiving lot's account
    purchased_account = await new_lot_account(session, company_id, AccountRole.INVENTORY_PURCHASED)

    for line_no, (it, (conversion, stock_qty_received, received_cost)) in enumerate(zip(payload.received_items, priced)):
        if it.item_id and not is_inbound and it.receive_as == "stock":
            # PO (outbound-style): adjust quantity on the canonical catalog item.
            item = await session.get(Projection, {"company_id": company_id, "entity_id": it.item_id})
            if item is None:
                raise HTTPException(status_code=404, detail=f"Item not found: {it.item_id}")
            new_qty = float(item.state.get("quantity", 0) or 0) + stock_qty_received
            # The receipt adds what these goods cost to the lot's basis, so a delivery at a new
            # price moves the lot's unit cost to the weighted average of old and new stock.
            adjustment: dict = {"new_qty": new_qty,
                                "cost_base": round_basis((goods_basis(item.state) or 0.0) + received_cost)}
            await emit_event(
                session, company_id=company_id, entity_id=it.item_id, entity_type="item", event_type="item.quantity.adjusted",
                data=adjustment,
                actor_id=user.id, location_id=None, source="api",
                idempotency_key=_step_key(key, "line", line_no), metadata_={"source_doc": entity_id},
            )
            added_to_lot[line_no] = {"lot_quantity_added": stock_qty_received, "lot_cost_added": received_cost}
            if not is_consignment:
                line_lot_account[line_no] = lot_account(item.state)
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
            catalog_item_id: str | None = None
            if it.item_id:
                tmpl_row = await session.get(Projection, {"company_id": company_id, "entity_id": it.item_id})
                if tmpl_row:
                    template_state = tmpl_row.state
                    if tmpl_row.entity_type == "item":
                        try:
                            catalog_item_id = (
                                await resolve_catalog_anchor_for_item(
                                    session, company_id, tmpl_row.entity_id
                                )
                            ).entity_id
                        except ValueError:
                            # A historical physical row may not have a resolvable
                            # catalog family. Keep receiving safe without asserting
                            # a false product relation.
                            catalog_item_id = None
            if not template_state:
                template_state = next(
                    (r.state for r in all_item_rows
                     if str(r.state.get("sku") or "").strip() == _sku.strip()),
                    {},
                )
            # sku_ref is an alias kept for clarity below
            sku_ref: dict = template_state

            # Bill line item: explicit user-set fields take highest priority over sku_ref
            _lines = row.state.get("line_items") or []
            doc_line: dict = _lines[it.po_line_index] if 0 <= it.po_line_index < len(_lines) else {}

            # Fields to inherit from existing item; barcode and rfid_epc excluded (both are
            # per-physical-unit: a received parcel is a new unit, so it mints a fresh barcode
            # and carries no physical RFID/EPC tag). gtin is a product identifier and IS
            # inherited from the catalog template.
            _INHERIT = (
                "category", "unit", "sell_by", "description",
                "cost_price", "wholesale_price", "retail_price",
                "tax_codes", "hs_code", "weight", "weight_unit",
                "dimensions", "dimensions_unit", "purchase_sku",
                "purchase_name", "purchase_unit", "purchase_conversion_factor",
                "allow_splitting", "pick_method", "gtin",
            )
            item_data: dict = {k: sku_ref[k] for k in _INHERIT if k in sku_ref and sku_ref[k] is not None}
            if catalog_item_id:
                item_data["catalog_item_id"] = catalog_item_id
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
            # Attributes from the bill line or the request are caller-authored, so a price
            # among them (other than one carried over unchanged from the item) takes the
            # same set_inventory_prices gate as every inventory writer.
            _inherited = (sku_ref.get("attributes") or {}) | {k: sku_ref.get(k) for k in _INHERIT}
            _authored = {k: v for k, v in (item_data.get("attributes") or {}).items() if _inherited.get(k) != v}
            reject_system_item_fields({"attributes": _authored})
            reject_price_change(price_keys_in({"attributes": _authored}, _recv_price_lists), role, settings)
            # Payload values always take precedence for the fields below
            item_data.update({
                "sku": _sku,
                "name": it.name,
                "quantity": stock_qty_received,
                "location_id": payload.location_id,
            })
            # Fresh sequential barcode per physical lot (unique + scannable), taken
            # from the batch allocated under the code-namespace lock above.
            item_data["barcode"] = _recv_barcodes[_recv_barcode_idx]
            _recv_barcode_idx += 1
            if received_cost is not None:
                item_data["cost_total"] = received_cost
            # Attach the landed cost allocated to this goods line, per stock unit: the projection
            # derives cost_total = cost_base + Σ(unit × quantity), so the parcel carries its landed share.
            _landed = {k: u / conversion for k, u in bill_alloc.get(it.po_line_index, {}).items()}
            if _landed:
                item_data["landed_contributions"] = {f"{entity_id}::{k}": u for k, u in _landed.items()}
                for _k, _u in _landed.items():
                    landed_drawdown[_k] = round_basis(landed_drawdown.get(_k, 0.0) + _u * stock_qty_received)
            if not is_consignment:
                # Received goods are booked as purchased inventory, so the lot records that account.
                item_data[LOT_ACCOUNT_FIELD] = purchased_account
            if is_consignment:
                item_data["consignment_flag"] = "in"
                # Pair the new parcel with the consignment doc: inventory renders the
                # number in the status cell and q-search matches it.
                item_data["status_doc_id"] = entity_id
                item_data["status_doc_number"] = row.state.get("doc_number") or row.state.get("ref_id") or ""
            new_eid = f"item:{_step_id(key, 'line', line_no)}"
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
                idempotency_key=_step_key(key, "line", line_no),
                metadata_={"source_doc": entity_id},
            )
            if not is_consignment and (doc_type == "purchase_order" or _landed):
                parcel = await session.get(Projection, {"company_id": company_id, "entity_id": new_eid})
                line_lot_account[line_no] = lot_account(parcel.state)
                if _landed:
                    code = line_lot_account[line_no]
                    landed_by_account[code] = landed_by_account.get(code, 0.0) + sum(
                        u * stock_qty_received for u in _landed.values())

    # What this receipt did with each line it received: stock lines add stock, the rest none.
    line_counts = {"stock": 0, "expense": 0, "asset": 0}
    for it in payload.received_items:
        line_counts[it.receive_as] += 1
    result = {"line_counts": line_counts}
    entry = await emit_event(
        session, company_id=company_id, entity_id=entity_id, entity_type="doc", event_type="doc.received",
        data={
            "received_items": [{**x.model_dump(exclude_none=True), **added_to_lot.get(i, {})}
                               for i, x in enumerate(payload.received_items)],
            "location_id": payload.location_id,
            "received_by": str(user.id),
            "notes": payload.notes,
            "created_item_ids": created_item_ids,
        },
        actor_id=user.id, location_id=location_uuid, source="api",
        idempotency_key=key, metadata_={"request": digest, "result": result},
    )

    if doc_type == "purchase_order":
        # A purchase order books the goods this receipt brought in: Dr inventory / Cr AP. The
        # bill it becomes books only what its receipts have not.
        # Goods added to stock are booked on their lot's inventory account; anything else
        # on the account of the role it was received as.
        debits: dict = {}
        for line_no, (it, (_, _, received_cost)) in enumerate(zip(payload.received_items, priced)):
            role = auto_je.po_receipt_role(row.state, it.receive_as)
            target = line_lot_account.get(line_no, role) if role == AccountRole.INVENTORY_PURCHASED else role
            debits[target] = debits.get(target, 0.0) + received_cost
        await auto_je.create_for_po_receipt(
            session, company_id=company_id, user_id=user.id, po_id=entity_id,
            receipt_key=key, debits=debits,
        )
    elif doc_type == "bill" and landed_drawdown:
        # A bill already recognised goods + AP at finalize (create_for_bill_conversion); receiving must
        # NOT re-post that JE (it would double-count inventory and AP). Receipt only capitalises the
        # received landed cost from the clearing accounts into inventory (Dr inventory / Cr clearing).
        await auto_je.create_for_landed_capitalisation(
            session, company_id=company_id, user_id=user.id, doc_id=entity_id,
            landed_by_kind=landed_drawdown, landed_by_account=landed_by_account, receive_suffix=key,
        )
    # consignment_in: no JE (goods not owned).
    await session.commit()
    return {"event_id": entry.id, **result}


def _lot_additions(doc: dict) -> dict[str, tuple[float, float]]:
    """Lot id -> (stock quantity, cost) the document's receipts added to lots already on hand
    and that is still there: what came in, less what went back."""
    added: dict[str, tuple[float, float]] = {}
    for x in doc.get("received_items") or []:
        if "lot_quantity_added" in x:
            qty, cost = added.get(x["item_id"], (0.0, 0.0))
            added[x["item_id"]] = (qty + float(x["lot_quantity_added"]), cost + float(x["lot_cost_added"] or 0))
    for x in doc.get("returned_items") or []:
        if x["item_id"] in added and "lot_quantity_taken" in x:
            qty, cost = added[x["item_id"]]
            added[x["item_id"]] = (qty - float(x["lot_quantity_taken"]), cost - float(x["lot_cost_taken"] or 0))
    return {lot: (max(0.0, round_basis(qty)), max(0.0, round_basis(cost))) for lot, (qty, cost) in added.items()}


def _line_quantities_received(doc: dict) -> dict[int, float]:
    """Line index -> purchase units received on the line so far."""
    lines = doc.get("line_items") or []
    received: dict[int, float] = {}
    for x in doc.get("received_items") or []:
        line_index = received_line_index(lines, x)
        if line_index is not None:
            received[line_index] = received.get(line_index, 0.0) + float(x.get("quantity_received") or 0)
    return received


async def _historical_doc(session: AsyncSession, company_id, entity_id: str, *, doc_type: str,
                          event_types: tuple[str, ...], idempotency_key: str):
    """(locked doc row, earlier entry) for a historical receipt or delivery: the earlier entry
    when ``idempotency_key`` was already used for it, else None once the doc is checked to be
    an issued ``doc_type``."""
    row = await _get_doc(session, company_id, entity_id, for_update=True)
    replay = await find_event_by_idempotency(session, company_id, idempotency_key)
    if replay is not None:
        if replay.event_type not in event_types or replay.entity_id != entity_id:
            raise HTTPException(status_code=409, detail="Idempotency key was already used for another operation")
        replay.was_deduped = True
        return row, replay
    if row.state.get("doc_type") != doc_type or row.state.get("status") in ("draft", "void"):
        raise HTTPException(status_code=409, detail=f"Only an issued {doc_type} can record goods moved before it came to Celerp")
    return row, None


def _historical_line(doc: dict, moved: dict, already: float) -> dict:
    """The doc line a historical movement names, checked to hold its item and to have room
    for its quantity beside the ``already`` moved on it."""
    lines = doc.get("line_items") or []
    index = moved["line"]
    line = lines[index] if 0 <= index < len(lines) else None
    if line is None or line.get("item_id") != moved["item_id"]:
        raise HTTPException(status_code=422, detail=f"Line {index + 1} does not hold item {moved['item_id']}")
    still_open = float(line.get("quantity") or 0) - already
    if float(moved["quantity"]) > still_open + 1e-9:
        raise HTTPException(status_code=422, detail=f"Line {index + 1}: at most {still_open:g} can be moved")
    return line


async def record_historical_receipt(session: AsyncSession, company_id, entity_id: str, *, lines: list[dict],
                                    received_on: str, actor_id, source: str, idempotency_key: str):
    """Record goods an issued bill received before its books came to Celerp, as receiving
    onto a lot already on hand records them: each of ``lines`` ({line, item_id, quantity,
    cost}) names the bill line, the lot, and the stock quantity and cost it added to that
    lot. The stock itself is carried separately, so this moves no stock and posts no
    journal entry. Returns the doc.received entry, or the earlier one when
    ``idempotency_key`` was already used for this receipt."""
    row, replay = await _historical_doc(session, company_id, entity_id, doc_type="bill",
                                        event_types=("doc.received",), idempotency_key=idempotency_key)
    if replay is not None:
        return replay
    held = _line_quantities_received(row.state)
    received_items = []
    for moved in lines:
        line = _historical_line(row.state, moved, held.get(moved["line"], 0.0))
        quantity = float(moved["quantity"])
        held[moved["line"]] = held.get(moved["line"], 0.0) + quantity
        received_items.append({
            "po_line_index": moved["line"], "item_id": moved["item_id"], "quantity_received": quantity,
            "receive_as": "stock", **{k: line[k] for k in ("sku", "name") if line.get(k)},
            "lot_quantity_added": quantity, "lot_cost_added": float(moved["cost"]),
        })
    return await emit_event(
        session, company_id=company_id, entity_id=entity_id, entity_type="doc", event_type="doc.received",
        data={"received_items": received_items, "location_id": "",
              "received_by": str(actor_id), "created_item_ids": [], "ts": received_on},
        actor_id=actor_id, location_id=None, source=source, idempotency_key=idempotency_key,
    )


async def record_historical_delivery(session: AsyncSession, company_id, entity_id: str, *, lines: list[dict],
                                     actor_id, source: str, idempotency_key: str):
    """Record goods an issued invoice delivered before its books came to Celerp, as fulfilling
    it records them: each of ``lines`` ({line, item_id, quantity, cost, lot_id, date}) becomes
    a sold lot ``lot_id`` of that quantity and cost, taken from the line's item and fulfilled
    on the invoice at that line on ``date``. A line delivered more than once has one lot per
    delivery: the line names the first, and the others belong to it as the lots a fulfilment
    draws beside the line's own do. The stock that left is carried separately, so the item's
    own quantity is not changed and no journal entry posts. Returns the doc.fulfilled or
    doc.partially_fulfilled entry, or the earlier one when ``idempotency_key`` was already
    used for these deliveries. Each lot is stock of the product its line's item records
    (``historical_lots``)."""
    from celerp_docs.historical_lots import link_historical_lots
    from celerp_inventory.services import allocate_internal_codes, lot_fields

    row, replay = await _historical_doc(session, company_id, entity_id, doc_type="invoice",
                                        event_types=("doc.fulfilled", "doc.partially_fulfilled"),
                                        idempotency_key=idempotency_key)
    if replay is not None:
        return replay
    state = row.state
    new_lines = [dict(li) for li in state.get("line_items") or []]
    barcodes = await allocate_internal_codes(session, company_id, len(lines))
    doc_number = state.get("doc_number") or state.get("ref_id") or ""
    delivered: dict[int, float] = {}
    for moved, barcode in zip(lines, barcodes):
        index = moved["line"]
        line = _historical_line(state, moved, delivered.get(index, 0.0))
        item = await session.get(Projection, {"company_id": company_id, "entity_id": moved["item_id"]})
        if item is None:
            raise HTTPException(status_code=422, detail=f"Item {moved['item_id']} does not exist")
        quantity, lot_id = float(moved["quantity"]), moved["lot_id"]
        await emit_event(
            session, company_id=company_id, entity_id=lot_id, entity_type="item", event_type="item.created",
            data={**lot_fields(item.state), "sku": item.state.get("sku", ""), "name": item.state.get("name", ""),
                  "quantity": quantity, "status": "available", "barcode": barcode,
                  "allow_splitting": splitting_allowed(item.state), "cost_total": float(moved["cost"])},
            actor_id=actor_id, location_id=None, source=source,
            idempotency_key=f"{idempotency_key}:lot:{lot_id}", metadata_={"parent_id": moved["item_id"]},
        )
        await emit_event(
            session, company_id=company_id, entity_id=lot_id, entity_type="item", event_type="item.fulfilled",
            data={"source_doc_id": entity_id, "doc_number": doc_number, "quantity_fulfilled": quantity,
                  "fulfilled_by": str(actor_id), "doc_type": "invoice", "ts": moved["date"]},
            actor_id=actor_id, location_id=None, source=source,
            idempotency_key=f"{idempotency_key}:fulfilled:{lot_id}",
            metadata_={"doc_id": entity_id, "line_index": index,
                       "source_line_id": (state.get("line_items") or [])[index].get("line_id")},
        )
        if index not in delivered:
            new_lines[index] = {**line, "entity_id": lot_id, "item_id": lot_id}
        delivered[index] = delivered.get(index, 0.0) + quantity
    full = {i for i, quantity in delivered.items()
            if abs(quantity - float(new_lines[i].get("quantity") or 0)) <= 1e-9}
    await emit_event(
        session, company_id=company_id, entity_id=entity_id, entity_type="doc", event_type="doc.updated",
        data={"fields_changed": {"line_items": {"old": state.get("line_items"), "new": new_lines}}},
        actor_id=actor_id, location_id=None, source=source, idempotency_key=f"{idempotency_key}:lines",
    )
    await link_historical_lots(session, company_id, [moved["lot_id"] for moved in lines])
    stock_lines = [i for i, li in enumerate(new_lines) if li.get("entity_id") or li.get("item_id")]
    data = {"fulfilled_items": _line_item_brief(new_lines, [new_lines[i]["entity_id"] for i in sorted(delivered)]),
            "fulfilled_by": str(actor_id), "fulfilled_at": max(m["date"] for m in lines),
            "strategy": "per_line", "ts": max(m["date"] for m in lines)}
    if all(i in full for i in stock_lines):
        event_type, data = "doc.fulfilled", {**data, "total_cogs": sum(float(m["cost"]) for m in lines)}
    else:
        event_type = "doc.partially_fulfilled"
        data["unfulfilled_items"] = _line_item_brief(
            new_lines, [new_lines[i].get("entity_id") or new_lines[i].get("item_id") for i in stock_lines if i not in full])
    return await emit_event(
        session, company_id=company_id, entity_id=entity_id, entity_type="doc", event_type=event_type,
        data=data, actor_id=actor_id, location_id=None, source=source, idempotency_key=idempotency_key,
    )


async def _returnable_quantities(session: AsyncSession, company_id, doc: dict) -> dict[str, float]:
    """Item id -> stock units the document's receipts brought in and it has not sent back."""
    from celerp.models.ledger import LedgerEntry

    got: dict[str, float] = {}
    legacy: dict[str, float] = {}  # purchase order receipts made before lots recorded what they added
    for x in doc.get("received_items") or []:
        if "lot_quantity_added" in x:
            got[x["item_id"]] = got.get(x["item_id"], 0.0) + float(x["lot_quantity_added"] or 0)
        elif (doc.get("doc_type") == "purchase_order" and x.get("item_id")
              and (x.get("receive_as") or "stock") == "stock"):
            legacy[x["item_id"]] = legacy.get(x["item_id"], 0.0) + float(x.get("quantity_received") or 0)
    if legacy:
        rows = (await session.execute(select(Projection).where(
            Projection.company_id == company_id, Projection.entity_id.in_(list(legacy))))).scalars().all()
        conversion = {r.entity_id: float(r.state.get("purchase_conversion_factor") or 1) for r in rows}
        for item_id, qty in legacy.items():
            got[item_id] = got.get(item_id, 0.0) + qty * conversion.get(item_id, 1)
    created = doc.get("received_item_ids") or []
    if created:
        for entry in (await session.execute(select(LedgerEntry).where(
                LedgerEntry.company_id == company_id, LedgerEntry.entity_id.in_(created),
                LedgerEntry.event_type == "item.created"))).scalars():
            got[entry.entity_id] = got.get(entry.entity_id, 0.0) + float((entry.data or {}).get("quantity") or 0)
    for x in doc.get("returned_items") or []:
        if x.get("item_id") in got:
            got[x["item_id"]] -= float(x.get("quantity_returned") or 0)
    return got


def _receipt_lots_by_line(state: dict) -> tuple[dict[int, list[str]], set[str]] | None:
    """The lots each document line's stock receipts went into, in receipt order, and the lots
    more than one line fed. A purchase order receipt names the lot it added to; every other
    stock receipt made the next parcel the document records. None when a receipt cannot be
    paired with its lot and its line, as then no line's goods can be told apart."""
    lines = state.get("line_items") or []
    created = iter(state.get("received_item_ids") or [])
    inbound = state.get("doc_type") in INBOUND_DOC_TYPES
    by_line: dict[int, list[str]] = {}
    fed_by: dict[str, set[int]] = {}
    for x in state.get("received_items") or []:
        if (x.get("receive_as") or "stock") != "stock":
            continue
        lot = x["item_id"] if x.get("item_id") and not inbound else next(created, None)
        index = received_line_index(lines, x)
        if lot is None or index is None:
            return None
        if lot not in by_line.setdefault(index, []):
            by_line[index].append(lot)
        fed_by.setdefault(lot, set()).add(index)
    if next(created, None) is not None:
        return None
    return by_line, {lot for lot, fed in fed_by.items() if len(fed) > 1}


def _free_on_hand(state: dict | None) -> float:
    """Stock units of a lot on our shelf and held for nothing: none unless the lot is available."""
    from celerp_inventory.projections import is_item_available

    if state is None or not is_item_available(state):
        return 0.0
    return max(0.0, float(state.get("quantity") or 0) - float(state.get("reserved_quantity") or 0))


# What can hold goods a document brought in so they cannot go back to the supplier yet.
_RETURN_HOLDS = ("reserved", "sold", "memo_out")


def _held_back(state: dict | None, kept: float, free: float) -> dict[str, float]:
    """Why ``kept - free`` of the stock units a document brought into a lot and has not sent
    back cannot go back now: reason -> units. Reserved, sold or out on memo; whatever is no
    longer in the lot at all is ``not_in_stock``."""
    from celerp_inventory.projections import is_item_available

    held = kept - free
    if held <= 1e-9:
        return {}
    if state is None:
        return {"not_in_stock": held}
    if not is_item_available(state):
        status = str(state.get("status") or "").lower()
        return {status if status in _RETURN_HOLDS else "not_in_stock": held}
    gone = min(held, max(0.0, kept - float(state.get("quantity") or 0)))
    return {k: v for k, v in (("reserved", held - gone), ("not_in_stock", gone)) if v > 1e-9}


class _LineReturnLots(NamedTuple):
    by_line: dict[int, list[tuple[str, float]]]  # line -> [(lot, units it can send back now)]
    shared: set[str]                               # lots more than one line fed
    kept: dict[str, float]                         # lot -> units brought in, not sent back
    held: dict[int, dict[str, float]]              # line -> reason -> units it cannot send back now


async def _line_return_lots(session: AsyncSession, company_id, doc: dict, *, lock: bool
                            ) -> _LineReturnLots | None:
    """Per line, the lots its stock receipts went into with the stock units of each it can send
    back now, in receipt order, and why the rest of what it brought in cannot go back; None when
    the receipts cannot be traced to their lines. A line sends back what it brought in and has
    not sent back, while it is on hand and free."""
    traced = _receipt_lots_by_line(doc)
    if traced is None:
        return None
    lots_by_line, shared = traced
    ids = sorted({lot for lots in lots_by_line.values() for lot in lots})
    if lock:
        states = {eid: r.state for eid, r in (await lock_projections(session, company_id, ids)).items()}
    else:
        states = {r.entity_id: r.state for r in (await session.execute(select(Projection).where(
            Projection.company_id == company_id, Projection.entity_id.in_(ids)))).scalars()} if ids else {}
    kept = await _returnable_quantities(session, company_id, doc)
    by_line: dict[int, list[tuple[str, float]]] = {}
    held: dict[int, dict[str, float]] = {}
    for index, lots in lots_by_line.items():
        by_line[index] = []
        for lot in lots:
            k = max(0.0, kept.get(lot, 0.0))
            free = min(k, _free_on_hand(states.get(lot)))
            by_line[index].append((lot, free))
            for reason, units in _held_back(states.get(lot), k, free).items():
                held.setdefault(index, {})[reason] = held.get(index, {}).get(reason, 0.0) + units
    return _LineReturnLots(by_line, shared, kept, held)


class ReturnItem(BaseModel):
    item_id: str
    quantity_returned: FiniteFloat = Field(gt=0)


class ReturnLine(BaseModel):
    """A document line to send goods back from, by its id (or its position when it has none),
    and the stock units to send back. The server finds the lots."""
    line_id: str | None = None
    line_index: int | None = None
    quantity_returned: FiniteFloat = Field(gt=0)

    @model_validator(mode="after")
    def _names_one_line(self):
        if (self.line_id is None) == (self.line_index is None):
            raise ValueError("Name each line by its line_id, or by line_index when it has no id.")
        return self


class ReturnBody(BaseModel):
    items: list[ReturnItem] = Field(default_factory=list)
    lines: list[ReturnLine] | None = None
    notes: str | None = None
    idempotency_key: str | None = None

    @model_validator(mode="after")
    def _items_or_lines(self):
        if bool(self.items) == bool(self.lines):
            raise ValueError("Give the items or the lines to return, one of the two.")
        return self

    @model_serializer(mode="wrap")
    def _omit_absent_lines(self, handler):
        # A request naming lots serializes as it always has, so its retries still match.
        data = handler(self)
        if data.get("lines") is None:
            data.pop("lines", None)
        return data


def _line_label(line: dict) -> str:
    return str(line.get("sku") or line.get("name") or line.get("description") or "--")


async def _return_lines_as_lots(session: AsyncSession, company_id, doc: dict,
                                lines: list[ReturnLine]) -> list[tuple[ReturnItem, str | None]]:
    """The lots and quantities selected lines send back, each with its line's id: every line's
    quantity is taken from its lots in receipt order, from goods on hand and free."""
    doc_lines = doc.get("line_items") or []
    picked: list[int] = []
    for ln in lines:
        if ln.line_id is not None:
            found = [i for i, li in enumerate(doc_lines) if li.get("line_id") == ln.line_id]
            index = found[0] if len(found) == 1 else None
        else:
            index = (ln.line_index if 0 <= ln.line_index < len(doc_lines)
                     and not doc_lines[ln.line_index].get("line_id") else None)
        if index is None:
            raise HTTPException(status_code=422, detail=refusal(
                "docs.return_line_unknown",
                "A selected line is not on this document as named. Reload the document and select the lines again."))
        if index in picked:
            raise HTTPException(status_code=422, detail="Each line can be selected once.")
        picked.append(index)
    traced = await _line_return_lots(session, company_id, doc, lock=True)
    if traced is None:
        raise HTTPException(status_code=409, detail=refusal(
            "docs.return_line_untraced",
            "Some goods received on this document cannot be traced to their line, so they cannot be returned by line."))
    by_line, shared = traced.by_line, traced.shared
    out: list[tuple[ReturnItem, str | None]] = []
    for ln, index in zip(lines, picked):
        line = doc_lines[index]
        lots = by_line.get(index, [])
        if any(lot in shared for lot, _ in lots):
            raise HTTPException(status_code=409, detail=refusal(
                "docs.return_line_shared",
                f"{_line_label(line)}: goods received on this line went into the same stock as another line's, "
                "so they cannot be returned by line.", name=_line_label(line)))
        free = sum(q for _, q in lots)
        if ln.quantity_returned > free + 1e-9:
            raise HTTPException(status_code=422, detail=refusal(
                "docs.return_line_not_on_hand",
                f"{_line_label(line)}: at most {free:g} received on this line is on hand and free to return. "
                "Goods sold, out on memo or reserved go back to the supplier only once they are back in stock "
                "and free.", name=_line_label(line), qty=f"{free:g}"))
        left = float(ln.quantity_returned)
        for lot, q in lots:
            take = min(q, left)
            if take > 1e-9:
                out.append((ReturnItem(item_id=lot, quantity_returned=take), line.get("line_id")))
                left -= take
    return out


@router.post("/{entity_id}/return-items")
async def return_consignment_items(entity_id: str, payload: ReturnBody, company_id: str = Depends(get_current_company_id), _: None = require_permission("fulfill_documents"), user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    """Send received goods back to the supplier, named either by lot (``items``) or by document
    line (``lines``, a stock quantity per line, taken from that line's lots on the server)."""
    # A return takes goods off the lots it reads, so it waits for any receipt or cost
    # change in flight and reads what that one committed.
    row = await _get_doc(session, company_id, entity_id, for_update=True)
    key, digest = _operation("return-items", entity_id, payload)
    if (done := await _earlier_run(session, company_id, key, event_type="doc.items_returned",
                                   entity_id=entity_id, digest=digest)) is not None:
        return done
    doc_type = row.state.get("doc_type")
    if doc_type not in ("consignment_in", "bill", "purchase_order"):
        raise HTTPException(status_code=409, detail="return-items is only valid for bills, POs, and consignment_in documents")
    if row.state.get("status") not in SUPPLIER_RETURN_STATUSES:
        status = row.state.get("status") or ""
        raise HTTPException(status_code=409, detail=refusal(
            "docs.return_not_open",
            f"Nothing can be returned on this document while it is {status.replace('_', ' ')}. Goods go back "
            "to the supplier only after they are received, from a document that is still open.",
            doc_status=status))

    from celerp_inventory.projections import is_item_available
    from celerp_inventory.services import goods_basis

    if payload.lines:
        picked = await _return_lines_as_lots(session, company_id, row.state, payload.lines)
    else:
        picked = [(it, None) for it in payload.items]
    items = [it for it, _ in picked]

    # A document sends back only goods it brought in, and no more than it still holds of them.
    label = {**_RECEIVING_DOC_LABEL, "consignment_in": "consignment"}[doc_type]
    returnable = await _returnable_quantities(session, company_id, row.state)
    for it in items:
        if it.quantity_returned <= 0:
            raise HTTPException(status_code=422, detail=f"{it.item_id}: the quantity to return must be more than 0.")
        left = returnable.get(it.item_id)
        if left is None:
            raise HTTPException(status_code=422,
                                detail=f"{it.item_id} was not received on this {label}, so it cannot be returned on it.")
        if it.quantity_returned > left + 1e-9:
            raise HTTPException(
                status_code=422,
                detail=f"{it.item_id}: at most {max(0.0, left):g} received on this {label} can still be returned.")
        returnable[it.item_id] = left - it.quantity_returned

    # Owned goods leave the books at what they carried; consigned goods were never on them.
    owned = doc_type != "consignment_in"
    lots = await lock_projections(session, company_id, [it.item_id for it in items])
    added = _lot_additions(row.state)
    unit_map = await _get_unit_map(session, company_id)
    currency = await auto_je.company_currency(session, company_id)
    goods_role = auto_je.po_receipt_role(row.state)
    goods: dict = {}  # cost leaving, per lot inventory account (or role, for goods not held as stock)
    landed_by_kind: dict[str, float] = {}
    landed_by_account: dict[str, float] = {}
    returned: list[dict] = []
    for line_no, (it, source_line_id) in enumerate(picked):
        item = lots.get(it.item_id)
        if item is None or item.entity_type != "item":
            raise HTTPException(status_code=404, detail=f"Item not found: {it.item_id}")
        # Only goods on the shelf and held for nothing can go back to a supplier. Goods out on
        # memo are at a customer's site, sold goods have left, and reserved goods are promised:
        # shrinking any of them here would write off stock still owed to someone.
        sku = item.state.get("sku") or it.item_id
        status = str(item.state.get("status") or "").lower()
        if not is_item_available(item.state):
            raise HTTPException(status_code=409, detail=refusal(
                "docs.return_not_on_hand",
                f"Cannot return {sku}: it is {status}, not on hand. Goods sold, out on memo or reserved go back "
                "to the supplier only once they are back in stock and free.", sku=sku, status=status))
        validate_line_quantity(it.quantity_returned, item.state.get("sell_by"), unit_map, label=sku)
        current_qty = float(item.state.get("quantity", 0) or 0)
        free = _free_on_hand(item.state)
        if it.quantity_returned > free + 1e-9:
            raise HTTPException(status_code=409, detail=refusal(
                "docs.return_more_than_free",
                f"Cannot return {it.quantity_returned:g} of {sku}: {free:g} is on hand and not reserved.",
                qty=f"{it.quantity_returned:g}", sku=sku, free=f"{free:g}"))
        new_qty = max(0.0, current_qty - it.quantity_returned)
        adjustment: dict = {"new_qty": new_qty}
        returned.append({**it.model_dump(), **({"source_line_id": source_line_id} if source_line_id else {})})
        if not owned:
            adjustment["consignment_flag"] = None if new_qty == 0 else "in"
        else:
            # Units this document added to a lot already on hand and that are still there go
            # back first, at what they were received for; any other units take their share of
            # the rest of the lot's cost. The last units out take whatever cost is left.
            basis = goods_basis(item.state) or 0.0
            qty_added, cost_added = added.get(it.item_id, (0.0, 0.0))
            taken = min(it.quantity_returned, qty_added)
            taken_cost = cost_added if taken == qty_added else cost_added * taken / qty_added if qty_added else 0.0
            others_qty = current_qty - qty_added
            others_cost = ((basis - cost_added) * (it.quantity_returned - taken) / others_qty
                           if others_qty > 1e-9 else 0.0)
            share = basis if new_qty == 0 else min(
                basis, to_stored_float(round_money(taken_cost + others_cost, currency)))
            adjustment["cost_base"] = round_basis(basis - share)
            origin = lot_account(item.state)
            target = origin if goods_role == AccountRole.INVENTORY_PURCHASED else goods_role
            goods[target] = goods.get(target, 0.0) + share
            if it.item_id in added:
                taken_cost = min(share, to_stored_float(round_money(taken_cost, currency)))
                returned[-1].update({"lot_quantity_taken": taken, "lot_cost_taken": taken_cost})
            for contribution, unit in (item.state.get("landed_contributions") or {}).items():
                kind = contribution.rsplit("::", 1)[-1]
                landed_by_kind[kind] = landed_by_kind.get(kind, 0.0) + float(unit or 0) * it.quantity_returned
                landed_by_account[origin] = landed_by_account.get(origin, 0.0) + float(unit or 0) * it.quantity_returned
        await emit_event(
            session, company_id=company_id, entity_id=it.item_id, entity_type="item",
            event_type="item.quantity.adjusted", data=adjustment,
            actor_id=user.id, location_id=None, source="api",
            idempotency_key=_step_key(key, "line", line_no), metadata_={"source_return": entity_id},
        )

    if owned:
        await auto_je.create_for_supplier_return(
            session, company_id=company_id, user_id=user.id, doc_id=entity_id, return_key=key,
            goods=goods, landed_by_kind=landed_by_kind, landed_by_account=landed_by_account,
        )
    entry = await emit_event(
        session, company_id=company_id, entity_id=entity_id, entity_type="doc",
        event_type="doc.items_returned",
        data={
            "items": returned,
            "returned_by": str(user.id),
            "notes": payload.notes,
            # Whether the document now holds nothing to send back, judged in stock units here
            # because a line's received quantity may be in a purchase unit.
            "all_returned": all(left <= 1e-9 for left in returnable.values()) and not any(
                (x.get("receive_as") or "stock") != "stock" for x in row.state.get("received_items") or []),
        },
        actor_id=user.id, location_id=None, source="api",
        idempotency_key=key, metadata_={"request": digest},
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
    named = await _lock_copied_contacts(session, company_id, doc_ids)
    # The company comes next: the shipment draws the next shipping-doc number.
    company = await locked_company(session, company_id)
    locked = await _get_docs_for_update(session, company_id, doc_ids)
    _assert_contacts_unchanged(named, locked.values())
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
        "contact_name": (first.get("contact_name") or first.get("customer_name"))
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


class DocConvertBody(BaseModel):
    idempotency_key: str | None = None


@router.post("/{entity_id}/convert")
async def convert_doc(entity_id: str, payload: DocConvertBody | None = None, company_id: str = Depends(get_current_company_id), _: None = require_permission("edit_documents"), user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    named = await _lock_copied_contacts(session, company_id, [entity_id])
    # Company before the doc row: a conversion draws the next document number.
    company = await locked_company(session, company_id)
    row = await _get_doc(session, company_id, entity_id, for_update=True)
    key, digest = _operation("convert", entity_id, payload or DocConvertBody())
    if (done := await _earlier_run(session, company_id, key, event_type="doc.converted",
                                   entity_id=entity_id, digest=digest, with_event_id=True)) is not None:
        return done
    _assert_contacts_unchanged(named, [row])
    state = row.state
    if state.get("doc_type") == "quotation":
        if state.get("status") in {"void", "converted"}:
            raise HTTPException(status_code=409, detail="Cannot convert quotation in current status")
        valid_until = state.get("valid_until")
        if valid_until and valid_until < business_date_of(None, (company.settings or {}).get("timezone")):
            raise HTTPException(status_code=409, detail="Cannot convert expired quotation")
        ref = next_draft_ref(company, "invoice")
        new_doc_id = f"doc:{ref}"
        # The invoice is a new draft: none of the quotation's own lifecycle carries over.
        new_data = {k: v for k, v in state.items() if k not in LIFECYCLE_OWNED_FIELDS}
        new_data.update({"doc_type": "invoice", "ref_id": ref, "source_quotation_id": entity_id, "status": "draft"})
        await emit_event(
            session, company_id=company_id, entity_id=new_doc_id, entity_type="doc", event_type="doc.created", data=new_data,
            actor_id=user.id, location_id=None, source="api", idempotency_key=str(uuid.uuid4()), metadata_={},
        )
        entry = await emit_event(
            session, company_id=company_id, entity_id=entity_id, entity_type="doc", event_type="doc.converted",
            data={"target_doc_id": new_doc_id, "target_doc_type": "invoice"}, actor_id=user.id, location_id=None,
            source="api", idempotency_key=key, metadata_={"request": digest, "result": {"target_doc_id": new_doc_id}},
        )
        await session.commit()
        return {"event_id": entry.id, "target_doc_id": new_doc_id}

    if state.get("doc_type") == "memo":
        # An already-converted memo falls through to the allocation-set guard below,
        # which refuses with the clearer "nothing On Memo" 422: convert hands every
        # backing lot to the invoice, so a converted memo has an empty allocation set and
        # can never re-bill. Only a genuinely-not-issued memo (draft/void) is rejected here.
        if state.get("status") not in ("final", "sent", "received", "partially_received", "converted"):
            raise HTTPException(status_code=409, detail="Memo must be issued before converting to invoice")
        ref = next_draft_ref(company, "invoice")
        new_doc_id = f"doc:{ref}"

        # The memo's settlement unit is its allocation set (every lot stamped
        # status_doc_id==this memo), not line_items: a memo line whose quantity exceeds
        # its bound lot draws cross-lot siblings from other lots of the same SKU, and each
        # sibling carries the memo stamp but never appears in line_items. Bill each line
        # for the full still-out quantity of its SKU summed across the allocation set, at
        # the line's own unit_price (revenue is per-SKU and identical across lots; the cost
        # of each lot is booked when the invoice is finalized).
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
        # original line at that line's unit_price - the sibling's revenue is per-SKU.
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
        # The invoice may bill fewer goods than the memo held, so the memo's totals are stale:
        # the invoice's money is computed from the lines it bills. It is a new draft, so none
        # of the memo's own lifecycle (finalized, sent, fulfilled) carries over: finalizing the
        # invoice is what books its revenue and the cost of the goods sold.
        _MEMO_TOTAL_FIELDS = frozenset({"total", "outstanding", "tax_total", "discount_total", "subtotal", "amount_due"})
        new_data = {k: v for k, v in filtered_state.items() if k not in LIFECYCLE_OWNED_FIELDS | _MEMO_TOTAL_FIELDS}
        new_data.update(document_money(new_data, qualifying_line_items, _currency, keep_unrated_tax=True))
        new_data.update({"doc_type": "invoice", "ref_id": ref, "source_memo_id": entity_id, "status": "draft"})
        await emit_event(
            session, company_id=company_id, entity_id=new_doc_id, entity_type="doc", event_type="doc.created", data=new_data,
            actor_id=user.id, location_id=None, source="api", idempotency_key=str(uuid.uuid4()), metadata_={},
        )
        entry = await emit_event(
            session, company_id=company_id, entity_id=entity_id, entity_type="doc", event_type="doc.converted",
            data={"target_doc_id": new_doc_id, "target_doc_type": "invoice", "pre_convert_status": state.get("status")},
            actor_id=user.id, location_id=None, source="api",
            idempotency_key=key, metadata_={"request": digest, "result": {"target_doc_id": new_doc_id}},
        )
        # The invoice owns every backing lot from here, cross-lot siblings included. The
        # goods are still out with the customer and the draft cannot ship, so each stays
        # memo_out, now under the invoice: finalizing it sells them, taking them back on it
        # returns them to stock, and voiding or deleting the draft gives them back to this
        # memo (_return_to_source_memo).
        for item_proj in memo_out_backing:
            await emit_event(
                session, company_id=company_id, entity_id=item_proj.entity_id, entity_type="item",
                event_type="item.status.set",
                data={"new_status": "memo_out", "source_doc_id": new_doc_id, "doc_number": ref},
                actor_id=user.id, location_id=None, source="memo_convert",
                idempotency_key=_step_key(key, item_proj.entity_id), metadata_={"doc_id": entity_id},
            )
        await session.commit()
        return {"event_id": entry.id, "target_doc_id": new_doc_id}

    if state.get("doc_type") == "consignment_in":
        if state.get("status") not in ("final", "sent", "received", "partially_received"):
            raise HTTPException(status_code=409, detail="Consignment In must be issued before converting to vendor bill")
        await require_line_destinations(session, company_id, state.get("line_items"))
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
        _consign_base_currency = (company.settings.get("currency", "USD") if company else "USD")
        await auto_je.create_for_bill_conversion(
            session, company_id=company_id, user_id=user.id, doc_id=new_doc_id, doc={**state, "total": bill_total},
            base_currency=_consign_base_currency,
        )
        entry = await emit_event(
            session, company_id=company_id, entity_id=entity_id, entity_type="doc", event_type="doc.converted",
            data={"target_doc_id": new_doc_id, "target_doc_type": "bill"}, actor_id=user.id, location_id=None,
            source="api", idempotency_key=key, metadata_={"request": digest, "result": {"target_doc_id": new_doc_id}},
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
    __: None = require_permission("import_export_data"),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    # Raw import is a snapshot-create transport, not a lifecycle/event escape hatch.
    # Updates go through PATCH and state transitions through their dedicated endpoints.
    if body.event_type != "doc.created":
        raise HTTPException(status_code=422, detail=f"Event type {body.event_type!r} is not import-safe")
    role, settings = await locked_authority(session, company_id, user.id, ("edit_documents", "import_export_data"))
    _assert_doc_import_permissions(settings, role, body.data)

    replay = await find_event_by_idempotency(session, company_id, body.idempotency_key)
    if replay is not None:
        if replay.event_type != "doc.created" or replay.entity_id != body.entity_id:
            raise HTTPException(status_code=409, detail="Idempotency key was already used for another operation")
        return {"event_id": replay.id, "id": replay.entity_id, "idempotency_hit": True}

    # Entity guard: one create event per document identity.
    existing = await session.get(Projection, {"company_id": company_id, "entity_id": body.entity_id})
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail=f"Document {body.entity_id} already exists (status: {existing.state.get('status', 'unknown')}). "
            f"Use PATCH to update or lifecycle endpoints to advance its state.",
        )

    await _lock_imported_contact(session, company_id, "doc", body.data)
    await _assert_import_number_free(session, company_id, "doc", body.data)
    _imp_company = await session.get(Company, company_id)
    _imp_base_currency = (_imp_company.settings.get("currency", "USD") if _imp_company else "USD")
    if auto_je.import_auto_je_kind(body.data) is not None:
        _require_doc_rate_http(body.data, _imp_base_currency)
    if auto_je.import_auto_je_kind(body.data) == "bill":
        await require_line_destinations(session, company_id, body.data.get("line_items"))

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
        metadata_=_import_metadata(body.source_ts),
    )

    # The event type is doc.created by the guard above. Drafts return immediately.
    await _import_auto_je(
        session, company_id, user.id, body.entity_id, body.data,
        base_currency=_imp_base_currency,
    )

    await session.commit()
    return {"event_id": entry.id, "id": entry.entity_id, "idempotency_hit": False}


def _assert_doc_import_permissions(settings: dict, role: str, data: dict) -> None:
    """Require the normal lifecycle permissions for snapshot state an import bypasses.

    ``import_export_data`` authorizes moving data, not issuing documents or recording
    payments.  Draft snapshots need only edit permission; non-draft/payment snapshots
    additionally require the same permissions as their normal product operations.
    """
    status = str(data.get("status") or "draft")
    if status != "draft" or data.get("finalized"):
        assert_role_permission(settings, role, "finalize_documents")
    amount_paid = float(data.get("amount_paid") or 0)
    if amount_paid > 0 or status in {"partial", "paid", "partially_received"}:
        assert_role_permission(settings, role, "record_payments")


# Lifecycle state is owned by dedicated document operations, and the type and
# number are established by the original create; import-upsert rewrites neither.
_DOC_IMPORT_UPSERT_EXCLUDED = LIFECYCLE_OWNED_FIELDS | {"doc_type", "ref_id"}


def _doc_import_fields_changed(state: dict, incoming: dict) -> dict[str, dict]:
    """Translate an imported snapshot into the canonical PATCH shape.

    Import-upsert may refresh editable document data, but cannot manufacture lifecycle
    transitions or replace the document's identity/type.  The normal ``patch_doc``
    implementation remains authoritative for draft/finalized edit rules, line validation,
    price permissions, dates and foreign-reservation checks.
    """
    return {
        key: {"old": state.get(key), "new": value}
        for key, value in incoming.items()
        if key not in _DOC_IMPORT_UPSERT_EXCLUDED and state.get(key) != value
    }


def _import_metadata(source_ts: str | None) -> dict:
    """Ledger metadata of a raw snapshot import, recording that it came through import."""
    meta: dict = {auto_je.IMPORTED_SNAPSHOT: True}
    if source_ts:
        meta["source_ts"] = source_ts
    return meta


async def _import_auto_je(session: AsyncSession, company_id, user_id, entity_id: str, data: dict, base_currency: str = "USD") -> None:
    """Create the accounting entry implied by an imported non-draft snapshot.

    Payment entries are never synthesized from snapshot totals because their bank
    account and settlement date/rate are separate facts that the snapshot cannot supply.
    """
    kind = auto_je.import_auto_je_kind(data)
    if kind is None:
        return
    total = float(data.get("total", 0) or 0)

    if kind == "invoice":
        await auto_je.create_for_doc_finalized(
            session, company_id=company_id, user_id=user_id, doc_id=entity_id,
            doc=data, base_currency=base_currency,
        )
    elif kind == "purchase_order":
        await auto_je.create_for_po_received(
            session, company_id=company_id, user_id=user_id, po_id=entity_id,
            doc=data, total=total, base_currency=base_currency,
            receive_date=data.get("issue_date"),
        )
    elif kind == "bill":
        await auto_je.create_for_bill_conversion(
            session, company_id=company_id, user_id=user_id, doc_id=entity_id,
            doc=data, base_currency=base_currency,
        )


@router.post("/import/batch", response_model=BatchImportResult)
async def batch_import_docs(
    body: DocBatchImportRequest,
    company_id: str = Depends(get_current_company_id),
    _: None = require_permission("edit_documents"),
    __: None = require_permission("import_export_data"),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> BatchImportResult:
    from celerp_docs import import_service

    role, settings = await locked_authority(session, company_id, user.id, ("edit_documents", "import_export_data"))
    outcome = await import_service.import_doc_records(
        session, company_id, user, role, settings, body.records, upsert=body.upsert,
    )
    await session.commit()
    return BatchImportResult(**outcome.route_counts())


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------


_DOC_EXPORT_COLS = ["entity_id", "doc_number", "doc_type", "contact_name", "issue_date", "due_date", "total", "amount_outstanding", "status"]


@router.get("/export/csv", dependencies=[require_permission("view_documents"), require_permission("import_export_data")])
async def export_docs_csv(
    filters: DocListFilters = Depends(),
    cols: str | None = None,
    company_id: str = Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
) -> StreamingResponse:
    """The document list as CSV: the same filters, order and displayed values as the index, every
    matching row (no page), and the columns the screen asked for via ``cols``."""
    out_cols = resolve_export_cols(cols, _DOC_EXPORT_COLS, _DOC_EXPORT_COLS)
    base_where = _doc_sql_where(company_id, filters)
    order_by = _doc_sql_order(_doc_sort_field(filters), filters.dir == "desc")
    keep = _doc_filter(filters, today_iso())
    # Read only the state keys the exported columns and the row filters need, never whole documents.
    fields = {c for c in out_cols if c != "entity_id"} | (set(_DOC_FILTER_KEYS) if keep else set())
    keys = sorted(fields | {k for field in fields for k in DOC_FIELD_FALLBACKS.get(field, ())})

    async def _rows():
        stmt = (
            select(Projection.entity_id, *(Projection.state[k] for k in keys))
            .where(*base_where)
            .order_by(*order_by)
            .execution_options(yield_per=500)
        )
        result = await session.stream(stmt)
        try:
            async for entity_id, *values in result:
                row = _doc_display({k: v for k, v in zip(keys, values) if v is not None})
                if keep is None or keep(row):
                    yield row | {"entity_id": entity_id}
        finally:
            await result.close()

    return StreamingResponse(
        csv_stream(out_cols, _rows()),
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
    contact_id: str | None = None
    contact_name: str | None = None
    line_items: list[dict] = Field(default_factory=list)
    subtotal: FiniteFloat = 0
    discount: FiniteFloat = 0
    discount_type: str = "flat"
    tax: FiniteFloat = 0
    total: FiniteFloat = 0
    currency: str | None = None
    notes: str | None = None
    status: Literal["draft"] = "draft"
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

    _contact_fields = model_validator(mode="before")(_canonical_contact_payload)
    _no_lifecycle_state = model_validator(mode="before")(_reject_lifecycle_fields)
    _draft_only = field_validator("status", mode="before")(_created_as_draft)

    @model_validator(mode="after")
    def _validate_shipment_enums(self) -> "ListCreatePayload":
        _validate_shipment_values({"incoterms": self.incoterms,
                                   "reason_for_export": self.reason_for_export})
        return self


ListPatch = DocPatch


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
    session's identity map. The company lock comes first, as for documents."""
    row = (await lock_projections(session, company_id, [entity_id])).get(entity_id)
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


def _list_customer():
    """The customer a list shows. The index rows, the search and the CSV export all read this one expression."""
    return _func.nullif(Projection.state["contact_name"].as_string(), "")


def _list_converted(target_type: str | None = None) -> list:
    """A list closed by converting it, optionally into ``target_type``. Reopening a list clears
    its result, so a reopened list is no longer converted even though it still names the
    document it was once converted to. Shared by the converted cards and the filter they open."""
    where = [Projection.state["status"].as_string() == CLOSED, Projection.state["result"].as_string() == "converted"]
    if target_type:
        where.append(Projection.state["converted_to_type"].as_string() == target_type)
    return where


def _list_search_where(q: str):
    """SQL predicate for the free-text list search over the reference, the shown customer and
    the contact id."""
    ql = f"%{q.lower()}%"
    return _sa.or_(
        _func.lower(Projection.state["ref_id"].as_string()).like(ql),
        _func.lower(_list_customer()).like(ql),
        _func.lower(Projection.state["contact_id"].as_string()).like(ql),
    )


@dataclass
class ListIndexFilters:
    """Every filter the list index accepts, as one query-parameter dependency shared with its CSV
    export so both narrow the same way."""

    list_type: str | None = None
    status: str | None = None
    exclude_status: str | None = None
    date_from: str | None = None
    date_to: str | None = None
    q: str | None = None
    all_issued: bool = False
    converted_to_type: str | None = None


def _list_index_where(company_id, f: ListIndexFilters, sort_date) -> list:
    """The index's full WHERE for ``f`` (company scope included); ``sort_date`` is the
    ``_list_sort_date()`` expression the caller also orders by."""
    base_where = _list_base_where(company_id)
    if f.list_type:
        base_where.append(Projection.state["list_type"].as_string() == f.list_type)
    if f.all_issued:
        base_where.append(Projection.state["status"].as_string().notin_((DRAFT, VOID)))
    elif f.status:
        base_where.append(Projection.state["status"].as_string() == f.status)
    if f.exclude_status:
        base_where.append(Projection.state["status"].as_string() != f.exclude_status)
    if f.converted_to_type:
        base_where.extend(_list_converted(f.converted_to_type))
    if f.date_from:
        base_where.append(sort_date >= f.date_from)
    if f.date_to:
        base_where.append(sort_date <= f.date_to)
    if f.q:
        base_where.append(_list_search_where(f.q))
    return base_where


def _list_columns(sort_date) -> dict:
    """Every header value the index shows and the CSV exports, by column name, as one set of
    SQL expressions, so a row on screen and its CSV line carry the same values. ``date`` is
    ``sort_date``, the date the index orders and windows by."""
    return {
        "id": Projection.entity_id,
        "ref_id": Projection.state["ref_id"].as_string(),
        "list_type": Projection.state["list_type"].as_string(),
        "customer": _list_customer(),
        "date": sort_date,
        "total": Projection.state["total"].as_string(),
        "status": Projection.state["status"].as_string(),
    }


@lists_router.get("", dependencies=[require_permission("view_documents")])
async def list_lists(
    filters: ListIndexFilters = Depends(),
    limit: int | None = None,
    offset: int = 0,
    company_id: str = Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    sort_date = _list_sort_date()
    base_where = _list_index_where(company_id, filters, sort_date)

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
    columns = _list_columns(sort_date)
    list_q = (
        select(
            *(expr.label(name) for name, expr in columns.items()),
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
        **{name: getattr(r, name) for name in columns},
        "item_count": r.item_count,
        "total_weight": float(r.total_weight or 0),
    } for r in rows]
    return {"items": out, "total": total}


@lists_router.get("/numbered", dependencies=[require_permission("view_documents")])
async def lists_numbered(
    number: str = Query(min_length=1),
    company_id: str = Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """The ids of the lists numbered exactly ``number``."""
    return {"ids": await _ids_numbered(session, company_id, "list", number)}


@lists_router.get("/summary", dependencies=[require_permission("view_documents")])
async def get_list_summary(
    filters: ListIndexFilters = Depends(),
    company_id: str = Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Status counts for the lists page, over the same type, search and date window as list_lists
    so the cards count the rows the page shows. The status filters are ignored: the cards split
    the filtered set by status. draft_count is never date-windowed, because the drafts view the
    card opens is not."""
    sort_date = _list_sort_date()
    unsplit = _dc_replace(filters, status=None, exclude_status=None, all_issued=False, converted_to_type=None)
    base_where = _list_index_where(company_id, unsplit, sort_date)
    status_expr = _func.coalesce(Projection.state["status"].as_string(), "")
    # One grouped pass over the projection: a bounded histogram (one row per status), with the
    # value sum carried per group so total_value is derived without a second scan. total is text
    # in the json state, so read the ->> output as a number - never a jsonb cast.
    total_num = _sql_number(Projection.state["total"].as_string())
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
    all_issued_count = sum(v for k, v in count_by_status.items() if k not in (DRAFT, VOID))
    draft_count = (await session.execute(
        select(_func.count()).select_from(Projection)
        .where(*_list_index_where(company_id, _dc_replace(unsplit, status=DRAFT, date_from=None, date_to=None), sort_date))
    )).scalar_one()

    # Converted outcomes: closed lists whose result is a conversion, split by target type.
    converted_where = base_where + _list_converted()
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


_LIST_EXPORT_COLS = ["id", "ref_id", "list_type", "customer", "date", "total", "status"]


@lists_router.get("/export/csv", dependencies=[require_permission("view_documents"), require_permission("import_export_data")])
async def export_lists_csv(
    filters: ListIndexFilters = Depends(),
    cols: str | None = None,
    company_id: str = Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
) -> StreamingResponse:
    """The list index as CSV: the index's filters and order, every matching row (no page), and the
    columns the screen asked for via ``cols``."""
    out_cols = resolve_export_cols(cols, _LIST_EXPORT_COLS, _LIST_EXPORT_COLS)
    sort_date = _list_sort_date()
    base_where = _list_index_where(company_id, filters, sort_date)
    # Select only the exported columns, from the same expressions the index rows use, so a list's
    # whole line_items array is never deserialized just to write its row.
    columns = _list_columns(sort_date)
    col_exprs = [columns[c].label(c) for c in out_cols]

    async def _rows():
        stmt = (
            select(*col_exprs)
            .where(*base_where)
            .order_by(sort_date.desc(), Projection.entity_id.desc())
            .execution_options(yield_per=500)
        )
        result = await session.stream(stmt)
        try:
            async for r in result:
                yield {c: getattr(r, c) for c in out_cols}
        finally:
            await result.close()

    return StreamingResponse(
        csv_stream(out_cols, _rows()),
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
    return row.state | {"id": row.entity_id, "version": row.version,
                        "line_holds": await _line_holds(session, company_id, entity_id)}


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
            "item_meta": item_meta, "line_holds": await _line_holds(session, company_id, entity_id)}


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
    role: str = Depends(get_current_role),
    settings: dict = Depends(get_current_company_settings),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    idem_key = payload.idempotency_key or str(uuid.uuid4())
    digest = _request_digest("list", None, None, payload.model_dump(mode="json", exclude={"idempotency_key"}))

    async def _replay() -> dict | None:
        if not payload.idempotency_key:
            return None
        replay = await find_event_by_idempotency(session, company_id, idem_key)
        if replay is None:
            return None
        return _replay_result(replay, event_type="list.created", digest=digest)

    if (done := await _replay()) is not None:
        return done
    require_currency_code(payload.currency)
    # The contact first, as every contact-reference writer takes it (its lock takes the
    # company lock, then the contact row).
    contact = await _lock_selected_contact(session, company_id, settings, role, payload.contact_id or "")
    # Lock the company row so concurrent creates cannot read the same numbering counter, then
    # re-check the key under that lock: a retry racing the first request returns the original
    # list instead of consuming a second number. Mirrors create_doc.
    company = await locked_company(session, company_id)
    if (done := await _replay()) is not None:
        return done
    ref_id = payload.ref_id or next_doc_ref(company, list_sequence_key(payload.list_type))
    entity_id = f"list:{ref_id}"

    # Uniqueness check
    existing = await session.get(Projection, {"company_id": company_id, "entity_id": entity_id})
    if existing is not None:
        raise HTTPException(status_code=409, detail=f"List number '{ref_id}' already exists")

    data = payload.model_dump(exclude_none=True)
    data["ref_id"] = ref_id
    chosen = _chosen_terms(data)
    data.setdefault("currency", company.settings.get("currency", "USD"))
    if contact is not None:
        data.update(await _contact_selection_values(
            session, company_id, settings, role, data,
            kind="list", contact_id=payload.contact_id, contact=contact, client_values=data, chosen=chosen,
        ))
    await _validate_list_line_quantities(
        data.get("line_items") or [], session, company_id,
        require_positive=(payload.list_type != "audit"),
    )
    if is_money_list(payload.list_type):
        await _assert_sales_line_price_permission(
            session, company_id, settings, role, data.get("line_items") or [], None,
        )
    entry = await _emit_list(session, company_id, entity_id, "list.created", data, user, idem_key,
                             meta={"request": digest})
    await session.commit()
    return {"event_id": entry.id, "id": entity_id}


@lists_router.patch("/{entity_id}")
async def patch_list(
    entity_id: str,
    payload: ListPatch,
    company_id: str = Depends(get_current_company_id),
    _: None = require_permission("edit_documents"),
    role: str = Depends(get_current_role),
    settings: dict = Depends(get_current_company_settings),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    result = await write_list_patch(session, company_id, role, settings, user, entity_id, payload)
    await session.commit()
    return result


async def write_list_patch(session: AsyncSession, company_id, role: str, settings: dict, user, entity_id: str, payload: ListPatch) -> dict:
    """Apply a List edit without committing, so an import can make it part of a larger unit."""
    fields_changed = dict(payload.fields_changed)
    require_currency_code((fields_changed.get("currency") or {}).get("new"))
    _refuse_protected_fields(fields_changed)
    selecting = "contact_id" in fields_changed
    new_contact_id = str((fields_changed.get("contact_id") or {}).get("new") or "")
    if selecting and "line_items" in fields_changed:
        raise HTTPException(status_code=422, detail="Change the customer and the line items in separate saves.")
    digest, idem_key = _patch_identity("list", entity_id, payload)
    if (done := await _find_patch_replay(session, company_id, idem_key, "list.updated", entity_id, digest)) is not None:
        return done
    # The selected contact is locked before the List, the order merge and delete use.
    contact = await _lock_selected_contact(session, company_id, settings, role, new_contact_id) if selecting else None
    # Locked load so the version check and the emit are one atomic compare-and-set: two concurrent
    # patches cannot both read version N, both pass the check, and both write (the second clobbering
    # the first). The second waits, re-reads the advanced version, and its stale expected_version fails.
    row = await _get_list_for_update(session, company_id, entity_id)
    if (done := await _find_patch_replay(session, company_id, idem_key, "list.updated", entity_id, digest)) is not None:
        return done
    if row.state.get("status") != "draft":
        raise HTTPException(status_code=409, detail="Cannot edit non-draft list")
    # Replacing line_items is a read-modify-write over the whole array: two concurrent editors (or a
    # scan and a line edit) would each save their own full array and the second would silently drop the
    # first's lines. Require expected_version for it so the stale writer is rejected; scalar-only patches
    # touch independent fields and stay backward compatible without a version.
    _new_lines = (fields_changed.get("line_items") or {}).get("new")
    if isinstance(_new_lines, list) and payload.expected_version is None:
        raise HTTPException(status_code=409, detail="Reload the list to get its latest version before saving line changes")
    if payload.expected_version is not None and row.version != payload.expected_version:
        raise HTTPException(status_code=409, detail="This list was changed by someone else; reload to get the latest before saving")
    _new_values = {f: (c or {}).get("new") for f, c in fields_changed.items()}
    if selecting:
        # Lines a selection reprices are computed from the locked List, so they cannot
        # clobber a concurrent edit and need no client version; their price gate runs below.
        selection = await _contact_selection_values(
            session, company_id, settings, role, row.state,
            kind="list", contact_id=new_contact_id, contact=contact, client_values=_new_values,
        )
        fields_changed.update({f: {"old": row.state.get(f), "new": v} for f, v in selection.items()})
        _new_values.update(selection)
        _new_lines = _new_values.get("line_items")
    try:
        _validate_shipment_values(_new_values)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    if "discount" in _new_values and discount_from_inputs(_new_values, 0, row.state.get("currency")) is None:
        raise HTTPException(status_code=422, detail="Discount must be a number")
    if isinstance(_new_lines, list):
        _normalize_line_item_ids(_new_lines)  # keep the item link the editable UI sends as entity_id
        await _validate_list_line_quantities(
            _new_lines, session, company_id,
            require_positive=((row.state.get("list_type") or DEFAULT_LIST_TYPE) != "audit"),
            stored=row.state.get("line_items"),
        )
        if is_money_list(row.state.get("list_type")):
            await _assert_sales_line_price_permission(
                session, company_id, settings, role, _new_lines,
                {i: line for i, line in enumerate(row.state.get("line_items") or [])},
            )
    # expected_version is a concurrency guard, not list data - it never enters the event payload.
    entry = await _emit_list(session, company_id, entity_id, "list.updated",
                             {"fields_changed": fields_changed}, user, idem_key, meta={"request": digest})
    if getattr(entry, "was_deduped", False):
        return _patch_replay(entry, "list.updated", entity_id, digest)
    # entry.id is the list's new version (the projection version tracks the latest entry id), so the
    # client refreshes its cached version from here and its next save pins the value it just wrote.
    return {"event_id": entry.id, "version": entry.id}


class RepriceBody(BaseModel):
    price_list: str
    expected_version: int


def _assert_reprice_access(settings: dict, role: str, price_list: str) -> None:
    """Authorization shared by every whole-entity repricing door."""
    if is_cost_list_name(price_list):
        assert_role_permission(settings, role, "view_inventory_costs")


def _reprice_idempotency_key(kind: str, entity_id: str, payload: RepriceBody) -> str:
    canonical = json.dumps(
        [kind, entity_id, payload.expected_version, payload.price_list],
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return f"{kind}:reprice:{hashlib.sha256(canonical.encode()).hexdigest()}"


def _reprice_replay_result(
    replay, *, entity_id: str, event_type: str, payload: RepriceBody,
) -> dict:
    meta = replay.metadata_ or {}
    if (
        replay.event_type != event_type
        or replay.entity_id != entity_id
        or meta.get("operation") != "reprice"
        or meta.get("expected_version") != payload.expected_version
        or meta.get("price_list") != payload.price_list
    ):
        raise HTTPException(
            status_code=409,
            detail="Idempotency key was already used for another operation",
        )
    return {
        "ok": True,
        "event_id": replay.id,
        "version": replay.id,
        "repriced": int(meta.get("repriced") or 0),
        "skipped": list(meta.get("skipped") or []),
        "price_list": payload.price_list,
    }


async def _reprice_catalog_lines(
    session: AsyncSession,
    company_id,
    stored_lines: list,
    price_list: str,
    *,
    currency: str | None,
) -> tuple[list, int, list[dict], str]:
    """Canonical whole-entity catalog repricing.

    Identity is exact item_id/entity_id only: SKU is display data and may identify
    many physical lots. Free-text lines are untouched. Missing linked items keep
    their stored snapshot and are reported to the caller. Price-list validation,
    flattening, derived-price resolution, rate rounding, and line-total math live
    here so Docs and Lists cannot drift into separate pricing implementations.
    """
    price_config = await get_price_config(session, company_id)
    price_lists, _base_name, company_currency = price_config
    configured_names = {str(pl.get("name") or "") for pl in price_lists}
    if price_list not in configured_names:
        raise HTTPException(status_code=422, detail=f"Unknown price list: {price_list}")

    effective_currency = currency or company_currency
    item_ids = {
        line_item_id(line)
        for line in stored_lines
        if isinstance(line, dict) and line_item_id(line)
    }
    items: dict[str, Projection] = {}
    if item_ids:
        item_rows = (
            await session.execute(
                select(Projection).where(
                    Projection.company_id == company_id,
                    Projection.entity_type == "item",
                    Projection.entity_id.in_(item_ids),
                )
            )
        ).scalars().all()
        items = {item.entity_id: item for item in item_rows}

    from celerp_inventory.routes import flatten_item

    repriced = 0
    skipped: list[dict] = []
    updated_lines: list = []
    for stored_line in stored_lines:
        if not isinstance(stored_line, dict):
            updated_lines.append(stored_line)
            continue
        line = dict(stored_line)
        item_id = line_item_id(line)
        if item_id is None:
            updated_lines.append(line)
            continue
        item = items.get(item_id)
        if item is None:
            skipped.append({"item_id": item_id, "reason": "item_not_found"})
            updated_lines.append(line)
            continue

        flat = flatten_item(item.state or {}, item.entity_id, price_config=price_config)
        new_rate = round_rate(resolve_price(flat, price_list), effective_currency)
        line["unit_price"] = float(new_rate)
        quantity = to_decimal(line.get("quantity", 0) or 0)
        discount_pct = to_decimal(line.get("discount_pct", 0) or 0)
        amount = quantity * new_rate
        if discount_pct:
            amount *= to_decimal(1) - discount_pct / 100
        line["line_total"] = to_stored_float(round_money(amount, effective_currency))
        repriced += 1
        updated_lines.append(line)

    return updated_lines, repriced, skipped, effective_currency


_CONTACT_SNAPSHOT_FIELDS: tuple[str, ...] = tuple(contact_snapshot({}))


def _chosen_terms(values: dict) -> frozenset[str]:
    """The commercial terms a new record's caller set itself, which its contact does not replace."""
    chosen = {f for f in ("currency", "price_list", "payment_terms", "due_date") if values.get(f) not in (None, "")}
    if values.get("terms") not in (None, ""):
        chosen.add("payment_terms")
    return frozenset(chosen)


def _assert_contact_side(cstate: dict, kind: str, state: dict) -> None:
    """Refuse a contact whose type does not fit the Document or List it is named on."""
    vendor_side = kind == "doc" and state.get("doc_type") in VENDOR_DOC_TYPES
    role_name = "vendor" if vendor_side else "customer"
    if not contact_accepts(cstate, role_name):
        raise HTTPException(
            status_code=422,
            detail=f"{cstate.get('name') or 'This contact'} is not a {role_name}. "
                   f"Choose a {role_name}, or change the contact's type to {role_name} or both.",
        )


async def _lock_imported_contact(session: AsyncSession, company_id, kind: str, data: dict) -> None:
    """Lock the local contact an imported Document or List names, and refuse a wrong one."""
    if not data.get("contact_id"):
        return
    contact = await _lock_contact_reference(session, company_id, str(data["contact_id"]))
    if contact is not None:
        _assert_contact_side(contact.state or {}, kind, data)


async def _lock_selected_contact(session: AsyncSession, company_id, settings: dict, role: str, contact_id: str) -> Projection | None:
    """Authorize and lock the contact a patch selects, before the Document or List row is locked.

    Contact rows are always locked before the records that reference them (merge and delete
    take the same order), so a selection cannot commit a reference to a contact that a
    concurrent merge or delete has retired. Returns None when clearing the contact or when
    the id has no local contact record (an imported or external reference).
    """
    if not contact_id:
        return None
    # The selection copies the contact's addresses, email and phone onto the record.
    assert_role_permission(settings, role, "view_contacts")
    return await _lock_contact_reference(session, company_id, contact_id)


async def _contact_selection_values(
    session: AsyncSession,
    company_id,
    settings: dict,
    role: str,
    state: dict,
    *,
    kind: Literal["doc", "list"],
    contact_id: str,
    contact: Projection | None,
    client_values: dict,
    chosen: frozenset[str] = frozenset(),
) -> dict:
    """The complete header transition for selecting contact_id on a Document or List.

    Returns {field: new value} for the contact, its snapshot, and on drafts the commercial
    defaults, prices and totals that follow from it, all computed from the locked record so
    they are written as one event. Client-sent snapshot fields are ignored for a local
    contact. Terms named in *chosen* were set by the caller of a create and are kept.
    Raises before anything is written when the contact is the wrong type, its currency
    is invalid, the price list is unknown, or the caller may not set the prices.
    """
    values: dict = {"contact_id": contact_id, **{f: "" for f in _CONTACT_SNAPSHOT_FIELDS}}
    if not contact_id:
        return values
    if contact is None:
        # No local record to copy from: the caller's snapshot is the only source.
        values.update({f: client_values[f] for f in _CONTACT_SNAPSHOT_FIELDS if f in client_values})
        return values

    cstate = contact.state or {}
    _assert_contact_side(cstate, kind, state)
    values.update(contact_snapshot(cstate))
    if kind == "doc" and "payment_terms" in chosen:
        values["payment_terms"] = state.get("payment_terms")
    elif kind == "doc":
        # A contact without terms takes the configured contact default, else none:
        # the previous contact's terms are never carried over.
        values["payment_terms"] = (
            cstate.get("payment_terms")
            or (settings.get("contact_defaults") or {}).get("default_payment_terms")
            or None
        )
    if state.get("status") != "draft":
        return values

    priced = (state.get("doc_type") in SALES_PRICED_DOC_TYPES) if kind == "doc" else is_money_list(state.get("list_type"))
    if kind == "doc" and values["payment_terms"] and "due_date" not in chosen:
        due = due_date_for_terms(state.get("issue_date"), values["payment_terms"], company_payment_terms(settings))
        if due:
            values["due_date"] = due
    currency = state.get("currency")
    if (kind == "doc" or priced) and "currency" not in chosen:
        contact_currency = cstate.get("currency")
        if contact_currency and contact_currency != currency:
            if contact_currency not in CURRENCY_CODES:
                raise HTTPException(
                    status_code=422,
                    detail=f"The contact's currency {contact_currency!r} is not a valid currency code. Correct it on the contact first.",
                )
            currency = values["currency"] = contact_currency
            if kind == "doc":
                # A rate quoted for the old currency is meaningless for the new one.
                values["conversion_rate"] = None
    lines = None
    if priced and "price_list" in chosen:
        _assert_reprice_access(settings, role, state.get("price_list") or "")
    elif priced:
        price_list = cstate.get("price_list") or settings.get("default_price_list") or DEFAULT_PRICE_LIST_NAME
        _assert_reprice_access(settings, role, price_list)
        stored_lines = list(state.get("line_items") or [])
        lines, _repriced, _skipped, currency = await _reprice_catalog_lines(
            session, company_id, stored_lines, price_list, currency=currency,
        )
        await _assert_sales_line_price_permission(
            session, company_id, settings, role, lines, dict(enumerate(stored_lines)),
        )
        values["price_list"] = price_list
        values["line_items"] = lines
    if kind == "doc" and (lines is not None or "currency" in values):
        merged = {**state, **values}
        values.update(document_money(merged, merged.get("line_items") or [], currency, keep_unrated_tax=True))
    return values


@router.post("/{entity_id}/reprice")
async def reprice_doc(
    entity_id: str,
    payload: RepriceBody,
    company_id: str = Depends(get_current_company_id),
    _: None = require_permission("edit_documents"),
    role: str = Depends(get_current_role),
    settings: dict = Depends(get_current_company_settings),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Atomically reprice every catalog-backed line on a draft document."""
    _assert_reprice_access(settings, role, payload.price_list)
    idem_key = _reprice_idempotency_key("doc", entity_id, payload)
    replay = await find_event_by_idempotency(session, company_id, idem_key)
    if replay is not None:
        return _reprice_replay_result(
            replay, entity_id=entity_id, event_type="doc.updated", payload=payload)

    row = await _get_doc(session, company_id, entity_id, for_update=True)
    replay = await find_event_by_idempotency(session, company_id, idem_key)
    if replay is not None:
        return _reprice_replay_result(
            replay, entity_id=entity_id, event_type="doc.updated", payload=payload)
    if row.state.get("status") != "draft":
        raise HTTPException(status_code=409, detail="Cannot reprice a non-draft document")
    if row.version != payload.expected_version:
        raise HTTPException(
            status_code=409,
            detail="This document was changed by someone else; reload to get the latest before repricing",
        )

    stored_lines = list(row.state.get("line_items") or [])
    updated_lines, repriced, skipped, currency = await _reprice_catalog_lines(
        session, company_id, stored_lines,
        payload.price_list, currency=row.state.get("currency"),
    )
    # Preserve the canonical document price-override authorization that the old
    # patch-based repricer inherited indirectly. Repricing is not a bypass around
    # set_sales_doc_prices; no-op prices remain allowed exactly as patch_doc allows.
    if row.state.get("doc_type") in SALES_PRICED_DOC_TYPES:
        await _assert_sales_line_price_permission(
            session, company_id, settings, role, updated_lines,
            {i: line for i, line in enumerate(stored_lines)},
        )
    new_values = {
        "price_list": payload.price_list,
        "line_items": updated_lines,
        **document_money(row.state, updated_lines, currency, keep_unrated_tax=True),
    }
    fields_changed = {
        field: {"old": row.state.get(field), "new": value}
        for field, value in new_values.items()
        if row.state.get(field) != value
    }
    entry = await emit_event(
        session,
        company_id=company_id,
        entity_id=entity_id,
        entity_type="doc",
        event_type="doc.updated",
        data={"fields_changed": fields_changed},
        actor_id=user.id,
        location_id=None,
        source="api",
        idempotency_key=idem_key,
        metadata_={
            "operation": "reprice",
            "expected_version": payload.expected_version,
            "price_list": payload.price_list,
            "repriced": repriced,
            "skipped": skipped,
        },
    )
    await session.commit()
    return {
        "ok": True,
        "event_id": entry.id,
        "version": entry.id,
        "repriced": repriced,
        "skipped": skipped,
        "price_list": payload.price_list,
    }


@lists_router.post("/{entity_id}/reprice")
async def reprice_list(
    entity_id: str,
    payload: RepriceBody,
    company_id: str = Depends(get_current_company_id),
    _: None = require_permission("edit_documents"),
    role: str = Depends(get_current_role),
    settings: dict = Depends(get_current_company_settings),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Atomically reprice every catalog-backed line on a draft List."""
    _assert_reprice_access(settings, role, payload.price_list)
    idem_key = _reprice_idempotency_key("list", entity_id, payload)
    replay = await find_event_by_idempotency(session, company_id, idem_key)
    if replay is not None:
        return _reprice_replay_result(
            replay, entity_id=entity_id, event_type="list.updated", payload=payload)

    row = await _get_list_for_update(session, company_id, entity_id)
    if not is_money_list(row.state.get("list_type")):
        raise HTTPException(
            status_code=422,
            detail="This list type does not support repricing",
        )
    replay = await find_event_by_idempotency(session, company_id, idem_key)
    if replay is not None:
        return _reprice_replay_result(
            replay, entity_id=entity_id, event_type="list.updated", payload=payload)
    if row.state.get("status") != "draft":
        raise HTTPException(status_code=409, detail="Cannot edit non-draft list")
    if row.version != payload.expected_version:
        raise HTTPException(
            status_code=409,
            detail="This list was changed by someone else; reload to get the latest before repricing",
        )

    stored_lines = list(row.state.get("line_items") or [])
    updated_lines, repriced, skipped, _currency = await _reprice_catalog_lines(
        session, company_id, stored_lines,
        payload.price_list, currency=row.state.get("currency"),
    )
    await _assert_sales_line_price_permission(
        session, company_id, settings, role, updated_lines,
        {i: line for i, line in enumerate(stored_lines)},
    )
    fields_changed = {
        "price_list": {"old": row.state.get("price_list"), "new": payload.price_list},
        "line_items": {"old": row.state.get("line_items") or [], "new": updated_lines},
    }
    entry = await _emit_list(
        session, company_id, entity_id, "list.updated",
        {"fields_changed": fields_changed}, user, idem_key,
        meta={
            "operation": "reprice",
            "expected_version": payload.expected_version,
            "price_list": payload.price_list,
            "repriced": repriced,
            "skipped": skipped,
        },
    )
    await session.commit()
    return {
        "ok": True,
        "event_id": entry.id,
        "version": entry.id,
        "repriced": repriced,
        "skipped": skipped,
        "price_list": payload.price_list,
    }


class ListLinePagePatch(BaseModel):
    line_items: list[dict] = Field(default_factory=list)
    offset: int = 0
    original_count: int | None = None
    expected_version: int | None = None


@lists_router.patch("/{entity_id}/line-page")
async def patch_list_line_page(
    entity_id: str,
    payload: ListLinePagePatch,
    company_id: str = Depends(get_current_company_id),
    _: None = require_permission("edit_documents"),
    role: str = Depends(get_current_role),
    settings: dict = Depends(get_current_company_settings),
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
    elif original_count > _PAGE_LIMIT_MAX:
        # A page fetch returns at most _PAGE_LIMIT_MAX rows, so the client can never have loaded a
        # window wider than that. A claimed original_count above the cap - even one that still fits
        # inside the stored array - is a forged window that would splice away rows beyond the page the
        # client actually read (150 stored, offset 0, original_count 149, a one-row page fits the
        # array yet collapses 148 unseen rows). Reject rather than mutate rows the client never loaded.
        raise HTTPException(
            status_code=400,
            detail=f"original_count cannot exceed the {_PAGE_LIMIT_MAX}-line page limit")
    # The loaded window [offset:offset+original_count] must lie within the stored array: it is the
    # exact slice the client read, so it can neither start past the end nor run beyond it. A larger
    # claimed window would splice away tail rows the client never loaded and cannot have edited
    # (offset 0 + a huge original_count + a one-row page would collapse the whole list to that row).
    # The optimistic version guard above already pins `stored` to exactly what the client loaded, so
    # any window outside it means the client's view is stale: reject and reload rather than mutate.
    if offset > len(stored) or offset + original_count > len(stored):
        raise HTTPException(
            status_code=409,
            detail="This list was changed by someone else; reload to get the latest before saving")
    # No positional id comparison: the page replaces the WHOLE window [offset:offset+original_count],
    # so a delete or insert legitimately shifts the surviving rows out of id-for-id alignment with
    # `stored`. Concurrency is guarded by the version pin (every save bumps the list version), not by
    # matching incoming rows to stored positions, which would falsely reject a mid-window delete.

    await _validate_list_line_quantities(
        page, session, company_id,
        require_positive=((row.state.get("list_type") or DEFAULT_LIST_TYPE) != "audit"),
        stored=stored[offset:offset + original_count],
    )
    if is_money_list(row.state.get("list_type")):
        stored_window = stored[offset:offset + original_count]
        await _assert_sales_line_price_permission(
            session, company_id, settings, role, page,
            {i: line for i, line in enumerate(stored_window)},
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
        await _lock_audit_lines(session, company_id, lines, keep_unlinked_on_hand=False)
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
    """Reserve or release chosen lines of a List of any type (ledger-neutral). Release works
    in any status; newly reserving stock needs a draft or finalized List."""
    # Company first, as the document path does, so a document being created with one of
    # these items either sees the reservation or is seen by it.
    row = await _get_list_for_update(session, company_id, entity_id)
    may_acquire = row.state.get("status") in (DRAFT, FINALIZED)
    return await _line_action(
        session, company_id, user, row, "reserve", body,
        lambda indices: _reserve_lines_impl(row, entity_id, body.new_status, indices, user, session,
                                            may_acquire=may_acquire, commit=False))


@lists_router.post("/{entity_id}/set-available")
async def set_list_lines_available(
    entity_id: str,
    body: FulfillLinesRequest,
    company_id: str = Depends(get_current_company_id),
    _: None = require_permission("fulfill_documents"),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Give back what chosen List lines hold. A List never ships, so a line whose goods are
    sold or out on memo is refused: they are taken back on the document that shipped them."""
    row = await _get_list_for_update(session, company_id, entity_id)

    async def run(indices: list[int]) -> dict:
        line_items = row.state.get("line_items", [])
        locked, by_line = await _line_stock(session, company_id, entity_id, line_items, indices)
        shipped = []
        for i in indices:
            bound = locked.get(str(line_item_id(line_items[i]) or ""))
            if not by_line.get(i) and bound is not None and bound.state.get("status") in ("sold", "memo_out"):
                shipped.append(str(line_items[i].get("sku") or i + 1))
        if shipped:
            names = ", ".join(shipped)
            raise HTTPException(status_code=422, detail=refusal(
                "lines.shipped_elsewhere",
                f"{names} went out on a document. Set it as available on that document.", lines=names))
        return await _reserve_lines_impl(row, entity_id, "available", indices, user, session,
                                         may_acquire=False, commit=False)

    return await _line_action(session, company_id, user, row, "set-available", body, run)


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
    row = await _get_list_for_update(session, company_id, entity_id)
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
    row = await _get_list_for_update(session, company_id, entity_id)
    status = row.state.get("status")
    if status == VOID:
        raise HTTPException(status_code=409, detail="Already voided")
    if status == CLOSED:
        raise HTTPException(status_code=409, detail="Cannot void a closed list; undo its terminal action first")
    await _release_holds(session, company_id=company_id, uid=user.id, owner=row)
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
    row = await _get_list_for_update(session, company_id, entity_id)
    if row.state.get("status") != "draft":
        raise HTTPException(status_code=409, detail="Only draft lists can be deleted")
    from celerp.models.ledger import LedgerEntry
    import sqlalchemy as _sa
    await _release_holds(session, company_id=company_id, uid=user.id, owner=row)
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
    named = await _lock_copied_contacts(session, company_id, [entity_id])
    company = await locked_company(session, company_id)
    row = await _get_list_for_update(session, company_id, entity_id)
    _assert_contacts_unchanged(named, [row])
    state = row.state
    lt = state.get("list_type") or DEFAULT_LIST_TYPE
    if payload.target_type not in ("invoice", "memo"):
        raise HTTPException(status_code=422, detail="target_type must be 'invoice' or 'memo'")
    if terminal_action(lt, f"convert-{payload.target_type}") is None:
        raise HTTPException(status_code=409,
                            detail=f"{behavior(lt).label} lists cannot be converted to a sales document")
    if state.get("status") != FINALIZED:
        raise HTTPException(status_code=409, detail="Finalize the quotation before converting it")

    # Every hold of this list moves with the conversion, still for the same line (the new
    # document keeps the line ids): it is re-stamped to the new document first, so it is
    # its own when it is created; the create then refuses a line reserved elsewhere or a draft.
    to_transfer = await _held_lots(session, company_id, entity_id)
    ref = next_draft_ref(company, payload.target_type)
    new_doc_id = f"doc:{ref}"
    for li_eid, held in to_transfer.items():
        data = {"new_status": "reserved", "source_doc_id": new_doc_id, "doc_number": ref}
        if held.state.get("status_line_entity_id"):
            data["source_line_entity_id"] = held.state["status_line_entity_id"]
        await emit_event(
            session, company_id=company_id, entity_id=li_eid, entity_type="item",
            event_type="item.status.set", data=data,
            actor_id=user.id, location_id=None, source="reservation",
            idempotency_key=str(uuid.uuid4()), metadata_={"doc_id": new_doc_id},
        )
    new_data = {k: v for k, v in state.items()
                if k not in {"status", "result", "entity_type", "list_type", "finalized_at", "sent_at", "accepted_at"}}
    new_data.update({"doc_type": payload.target_type, "ref_id": ref, "source_list_id": entity_id, "status": "draft"})
    await emit_event(
        session, company_id=company_id, entity_id=new_doc_id, entity_type="doc",
        event_type="doc.created", data=new_data, actor_id=user.id, location_id=None,
        source="api", idempotency_key=str(uuid.uuid4()), metadata_={},
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
    named = await _lock_copied_contacts(session, company_id, [entity_id])
    company = await locked_company(session, company_id)
    row = await _get_list_for_update(session, company_id, entity_id)
    _assert_contacts_unchanged(named, [row])
    state = row.state
    ref_id = next_doc_ref(company, list_sequence_key(state.get("list_type")))
    new_entity_id = f"list:{ref_id}"
    new_data = {k: v for k, v in state.items() if k not in {"status", "entity_type", "ref_id", "share_token"}}
    new_data.update({"ref_id": ref_id, "status": "draft", "source_list_id": entity_id,
                     "line_items": strip_line_ids(state.get("line_items"))})
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



_LIST_IMPORT_UPSERT_EXCLUDED = LIFECYCLE_OWNED_FIELDS | {"list_type", "ref_id"}


def _list_import_fields_changed(state: dict, incoming: dict) -> dict[str, dict]:
    return {
        key: {"old": state.get(key), "new": value}
        for key, value in incoming.items()
        if key not in _LIST_IMPORT_UPSERT_EXCLUDED and state.get(key) != value
    }


def _assert_list_import_permissions(settings: dict, role: str, data: dict) -> None:
    if str(data.get("status") or "draft") != "draft":
        assert_role_permission(settings, role, "finalize_documents")


@lists_router.post("/import")
async def import_list(
    body: ListImportRecord,
    company_id: str = Depends(get_current_company_id),
    _: None = require_permission("edit_documents"),
    __: None = require_permission("import_export_data"),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    if body.event_type != "list.created":
        raise HTTPException(status_code=422, detail=f"Event type {body.event_type!r} is not import-safe")
    role, settings = await locked_authority(session, company_id, user.id, ("edit_documents", "import_export_data"))
    _assert_list_import_permissions(settings, role, body.data)

    replay = await find_event_by_idempotency(session, company_id, body.idempotency_key)
    if replay is not None:
        if replay.event_type != "list.created" or replay.entity_id != body.entity_id:
            raise HTTPException(status_code=409, detail="Idempotency key was already used for another operation")
        return {"event_id": replay.id, "id": replay.entity_id, "idempotency_hit": True}

    existing = await session.get(Projection, {"company_id": company_id, "entity_id": body.entity_id})
    if existing is not None:
        raise HTTPException(status_code=409, detail=f"List {body.entity_id} already exists")
    await _lock_imported_contact(session, company_id, "list", body.data)
    await _assert_import_number_free(session, company_id, "list", body.data)

    entry = await emit_event(
        session, company_id=company_id, entity_id=body.entity_id, entity_type="list",
        event_type="list.created", data=body.data, actor_id=user.id, location_id=None,
        source=body.source, idempotency_key=body.idempotency_key,
        metadata_={"source_ts": body.source_ts} if body.source_ts else {},
    )
    await session.commit()
    return {"event_id": entry.id, "id": entry.entity_id, "idempotency_hit": False}


@lists_router.get("/import/template", include_in_schema=False)
async def import_lists_template():
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse(
        "entity_id,event_type,idempotency_key,ref_id,list_type,contact_id,contact_name,total,currency,status\n",
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=lists.csv"},
    )


@lists_router.post("/import/batch", response_model=BatchImportResult)
async def batch_import_lists(
    body: ListBatchImportRequest,
    company_id: str = Depends(get_current_company_id),
    _: None = require_permission("edit_documents"),
    __: None = require_permission("import_export_data"),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> BatchImportResult:
    role, settings = await locked_authority(session, company_id, user.id, ("edit_documents", "import_export_data"))
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
    # Authorization is checked for the whole batch before the first write.
    for rec in body.records:
        if rec.event_type == "list.created":
            _assert_list_import_permissions(settings, role, rec.data)

    for rec in body.records:
        if rec.event_type != "list.created":
            if len(errors) < 10:
                errors.append(f"{rec.entity_id}: event type {rec.event_type!r} is not import-safe")
            skipped += 1
            continue
        if rec.idempotency_key in existing_keys:
            replay = await find_event_by_idempotency(session, company_id, rec.idempotency_key)
            if replay is None or replay.event_type != "list.created" or replay.entity_id != rec.entity_id:
                if len(errors) < 10:
                    errors.append(f"{rec.entity_id}: idempotency key belongs to another operation")
                skipped += 1
                continue
            if not body.upsert:
                skipped += 1
                continue
            try:
                row = await _get_list(session, company_id, replay.entity_id)
                fields_changed = _list_import_fields_changed(row.state, rec.data)
                if not fields_changed:
                    skipped += 1
                    continue
                canonical = json.dumps(fields_changed, sort_keys=True, separators=(",", ":"), default=str)
                upsert_idem = f"{rec.idempotency_key}:upsert:{hashlib.sha256(canonical.encode()).hexdigest()}"
                result = await write_list_patch(
                    session, company_id, role, settings, user, replay.entity_id,
                    ListPatch(
                        fields_changed=fields_changed,
                        idempotency_key=upsert_idem,
                        expected_version=row.version if "line_items" in fields_changed else None,
                    ),
                )
                if result.get("event_id") is None:
                    skipped += 1
                else:
                    updated += 1
            except Exception as exc:
                if len(errors) < 10:
                    errors.append(f"{replay.entity_id}: {failure_reason(exc)}")
            continue
        if rec.entity_id in existing_entities:
            skipped += 1
            continue
        try:
            await _lock_imported_contact(session, company_id, "list", rec.data)
            await _assert_import_number_free(session, company_id, "list", rec.data)
            entry = await emit_event(
                session, company_id=company_id, entity_id=rec.entity_id, entity_type="list",
                event_type="list.created", data=rec.data, actor_id=user.id, location_id=None,
                source=rec.source, idempotency_key=rec.idempotency_key,
                metadata_={"source_ts": rec.source_ts} if rec.source_ts else {},
            )
            existing_keys.add(rec.idempotency_key)
            existing_entities.add(entry.entity_id)
            if getattr(entry, "was_deduped", False):
                skipped += 1
            else:
                created += 1
        except Exception as exc:
            if len(errors) < 10:
                errors.append(f"{rec.entity_id}: {failure_reason(exc)}")

    await session.commit()
    return BatchImportResult(created=created, skipped=skipped, updated=updated, errors=errors)


# ---------------------------------------------------------------------------
# Fulfillment endpoints
# ---------------------------------------------------------------------------
def _selected_line_indices(line_items: list[dict], body: FulfillLinesRequest) -> list[int]:
    """The lines a request names, as positions in ``line_items``, in document order.

    ``line_ids`` must each name a line on the record. A legacy ``line_entity_ids`` entry
    names the line that binds that item, and only when exactly one line does: an item on
    several lines does not say which of them is meant."""
    if body.line_ids and body.line_entity_ids:
        raise HTTPException(status_code=422, detail=refusal(
            "lines.choose_one_selector", "Name the lines by line id or by item, not both."))
    if not body.line_ids and not body.line_entity_ids:
        raise HTTPException(status_code=422, detail=refusal("lines.none_selected", "Choose at least one line."))
    if body.line_ids:
        index_of = {li.get("line_id"): i for i, li in enumerate(line_items) if li.get("line_id")}
        if any(lid not in index_of for lid in body.line_ids):
            raise HTTPException(status_code=422, detail=refusal(
                "lines.not_on_document", "A line you chose is no longer on this record. Reload it and choose again."))
        return sorted({index_of[lid] for lid in body.line_ids})
    binders: dict[str, list[int]] = {}
    for i, li in enumerate(line_items):
        if line_item_id(li):
            binders.setdefault(str(line_item_id(li)), []).append(i)
    chosen: set[int] = set()
    for eid in body.line_entity_ids:
        lines = binders.get(eid, [])
        if not lines:
            raise HTTPException(status_code=422, detail=refusal(
                "lines.not_on_document", "A line you chose is no longer on this record. Reload it and choose again."))
        if len(lines) > 1:
            sku = str(line_items[lines[0]].get("sku") or "")
            raise HTTPException(status_code=409, detail=refusal(
                "lines.item_on_several_lines",
                f"{sku} is on more than one line, so choose the line itself.", sku=sku))
        chosen.add(lines[0])
    return sorted(chosen)


async def _line_stock(
    session, company_id, owner_id: str, line_items: list[dict], indices: list[int],
) -> tuple[dict[str, Projection], dict[int, list[str]]]:
    """Lock the stock the chosen lines can touch and say which line holds which lot.

    Locks every lot a chosen line binds, every lot the owner holds or shipped, and all
    other lots of those products. Returns the locked lots and the owner's holds by line.
    A hold several lines bind with nothing recording which one holds it is refused when
    one of them is chosen: no line may treat it as its own."""
    from celerp.services.pick import attribute_holds
    owned = await _memo_allocation_items(session, company_id, owner_id)
    seeds = {str(line_item_id(line_items[i])) for i in indices if line_item_id(line_items[i])}
    locked = await _lock_item_sku_lots(session, company_id, seeds | {p.entity_id for p in owned})
    held = {eid: p.state for eid, p in locked.items()
            if p.state.get("status") == "reserved" and p.state.get("status_doc_id") == owner_id}
    by_line, _orphans, ambiguous = attribute_holds(line_items, held)
    for eid, binders in ambiguous.items():
        if set(binders) & set(indices):
            sku = str(locked[eid].state.get("sku") or "")
            raise HTTPException(status_code=409, detail=refusal(
                "lines.hold_ambiguous",
                f"{sku} is held for more than one line and nothing records which. "
                "Release those lines and reserve them again.", sku=sku))
    return locked, by_line


def _stock_line(line_items: list[dict], index: int, locked: dict[str, Projection]) -> Projection | None:
    """The lot line ``index`` binds, or None for a non-stock line (a service or charge).
    A line that binds no item has nothing to reserve or ship and is refused."""
    proj = locked.get(str(line_item_id(line_items[index]) or ""))
    if proj is None:
        raise HTTPException(status_code=422, detail=refusal(
            "lines.no_item", f"Line {index + 1} names no inventory item.", line=index + 1))
    if is_non_stock_line(proj.state.get("inventory_type"), proj.state.get("sell_by")):
        return None
    return proj


def _line_plan(
    line_items: list[dict], index: int, locked: dict[str, Projection], by_line: dict[int, list[str]],
    owner_id: str, company_settings: dict, remaining: dict[str, float],
) -> tuple[list[tuple[dict, float, bool]], float, list[dict]]:
    """Plan line ``index`` against the locked lots: ``(draws, shortfall, own holds)``.

    The line takes its own holds first, then its free bound lot, then (when the product may
    be split across lots) free lots of the same product in pick order. ``remaining`` is
    shared by every line of one operation, so two lines never take the same stock."""
    from celerp.services.pick import as_lot, line_draw_sources, plan_line_draws, resolve_pick_method
    from celerp_inventory.projections import demand_claim
    bound = locked[str(line_item_id(line_items[index]))]
    lots = {eid: as_lot(eid, p.created_at, p.state, demand_claim(p.state, owner_id)) for eid, p in locked.items()}
    method = resolve_pick_method(bound.state, company_settings)
    own, primary, free = line_draw_sources(line_items, index, lots, by_line, method)
    needed = float(line_items[index].get("quantity") or 0)
    draws, short = plan_line_draws(needed, own=own, primary=primary, free=free, method=method,
                                   remaining=remaining, span=splitting_allowed(bound.state))
    return draws, short, own


def _unavailable(bound: Projection, owner_id: str) -> dict | None:
    """Why line stock bound to ``bound`` cannot be taken by ``owner_id``, or None when it can."""
    from celerp_inventory.projections import demand_claim
    st = bound.state
    if demand_claim(st, owner_id) is not None:
        return None
    sku, status = st.get("sku", ""), st.get("status", "")
    by = st.get("status_doc_number")
    if by and st.get("status_doc_id") != owner_id:
        return refusal("lines.lot_held_by", f"{sku} is held by {by} (status: {status}).",
                       sku=sku, doc=by, status=status)
    return refusal("lines.lot_status", f"{sku} has status {status}.", sku=sku, status=status)


def _stands_in(bound: Projection, owner_id: str, draws: list) -> bool:
    """Whether a line's plan takes other stock in place of its bound lot, which went out
    on another record: that line's stock is gone, and no other lot may quietly replace it."""
    st = bound.state
    return (st.get("status") in ("sold", "memo_out") and st.get("status_doc_id") not in (None, "", owner_id)
            and any(lot.get("claim") != "reserved" for lot, _take, _full in draws))


def _refuse_unavailable(reasons: list[dict]) -> None:
    if reasons:
        text = " ".join(r["message"] for r in reasons)
        raise HTTPException(status_code=422, detail=refusal(
            "lines.unavailable", f"Not available: {text}", reasons=reasons))


def _carve_measures(line: dict, lot_state: dict, qty: float, unit_map: dict, *, whole_line: bool) -> dict:
    """The measures of a ``qty`` part carved off a lot. The part that is a line's whole
    quantity off its own bound lot takes the line's stated weight and pieces; any other
    part only the measure the lot is sold by (split_off_child refuses what that leaves out)."""
    if whole_line:
        return _plan_line_carve({**line, "quantity": qty}, lot_state, unit_map)
    sell_by = lot_state.get("sell_by")
    return {"child_qty": qty,
            "child_weight": qty if is_weight_unit(sell_by, unit_map) else None,
            "child_pieces": qty if is_pieces_unit(sell_by, unit_map) else None}


async def _apply_split_plan(
    session, *, company_id, uid, owner: Projection, splits: list[dict], source: str,
) -> list[str]:
    """Carve each planned part off its lot, in plan order, and point each line that named
    the lot at its part. Each split is ``{"lot": Projection, "line": index or None, and the
    child measures}``; returns the part ids in the same order.

    Emits no status or transition event: each caller applies its own afterwards (fulfil
    ships the parts, reserve holds or frees them). The row lock that keeps two carves of
    one lot from losing an update lives in split_off_child, the one carve primitive. The
    line update is the owner's own event (``doc.updated`` or ``list.updated``)."""
    from celerp_inventory.routes import split_off_child

    state = owner.state
    new_lines = [dict(li) for li in state.get("line_items", [])]
    retargeted = False
    children: list[str] = []
    for split in splits:
        lot = split["lot"]
        try:
            child_eid, child_sku = await split_off_child(
                session, company_id=uuid.UUID(str(company_id)), user_id=uid, parent_proj=lot,
                child_qty=split["child_qty"], child_weight=split.get("child_weight"),
                child_pieces=split.get("child_pieces"),
            )
        except ValueError as exc:
            sku = str(lot.state.get("sku") or "")
            raise HTTPException(status_code=409, detail=refusal(
                "lines.cannot_split", f"Cannot split {sku}: {exc}", sku=sku, reason=str(exc)))
        children.append(child_eid)
        if split.get("line") is not None:
            li = new_lines[split["line"]]
            li["entity_id"] = child_eid
            li["item_id"] = child_eid
            li["sku"] = child_sku
            retargeted = True
    if retargeted:
        await emit_event(
            session, company_id=uuid.UUID(str(company_id)), entity_id=owner.entity_id,
            entity_type=owner.entity_type, event_type=f"{owner.entity_type}.updated",
            data={"fields_changed": {"line_items": {"old": state.get("line_items"), "new": new_lines}}},
            actor_id=uid, location_id=None, source=source,
            idempotency_key=str(uuid.uuid4()), metadata_={},
        )
        state["line_items"] = new_lines
    return children


async def _give_lines_ids(session, *, company_id, uid, owner: Projection, source: str) -> None:
    """Give each line of ``owner`` that has no line id (a record from an older version) its
    id, through the owner's own update event, so a hold or a shipment names its line the
    way it does on a newer record. A record whose lines all have ids is left alone."""
    state = owner.state
    lines = state.get("line_items") or []
    if all(li.get("line_id") for li in lines if isinstance(li, dict)):
        return
    new_lines = [dict(li) if isinstance(li, dict) else li for li in lines]
    # emit_event gives every id-less line of the new set its id, in place.
    await emit_event(
        session, company_id=uuid.UUID(str(company_id)), entity_id=owner.entity_id,
        entity_type=owner.entity_type, event_type=f"{owner.entity_type}.updated",
        data={"fields_changed": {"line_items": {"old": lines, "new": new_lines}}},
        actor_id=uid, location_id=None, source=source,
        idempotency_key=str(uuid.uuid4()), metadata_={},
    )
    state["line_items"] = new_lines


async def _set_lot_status(session, *, company_id, uid, owner: Projection, lot_id: str,
                          line_id: str | None) -> None:
    """Hold ``lot_id`` for the owner's line ``line_id``, or with no line free it."""
    data: dict = {"new_status": "reserved" if line_id else "available"}
    if line_id:
        data.update({"source_doc_id": owner.entity_id,
                     "doc_number": owner.state.get("doc_number") or owner.state.get("ref_id") or "",
                     "source_line_entity_id": line_id})
    await emit_event(
        session, company_id=uuid.UUID(str(company_id)), entity_id=lot_id, entity_type="item",
        event_type="item.status.set", data=data, actor_id=uid, location_id=None,
        source="reservation", idempotency_key=str(uuid.uuid4()),
        metadata_={"doc_id": owner.entity_id},
    )


async def _held_lots(session, company_id, owner_id: str) -> dict[str, Projection]:
    """Every lot ``owner_id`` holds reserved, locked, whichever line (if any) it is held for.
    Goods it shipped or has out on memo are not holds and are left alone."""
    ids = [p.entity_id for p in await _memo_allocation_items(session, company_id, owner_id)]
    locked = await lock_projections(session, company_id, ids)
    return {eid: p for eid, p in sorted(locked.items())
            if p.state.get("status") == "reserved" and p.state.get("status_doc_id") == owner_id}


async def _source_memo(session, company_id, state: dict, *, for_update: bool = False) -> Projection | None:
    """The memo an invoice was converted from, or None when it was not converted from one."""
    memo_id = state.get("source_memo_id")
    if not memo_id or state.get("doc_type") != "invoice":
        return None
    if for_update:
        return (await lock_projections(session, company_id, [memo_id])).get(memo_id)
    return await session.get(Projection, {"company_id": company_id, "entity_id": memo_id})


async def _return_to_source_memo(session, *, company_id, uid, owner: Projection) -> None:
    """Give the goods of an invoice converted from a memo back to that memo, for void and
    delete. Every lot the invoice holds out with the customer or sold goes back out on the
    memo, and the memo is live again in the status it was converted from, so it can be
    converted again. A memo converted to some other invoice since keeps that conversion.
    Does not commit."""
    memo = await _source_memo(session, company_id, owner.state, for_update=True)
    if memo is None:
        return
    memo_number = memo.state.get("doc_number") or memo.state.get("ref_id") or ""
    held = [p.entity_id for p in await _memo_allocation_items(session, company_id, owner.entity_id)]
    for eid, lot in sorted((await lock_projections(session, company_id, held)).items()):
        if lot.state.get("status_doc_id") != owner.entity_id or lot.state.get("status") not in ("memo_out", "sold"):
            continue
        await emit_event(
            session, company_id=uuid.UUID(str(company_id)), entity_id=eid, entity_type="item",
            event_type="item.status.set",
            data={"new_status": "memo_out", "source_doc_id": memo.entity_id, "doc_number": memo_number},
            actor_id=uid, location_id=None, source="memo_convert_undone",
            idempotency_key=str(uuid.uuid4()), metadata_={"doc_id": owner.entity_id},
        )
    restored = memo.state.get("pre_convert_status")
    if memo.state.get("converted_to") == owner.entity_id and restored:
        await emit_event(
            session, company_id=company_id, entity_id=memo.entity_id, entity_type="doc",
            event_type="doc.reopened", data={"restored_status": restored},
            actor_id=uid, location_id=None, source="api",
            idempotency_key=str(uuid.uuid4()), metadata_={"doc_id": owner.entity_id},
        )


async def _release_holds(session, *, company_id, uid, owner: Projection) -> list[str]:
    """Give back every lot the owner holds, for the terminal actions that end a record
    (void, delete, write-off, audit adjustment). Does not commit: it is part of the
    terminal's own transaction, so a refused terminal gives nothing back."""
    released = list(await _held_lots(session, company_id, owner.entity_id))
    for eid in released:
        await _set_lot_status(session, company_id=company_id, uid=uid, owner=owner, lot_id=eid, line_id=None)
    return released


async def _lock_item_sku_lots(
    session, company_id, item_ids: list[str] | set[str],
) -> dict[str, Projection]:
    """Lock selected items and every same-SKU lot in one deterministic batch.

    Takes the company code namespace first: a partial line later carves a child lot, which
    needs that lock, and taking it after these item row locks would invert the canonical
    order (see lock_item_code_namespace).
    """
    ids = {eid for eid in item_ids if eid}
    if not ids:
        return {}
    await lock_item_code_namespace(session, company_id)
    seeds = (await session.execute(select(Projection).where(
        Projection.company_id == company_id,
        Projection.entity_type == "item",
        Projection.entity_id.in_(ids),
    ))).scalars().all()
    skus = {str(row.state.get("sku") or "").strip() for row in seeds}
    skus.discard("")
    clauses = [Projection.entity_id.in_(ids)]
    if skus:
        clauses.append(Projection.state["sku"].as_string().in_(sorted(skus)))
    rows = (await session.execute(
        select(Projection).where(
            Projection.company_id == company_id,
            Projection.entity_type == "item",
            _sa.or_(*clauses),
        ).order_by(Projection.entity_id).with_for_update().execution_options(populate_existing=True)
    )).scalars().all()
    locked = {row.entity_id: row for row in rows}
    for eid in ids:
        if eid not in locked:
            raise HTTPException(status_code=409, detail="Inventory changed; retry.")
    return locked


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


async def _fulfill_lines_impl(
    entity_id: str,
    indices: list[int],
    company_id: str,
    user,
    session: AsyncSession,
    *,
    commit: bool = True,
) -> dict:
    """Ship the lines at ``indices``. Valid for memo and invoice docs only.

    Each line ships what it holds and, when that is short of its quantity, free stock as a
    reservation would take it; a line holding more than its quantity is refused until it is
    reserved again. Another line's hold is never shipped. A shortage on any line refuses the
    whole request. Inbound doc types (bill, consignment_in) must use POST /receive instead.
    """
    row = await _get_doc(session, company_id, entity_id, for_update=True)
    state = row.state
    doc_type = state.get("doc_type", "")

    allowed_statuses = FULFILLABLE_STATUSES.get(doc_type)
    if allowed_statuses is None:
        raise HTTPException(status_code=422, detail=f"fulfill-lines is not supported for doc type: {doc_type}")
    if state.get("status") not in allowed_statuses:
        raise HTTPException(status_code=409, detail=refusal(
            "lines.fulfil_status", f"Lines cannot be fulfilled while this record's status is {state.get('status')}.",
            doc_status=state.get("status")))

    await _give_lines_ids(session, company_id=company_id, uid=user.id, owner=row, source="fulfillment")
    line_items = state.get("line_items", [])
    locked, by_line = await _line_stock(session, company_id, entity_id, line_items, indices)
    unit_map = await _get_unit_map(session, company_id)
    company = await session.get(Company, company_id)
    company_settings = (company.settings or {}) if company else {}

    blocked: list[str] = []
    unavailable: list[dict] = []
    service_lines: set[int] = set()
    remaining: dict[str, float] = {}
    # (line index, lot, quantity taken, whole lot) in plan order.
    shipments: list[tuple[int, Projection, float, bool]] = []
    for idx in indices:
        bound = _stock_line(line_items, idx, locked)
        if bound is None:
            service_lines.add(idx)
            continue
        line = line_items[idx]
        sku = bound.state.get("sku", "")
        line_qty = float(line.get("quantity") or 0)
        draws, short, own = _line_plan(line_items, idx, locked, by_line, entity_id, company_settings, remaining)
        held = sum(lot["quantity"] for lot in own)
        if held > line_qty + 1e-9:
            blocked.append(f"{sku}: holds {held:g} for a line of {line_qty:g}; reserve the line again first")
            continue
        if short > 1e-9 or _stands_in(bound, entity_id, draws):
            if reason := _unavailable(bound, entity_id):
                unavailable.append(reason)
            else:
                blocked.append(f"{sku}: insufficient stock - invoiced {line_qty:g}, available {line_qty - short:g}")
            continue
        for lot, take, full in draws:
            proj = locked[lot["entity_id"]]
            if not full and not splitting_allowed(proj.state):
                blocked.append(
                    f"{sku}: invoiced {take:g} of {lot['quantity']:g} but 'Allow Splitting' is off - "
                    f"enable splitting or invoice the full quantity")
                continue
            if full and proj.entity_id == line_item_id(line) and abs(take - line_qty) <= 1e-9:
                # Taking a whole lot as the whole line: the secondary measures the line
                # states (not the sell-by one) must match the lot exactly.
                sell_by = proj.state.get("sell_by")
                checks = []
                if not is_pieces_unit(sell_by, unit_map):
                    checks.append(("pcs", (proj.state.get("attributes") or {}).get("pieces"), line.get("pieces")))
                if not is_weight_unit(sell_by, unit_map):
                    checks.append(("weight", proj.state.get("weight"), line.get("weight")))
                bad = next(((n, p, v) for n, p, v in checks
                            if p is not None and v is not None and abs(float(v) - float(p)) > 1e-9), None)
                if bad:
                    blocked.append(f"{sku}: invoicing the whole quantity must take all {float(bad[1]):g} {bad[0]} "
                                   f"(got {float(bad[2]):g})")
                    continue
            shipments.append((idx, proj, take, full))

    # A shortage, a non-splittable part or a line holding too much refuses the whole fulfil.
    if blocked:
        reasons = "; ".join(blocked)
        raise HTTPException(status_code=409, detail=refusal(
            "lines.cannot_fulfil", f"Cannot fulfill: {reasons}", reasons=reasons))
    _refuse_unavailable(unavailable)

    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    cid = uuid.UUID(str(company_id))
    uid = user.id
    fulfillment_date = business_date_at(now_dt, company_settings.get("timezone"))

    # A part of a lot ships as its own lot carved off first; a line whose bound lot is
    # carved names the carved part from then on.
    splits = [{"lot": proj, "line": idx if proj.entity_id == line_item_id(line_items[idx]) else None,
               **_carve_measures(line_items[idx], proj.state, take, unit_map,
                                 whole_line=proj.entity_id == line_item_id(line_items[idx])
                                 and abs(take - float(line_items[idx].get("quantity") or 0)) <= 1e-9)}
              for idx, proj, take, full in shipments if not full]
    children = iter(await _apply_split_plan(
        session, company_id=company_id, uid=uid, owner=row, splits=splits, source="fulfillment"))

    total_cogs = 0.0
    shipped: list[str] = []
    fulfilled_lines: set[int] = set(service_lines)
    for idx, proj, take, full in shipments:
        lot_id = proj.entity_id if full else next(children)
        lot = await session.get(Projection, {"company_id": company_id, "entity_id": lot_id})
        total_cogs += auto_je.lot_cost_of_sale(lot.state)
        shipped.append(lot_id)
        fulfilled_lines.add(idx)
        await emit_event(
            session,
            company_id=cid,
            entity_id=lot_id,
            entity_type="item",
            event_type="item.fulfilled",
            data={
                "source_doc_id": entity_id,
                "doc_number": state.get("doc_number") or state.get("ref_id") or "",
                "quantity_fulfilled": take,
                "fulfilled_by": str(uid),
                "doc_type": doc_type,
                "ts": fulfillment_date,
            },
            actor_id=uid,
            location_id=None,
            source="fulfillment",
            idempotency_key=str(uuid.uuid4()),
            metadata_={"doc_id": entity_id, "line_index": idx,
                       "source_line_id": line_items[idx].get("line_id")},
        )

    # True up the invoice's recognized COGS to the actual cost of what it shipped.
    if doc_type == "invoice" and shipped:
        _lines = "-".join(str(i) for i in sorted(fulfilled_lines - service_lines))
        try:
            await auto_je.reconcile_doc_cogs(
                session, company_id=cid, user_id=uid, doc_id=entity_id,
                cycle_tag=f"fulfill-{int(state.get('fulfill_cycle') or 0)}:l{_lines}",
                ts=fulfillment_date, trigger="doc.fulfilled",
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    # Optimistically compute doc fulfillment_status. Service lines count as fulfilled (they are
    # rendered, not drawn from stock) so a service-only or mixed doc can reach "fulfilled".
    line_items = state.get("line_items", [])
    all_statuses: list[str] = []
    for _idx, li in enumerate(line_items):
        li_eid = line_item_id(li)
        if not li_eid:
            continue
        if _idx in fulfilled_lines:
            all_statuses.append("memo_out")
        else:
            li_proj = await session.get(Projection, {"company_id": company_id, "entity_id": li_eid})
            all_statuses.append(li_proj.state.get("status", "available") if li_proj else "available")

    done_eids = shipped + [str(line_item_id(line_items[i])) for i in sorted(service_lines)]
    fulfilled_brief = _line_item_brief(line_items, done_eids)
    if all_statuses and all(s in ("memo_out", "sold") for s in all_statuses):
        doc_fulfillment_status = "fulfilled"
        doc_event_type = "doc.fulfilled"
        doc_event_data: dict = {
            "fulfilled_items": fulfilled_brief,
            "fulfilled_by": str(uid),
            "fulfilled_at": now,
            "strategy": "per_line",
            "total_cogs": total_cogs,
            "ts": fulfillment_date,
        }
    else:
        doc_fulfillment_status = "partial"
        doc_event_type = "doc.partially_fulfilled"
        # Lines on the doc not in this fulfill batch are still pending.
        unfulfilled_brief = _line_item_brief(
            line_items, [line_item_id(li) for _idx, li in enumerate(line_items)
                         if line_item_id(li) and _idx not in fulfilled_lines])
        doc_event_data = {
            "fulfilled_items": fulfilled_brief,
            "unfulfilled_items": unfulfilled_brief,
            "fulfilled_by": str(uid),
            "fulfilled_at": now,
            "strategy": "per_line",
            "ts": fulfillment_date,
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

    if commit:
        await session.commit()
    return {"fulfillment_status": doc_fulfillment_status, "fulfilled": shipped}


async def _line_action(session, company_id, user, owner: Projection, action: str,
                       body: FulfillLinesRequest, run, select_lines=None) -> dict:
    """Run one line action on ``owner`` once per key: resolve the chosen lines (with
    ``select_lines``, by default _selected_line_indices), call ``run(indices)`` and record
    the request and its result, even when it changed nothing, so the same key replays that
    result and a different request under it is refused."""
    key, digest = _operation(f"{action}-lines", owner.entity_id, body)
    record_id = f"line_action:{_step_id(key)}"
    if (done := await _earlier_run(session, company_id, key, event_type="line_action.recorded",
                                   entity_id=record_id, digest=digest)) is not None:
        return done
    line_items = owner.state.get("line_items", [])
    indices = (select_lines or _selected_line_indices)(line_items, body)
    result = await run(indices)
    # Read after the action, which gives lines from an older version their ids.
    line_items = owner.state.get("line_items", [])
    chosen = [str(line_items[i].get("line_id")) for i in indices if line_items[i].get("line_id")]
    await emit_event(
        session, company_id=uuid.UUID(str(company_id)), entity_id=record_id,
        entity_type="line_action", event_type="line_action.recorded",
        data={"owner_id": owner.entity_id, "action": action, "line_ids": chosen},
        actor_id=user.id, location_id=None, source="api",
        idempotency_key=key, metadata_={"request": digest, "result": result},
    )
    await session.commit()
    return result


@router.post("/{entity_id}/fulfill-lines")
async def fulfill_lines(
    entity_id: str,
    body: FulfillLinesRequest,
    company_id: str = Depends(get_current_company_id),
    _: None = require_permission("fulfill_documents"),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    row = await _get_doc(session, company_id, entity_id, for_update=True)
    return await _line_action(
        session, company_id, user, row, "fulfill", body,
        lambda indices: _fulfill_lines_impl(entity_id, indices, company_id, user, session, commit=False))


async def _expand_invoice_line_allocations(
    session, company_id, doc_id: str, doc_state: dict,
    item_ids: list[str], fetched: dict[str, Projection],
) -> set[int]:
    requested_indices: set[int] = set()
    for item_eid in list(item_ids):
        idx = await auto_je.doc_line_of_lot(session, company_id, doc_id, doc_state, item_eid, fetched[item_eid].state or {})
        if idx is None:
            raise HTTPException(status_code=409, detail="Cannot safely identify the invoice line for this legacy fulfillment.")
        requested_indices.add(idx)
    requested_skus = {
        str((doc_state.get("line_items") or [])[idx].get("sku") or "").strip()
        for idx in requested_indices if 0 <= idx < len(doc_state.get("line_items") or [])
    }
    for item in await _memo_allocation_items(session, company_id, doc_id):
        idx = await auto_je.doc_line_of_lot(session, company_id, doc_id, doc_state, item.entity_id, item.state or {})
        if idx is None:
            if item.entity_id not in item_ids and str((item.state or {}).get("sku") or "").strip() in requested_skus:
                raise HTTPException(status_code=409, detail="Cannot safely identify every lot in this legacy invoice fulfillment.")
            continue
        if idx in requested_indices and item.entity_id not in fetched:
            fetched[item.entity_id] = item
            item_ids.append(item.entity_id)
    return requested_indices

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
) -> tuple[str, Callable[[], Awaitable[None]]]:
    """Reverse whole-line fulfillment: restore each lot to stock, recompute the doc's
    fulfillment status and log the doc-level revert. Emits events only - the caller owns
    the commit. Returns the doc's fulfillment status and the invoice's COGS true-up, which
    the caller awaits once the lots are where they will stay: a lot set available takes
    its line's cost of sales back, a lot reserved to the doc again keeps it.

    Shared by revert-lines (Set as available on sold/memo lines) and reserve-lines
    (Set as reserved on a line this doc already shipped: reverse, then reserve).
    """
    reversed_line_indices: set[int] = set()
    if doc_type == "invoice" and to_revert:
        reversed_line_indices = await _expand_invoice_line_allocations(
            session, company_id, entity_id, state, to_revert, fetched
        )
    now_dt = datetime.now(timezone.utc)
    company = await session.get(Company, company_id)
    settings = (company.settings or {}) if company else {}
    reversal_date = business_date_at(now_dt, settings.get("timezone"))
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
                "ts": reversal_date,
            },
            actor_id=uid,
            location_id=None,
            source="fulfillment",
            idempotency_key=str(uuid.uuid4()),
            metadata_={"doc_id": entity_id},
        )
    async def reconcile() -> None:
        if not reversed_line_indices:
            return
        _lines = "-".join(str(i) for i in sorted(reversed_line_indices))
        try:
            await auto_je.reconcile_doc_cogs(
                session, company_id=cid, user_id=uid, doc_id=entity_id,
                cycle_tag=f"reverse-{int(state.get('fulfill_cycle') or 0)}:l{_lines}",
                ts=reversal_date, trigger="doc.fulfillment_reversed",
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

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
        "ts": reversal_date,
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

    return doc_fulfillment_status, reconcile


def _revert_selection(line_items: list[dict], body: FulfillLinesRequest) -> list[int]:
    """The lines a revert names. A legacy ``line_entity_ids`` body names the lots to take
    back themselves (a lot drawn from another lot of a line's product is on no line), so it
    selects no line; the lots are proved one by one instead."""
    if body.line_entity_ids and not body.line_ids:
        return []
    return _selected_line_indices(line_items, body)


def _return_order(lots: list[Projection], bound: str | None) -> list[Projection]:
    """The order a part of a line comes back in: lots drawn from elsewhere first, the
    line's own lot last, so the line keeps naming what is still out."""
    return sorted(lots, key=lambda p: (p.entity_id == bound, p.entity_id))


async def _lots_out(session, company_id, entity_id: str, state: dict,
                    lots: Iterable[Projection]) -> tuple[dict[str, Projection], dict[str, int | None]]:
    """The lots this document has out (sold or on memo), and the line whose shipment sent
    each, None where nothing records it."""
    out = {p.entity_id: p for p in lots
           if p.state.get("status_doc_id") == entity_id and p.state.get("status") in ("memo_out", "sold")}
    line_of = {eid: await auto_je.doc_line_of_lot(session, company_id, entity_id, state, eid, p.state)
               for eid, p in out.items()}
    return out, line_of


def _check_revert_status(state: dict) -> None:
    """Refuse taking goods back while the document's status does not allow it."""
    status = state.get("status")
    if status == "closed":
        raise HTTPException(status_code=409, detail=refusal(
            "lines.revert_closed", "Goods cannot be taken back on a closed record; reopen it first."))
    if status not in REVERTIBLE_STATUSES[state.get("doc_type", "")]:
        raise HTTPException(status_code=409, detail=refusal(
            "lines.revert_status", "Goods cannot be taken back while the record is in this status."))


async def _revert_lines_impl(row: Projection, body: RevertLinesRequest, indices: list[int], user, session,
                             *, holding: frozenset[int] | None = None) -> dict:
    """Take back what this document shipped, whole or in part, without committing.

    ``indices`` are the chosen lines: each gives back every lot it shipped. With none, the
    legacy body names the lots. Every lot is proved before anything changes: it is out
    (sold or on memo) for this document and its shipment's line can be told. Any line or
    lot that fails refuses the whole request. ``holding`` is given when the user chose Set as
    available, and the refusal names that action: a line in it gives back a hold elsewhere,
    so having nothing out is not an error for it; the status is checked only when something
    is to come back."""
    set_available = holding is not None
    company_id = row.company_id
    entity_id = row.entity_id
    state = row.state
    doc_type = state.get("doc_type", "")
    if doc_type not in REVERTIBLE_STATUSES:
        raise HTTPException(status_code=422, detail=f"revert-lines is not supported for doc type: {doc_type}")
    if not set_available:
        _check_revert_status(state)

    line_items = state.get("line_items", [])
    quantities = body.quantities or {}
    keys = [str(line_items[i].get("line_id")) for i in indices] if indices else body.line_entity_ids
    unknown = [k for k in quantities if k not in keys]
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=f"quantities names items that are not in line_entity_ids: {', '.join(sorted(unknown))}",
        )

    named = set(body.line_entity_ids)
    present = set((await session.execute(select(Projection.entity_id).where(
        Projection.company_id == company_id, Projection.entity_type == "item",
        Projection.entity_id.in_(named),
    ))).scalars().all()) if named else set()
    owned = {p.entity_id for p in await _memo_allocation_items(session, company_id, entity_id)}
    locked = await _lock_item_sku_lots(session, company_id, owned | present)
    out, line_of = await _lots_out(session, company_id, entity_id, state, locked.values())

    def _sku(eid: str) -> str:
        return str(locked[eid].state.get("sku") or "") if eid in locked else ""

    def _unattributed(eid: str) -> dict:
        return refusal("lines.revert_unattributed",
                       f"{_sku(eid)} was shipped on this record but nothing records for which line",
                       sku=_sku(eid))

    errors: list[dict] = []
    # (key the request names it by, lots it covers) per chosen line or legacy lot.
    groups: list[tuple[str, list[Projection], str | None]] = []
    if indices:
        chosen_skus = {str(line_items[i].get("sku") or "").strip() for i in indices} - {""}
        chosen_items = {str(line_item_id(line_items[i]) or "") for i in indices} - {""}
        for eid, idx in line_of.items():
            if idx is None and (eid in chosen_items or _sku(eid).strip() in chosen_skus):
                errors.append(_unattributed(eid))
        for i in indices:
            lots = [out[eid] for eid, idx in line_of.items() if idx == i]
            name = line_items[i].get('sku') or line_items[i].get('description') or i + 1
            if not lots:
                if set_available and i not in holding:
                    errors.append(refusal("lines.available_nothing_held",
                                          f"{name}: nothing is held or out on this line", name=name))
                elif not set_available:
                    errors.append(refusal("lines.revert_nothing_out", f"{name}: nothing is out on this line",
                                          name=name))
                elif str(line_items[i].get("line_id")) in quantities:
                    errors.append(refusal("lines.revert_hold_whole", f"{name}: a hold is given back whole",
                                          name=name))
                continue
            groups.append((str(line_items[i].get("line_id")), lots, line_item_id(line_items[i])))
    else:
        for eid in body.line_entity_ids:
            proj = locked.get(eid)
            if proj is None:
                errors.append(refusal("lines.revert_item_missing", f"{eid}: item not found", name=eid))
            elif proj.state.get("status") not in ("memo_out", "sold"):
                status = proj.state.get("status") or ""
                errors.append(refusal(
                    "lines.revert_not_out",
                    f"{_sku(eid) or eid}: only goods out on memo or sold can be taken back, "
                    f"this one is {status.replace('_', ' ')}", name=_sku(eid) or eid, status=status))
            elif eid not in out:
                errors.append(refusal("lines.revert_not_shipped_here",
                                      f"{_sku(eid) or eid}: was not shipped by this record", name=_sku(eid) or eid))
            elif line_of[eid] is None:
                errors.append(_unattributed(eid))
            else:
                groups.append((eid, [proj], eid))

    to_revert: list[str] = []
    # lot -> quantity coming back, for lots where only part of it returns.
    partial_plan: dict[str, tuple[float, str]] = {}
    unit_map = await _get_unit_map(session, company_id) if quantities else {}
    for key, lots, bound in groups:
        label = str(lots[0].state.get("sku") or key)
        total = sum(float(p.state.get("quantity") or 0) for p in lots)
        back = quantities.get(key)
        if back is None or abs(back - total) <= 1e-9:
            to_revert.extend(p.entity_id for p in lots)
            continue
        if back <= 0:
            errors.append(refusal("lines.revert_quantity_positive",
                                  f"{label}: returned quantity must be greater than zero", name=label))
            continue
        if back > total + 1e-9:
            errors.append(refusal("lines.revert_more_than_out",
                                  f"{label}: cannot return {back:g} of {total:g} that went out",
                                  name=label, qty=f"{back:g}", total=f"{total:g}"))
            continue
        validate_line_quantity(back, lots[0].state.get("sell_by"), unit_map, label=label, require_positive=False)
        remaining = float(back)
        for proj in _return_order(lots, bound):
            qty = float(proj.state.get("quantity") or 0)
            if remaining <= 1e-9:
                break
            if remaining + 1e-9 >= qty:
                to_revert.append(proj.entity_id)
                remaining -= qty
            elif proj.state.get("status") != "memo_out":
                status = proj.state.get("status") or ""
                errors.append(refusal(
                    "lines.revert_part_not_memo",
                    f"{label}: only goods out on memo can be part-returned, this one is {status.replace('_', ' ')}",
                    name=label, status=status))
                break
            else:
                partial_plan[proj.entity_id] = (remaining, key)
                remaining = 0.0

    if errors:
        reasons = "; ".join(e["message"] for e in errors)
        raise HTTPException(status_code=422, detail=refusal(
            "lines.cannot_set_available", f"Cannot set as available: {reasons}", reasons=errors)
            if set_available else
            refusal("lines.cannot_revert", f"Cannot take back: {reasons}", reasons=errors))
    if set_available:
        if not groups:
            return {"fulfillment_status": state.get("fulfillment_status"), "reverted": [], "partially_returned": []}
        _check_revert_status(state)

    cid = uuid.UUID(str(company_id))
    uid = user.id
    fetched = {eid: locked[eid] for eid in to_revert}

    # Partial returns first: split the returned amount off the lot that is out. The child
    # carries the returned goods and starts available (back in stock); the mother keeps the
    # remainder and stays memo_out, so what the customer still holds is never lost.
    returned_brief: list[dict] = []
    if partial_plan:
        from celerp_inventory.routes import split_off_child
        for parent_eid, (qty_back, key) in partial_plan.items():
            parent_proj = locked[parent_eid]
            _sb = parent_proj.state.get("sell_by") or ""
            _sku_p = parent_proj.state.get("sku", "")
            child_weight = qty_back if is_weight_unit(_sb, unit_map) else (body.weights or {}).get(key)
            child_pieces = qty_back if is_pieces_unit(_sb, unit_map) else (body.pieces or {}).get(key)
            try:
                child_eid, _child_sku = await split_off_child(
                    session, company_id=cid, user_id=uid, parent_proj=parent_proj,
                    child_qty=qty_back, child_weight=child_weight, child_pieces=child_pieces,
                )
            except ValueError as exc:
                raise HTTPException(
                    status_code=409,
                    detail=f"Cannot return part of {_sku_p}: {exc}",
                )
            returned_brief.append({"item_id": child_eid, "sku": _sku_p, "quantity": qty_back})

    doc_fulfillment_status, reconcile = await _reverse_whole_lines(
        session, company_id=company_id, cid=cid, uid=uid, entity_id=entity_id, state=state,
        doc_type=doc_type, to_revert=to_revert, fetched=fetched, returned_brief=returned_brief,
    )
    await reconcile()
    return {
        "fulfillment_status": doc_fulfillment_status,
        "reverted": to_revert,
        # Lots that came back in part: the new in-stock parcel per part-returned line.
        "partially_returned": returned_brief,
    }


@router.post("/{entity_id}/revert-lines")
async def revert_lines(
    entity_id: str,
    body: RevertLinesRequest,
    company_id: str = Depends(get_current_company_id),
    _: None = require_permission("fulfill_documents"),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Take back goods this memo or invoice shipped (Set as available on shipped lines).

    Inbound doc types (bill, consignment_in) must use DELETE /receive instead.
    """
    row = await _get_doc(session, company_id, entity_id, for_update=True)
    return await _line_action(
        session, company_id, user, row, "revert", body,
        lambda indices: _revert_lines_impl(row, body, indices, user, session),
        select_lines=_revert_selection)


async def _reserve_lines_impl(
    row, entity_id, new_status, indices: list[int], user, session, *,
    may_acquire: bool, commit: bool = True,
) -> dict:
    """Reserve or release the lines at ``indices`` of a document or List.

    Release gives back everything each line holds. Reserve makes each line hold exactly
    its quantity: what it holds already stays, a shortfall is taken from its free bound lot
    and then (when the product may span lots) from free lots of the same product, and a
    surplus goes back to stock last-picked first, so the line keeps its bound lot. Taking
    new stock needs ``may_acquire`` (the record's status allows reserving); keeping or
    giving back stock does not. A line this document already shipped is taken back into
    stock and held again in the same request. Every line is planned before anything
    changes, so a shortage or refusal on one line changes nothing on any line.
    """
    company_id = row.company_id
    state = row.state
    line_items = state.get("line_items", [])
    locked, by_line = await _line_stock(session, company_id, entity_id, line_items, indices)
    uid = user.id

    if new_status == "available":
        empty = [i for i in indices if not by_line.get(i)]
        if empty:
            names = ", ".join(str(line_items[i].get("sku") or i + 1) for i in empty)
            raise HTTPException(status_code=422, detail=refusal(
                "lines.nothing_held", f"Nothing is held for {names}.", lines=names))
        released = [eid for i in indices for eid in by_line[i]]
        for eid in released:
            await _set_lot_status(session, company_id=company_id, uid=uid, owner=row, lot_id=eid, line_id=None)
        if commit:
            await session.commit()
        return {"new_status": new_status, "reserved": [], "released": released}

    await _give_lines_ids(session, company_id=company_id, uid=uid, owner=row, source="reservation")
    line_items = state.get("line_items", [])
    unit_map = await _get_unit_map(session, company_id)
    company = await session.get(Company, company_id)
    company_settings = (company.settings or {}) if company else {}

    # Lots this record shipped, by the line that shipped them. A line with a shipment is
    # held again by taking the shipment back, not by drawing new stock.
    shipped: dict[int, list[str]] = {}
    for eid, proj in locked.items():
        st = proj.state
        if st.get("status_doc_id") != entity_id or st.get("status") not in ("sold", "memo_out"):
            continue
        idx = await auto_je.doc_line_of_lot(session, company_id, entity_id, state, eid, st)
        sku = str(st.get("sku") or "")
        if idx is None:
            if any(str(line_items[i].get("sku") or "") == sku for i in indices):
                raise HTTPException(status_code=409, detail=refusal(
                    "lines.shipment_ambiguous",
                    f"{sku} was shipped on this record but nothing records for which line. "
                    "Set it as available first.", sku=sku))
            continue
        if idx not in indices:
            continue
        if st.get("status") == "memo_out":
            raise HTTPException(status_code=422, detail=refusal(
                "lines.out_on_memo",
                f"{sku} is out on memo. Use Set as available to take it back first.", sku=sku))
        shipped.setdefault(idx, []).append(eid)

    blocked: list[str] = []
    unavailable: list[dict] = []
    needs_status: list[str] = []
    remaining: dict[str, float] = {}
    # Planned changes, in plan order: (kind, line index, lot, quantity).
    #   carve_held - carve ``qty`` off a lot already held as its own lot and hold that
    #   carve_excess - carve ``qty`` off a held lot and give the part back
    #   carve_take - carve ``qty`` off a free lot and hold the part
    #   take - hold a whole free lot
    #   give - give a whole held lot back
    changes: list[tuple[str, int, Projection, float]] = []
    for idx in indices:
        bound = _stock_line(line_items, idx, locked)
        sku = str((bound.state if bound else {}).get("sku") or line_items[idx].get("sku") or f"Line {idx + 1}")
        if bound is None:
            raise HTTPException(status_code=422, detail=refusal(
                "lines.not_stock", f"{sku} is not a stock item, so nothing can be reserved.", sku=sku))
        if idx in shipped:
            if not may_acquire:
                needs_status.append(sku)
            continue
        line_qty = float(line_items[idx].get("quantity") or 0)
        draws, short, own = _line_plan(line_items, idx, locked, by_line, entity_id, company_settings, remaining)
        if short > 1e-9 or _stands_in(bound, entity_id, draws):
            if reason := _unavailable(bound, entity_id):
                unavailable.append(reason)
            else:
                blocked.append(f"{sku}: needs {line_qty:g}, {line_qty - short:g} available")
            continue
        drawn = {lot["entity_id"] for lot, _take, _full in draws}
        for lot, take, full in draws:
            proj = locked[lot["entity_id"]]
            is_own = lot.get("claim") == "reserved"
            whole = float(proj.state.get("quantity") or 0)
            if not is_own and not may_acquire:
                needs_status.append(sku)
                break
            if full:
                if not is_own:
                    changes.append(("take", idx, proj, take))
                continue
            if not splitting_allowed(proj.state):
                blocked.append(f"{sku}: needs {take:g} of {whole:g} but 'Allow Splitting' is off; "
                               "enable splitting or reserve the full quantity")
                continue
            if not is_own:
                changes.append(("carve_take", idx, proj, take))
            elif proj.entity_id == line_item_id(line_items[idx]) and abs(take - line_qty) <= 1e-9:
                # The bound lot shrinks to exactly the line: carve the line's part with the
                # line's own measures so weight and pieces follow the line.
                changes.append(("carve_held", idx, proj, take))
            else:
                changes.append(("carve_excess", idx, proj, whole - take))
        changes.extend(("give", idx, locked[lot["entity_id"]], 0.0)
                       for lot in own if lot["entity_id"] not in drawn)

    if blocked:
        reasons = "; ".join(blocked)
        raise HTTPException(status_code=409, detail=refusal(
            "lines.cannot_reserve", f"Cannot reserve: {reasons}", reasons=reasons))
    _refuse_unavailable(unavailable)
    if needs_status:
        skus = ", ".join(needs_status)
        raise HTTPException(status_code=409, detail=refusal(
            "lines.reserve_status",
            f"Cannot reserve {skus}: stock can only be newly reserved on a record in a status that allows it.",
            skus=skus))

    reconcile = None
    held: list[str] = []
    if shipped:
        # Take the shipped goods back into stock before holding them again; both share this
        # transaction, so a failure keeps neither.
        line_of = {eid: idx for idx, eids in shipped.items() for eid in eids}
        to_revert = [eid for eids in shipped.values() for eid in eids]
        fetched = {eid: locked[eid] for eid in to_revert}
        _status, reconcile = await _reverse_whole_lines(
            session, company_id=company_id, cid=uuid.UUID(str(company_id)), uid=uid, entity_id=entity_id,
            state=state, doc_type=state.get("doc_type", ""), to_revert=to_revert, fetched=fetched,
        )
        for eid in to_revert:
            if eid not in line_of:
                lot = fetched.get(eid) or locked.get(eid)
                idx = await auto_je.doc_line_of_lot(session, company_id, entity_id, state, eid,
                                                    lot.state if lot else {})
                if idx is None:
                    sku = str((lot.state if lot else {}).get("sku") or "")
                    raise HTTPException(status_code=409, detail=refusal(
                        "lines.shipment_ambiguous",
                        f"{sku} was shipped on this record but nothing records for which line. "
                        "Set it as available first.", sku=sku))
                line_of[eid] = idx
            await _set_lot_status(session, company_id=company_id, uid=uid, owner=row, lot_id=eid,
                                  line_id=line_items[line_of[eid]].get("line_id"))
            held.append(eid)

    carves = [(kind, idx, proj, qty) for kind, idx, proj, qty in changes if kind.startswith("carve")]
    splits = [{"lot": proj,
               "line": idx if kind in ("carve_held", "carve_take")
               and proj.entity_id == line_item_id(line_items[idx]) else None,
               **_carve_measures(line_items[idx], proj.state, qty, unit_map,
                                 whole_line=kind != "carve_excess"
                                 and proj.entity_id == line_item_id(line_items[idx])
                                 and abs(qty - float(line_items[idx].get("quantity") or 0)) <= 1e-9)}
              for kind, idx, proj, qty in carves]
    children = dict(zip(range(len(carves)), await _apply_split_plan(
        session, company_id=company_id, uid=uid, owner=row, splits=splits, source="reservation")))

    released: list[str] = []
    n = 0
    for kind, idx, proj, _qty in changes:
        line_id = line_items[idx].get("line_id")
        if kind == "take":
            await _set_lot_status(session, company_id=company_id, uid=uid, owner=row,
                                  lot_id=proj.entity_id, line_id=line_id)
            held.append(proj.entity_id)
        elif kind == "give":
            await _set_lot_status(session, company_id=company_id, uid=uid, owner=row,
                                  lot_id=proj.entity_id, line_id=None)
            released.append(proj.entity_id)
        else:
            child = children[n]
            n += 1
            if kind == "carve_excess":
                # The carved part is a new lot, created available: it is already given back.
                released.append(child)
                continue
            await _set_lot_status(session, company_id=company_id, uid=uid, owner=row,
                                  lot_id=child, line_id=line_id)
            held.append(child)
            if kind == "carve_held":
                await _set_lot_status(session, company_id=company_id, uid=uid, owner=row,
                                      lot_id=proj.entity_id, line_id=None)
                released.append(proj.entity_id)
    if reconcile is not None:
        await reconcile()

    if commit:
        await session.commit()
    return {"new_status": new_status, "reserved": held, "released": released}


@router.post("/{entity_id}/reserve-lines")
async def reserve_lines(
    entity_id: str,
    body: ReserveLinesRequest,
    company_id: str = Depends(get_current_company_id),
    _: None = require_permission("fulfill_documents"),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Reserve or release chosen lines of an invoice or memo (ledger-neutral). Release works
    in any status; newly reserving stock needs a status that allows reserving."""
    row = await _get_doc(session, company_id, entity_id, for_update=True)
    doc_type = row.state.get("doc_type", "")
    allowed = RESERVABLE_DOC_STATUSES.get(doc_type)
    if allowed is None:
        raise HTTPException(status_code=422, detail=f"reserve-lines is not supported for doc type: {doc_type}")
    may_acquire = row.state.get("status") in allowed
    return await _line_action(
        session, company_id, user, row, "reserve", body,
        lambda indices: _reserve_lines_impl(row, entity_id, body.new_status, indices, user, session,
                                            may_acquire=may_acquire, commit=False))


class SetAvailableRequest(RevertLinesRequest):
    """Set chosen lines as available: a line that holds stock gives its hold back, a line
    this document shipped takes its goods back. ``quantities`` (by line id) returns part of
    a shipped line; a hold is always given back whole."""


async def _set_available_impl(row: Projection, body: SetAvailableRequest, indices: list[int], user, session) -> dict:
    """Give back holds and take back shipments on the chosen lines, without committing.
    Every line is proved before anything changes, so the lines come back together or not
    at all."""
    _locked, by_line = await _line_stock(session, row.company_id, row.entity_id,
                                         row.state.get("line_items", []), indices)
    holding = frozenset(i for i in indices if by_line.get(i))
    taken = await _revert_lines_impl(row, body, indices, user, session, holding=holding)
    released: list[str] = []
    if holding:
        released = (await _reserve_lines_impl(row, row.entity_id, "available", sorted(holding), user, session,
                                              may_acquire=False, commit=False))["released"]
    return {**taken, "released": released}


@router.post("/{entity_id}/set-available")
async def set_lines_available(
    entity_id: str,
    body: SetAvailableRequest,
    company_id: str = Depends(get_current_company_id),
    _: None = require_permission("fulfill_documents"),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Set chosen lines of an invoice or memo as available in one request: holds are given
    back and shipped goods are taken back, all or nothing."""
    row = await _get_doc(session, company_id, entity_id, for_update=True)
    doc_type = row.state.get("doc_type", "")
    if doc_type not in RESERVABLE_DOC_STATUSES:
        raise HTTPException(status_code=422, detail=f"set-available is not supported for doc type: {doc_type}")
    return await _line_action(
        session, company_id, user, row, "set-available", body,
        lambda indices: _set_available_impl(row, body, indices, user, session))


class ReturnReceivedItem(BaseModel):
    sku: str
    quantity: FiniteFloat
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

    row = await _get_doc(session, company_id, entity_id, for_update=True)
    key, digest = _operation("receive-return", entity_id, payload)
    if (done := await _earlier_run(session, company_id, key, event_type="doc.return_received",
                                   entity_id=entity_id, digest=digest)) is not None:
        return done
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
    lot_costs: dict[str, float] = {}
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
            # A returned parcel is a NEW physical lot of the SAME product, so the trade
            # identifier (GTIN) travels with it; the physical tag (rfid_epc) does not and
            # the barcode is freshly minted above.
            "gtin": ref.get("gtin") or li_fallback.get("gtin") or None,
            "description": ref.get("description") or li_fallback.get("description") or "",
            # Returned goods go back onto the account the sold lot was valued in.
            **({LOT_ACCOUNT_FIELD: lot_account(ref)}
               if ref and float(ref.get("cost_price") or 0) * it.quantity > 0 else {}),
            "category": ref.get("category") or li_fallback.get("category") or "",
            "attributes": ref.get("attributes") or li_fallback.get("attributes") or {},
            **extra_prices,
            "status": "available",
            "source_doc_id": entity_id,
            "created_at": now,
        }

        # The returned parcel is stock of the sold lot's product.
        if ref.get("id"):
            from celerp_inventory.services import resolve_catalog_anchor_for_item
            try:
                item_data["catalog_item_id"] = (
                    await resolve_catalog_anchor_for_item(session, company_id, ref["id"])
                ).entity_id
            except ValueError:
                pass  # A historical lot with no resolvable product stays unlinked.

        item_id = f"item:{_step_id(key, 'line', _ridx)}"
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
            idempotency_key=_step_key(key, "line", _ridx),
            metadata_={"source_return_cn": entity_id,
                       **({VALUED_FROM_KEY: ref["id"]} if ref.get("id") else {})},
        )
        cost_price = item_data["cost_price"]
        total_cogs += cost_price * it.quantity
        lot_costs[item_id] = cost_price * it.quantity
        received_items.append({
            "item_id": item_id,
            "sku": it.sku,
            "name": item_data["name"],
            "quantity": it.quantity,
            "cost_price": cost_price,
            "received_at": now,
        })

    result = {"received_items": received_items, "total_cogs_reversed": total_cogs}
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
        idempotency_key=key,
        metadata_={"request": digest, "result": result},
    )

    await auto_je.create_for_return_received(
        session,
        company_id=company_id,
        user_id=user.id,
        cn_id=entity_id,
        lot_costs=lot_costs,
        je_suffix=key,
        received_at=now,
    )

    await session.commit()
    return result


def _parcel_moved_on(item_state: dict | None, item_id: str, came_in: float) -> str | None:
    """Why a parcel a receipt created can no longer be taken back whole, or None when it is
    still as the receipt left it: available, holding what came in, none of it reserved.
    A split, a sale or an adjustment changes what it holds, so undoing the receipt would
    leave the moved part behind outside it."""
    if item_state is None:
        return f"{item_id} (not found - may have already been removed)"
    sku = item_state.get("sku") or item_id
    status = item_state.get("status") or "unknown"
    if status != "available":
        return f"SKU '{sku}' is '{status}' - cannot archive"
    held = float(item_state.get("quantity") or 0)
    if abs(held - came_in) > 1e-9:
        return f"SKU '{sku}' holds {held:g} of the {came_in:g} that came in (split, sold or adjusted since)"
    reserved = float(item_state.get("reserved_quantity") or 0)
    if reserved > 0:
        return f"SKU '{sku}' has {reserved:g} reserved"
    return None


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
    row = await _get_doc(session, company_id, entity_id, for_update=True)
    state = row.state
    if state.get("doc_type") != "credit_note":
        raise HTTPException(status_code=409, detail="undo-receive-return is only valid for credit notes")
    received_items = state.get("return_received_items") or []
    if not received_items:
        raise HTTPException(status_code=409, detail="No received return to undo")

    now = datetime.now(timezone.utc).isoformat()
    item_ids = [r["item_id"] for r in received_items if r.get("item_id")]
    lot_costs = {
        r["item_id"]: float(r.get("cost_total") or 0) or (float(r.get("cost_price") or 0) * float(r.get("quantity") or 0))
        for r in received_items if r.get("item_id")
    }
    total_cogs = sum(lot_costs.values())

    # Pre-flight: every returned item is still as the return left it before archiving.
    # If an item was re-sold, split or already archived, we cannot silently remove it.
    if item_ids:
        item_rows = {eid: r.state for eid, r in (await lock_projections(session, company_id, item_ids)).items()}
        came_in = {r["item_id"]: float(r.get("quantity") or 0) for r in received_items if r.get("item_id")}
        blocked = [why for iid in item_ids
                   if (why := _parcel_moved_on(item_rows.get(iid), iid, came_in[iid])) is not None]
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
            lot_costs=lot_costs,
            unique_suffix=undo_suffix,
            undone_at=now,
        )

    await session.commit()
    return {"undone": True, "item_ids": item_ids}


async def _receipt_already_undone(session: AsyncSession, company_id, entity_id: str) -> bool:
    """True when the document's latest receipt event is an undo."""
    from celerp.models.ledger import LedgerEntry

    latest = (await session.execute(
        select(LedgerEntry.event_type)
        .where(LedgerEntry.company_id == company_id, LedgerEntry.entity_id == entity_id,
               LedgerEntry.event_type.in_(("doc.received", "doc.receive_undone")))
        .order_by(LedgerEntry.id.desc()).limit(1)
    )).scalar_one_or_none()
    return latest == "doc.receive_undone"


@router.delete("/{entity_id}/receive")
async def undo_receive(
    entity_id: str,
    company_id: str = Depends(get_current_company_id),
    _: None = require_permission("fulfill_documents"),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Undo everything received on a bill.

    A receipt exists when the bill has received anything, expense and asset lines
    included. Archives the parcels the receipts created, takes back off each lot already
    on hand what the receipts added to it, and returns the landed cost they capitalised.
    The bill still stands, so what it booked stays booked. Goods already returned to the
    supplier keep the receipt in place: undoing it would orphan the posted return. Undoing
    a receipt already undone reports that instead of failing.
    """
    row = await _get_doc(session, company_id, entity_id, for_update=True)
    state = row.state
    if state.get("doc_type") != "bill":
        raise HTTPException(status_code=409, detail="undo-receive is only valid for bills")
    if not state.get("received_items"):
        if await _receipt_already_undone(session, company_id, entity_id):
            return {"undone": True, "item_ids": [], "already_undone": True}
        raise HTTPException(status_code=409, detail="No received goods to revert")
    if state.get("returned_items"):
        raise HTTPException(status_code=409, detail=refusal(
            "docs.undo_receipt_after_return",
            "Some of these goods were already returned to the supplier, so the receipt cannot be "
            "undone. The return stays on record.",
        ))
    received_item_ids = state.get("received_item_ids") or []
    added = _lot_additions(state)
    stock_lines = sum(1 for x in state.get("received_items") or []
                      if (x.get("receive_as") or "stock") == "stock" and "lot_quantity_added" not in x)
    if stock_lines != len(received_item_ids):
        raise HTTPException(
            status_code=409,
            detail=("Some goods on this document were added to stock already on hand by an earlier "
                    "version, so the receipt cannot be reverted here. Correct those quantities with a "
                    "stock adjustment."),
        )
    from celerp_inventory.services import goods_basis

    now = datetime.now(timezone.utc).isoformat()

    # Pre-flight: every parcel is still as the receipt left it and every lot still holds what came in.
    item_rows = {eid: r.state for eid, r in
                 (await lock_projections(session, company_id, [*received_item_ids, *added])).items()}
    came_in = await _returnable_quantities(session, company_id, state)
    blocked = [why for iid in received_item_ids
               if (why := _parcel_moved_on(item_rows.get(iid), iid, came_in.get(iid, 0.0))) is not None]
    for lot, (qty, _) in added.items():
        lot_state = item_rows.get(lot) or {}
        free = _free_on_hand(lot_state)
        if free + 1e-9 < qty:
            blocked.append(f"SKU '{lot_state.get('sku') or lot}' has {free:g} on hand and free, {qty:g} came in on this document")
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

    for lot, (qty, cost) in added.items():
        if qty <= 0:
            continue
        lot_state = item_rows[lot]
        new_qty = float(lot_state.get("quantity") or 0) - qty
        adjustment: dict = {"new_qty": new_qty}
        if new_qty > 0:
            # The receipt added its goods' cost to the lot, so undoing it takes that cost back.
            adjustment["cost_base"] = round_basis(max(0.0, (goods_basis(lot_state) or 0.0) - cost))
        await emit_event(
            session, company_id=company_id, entity_id=lot, entity_type="item",
            event_type="item.quantity.adjusted", data=adjustment,
            actor_id=user.id, location_id=None, source="receive_undo",
            idempotency_key=str(uuid.uuid4()), metadata_={"source_receive_undo": entity_id},
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

    await auto_je.void_landed_capitalisation(
        session, company_id=company_id, user_id=user.id, doc_id=entity_id, undo_key=undo_suffix,
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
    async with storing(session, company_id) as store:
        try:
            meta = await store.upload(file)
        except ValueError as exc:
            raise HTTPException(status_code=413, detail=str(exc))
        # Read after the upload so a document deleted meanwhile, or an uploader who has
        # lost access, leaves no file behind.
        await _get_doc(session, company_id, entity_id, for_update=True)
        entry = await attach_file(session, company_id, "doc", entity_id, meta, user.id)
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
    from celerp.services.attachments import local_attachment_url_path

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

    dest = local_attachment_url_path(str(company_id), url)
    if dest is None:
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
    row = await _get_doc(session, company_id, entity_id, for_update=True)
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
    row = await _get_doc(session, company_id, entity_id, for_update=True)
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
    row = await _get_doc(session, company_id, entity_id, for_update=True)
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
    counted_qty: FiniteFloat | None = None  # None clears the count (line skipped on adjust)


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
    return f"{code}: multiple items share this SKU; scan a barcode or choose a lot"


def _unknown_code_detail(code: str) -> str:
    return f"{code}: no matching barcode or SKU"


def _sku_note(code: str, item: Projection) -> str:
    """The item's SKU as context after the scanned code, unless the code already was that SKU."""
    sku = (item.state or {}).get("sku")
    return f" (SKU {sku})" if sku and sku != code else ""


def _locked_barcode_moved(code: str, item: Projection | None, lines: list[dict]) -> bool:
    """True when `code` is some line's locked barcode but no longer resolves to that line's item,
    even when it now resolves to another item that is also on the audit."""
    item_id = item.entity_id if item is not None else None
    return any(l.get("barcode") == code and l.get("item_id") != item_id for l in lines)


def _locked_manifest_failure(code: str, item: Projection | None, lines: list[dict]) -> tuple[str, str]:
    """(reason, label) for a code that checks off no line of a locked audit. A manifest keys
    physical identity by item_id; a SKU is only context. A code equal to a line's locked barcode
    that no longer resolves to that line's item means the identifier moved after locking: counting
    it against the old line could adjust the wrong stock, so it is reported, never checked off."""
    if _locked_barcode_moved(code, item, lines):
        return ("audit_identifier_changed",
                f"{code}: this barcode changed after the audit was locked; review the audit before counting")
    if item is None:
        return "unknown_code", _unknown_code_detail(code)
    sku = (item.state or {}).get("sku")
    if sku and any(l.get("sku") == sku for l in lines):
        return "not_on_audit", f"{code}: not on this audit. SKU {sku} is present on a different lot."
    return "not_on_audit", f"{code}: not on this audit{_sku_note(code, item)}"


# What a locked audit line records about its physical item. Refreshed from the item at lock time;
# counts, comments and any other line field are left as the user set them.
_AUDIT_IDENTITY_FIELDS = ("sku", "name", "barcode")


async def _lock_audit_lines(session: AsyncSession, company_id: str, lines: list[dict],
                            *, keep_unlinked_on_hand: bool) -> None:
    """In place: refresh each linked line's identity from its item and freeze its on-hand, so the
    locked manifest agrees with the item every later scan resolves to. A line whose item no longer
    exists, or was merged into another (no scan resolves to it again), blocks the lock: it could
    never be counted and its stock never adjusted. An unlinked line has no item to read: with
    `keep_unlinked_on_hand` it keeps the on-hand it already carries, otherwise it freezes at 0."""
    for l in lines:
        key = l.get("item_id")
        item = await session.get(Projection, {"company_id": company_id, "entity_id": key}) if key else None
        if key:
            label = l.get("sku") or l.get("name") or "A line"
            if item is None or item.entity_type != "item":
                raise HTTPException(status_code=409,
                                    detail=f"{label}: its inventory item no longer exists. Remove the line, then try again.")
            if str((item.state or {}).get("status") or "").lower() in PHYSICAL_CODE_RESOLVE_EXCLUDED_STATUSES:
                raise HTTPException(status_code=409,
                                    detail=f"{label}: its inventory item was merged into another item. "
                                           "Remove the line, then try again.")
        if item is None:
            l["on_hand"] = float(l.get("on_hand") or 0) if keep_unlinked_on_hand else 0.0
            continue
        fresh = _scan_line_from_item(item, "audit", None)
        l.update({f: fresh[f] for f in _AUDIT_IDENTITY_FIELDS})
        l["on_hand"] = float(item.state.get("quantity") or 0)


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
    # Carry the item's unit so the stored line renders with it (a 650 gram scan stays 650 gram, not a
    # bare 650). unit and sell_by are the same value; the line row reads either.
    line["unit"] = st.get("sell_by")
    line["sell_by"] = st.get("sell_by")
    # System qty snapshot for the Qty column; on_hand is frozen separately at finalize.
    line["quantity"] = float(st.get("quantity") or 0)
    if list_type != "audit":
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
    company = await locked_company(session, company_id)
    rows = (await session.execute(select(Projection).where(
        Projection.company_id == company_id, Projection.entity_type == "item"))).scalars().all()
    lines: list[dict] = []
    for r in rows:
        st = r.state
        if str(r.location_id or "") != payload.location_id:
            continue
        if (st.get("status") or "available") != "available":
            continue
        if not is_stock_type(st):  # services / non-stocked have no stock to count
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
    unit_map = None      # unit rules for scan-line validation; fetched once, lazily, only if needed
    results: list[dict] = []
    failed: list[dict] = []
    changed = False

    # Resolve the whole run against ONE inventory load (vs one load per code).
    from celerp_inventory.routes import duplicate_barcode_detail, resolve_items_by_codes
    resolved = await resolve_items_by_codes(session, company_id, codes)

    for code in codes:
        res = resolved.get(code)
        reason = detail = None
        if res is not None and res.duplicate_physical:
            item = None
            # The reason code "duplicate_barcode" is a stable external/audit contract; only
            # the resolver property generalized to cover barcode + RFID EPC.
            reason, detail = "duplicate_barcode", duplicate_barcode_detail(code)
        elif res is not None and res.ambiguous:
            item = None
            reason, detail = "ambiguous_sku", _ambiguous_sku_detail(code)
        else:
            item = res.one if res is not None else None
            if item is None:
                reason, detail = (_locked_manifest_failure(code, None, lines) if status == FINALIZED
                                  else ("unknown_code", _unknown_code_detail(code)))
            elif str((item.state or {}).get("status") or "").lower() == "draft":
                reason, detail = "draft_item", f"{code}: item is a draft - make it available first{_sku_note(code, item)}"
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
                    detail = f"{code}: already on the list{_sku_note(code, item)}"
                    failed.append({"code": code, "reason": "duplicate_scan", "label": detail})
                    results.append({"code": code, "state": "error", "reason": "duplicate_scan", "label": detail})
                    continue
                # Scan runs through the SAME per-line rule as an ordinary writer: a stocked line's
                # snapshot must satisfy its unit rule (a zero-on-hand non-audit line is invalid). A
                # failure is reported per-code and skipped, never persisted. sell_by is already in the
                # item's own state, so no per-item query is needed.
                if unit_map is None:
                    unit_map = await _get_unit_map(session, company_id)
                try:
                    _check_line_quantity(
                        item.state.get("quantity"), item.state.get("sell_by"), unit_map,
                        require_positive=(lt != "audit"),
                        label=item.state.get("sku") or code,
                    )
                except HTTPException as exc:
                    failed.append({"code": code, "reason": "invalid_quantity", "label": exc.detail})
                    results.append({"code": code, "state": "error", "reason": "invalid_quantity", "label": exc.detail})
                    continue
                if price_config is None:
                    price_config = await get_price_config(session, company_id)
                lines.append(_scan_line_from_item(item, lt, payload.price_list, price_config=price_config))
                result_state = "added"
        else:
            # FINALIZED audit: the manifest is LOCKED - scanning only checks off items already on the
            # list and never adds. An item not on the list is reported (add it while still a draft), as
            # is a code that is another line's locked barcode.
            if idx is None or _locked_barcode_moved(code, item, lines):
                reason, detail = _locked_manifest_failure(code, item, lines)
                failed.append({"code": code, "reason": reason, "label": detail})
                results.append({"code": code, "state": "error", "reason": reason, "label": detail})
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
    """Apply a finalized audit against fresh, locked inventory state."""
    at = datetime.now(timezone.utc).isoformat()  # one business day for the whole adjustment
    row = await _get_audit(session, company_id, entity_id, for_update=True)
    if row.state.get("status") != FINALIZED:
        raise HTTPException(status_code=409, detail="Finalize the count before adjusting stock")
    # The adjustment closes the audit, so it gives back what the audit holds first.
    await _release_holds(session, company_id=company_id, uid=user.id, owner=row)
    cycle = int(row.state.get("adjust_count") or 0)
    lines = [dict(l) for l in (row.state.get("line_items") or [])]
    audit_location = str(row.state.get("location_id") or "")
    counted_ids = sorted({
        str(l.get("item_id")) for l in lines
        if l.get("counted_qty") is not None and l.get("item_id")
    })
    locked: dict[str, Projection] = {}
    if counted_ids:
        locked = {
            p.entity_id: p for p in (await session.execute(
                select(Projection).where(
                    Projection.company_id == company_id,
                    Projection.entity_type == "item",
                    Projection.entity_id.in_(counted_ids),
                ).order_by(Projection.entity_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )).scalars().all()
        }

    shrink_val = Decimal(0)
    over_val = Decimal(0)
    # Value lost and gained per inventory account of the lots counted.
    shrink_by: dict[str, float] = {}
    over_by: dict[str, float] = {}
    adjusted = 0
    skipped = 0
    for l in lines:
        cq = l.get("counted_qty")
        if cq is None:
            skipped += 1
            continue
        item_id = str(l.get("item_id") or "")
        item = locked.get(item_id)
        item_location = str(item.location_id or item.state.get("location_id") or "") if item else ""
        if (item is None or (item.state.get("status") or "available") != "available"
                or (audit_location and item_location != audit_location)):
            raise HTTPException(
                status_code=409,
                detail=f"{l.get('sku') or item_id or 'Item'} changed after the audit was finalized; recount it.",
            )
        live = float(item.state.get("quantity") or 0)
        cqf = float(cq)
        if abs(cqf - live) < 1e-9:
            continue
        unit_cost = auto_je.lot_unit_cost(item.state)
        value = abs(to_decimal(live) - to_decimal(cqf)) * to_decimal(unit_cost)
        if value:
            bucket = shrink_by if cqf < live else over_by
            origin = lot_account(item.state)
            bucket[origin] = bucket.get(origin, 0.0) + float(value)
        if cqf < live:
            shrink_val += value
        else:
            over_val += value
        await emit_event(
            session, company_id=company_id, entity_id=item_id, entity_type="item",
            event_type="item.quantity.adjusted",
            data={"new_qty": cqf, "reason": "audit", "source_list_id": entity_id, "prior_qty": live},
            actor_id=user.id, location_id=None, source="audit", idempotency_key=str(uuid.uuid4()),
            metadata_={"audit_id": entity_id},
        )
        l["prior_qty"] = live
        l["adjustment_unit_cost"] = unit_cost
        l["adjusted"] = True
        adjusted += 1
    await auto_je.create_for_audit_adjustment(
        session, company_id=company_id, user_id=user.id, list_id=entity_id,
        shrinkage=shrink_by, overage=over_by, cycle=cycle, recorded=at,
    )
    await _emit_list(session, company_id, entity_id, "list.closed",
                     {"result": "stock_adjusted", "line_items": lines, "adjust_count": cycle + 1}, user)
    currency = await auto_je.company_currency(session, company_id)
    await session.commit()
    return {
        "adjusted": adjusted, "skipped": skipped,
        "shrinkage_value": to_stored_float(round_money(shrink_val, currency)),
        "overage_value": to_stored_float(round_money(over_val, currency)),
    }


@lists_router.post("/{entity_id}/undo-adjust")
async def undo_audit_adjust(
    entity_id: str, company_id=Depends(get_current_company_id),
    _: None = require_permission("adjust_inventory"), user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Undo only while stock still equals the quantity this audit applied."""
    row = await _get_audit(session, company_id, entity_id, for_update=True)
    if row.state.get("status") != CLOSED or row.state.get("result") != "stock_adjusted":
        raise HTTPException(status_code=409, detail="This audit has no adjustment to undo")
    cycle = int(row.state.get("adjust_count") or 1) - 1
    lines = [dict(l) for l in (row.state.get("line_items") or [])]
    audit_location = str(row.state.get("location_id") or "")
    targets = [l for l in lines if l.get("adjusted") and l.get("prior_qty") is not None]
    item_ids = sorted({str(l.get("item_id")) for l in targets if l.get("item_id")})
    locked: dict[str, Projection] = {}
    if item_ids:
        locked = {
            p.entity_id: p for p in (await session.execute(
                select(Projection).where(
                    Projection.company_id == company_id,
                    Projection.entity_type == "item",
                    Projection.entity_id.in_(item_ids),
                ).order_by(Projection.entity_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )).scalars().all()
        }
    for l in targets:
        item_id = str(l.get("item_id") or "")
        item = locked.get(item_id)
        applied = l.get("counted_qty")
        item_location = str(item.location_id or item.state.get("location_id") or "") if item else ""
        if (item is None or applied is None or (item.state.get("status") or "available") != "available"
                or (audit_location and item_location != audit_location)):
            raise HTTPException(
                status_code=409,
                detail=f"{l.get('sku') or item_id or 'Item'} changed after this audit; its adjustment cannot be undone safely.",
            )
        if abs(float(item.state.get("quantity") or 0) - float(applied)) > 1e-9:
            raise HTTPException(
                status_code=409,
                detail=f"{l.get('sku') or item_id}: stock changed after this audit; undo would overwrite later activity.",
            )
        recorded_cost = l.get("adjustment_unit_cost")
        if recorded_cost is None:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"{l.get('sku') or item_id}: this adjustment predates safe cost tracking; "
                    "it cannot be undone automatically."
                ),
            )
        current_cost = auto_je.lot_unit_cost(item.state)
        tolerance = 1e-9 * max(1.0, abs(float(recorded_cost)))
        if abs(current_cost - float(recorded_cost)) > tolerance:
            raise HTTPException(
                status_code=409,
                detail=f"{l.get('sku') or item_id}: item cost changed after this audit; undo would misstate inventory value.",
            )
    for l in targets:
        item_id = str(l["item_id"])
        await emit_event(
            session, company_id=company_id, entity_id=item_id, entity_type="item",
            event_type="item.quantity.adjusted",
            data={"new_qty": float(l["prior_qty"]), "reason": "audit_undo", "source_list_id": entity_id},
            actor_id=user.id, location_id=None, source="audit", idempotency_key=str(uuid.uuid4()),
            metadata_={"audit_id": entity_id},
        )
        l.pop("adjusted", None)
        l.pop("prior_qty", None)
        l.pop("adjustment_unit_cost", None)
    await auto_je.void_for_audit_adjustment(session, company_id=company_id, user_id=user.id,
                                            list_id=entity_id, cycle=cycle)
    await _emit_list(session, company_id, entity_id, "list.reopened", {"line_items": lines}, user)
    await session.commit()
    return {"ok": True}


# --- Write-off (disposal) list: seed from a selection, remove stock per line, post one JE ----------
# Mirrors the audit trio (create / set-line / terminal / undo) but is EVENT-based: the user enters the
# known quantity leaving stock per line, with a destination expense or equity account and a comment,
# rather than counting. The terminal carves or disposes each line's stock and posts one balanced JE.


class WriteoffCreateBody(BaseModel):
    entity_ids: list[str] = Field(default_factory=list)
    idempotency_key: str | None = None


class WriteoffLineBody(BaseModel):
    line_id: str | None = None
    item_id: str | None = None
    qty_out: FiniteFloat | None = None
    account: str | None = None
    comment: str | None = None


async def _get_writeoff(session: AsyncSession, company_id, entity_id: str, *, for_update: bool = False) -> Projection:
    row = await (_get_list_for_update if for_update else _get_list)(session, company_id, entity_id)
    if row.state.get("list_type") != "writeoff":
        raise HTTPException(status_code=404, detail="Write-off not found")
    return row


async def _validate_writeoff_account(session, company_id, code: str) -> None:
    """A write-off destination must be a destination ``require_destinations`` accepts, of a
    WRITEOFF_ACCOUNT_TYPES class. Validated at the function level, never only in the picker:
    a direct API call cannot post to a header, an inactive account, or an asset, revenue or
    cost of sales account."""
    accounts = await require_destinations(session, company_id, {code})
    account_type = (accounts or {}).get(code, {}).get("account_type")
    if accounts is not None and account_type not in WRITEOFF_ACCOUNT_TYPES:
        raise HTTPException(status_code=422, detail=refusal(
            "posting.destination.not_write_off",
            f"Account {code} is of type {account_type}. A write-off goes to an expense or equity account.",
            code=code, type=account_type))


def _writeoff_seed_line(item: Projection, settings: dict) -> dict:
    """One draft write-off line for an item, seeded with its live on-hand, the company's shrinkage
    and write-off account (blank when none is set) and blank entry fields."""
    st = item.state
    return {"line_id": uuid.uuid4().hex, "item_id": item.entity_id, "sku": st.get("sku"),
            "name": st.get("name"), "quantity": float(st.get("quantity") or 0),
            "qty_out": None, "account": role_map(settings).get(AccountRole.STOCK_SHRINKAGE.value), "comment": ""}


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
    company = await locked_company(session, company_id)
    lines: list[dict] = []
    for eid in payload.entity_ids:
        item = await session.get(Projection, {"company_id": company_id, "entity_id": eid})
        if item is None or item.entity_type != "item":
            continue  # non-item ids in the selection are skipped, never seeded as phantom lines
        lines.append(_writeoff_seed_line(item, company.settings or {}))
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
        line = _writeoff_seed_line(item, await current_settings(session, company_id))
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
    at = datetime.now(timezone.utc).isoformat()  # one business day for the whole write-off
    # Row-lock the list for the whole transaction: this terminal moves ledger value, so a second
    # concurrent run must serialize (a double run would double-carve and post twice). The audit terminal
    # only reads status; the ledger effect here is why the write-off locks and the audit does not.
    row = (await lock_projections(session, company_id, [entity_id])).get(entity_id)
    if row is None or row.state.get("list_type") != "writeoff":
        raise HTTPException(status_code=404, detail="Write-off not found")
    status = row.state.get("status")
    if status not in (DRAFT, FINALIZED):
        raise HTTPException(status_code=409, detail="This write-off has already been processed")
    # The write-off closes the list, so it gives back what the list holds first; the stock
    # it removes is then judged as the available stock it has become.
    await _release_holds(session, company_id=company_id, uid=user.id, owner=row)
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
    # unlocked-read hazard the plain get left open. The company lock taken with the list row above also
    # covers the child lot a partial line carves.
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
        unit_cost_by_item.setdefault(item.entity_id, auto_je.lot_unit_cost(item.state))
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
                         {"status": FINALIZED, "finalized_at": at}, user)
    # Each line's value is money in the company currency; the account debits and the Inventory
    # credit are sums of those rounded values, so the entry balances.
    currency = await auto_je.company_currency(session, company_id)
    origins = {item.entity_id: lot_account(item.state or {}) for _l, item, _q in prepared}
    debits: dict[str, Decimal] = {}
    credits: dict[str, Decimal] = {}
    written_off = 0
    remaining: dict[str, float] = dict(live_by_item)
    for l, item, qty_out in prepared:
        account = l.get("account")
        unit_cost = unit_cost_by_item[item.entity_id]
        value = round_money(unit_cost * qty_out, currency)
        origin = origins[item.entity_id]
        credits[origin] = credits.get(origin, Decimal(0)) + value
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
            data={"account": account, "qty": qty_out, "unit_cost": unit_cost, "cost_total": to_stored_float(value),
                  "reason": l.get("comment") or None, "source_list_id": entity_id},
            actor_id=user.id, location_id=None, source="writeoff", idempotency_key=str(uuid.uuid4()),
            metadata_={"writeoff_id": entity_id},
        )
        debits[account] = debits.get(account, Decimal(0)) + value
        l["disposed_entity_id"] = disposed_eid
        l["disposed_qty"] = qty_out
        l["adjusted"] = True
        written_off += 1
        remaining[item.entity_id] = round(rem - qty_out, 10)
    total_value = to_stored_float(sum(debits.values(), Decimal(0)))
    entries = [{"account": acct, "debit": to_stored_float(val), "credit": 0.0} for acct, val in debits.items()]
    if entries:
        entries += await auto_je.stock_relief_lines(
            session, company_id, {code: to_stored_float(v) for code, v in credits.items()})
    await auto_je.create_for_line_adjustment(
        session, company_id=company_id, user_id=user.id, list_id=entity_id,
        kind="writeoff", entries=entries, cycle=cycle, recorded=at,
    )
    await _emit_list(session, company_id, entity_id, "list.closed",
                     {"result": "written_off", "line_items": lines, "adjust_count": cycle + 1}, user)
    await session.commit()
    return {"written_off": written_off, "skipped": skipped, "value": total_value}


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
        await _lock_audit_lines(session, company_id, lines, keep_unlinked_on_hand=True)
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
    row = await _get_list_for_update(session, company_id, entity_id)
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
        contact = row.state.get("contact_name") or "there"
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
    row = await _get_list_for_update(session, company_id, entity_id)
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
