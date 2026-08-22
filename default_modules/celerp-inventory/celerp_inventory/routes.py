# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT

from __future__ import annotations

import csv
import io
import re
import uuid
from datetime import datetime, timezone

from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.db import get_session
from celerp.events.engine import emit_event
from celerp.models.projections import Projection
from celerp.services.auth import get_current_company_id, get_current_user, get_current_role, ROLE_LEVELS
from celerp.services.auto_je import create_for_item_transform
from celerp.services.cost_visibility import COST_ITEM_KEYS, apply_field_visibility
from celerp.services.field_schema import AMOUNT_EDIT_GATED_KEYS, AMOUNT_ITEM_KEYS
from celerp.services.permissions import (
    assert_role_permission,
    get_current_company_settings,
    require_permission,
    role_has_permission,
)
from celerp.services.pricing import (
    coerce_price,
    derived_price_keys,
    get_price_config,
    inject_derived_prices,
    is_cost_list_name,
    price_key,
    resolve_price,
)
from celerp.services.units import validate_quantity, build_unit_map, get_company_units, is_weight_unit, is_pieces_unit, LANDED_COST_KINDS
from celerp_inventory.projections import is_item_available

router = APIRouter(dependencies=[Depends(get_current_user)])


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

VALID_INVENTORY_TYPES: frozenset[str] = frozenset({"stocked", "component", "non_stocked", "service", "freight"})

# Company units config lives in celerp.services.units (shared with labels + CSV export).
_get_company_units = get_company_units


def _to_int_pieces(val) -> int:
    """Convert a pieces value to int, tolerating float strings like '25.0'."""
    return int(float(val))


def _read_pieces(state: dict) -> float | None:
    """Read pieces from item state, checking top-level then attributes (single source of truth)."""
    raw = state.get("pieces")
    if raw is None:
        raw = (state.get("attributes") or {}).get("pieces")
    return float(raw) if raw not in (None, "") else None


def _has_attr(state: dict, key: str) -> bool:
    """True if `key` is present as an attribute in EITHER storage location (top-level or attributes).

    Category attributes can live top-level (a field edit / POST /items keeps them there — only
    `pieces`/cost are normalized into `attributes`) or nested under `attributes` (create payload /
    import). Consumers must treat both as the same attribute."""
    return key in state or key in (state.get("attributes") or {})


def _read_attr(state: dict, key: str):
    """Read an attribute from top-level OR attributes (single source of truth for reads).

    Top-level wins when present (that is where a field edit stores it); otherwise fall back to the
    nested `attributes` dict."""
    if key in state:
        return state[key]
    return (state.get("attributes") or {}).get(key)


def _num_pieces(raw) -> Decimal | None:
    """Coerce a stored `pieces` value (int / float / numeric str) to a Decimal.

    Treats None / "" (and anything non-numeric) as *unset* → None. Different write paths
    persist pieces as int, float, or string; coercing here lets the merge compare and sum
    them uniformly (the rest of the system already coerces via float()/int(float())).
    """
    if raw is None or raw == "":
        return None
    try:
        return Decimal(str(raw))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _read_float(state: dict, key: str) -> float | None:
    """Safely read a numeric field from item state; treats None and '' as absent."""
    raw = state.get(key)
    return float(raw) if raw not in (None, "") else None


def _parse_uuid(value: str | None) -> uuid.UUID | None:
    """Safely parse a UUID string; returns None on empty or malformed input."""
    if not value:
        return None
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError):
        return None


# Fields that must NOT be inherited from parent in split/transform (child gets fresh values).
# Everything else in parent.state is inherited automatically (copy-all-then-override).
_CHILD_RESET_FIELDS: frozenset[str] = frozenset({
    # Identity — always overridden explicitly
    "sku",
    "barcode",      # recalculated: new entity needs a new unique barcode
    # Quantity / cost — set by split math or pricing events
    "quantity",
    "weight",
    "pieces",
    "cost_total",
    "cost_price",
    # Status — children start as available regardless of parent's terminal status
    "status",
    # Timestamps — set fresh; P2 will move these to Projection columns
    "created_at",
    "updated_at",
    # Relationship — set by split/transform logic
    "parent_id",
    "parent_sku",
})


def _recipe_standard_unit_cost(state: dict) -> float | None:
    """The rolled standard unit cost of a recipe-backed (manufactured) item, else None.

    For a manufactured item the recipe's rolled ``unit_cost`` is the SINGLE source of truth for
    cost — read at standard, never at a lingering build-lot ``cost_total``. Read ``recipe.unit_cost``
    (not the ``cost_price`` field, which ``_recompute_cost`` can pop) so it is robust and stays
    consistent with the manufacturing cost roll-up. Only a recipe with components carries a cost.
    """
    recipe = state.get("recipe") or {}
    unit_cost = recipe.get("unit_cost") if recipe.get("components") else None
    return float(unit_cost) if unit_cost is not None else None


def flatten_item(state: dict, entity_id: str, location_id: str | None = None, location_name: str | None = None, created_at: object | None = None, updated_at: object | None = None, price_config: tuple[list[dict], str, str] | None = None) -> dict:
    """Flatten attributes dict to top-level so schema-driven UI sees all fields.

    When ``price_config`` (``(price_lists, base_price_list, currency)`` from
    ``get_price_config``) is given, derived price lists are computed onto the result after
    the cost roll-up, so a Cost base prices from the same unit cost every other consumer sees.
    """
    flat = dict(state)
    flat["id"] = entity_id
    attrs = flat.pop("attributes", None) or {}
    for k, v in attrs.items():
        if k not in flat:
            flat[k] = v
    if location_id:
        flat["location_id"] = location_id
    if location_name:
        flat["location_name"] = location_name
    # created_at is authoritative from Projection column (set on INSERT by engine, never forgeable).
    if created_at is not None:
        flat["created_at"] = created_at.isoformat() if hasattr(created_at, "isoformat") else str(created_at)
    if updated_at is not None:
        flat["updated_at"] = updated_at.isoformat() if hasattr(updated_at, "isoformat") else str(updated_at)
    qty = float(flat.get("quantity") or 0)
    _recipe_unit = _recipe_standard_unit_cost(flat)
    if _recipe_unit is not None:
        # Recipe-backed item: derive cost from the rolled standard (single source of truth); never
        # let a lingering build-lot cost_total silently override it. See _recipe_standard_unit_cost.
        flat["cost_price"] = _recipe_unit
        flat["cost_total"] = round(_recipe_unit * qty, 2) if qty else 0.0
    elif flat.get("cost_total") is not None:
        flat["cost_price"] = round(float(flat["cost_total"]) / qty, 10) if qty else 0.0
    elif flat.get("cost_price") is not None:
        flat["cost_total"] = round(float(flat["cost_price"]) * qty, 2)
    # else: both remain absent (item has no cost set)
    if price_config is not None:
        inject_derived_prices(flat, *price_config)
    return flat


class ItemCreate(BaseModel):
    model_config = {"extra": "allow"}  # Accept dynamic price fields (e.g. vip_price)

    sku: str | None = None
    name: str
    sell_by: str                           # required - must be a valid unit name from company settings
    quantity: float = 0
    category: str | None = None
    location_id: uuid.UUID | None = None
    cost_price: float | None = None  # legacy alias; prefer cost_total
    cost_total: float | None = None
    wholesale_price: float | None = None
    retail_price: float | None = None
    description: str | None = None
    unit: str | None = None
    barcode: str | None = None             # digits only if provided
    hs_code: str | None = None             # Harmonized System code for trade/customs
    tax_codes: list[str] = Field(default_factory=list)
    purchase_sku: str | None = None        # vendor's SKU / part number
    purchase_name: str | None = None       # vendor's product name
    purchase_unit: str | None = None       # unit vendor sells in (e.g. "case", "box")
    purchase_conversion_factor: float | None = None  # sell units per purchase unit (e.g. 24 pcs/case)
    allow_splitting: bool = True
    attributes: dict = Field(default_factory=dict)
    idempotency_key: str | None = None
    inventory_type: str = "stocked"  # stocked | component | non_stocked | service | freight
    # Landed-cost charge lines (inventory_type=freight): refines reporting/GL routing.
    landed_cost_kind: str | None = None      # freight | insurance | duty | import_vat
    recoverable: bool | None = None          # import_vat only: recoverable VAT does not capitalise


class ItemPatch(BaseModel):
    fields_changed: dict[str, dict] = Field(default_factory=dict)
    idempotency_key: str | None = None


class TransferBody(BaseModel):
    to_location_id: uuid.UUID
    idempotency_key: str | None = None


class SplitChild(BaseModel):
    sku: str | None = None    # omitted → keeps the parent SKU (resolved in split_item)
    quantity: float
    weight: float | None = None
    pieces: int | None = None   # complement for weight-unit items (independent of weight)
    barcode: str | None = None  # auto-assigned from shared sequence if omitted
    attributes: dict = Field(default_factory=dict)


class SplitBody(BaseModel):
    children: list[SplitChild]
    mother_qty: float | None = None    # explicit mother qty override (used when user re-weighed mother)
    mother_weight: float | None = None # explicit mother weight override
    idempotency_key: str | None = None


class MergeBody(BaseModel):
    source_entity_ids: list[str]
    target_sku_from: str                       # entity_id of the source whose SKU/barcode to use
    resulting_quantity: float | None = None    # optional override (default = sum)
    resulting_cost_total: float | None = None  # optional override (default = sum of source cost_totals)
    resulting_name: str | None = None          # optional override (default = target's name)
    resulting_sku: str | None = None           # optional custom SKU (default = target's SKU); issue #190
    resolved_attributes: dict | None = None    # user picks for conflicting string attributes
    idempotency_key: str | None = None


class TransformBody(BaseModel):
    child_sku: str
    child_category: str
    child_sell_by: str
    child_quantity: float
    child_name: str | None = None
    child_weight: float | None = None
    child_weight_unit: str | None = None
    child_pieces: int | None = None
    child_cost_total: float | None = None  # final cost (permitted override); None or a restricted caller preserves parent cost
    idempotency_key: str | None = None


class AdjustBody(BaseModel):
    new_qty: float
    idempotency_key: str | None = None


class PriceBody(BaseModel):
    price_type: str
    new_price: float
    idempotency_key: str | None = None


class StatusBody(BaseModel):
    new_status: str
    idempotency_key: str | None = None


class ReserveBody(BaseModel):
    quantity: float
    idempotency_key: str | None = None


# Statuses hidden from the default inventory view. Users must explicitly request them.
_HIDDEN_STATUSES = frozenset({"sold", "archived", "merged", "expired", "disposed"})

# "Archived" tab shows all terminal/inactive statuses grouped together.
_ARCHIVED_GROUP = frozenset({"archived", "merged", "expired"})

# Every status an item can legally hold. The status write paths (single, bulk,
# patch) validate against this set; projection replay stays permissive so
# historic events are never rejected.
ITEM_STATUSES: frozenset[str] = frozenset({
    "draft", "available", "active", "reserved", "sold", "archived",
    "merged", "expired", "memo_out", "returned", "disposed",
})

# Authoring-only event types: none of these mean the item has circulated.
# Any other ledger event on the item (adjust, transfer, fulfill, reserve,
# split, receive, ...) counts as circulation and blocks a revert to draft.
# item.file.* is matched by prefix below.
_AUTHORING_EVENT_TYPES: frozenset[str] = frozenset({
    "item.created", "item.updated", "item.patched", "item.pricing.set",
    "item.status.set", "item.recipe.set", "item.workflow.set",
    "shop.sync.enabled", "shop.sync.disabled",
})


async def assert_status_change_allowed(
    session: AsyncSession, company_id, entity_id: str, new_status: str,
    role: str, settings: dict,
) -> None:
    """Function-level validation shared by every item-status write path (single,
    bulk, and PATCH). Unknown values are rejected with the allowed list. A draft
    item's amounts and costs are freely editable, so an item that has circulated
    must never quietly become one again: reverting a committed item to draft
    requires the revert_items_to_draft permission AND a clean history, and every
    rejection names its reason instead of hiding the control.
    """
    ns = str(new_status or "").lower()
    if ns not in ITEM_STATUSES:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown status '{new_status}'; allowed: {', '.join(sorted(ITEM_STATUSES))}",
        )
    if ns == "disposed":
        # disposed is off the books and is set atomically with a journal entry. It is reachable
        # only through the manager-gated Write off stock terminal, never a generic status edit -
        # otherwise edit_inventory alone could take stock off-books with no ledger effect.
        raise HTTPException(
            status_code=422,
            detail="Disposal is recorded through the Write off stock action, not a direct status edit.",
        )
    if ns != "draft":
        return
    row = await session.get(Projection, {"company_id": company_id, "entity_id": entity_id})
    state = (row.state if row else {}) or {}
    current = str(state.get("status") or "").lower()
    if current in ("", "draft"):
        return  # creating as draft / already draft: harmless no-op
    if not role_has_permission(settings, role, "revert_items_to_draft"):
        raise HTTPException(
            status_code=403,
            detail="Reverting an item to draft requires the 'Revert items to draft' permission (revert_items_to_draft)",
        )
    if current != "available":
        raise HTTPException(
            status_code=409,
            detail=f"Only an available item can be reverted to draft; this item is {current}",
        )
    if state.get("status_doc_id"):
        holder = state.get("status_doc_number") or state.get("status_doc_id")
        raise HTTPException(
            status_code=409,
            detail=f"Cannot revert to draft: the item's status is held by document {holder}",
        )
    from celerp.models.ledger import LedgerEntry
    event_types = set((await session.execute(
        select(LedgerEntry.event_type).distinct().where(
            LedgerEntry.company_id == company_id,
            LedgerEntry.entity_id == entity_id,
        )
    )).scalars().all())
    circulated = sorted(
        e for e in event_types
        if e not in _AUTHORING_EVENT_TYPES and not e.startswith("item.file.")
    )
    if circulated:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot revert to draft: the item has circulation history ({', '.join(circulated)})",
        )
    from sqlalchemy import cast, or_
    from sqlalchemy.dialects.postgresql import JSONB
    # Doc lines are item_id-keyed via POST /docs (LineItem normalizes entity_id)
    # but entity_id-keyed via the patch path and receiving/fulfillment writes, so
    # membership must match either key.
    _lines = cast(Projection.state["line_items"], JSONB)
    doc_ref = (await session.execute(
        select(Projection).where(
            Projection.company_id == company_id,
            Projection.entity_type.in_(("doc", "list")),
            or_(
                _lines.contains([{"item_id": entity_id}]),
                _lines.contains([{"entity_id": entity_id}]),
            ),
        ).limit(1)
    )).scalars().first()
    if doc_ref:
        ref_state = (doc_ref.state or {})
        ref = ref_state.get("ref_id") or ref_state.get("doc_number") or doc_ref.entity_id
        raise HTTPException(
            status_code=409,
            detail=f"Cannot revert to draft: the item is on document {ref}",
        )


async def reject_draft_status_change_via_generic_path(
    session: AsyncSession, company_id, entity_id: str, new_status: str,
) -> None:
    """Draft's only way out is Make Available, and the only way in is Revert to Draft -
    both dedicated actions, never a generic status write. Blocks every draft-origin
    transition (not just to "available" - a draft going to sold/reserved/archived/expired
    makes no more sense, since it isn't stock yet), independent of permission: a generic
    write must never touch draft in either direction. Every non-draft-origin transition
    (e.g. Restore, archived -> available) is untouched."""
    ns = str(new_status or "").lower()
    if ns == "draft":
        raise HTTPException(
            status_code=422,
            detail="Use the item's 'Revert to Draft' action, not a direct status edit.",
        )
    row = await session.get(Projection, {"company_id": company_id, "entity_id": entity_id})
    current = str(((row.state if row else {}) or {}).get("status") or "").lower()
    if current == "draft":
        raise HTTPException(
            status_code=422,
            detail="A draft item can only become available through the 'Make Available' action, not a direct status edit.",
        )


async def assert_make_available_allowed(session: AsyncSession, company_id, entity_id: str) -> None:
    """Already-available is a harmless no-op (mirrors the revert guard's own
    already-draft no-op), so a mixed bulk selection doesn't hard-fail."""
    row = await session.get(Projection, {"company_id": company_id, "entity_id": entity_id})
    current = str(((row.state if row else {}) or {}).get("status") or "").lower()
    if current in ("draft", "available"):
        return
    raise HTTPException(
        status_code=409,
        detail=f"Only a draft item can be made available; this item is {current}",
    )


async def assert_not_draft(session: AsyncSession, company_id, entity_id: str, action: str) -> None:
    """A draft isn't stock yet, so stock-circulation operations (reserve, expire, ...)
    make no sense on it until it is committed via Make Available."""
    row = await session.get(Projection, {"company_id": company_id, "entity_id": entity_id})
    current = str(((row.state if row else {}) or {}).get("status") or "").lower()
    if current == "draft":
        raise HTTPException(
            status_code=422,
            detail=f"Cannot {action} a draft item; make it available first.",
        )


# ── Search grammar ─────────────────────────────────────────────────────────────
# The inventory (and global) search bar accepts: `,` = OR groups, `&` = AND terms,
# `lo-hi` = numeric range over quantity/weight/pieces, a bare number = numeric-exact
# OR text substring, anything else = text substring over the fields below.
_SEARCH_FIELDS = ("name", "sku", "barcode", "description", "category")
_NUMERIC_FIELDS = ("quantity", "weight", "pieces")
# Keys excluded from the free-text substring loop (numeric columns are matched only
# by the explicit numeric path, never by substring, so "5" never matches "50").
_SKIP_KEYS = frozenset({"id", "entity_id", "company_id", "location_id", "quantity",
                        "weight", "pieces", "status", "created_at", "updated_at"})
# A range is PURE number-dash-number only, so a hyphenated SKU (SHOT274-005) stays literal.
_RANGE_RE = re.compile(r"^(\d+(?:\.\d+)?)-(\d+(?:\.\d+)?)$")


def _numeric_values(record: dict) -> list[tuple[str, float]]:
    """The item's numeric column (field, value) pairs, skipping missing/unparseable ones."""
    vals: list[tuple[str, float]] = []
    for f in _NUMERIC_FIELDS:
        v = record.get(f)
        if v is None or v == "":
            continue
        try:
            vals.append((f, float(v)))
        except (TypeError, ValueError):
            continue
    return vals


def _text_match(record: dict, term: str) -> str | None:
    """The name of the first text field containing term, or None. Named search fields
    first, then every other string field on the flattened record - which is where
    attribute values live, since flatten_item lifts them to the top level."""
    for field in _SEARCH_FIELDS:
        if term in str(record.get(field, "")).lower():
            return field
    for k, v in record.items():
        if k in _SKIP_KEYS or k in _SEARCH_FIELDS or k.endswith("_price"):
            continue
        if isinstance(v, str) and term in v.lower():
            return k
    return None


def _term_match_reason(record: dict, term: str) -> tuple[str, str] | None:
    """One AND-term: the (field, matched text) behind the hit, or None. The matched
    text is the term itself for substring hits and the whole number for numeric
    range/exact hits, so the UI can embolden exactly what matched."""
    m = _RANGE_RE.match(term)
    if m:
        lo, hi = float(m.group(1)), float(m.group(2))
        if lo <= hi:
            for f, n in _numeric_values(record):
                if lo <= n <= hi:
                    return f, format(n, "g")
            return None
        # lo > hi is not a usable range; fall through and treat the term as literal text.
    field = _text_match(record, term)
    if field is not None:
        return field, term
    try:
        num = float(term)
    except (TypeError, ValueError):
        return None
    for f, n in _numeric_values(record):
        if n == num:
            return f, format(n, "g")
    return None


def query_match_reasons(record: dict, q: str) -> list[tuple[str, str]] | None:
    """Match a flattened item dict against the search grammar. `,` ORs groups, `&`
    ANDs the terms within a group; empty terms and empty groups are dropped.
    Returns the first matching group's (field, matched text) pairs - one per
    AND-term, deduped, order preserved - or None when no group matches."""
    for group in q.split(","):
        terms = [t.strip().lower() for t in group.split("&") if t.strip()]
        if not terms:
            continue
        reasons = [_term_match_reason(record, term) for term in terms]
        if all(r is not None for r in reasons):
            deduped: list[tuple[str, str]] = []
            for r in reasons:
                if r not in deduped:
                    deduped.append(r)
            return deduped
    return None


def item_matches_query(record: dict, q: str) -> bool:
    """Boolean view of query_match_reasons (the grammar is documented there)."""
    return query_match_reasons(record, q) is not None


@router.get("")
async def list_items(
    request: Request,
    company_id=Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
    role: str = Depends(get_current_role),
    limit: int = 50,
    offset: int = 0,
    q: str | None = None,
    sku: str | None = None,
    skus: str | None = None,  # comma-separated exact SKU list
    barcode: str | None = None,
    status: str | None = None,
    category: str | None = None,
    inventory_type: str | None = None,
    location_id: str | None = None,
    source: str | None = None,
    filter: str | None = None,
    on_memo_to: str | None = None,
    consigned_from: str | None = None,
    sort: str | None = None,
    dir: str = "desc",
) -> dict:
    """List items with optional filters.

    status: exact status to show (e.g. "sold", "archived", "available").
            Pass "all" to skip status filtering entirely.
            Default (None): exclude sold + archived from results.
    category: exact category to filter on.
    filter: semantic filter. "low_stock" keeps only items at or below their
            reorder point (see celerp.services.reorder.is_below_reorder).
    on_memo_to: customer contact_id. Scope to items currently out on memo to that
            customer, valued (holding_value) at the price they were quoted.
    consigned_from: supplier contact_id. Scope to items currently held on
            consignment from that supplier, valued (holding_value) at cost.
            When a contact scope is active the response also carries value_total,
            the sum of holding_value over the whole scoped set (pre-pagination).
    """
    from celerp.services.reorder import is_below_reorder
    from celerp.models.company import Company, Location
    from celerp.services.field_schema import get_effective_field_schema
    stmt = select(Projection).where(Projection.company_id == company_id, Projection.entity_type == "item")
    rows = (await session.execute(stmt)).scalars().all()

    loc_rows = (await session.execute(select(Location).where(Location.company_id == company_id))).scalars().all()
    loc_map = {str(r.id): r.name for r in loc_rows}

    price_config = await get_price_config(session, company_id)
    result = [
        flatten_item(r.state, r.entity_id,
                      location_id=str(r.location_id) if r.location_id else None,
                      location_name=loc_map.get(str(r.location_id)) if r.location_id else None,
                      created_at=r.created_at,
                      updated_at=r.updated_at,
                      price_config=price_config)
        for r in rows
    ]

    # Status filtering: default excludes hidden statuses; "all" skips filtering; "archived" expands
    # to include merged/expired; a comma-separated value matches any (column-filter multi-select).
    status_set = {s.strip().lower() for s in status.split(",") if s.strip()} if (status and "," in status) else None
    if status == "all":
        pass  # no filter
    elif status_set:
        result = [r for r in result if str(r.get("status") or "").lower() in status_set]
    elif status == "archived":
        result = [r for r in result if str(r.get("status") or "").lower() in _ARCHIVED_GROUP]
    elif status:
        result = [r for r in result if str(r.get("status") or "").lower() == status.lower()]
    else:
        result = [r for r in result if str(r.get("status") or "").lower() not in _HIDDEN_STATUSES]

    # Contact-scoped holdings: memo out to a customer, or consignment in from a supplier.
    # Membership is derived from that contact's docs (celerp.services.holdings), and is
    # authoritative: it narrows result on its own. The per-item scope value (quoted memo
    # price / consignment cost) is attached after cost-visibility gating, below.
    holding_scoped = bool(on_memo_to or consigned_from)
    scope_value: dict[str, float] = {}
    if holding_scoped:
        from celerp.services.holdings import consignment_holdings, memo_holdings
        items_state = [(r.entity_id, r.state) for r in rows]
        scope_doc_type = "memo" if on_memo_to else "consignment_in"
        scope_contact = on_memo_to or consigned_from
        scope_docs = (
            await session.execute(
                select(Projection).where(
                    Projection.company_id == company_id,
                    Projection.entity_type == "doc",
                    Projection.state["doc_type"].as_string() == scope_doc_type,
                    Projection.state["contact_id"].as_string() == scope_contact,
                )
            )
        ).scalars().all()
        # Only issued docs contribute; a draft or voided doc must not seed the set.
        issued = [
            (d.entity_id, d.state) for d in scope_docs
            if str((d.state or {}).get("status") or "").lower() not in ("draft", "void")
        ]
        scope_value = (
            memo_holdings(items_state, issued) if on_memo_to
            else consignment_holdings(items_state, issued)
        )
        result = [r for r in result if r.get("id") in scope_value]

    # Sold price: when the sold view is active, price each sold item from the line of
    # the document that sold it (status_doc_id). A realized sale price is not a cost, so
    # (like the memo value below) it is not gated by view_inventory_costs. Computed here
    # over the loaded rows; attached to the result dicts after visibility rebuild.
    sold_scoped = "sold" in (status_set or {str(status).lower()} if status else set())
    sold_price: dict[str, float | None] = {}
    if sold_scoped:
        from celerp.services.holdings import sold_prices
        sold_rows = [r for r in rows if str((r.state or {}).get("status") or "").lower() == "sold"]
        sold_doc_ids = {
            str((r.state or {}).get("status_doc_id"))
            for r in sold_rows if (r.state or {}).get("status_doc_id")
        }
        if sold_doc_ids:
            sold_docs = (
                await session.execute(
                    select(Projection).where(
                        Projection.company_id == company_id,
                        Projection.entity_type == "doc",
                        Projection.entity_id.in_(sold_doc_ids),
                    )
                )
            ).scalars().all()
            sold_price = sold_prices(
                [(r.entity_id, r.state) for r in sold_rows],
                [(d.entity_id, d.state) for d in sold_docs],
            )

    if category:
        cats = {c.strip() for c in category.split(",") if c.strip()}
        result = [r for r in result if str(r.get("category") or "") in cats]

    # Connector source: items linked to a platform encode it in the idempotency key
    # (e.g. "shopify:123:456"). Powers the connector detail "View N synced products" link.
    if source:
        _prefix = f"{source.strip().lower()}:"
        result = [r for r in result if str(r.get("idempotency_key") or "").lower().startswith(_prefix)]

    if inventory_type:
        types = {it.strip() for it in inventory_type.split(",") if it.strip()}
        result = [r for r in result if (r.get("inventory_type") or "stocked") in types]

    if location_id:
        locs = {loc.strip() for loc in location_id.split(",") if loc.strip()}
        result = [r for r in result if str(r.get("location_id") or "") in locs]

    # Distinct attribute values for the column-filter funnels, over the status/category/type/location
    # scope and BEFORE attribute filters are applied (so every available value stays selectable).
    _FACET_MAX = 500
    attrs_by_id = {r.entity_id: (r.state.get("attributes") or {}) for r in rows}
    facet_sets: dict[str, set] = {}
    for r in result:
        for akey, aval in attrs_by_id.get(r.get("id"), {}).items():
            if aval in (None, ""):
                continue
            s = facet_sets.setdefault(akey, set())
            if len(s) < _FACET_MAX:
                s.add(str(aval))
    attribute_facets = {k: sorted(s) for k, s in facet_sets.items() if s}

    # Category-attribute column filters: ?attr.<key>=v1,v2 keeps items whose (flattened) attribute
    # value is in the chosen set. Multiple attribute filters AND together.
    for qk, qv in request.query_params.multi_items():
        if not qk.startswith("attr.") or not qv:
            continue
        akey = qk[len("attr."):]
        wanted = {x.strip() for x in qv.split(",") if x.strip()}
        if wanted:
            result = [r for r in result if str(r.get(akey) if r.get(akey) is not None else "") in wanted]

    if sku:
        result = [r for r in result if str(r.get("sku", "")) == sku]

    if skus:
        sku_set = {s.strip() for s in skus.split(",") if s.strip()}
        result = [r for r in result if str(r.get("sku", "")) in sku_set]

    if barcode:
        result = [r for r in result if str(r.get("barcode", "")) == barcode]

    # Semantic "low stock" filter: at or below reorder point (backs the dashboard
    # cards' /inventory?filter=low_stock link and the reorder alert action_url).
    if filter == "low_stock":
        # Drafts are not stock: an unfinished item must not raise a reorder alarm.
        result = [r for r in result
                  if is_below_reorder(r) and str(r.get("status") or "").lower() != "draft"]

    q_reasons: dict = {}
    if q:
        # Grammar: comma = OR groups, & = AND terms, lo-hi = numeric range, bare
        # number = numeric-exact OR text, else text substring (query_match_reasons).
        matched = []
        for r in result:
            reasons = query_match_reasons(r, q)
            if reasons is not None:
                q_reasons[r.get("id")] = reasons
                matched.append(r)
        result = matched

    # Apply visible_to_roles filtering from company field schema
    field_schema = await get_effective_field_schema(session, company_id, category=None)
    company = await session.get(Company, company_id)
    settings = (company.settings if company else {}) or {}
    can_see_costs = role_has_permission(settings, role, "view_inventory_costs")
    result = apply_field_visibility(
        result, role, field_schema, can_see_costs,
        can_author_drafts=role_has_permission(settings, role, "edit_inventory"),
    )

    # Attach search-match reasons AFTER visibility (so they survive the dict
    # rebuild). The UI reads each reason's value from the visibility-filtered
    # record itself, so a role-hidden field never leaks its value through a tag.
    if q:
        for r in result:
            r["q_match"] = [{"field": f, "match": m} for f, m in q_reasons.get(r.get("id"), [])]

    # Attach the per-item scope value AFTER visibility (so it survives any dict rebuild).
    # The consignment value is cost, so it is gated by view_inventory_costs exactly like
    # every other cost figure; the memo value is a quoted sale price and is not gated.
    gate_cost = bool(consigned_from) and not can_see_costs
    if holding_scoped:
        for r in result:
            r["holding_value"] = None if gate_cost else scope_value.get(r.get("id"), 0.0)

    # Attach the realized sale price to each sold row (ungated: a sale price is not a cost).
    if sold_scoped:
        for r in result:
            if str(r.get("status") or "").lower() == "sold":
                r["sold_price"] = sold_price.get(r.get("id"))

    # FEFO: when company uses fefo, sort available items by expires_at ascending (soonest first)
    # so staff always see the items that need to be picked/sold first at the top.
    if company and (company.settings or {}).get("inventory_method") == "fefo":
        def _fefo_key(item: dict):
            exp = item.get("expires_at")
            # Items without expiry float to the bottom; expired items sort before no-expiry
            return exp or "9999-99-99"
        result.sort(key=_fefo_key)

    # User-requested column sort - applied AFTER all filtering so pagination is globally correct.
    # FEFO overrides user sort for available items; explicit sort wins for all other statuses.
    if sort and (not company or (company.settings or {}).get("inventory_method") != "fefo" or status not in (None, "available", "")):
        reverse = dir.lower() != "asc"

        def _sort_key(item: dict):
            v = item.get(sort)
            if v is None:
                # Nulls always last regardless of direction
                return (1, "")
            if isinstance(v, (int, float)):
                return (0, v)
            s = str(v)
            # ISO date/datetime strings sort correctly as strings
            return (0, s.lower())

        result.sort(key=_sort_key, reverse=reverse)
    elif not sort and (not company or (company.settings or {}).get("inventory_method") != "fefo"):
        # Default: most-recently-updated first, then name asc, then entity_id asc for stability.
        # Use two-pass: primary descending on updated_at, then stable sub-sort on name+entity_id.
        result.sort(key=lambda item: (str(item.get("name") or "").lower(), str(item.get("entity_id") or "")))
        result.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)

    total = len(result)
    resp: dict = {"items": result[offset: offset + limit], "total": total,
                  "attribute_facets": attribute_facets}
    if holding_scoped and not gate_cost:
        # Total over the whole scoped set (post-filter, pre-pagination) so the contact
        # card reads it directly and reconciles with the list at the same value basis.
        resp["value_total"] = round(sum(float(scope_value.get(r.get("id"), 0.0)) for r in result), 2)
    return resp


@router.get("/valuation")
async def get_valuation(
    category: str | None = None,
    status: str | None = None,
    on_memo_to: str | None = None,
    consigned_from: str | None = None,
    company_id=Depends(get_current_company_id),
    role: str = Depends(get_current_role),
    settings: dict = Depends(get_current_company_settings),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Aggregate inventory valuation from projections.

    Optional ?category= and ?status= filters scope totals + count_by_status to that slice.
    on_memo_to: customer contact_id. Scope counts to items currently out on memo to that customer.
    consigned_from: supplier contact_id. Scope counts to items currently held on consignment.
    category_counts is always global (all active items) - used by the category tab bar.
    count_by_status is scoped to the current category/status/holdings filter - used by status cards.
    """
    rows = (
        await session.execute(
            select(Projection).where(Projection.company_id == company_id, Projection.entity_type == "item")
        )
    ).scalars().all()

    holding_scope: set[str] | None = None
    if on_memo_to or consigned_from:
        from celerp.services.holdings import consignment_holdings, memo_holdings
        items_state = [(r.entity_id, r.state) for r in rows]
        scope_doc_type = "memo" if on_memo_to else "consignment_in"
        scope_contact = on_memo_to or consigned_from
        scope_docs = (
            await session.execute(
                select(Projection).where(
                    Projection.company_id == company_id,
                    Projection.entity_type == "doc",
                    Projection.state["doc_type"].as_string() == scope_doc_type,
                    Projection.state["contact_id"].as_string() == scope_contact,
                )
            )
        ).scalars().all()
        issued = [
            (d.entity_id, d.state) for d in scope_docs
            if str((d.state or {}).get("status") or "").lower() not in ("draft", "void")
        ]
        scope_value = (
            memo_holdings(items_state, issued) if on_memo_to
            else consignment_holdings(items_state, issued)
        )
        holding_scope = set(scope_value.keys())

    # Compute price totals dynamically per price list
    _price_config = await get_price_config(session, company_id)
    _price_lists: list[dict] = _price_config[0]

    price_totals: dict[str, Decimal] = {}
    for pl in _price_lists:
        price_totals[pl.get("name", "")] = Decimal(0)
    active_item_count = 0
    draft_count = 0
    category_counts: dict[str, int] = {}
    count_by_status: dict[str, int] = {}

    for row in rows:
        state = row.state
        row_status = str(state.get("status") or "").lower()
        row_cat = str(state.get("category") or state.get("item_type") or "").strip()

        # Exclude consignment_in items: they are borrowed, not owned -- exclude from all valuation
        if row.consignment_flag == "in" or state.get("consignment_flag") == "in":
            continue

        # Exclude non-stocked and service items from valuation (only stocked items have physical value)
        inv_type = state.get("inventory_type") or "stocked"
        if inv_type != "stocked":
            continue

        # Holdings scope: when filtering by on_memo_to or consigned_from, include only matching items
        if holding_scope is not None and row.entity_id not in holding_scope:
            continue

        # category_counts: scoped to the active status filter (or global non-hidden when no filter)
        if status == "all":
            if row_cat:
                category_counts[row_cat] = category_counts.get(row_cat, 0) + 1
        elif status == "archived":
            if row_status in _ARCHIVED_GROUP and row_cat:
                category_counts[row_cat] = category_counts.get(row_cat, 0) + 1
        elif status:
            if row_status == status.lower() and row_cat:
                category_counts[row_cat] = category_counts.get(row_cat, 0) + 1
        else:
            if row_status not in _HIDDEN_STATUSES and row_cat:
                category_counts[row_cat] = category_counts.get(row_cat, 0) + 1

        # Apply category filter for scoped metrics
        if category and row_cat != category:
            continue

        # Totals and count_by_status: scoped to category + status filters (mirrors list_items logic)
        if status == "all":
            pass
        elif status == "archived":
            if row_status not in _ARCHIVED_GROUP:
                continue
        elif status:
            if row_status != status.lower():
                continue
        else:
            if row_status in _HIDDEN_STATUSES:
                continue

        # count_by_status: scoped to the same category+status slice as active_item_count
        count_by_status[row_status] = count_by_status.get(row_status, 0) + 1

        # Drafts are not stock yet: counted for the status card above, excluded
        # from the active count and every value total until committed to available.
        if row_status == "draft":
            draft_count += 1
            continue

        active_item_count += 1
        qty = float(state.get("quantity") or 0)
        # Value from the flattened item so cost (recipe standard / lot total) and derived
        # lists price identically to every other consumer of item state.
        flat = flatten_item(state, row.entity_id, price_config=_price_config)
        for pl in _price_lists:
            pl_name = pl.get("name", "")
            try:
                if is_cost_list_name(pl_name):
                    # Cost values at the lot total (recipe standard × qty when recipe-backed).
                    if flat.get("cost_total") is not None:
                        price_totals[pl_name] += Decimal(str(flat["cost_total"]))
                    elif flat.get(price_key(pl_name)) is not None:
                        price_totals[pl_name] += Decimal(str(flat[price_key(pl_name)])) * Decimal(str(qty))
                else:
                    v = resolve_price(flat, pl_name)
                    if v:
                        price_totals[pl_name] += Decimal(str(v)) * Decimal(str(qty))
            except Exception:
                pass

    _cost_pl_names = {pl.get("name", "") for pl in _price_lists if is_cost_list_name(pl.get("name", ""))}
    show_cost = role_has_permission(settings, role, "view_inventory_costs")

    price_totals_out = {
        k: float(v) for k, v in price_totals.items()
        if show_cost or k not in _cost_pl_names
    }

    result: dict = {
        "item_count": active_item_count,
        "active_item_count": active_item_count,
        "price_totals": price_totals_out,
        # Backward-compatible keys for existing UI
        "wholesale_total": float(price_totals.get("Wholesale", 0)),
        "retail_total": float(price_totals.get("Retail", 0)),
        "category_counts": dict(sorted(category_counts.items(), key=lambda x: -x[1])),
        # total_scoped_count backs the "All" tab: everything the scoped list shows,
        # which includes drafts even though they carry no stock value yet
        # (some items may have no category and won't appear in category_counts)
        "total_scoped_count": active_item_count + draft_count,
        "count_by_status": count_by_status,
    }
    if show_cost:
        result["cost_total"] = float(price_totals.get("Cost", 0))
    return result


# Fields eligible for per-tenant distinct-value suggestions.
# Only non-FK categorical fields from flattened item state.
# Must be declared BEFORE /{entity_id} so FastAPI matches it first.
# NOTE: gemstone-specific fields (stone_type, stone_color, stone_shape, etc.)
# are NOT listed here — they live in the gemstones module's category_schema slot.
# Any attribute stored in item.attributes is searchable generically via /search.
_SUGGESTION_FIELDS = frozenset({
    "category", "status", "weight_unit", "dimensions_unit", "unit",
})


@router.get("/field-values")
async def get_field_values(
    field: str,
    company_id=Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Return sorted distinct non-empty values for a categorical item field.

    Allowed fields:
    - Fields in _SUGGESTION_FIELDS (core categorical fields)
    - Any attribute field (any field stored under item.attributes) — these are
      module-defined and can include gemstone fields, restaurant fields, etc.

    Blocked fields: FK references, free-text blobs, internal identifiers.
    Returns {"values": [...]} so the caller can safely extend without breakage.
    """
    # Explicit blocklist: FK fields, blobs, internal IDs that are never categorical
    _BLOCKED_FIELDS = frozenset({
        "id", "entity_id", "company_id", "location_id", "user_id",
        "name", "description", "notes", "short_description",
        "barcode", "sku",
    })
    import re as _re
    if field in _BLOCKED_FIELDS or not _re.match(r'^[a-zA-Z][a-zA-Z0-9_]*$', field):
        raise HTTPException(status_code=400, detail=f"Field '{field}' not available for suggestions")
    from celerp.models.ledger import LedgerEntry as _LE
    demo_eids = set((await session.execute(
        select(_LE.entity_id).where(
            _LE.company_id == company_id,
            _LE.source == "demo",
            _LE.entity_type == "item",
        ).distinct()
    )).scalars().all())
    stmt = select(Projection).where(Projection.company_id == company_id, Projection.entity_type == "item")
    rows = (await session.execute(stmt)).scalars().all()
    seen: set[str] = set()
    found_in_known_fields = field in _SUGGESTION_FIELDS
    _price_config = await get_price_config(session, company_id)
    for row in rows:
        if row.entity_id in demo_eids:
            continue
        flat = flatten_item(row.state, row.entity_id, price_config=_price_config)
        val = flat.get(field)
        if val and str(val).strip():
            seen.add(str(val).strip())
            found_in_known_fields = True
    # If the field was never found AND is not in the known suggestion fields,
    # it might be a typo or unknown — but we still return empty list rather
    # than 400, because attribute fields are dynamic and may not appear yet.
    if not found_in_known_fields and not seen:
        # Only raise 400 for explicitly blocked fields (handled above)
        # For unknown fields, return empty list gracefully
        pass
    return {"values": sorted(seen)}


@router.get("/categories")
async def list_item_categories(
    company_id=Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
) -> list[str]:
    """Return distinct non-empty category values: union of category_schemas keys and item projections."""
    from celerp.models.company import Company as _Company
    import uuid as _uuid

    # Categories defined in company settings (category library / vertical presets)
    co = await session.get(
        _Company,
        _uuid.UUID(str(company_id)) if isinstance(company_id, str) else company_id,
    )
    schema_cats: set[str] = set()
    if co:
        schema_cats = {k.strip() for k in ((co.settings or {}).get("category_schemas") or {}).keys() if k.strip()}

    # Categories that exist on actual item projections
    stmt = select(Projection).where(Projection.company_id == company_id, Projection.entity_type == "item")
    rows = (await session.execute(stmt)).scalars().all()
    item_cats: set[str] = {
        str(r.state.get("category") or "").strip()
        for r in rows
        if r.state.get("category") and str(r.state.get("category") or "").strip()
    }

    return sorted(schema_cats | item_cats)


@router.get("/{entity_id}")
async def get_item(entity_id: str, company_id=Depends(get_current_company_id), role: str = Depends(get_current_role), settings: dict = Depends(get_current_company_settings), session: AsyncSession = Depends(get_session)) -> dict:
    from celerp.models.company import Location
    from celerp.services.field_schema import get_effective_field_schema
    row = await session.get(Projection, {"company_id": company_id, "entity_id": entity_id})
    if row is None:
        raise HTTPException(status_code=404, detail="Not found")
    loc_name: str | None = None
    if row.location_id:
        loc = await session.get(Location, row.location_id)
        loc_name = loc.name if loc else None
    flat = flatten_item(row.state, row.entity_id,
                         location_id=str(row.location_id) if row.location_id else None,
                         location_name=loc_name,
                         created_at=row.created_at,
                         updated_at=row.updated_at,
                         price_config=await get_price_config(session, company_id))
    field_schema = await get_effective_field_schema(session, company_id, category=flat.get("category"))
    can_see_costs = role_has_permission(settings, role, "view_inventory_costs")
    filtered = apply_field_visibility(
        [flat], role, field_schema, can_see_costs,
        can_author_drafts=role_has_permission(settings, role, "edit_inventory"),
    )
    return filtered[0]


@router.get("/{entity_id}/reorder-suggestion")
async def get_reorder_suggestion(entity_id: str, company_id=Depends(get_current_company_id), session: AsyncSession = Depends(get_session)) -> dict:
    """Suggested reorder_point / reorder_qty from trailing outbound velocity.

    Read-only assist for the item detail / bulk dialog - the stored fields stay the
    single source of truth. Returns nulls when there is no outbound history (never
    a fabricated number). See celerp.services.reorder.suggest_reorder.
    """
    from celerp.services.reorder import suggest_reorder
    row = await session.get(Projection, {"company_id": company_id, "entity_id": entity_id})
    if row is None:
        raise HTTPException(status_code=404, detail="Not found")
    return await suggest_reorder(session, company_id, entity_id)


async def _next_seq(session: AsyncSession, company_id: uuid.UUID) -> int:
    """Return the next integer in the shared SKU/barcode sequence for a company.

    Scans all integer-valued SKUs and barcodes together so the two namespaces
    never collide (e.g. a barcode assigned during a split won't be re-used as
    a SKU on the next new item creation).

    Only barcodes with ≤9 digits are considered - this excludes EAN-13/GTIN
    barcodes imported from external sources while still covering all internally
    assigned barcodes (which start at 6 digits and grow slowly).
    """
    sku_vals = (await session.execute(
        select(Projection.state["sku"].as_string()).where(
            Projection.company_id == company_id,
            Projection.entity_type == "item",
        )
    )).scalars().all()
    barcode_vals = (await session.execute(
        select(Projection.state["barcode"].as_string()).where(
            Projection.company_id == company_id,
            Projection.entity_type == "item",
        )
    )).scalars().all()
    _MAX_SEQ_DIGITS = 9  # excludes EAN-13/GTIN-14 imported barcodes
    all_vals = list(sku_vals) + [v for v in barcode_vals if v and len(v) <= _MAX_SEQ_DIGITS]
    return max((int(v) for v in all_vals if v and str(v).isdigit()), default=0) + 1


class ResolveResult:
    """Result of resolving a scanned/typed code to item(s).

    ``kind`` is "barcode" (matched a unique physical-lot barcode), "sku" (matched
    a product-type SKU, which may map to N physical lots), or "none". ``matches``
    is the list of matching item Projections (0, 1, or - for sku - N).
    ``ambiguous`` is True only when a SKU matched more than one lot: the caller
    must disambiguate (scan a barcode or pick a lot), never silently pick one.
    """
    __slots__ = ("kind", "matches")

    def __init__(self, kind: str, matches: list):
        self.kind = kind
        self.matches = matches

    @property
    def ambiguous(self) -> bool:
        return self.kind == "sku" and len(self.matches) > 1

    @property
    def one(self):
        """The single match, or None when there are zero or (ambiguously) many."""
        return self.matches[0] if len(self.matches) == 1 else None


async def resolve_item_by_code(session: AsyncSession, company_id, code: str) -> ResolveResult:
    """Canonical code -> item(s) resolver. Barcode (unique) wins; SKU may be N.

    This is the single disambiguation rule shared by every scan/lookup surface so
    they behave identically: an exact barcode match resolves to one physical lot;
    otherwise an exact SKU match may resolve to many lots (the caller then either
    acts on a single match, or - when ``ambiguous`` - asks the user to pick).
    """
    code = (code or "").strip()
    if not code:
        return ResolveResult("none", [])
    rows = (await session.execute(
        select(Projection).where(
            Projection.company_id == company_id,
            Projection.entity_type == "item",
        )
    )).scalars().all()
    barcode_matches = [r for r in rows if str((r.state or {}).get("barcode") or "") == code]
    if barcode_matches:
        return ResolveResult("barcode", barcode_matches)
    sku_matches = [r for r in rows if str((r.state or {}).get("sku") or "") == code]
    if sku_matches:
        return ResolveResult("sku", sku_matches)
    return ResolveResult("none", [])


@router.post("")
async def post_item(payload: ItemCreate, company_id=Depends(get_current_company_id), _: None = require_permission("edit_inventory"), user=Depends(get_current_user), role: str = Depends(get_current_role), settings: dict = Depends(get_current_company_settings), session: AsyncSession = Depends(get_session)) -> dict:
    # Guard: setting cost fields on creation requires set_inventory_prices, except that a
    # draft's creator authors cost with edit_inventory alone (the gate re-arms at commit) -
    # the same draft_cost_carveout the pricing surfaces use, so the three stay in lockstep.
    if payload.cost_price is not None or payload.cost_total is not None:
        _create_draft = str((payload.model_extra or {}).get("status") or "draft").lower() == "draft"
        if not draft_cost_carveout(_create_draft, role, settings):
            assert_role_permission(settings, role, "set_inventory_prices")

    if payload.inventory_type not in VALID_INVENTORY_TYPES:
        raise HTTPException(status_code=422, detail=f"inventory_type must be one of {sorted(VALID_INVENTORY_TYPES)}")

    if payload.landed_cost_kind is not None and payload.landed_cost_kind not in LANDED_COST_KINDS:
        raise HTTPException(status_code=422, detail=f"landed_cost_kind must be one of {sorted(LANDED_COST_KINDS)}")

    # Validate sell_by against company units
    units = await _get_company_units(session, company_id)
    unit_map = {u["name"]: u for u in units}
    if payload.sell_by not in unit_map:
        raise HTTPException(status_code=422, detail=f"sell_by '{payload.sell_by}' is not a valid unit name")

    # Validate quantity precision
    unit_cfg = unit_map[payload.sell_by]
    validate_quantity(payload.quantity, unit_cfg["decimals"])

    # Validate barcode format (digits only)
    if payload.barcode is not None and not payload.barcode.isdigit():
        raise HTTPException(status_code=422, detail="Barcode must contain digits only")

    # Auto-assign sequential SKU if not provided
    if not payload.sku:
        payload = payload.model_copy(update={"sku": str(await _next_seq(session, company_id)).zfill(6)})

    # Auto-copy SKU to barcode when barcode omitted and SKU is purely numeric.
    # SKU is now a (possibly repeated) product-type, so gate the copy on collision:
    # if another item already uses that barcode (e.g. a second item deliberately
    # sharing a numeric SKU), assign a fresh sequential barcode instead so the
    # duplicate-SKU create does not 409 on barcode. Single-SKU behaviour is
    # unchanged (the first/only item still gets barcode == sku).
    if payload.barcode is None and payload.sku.isdigit():
        clash = (await session.execute(
            select(Projection).where(
                Projection.company_id == company_id,
                Projection.entity_type == "item",
                Projection.state["barcode"].as_string() == payload.sku,
            )
        )).scalars().first()
        new_barcode = str(await _next_seq(session, company_id)).zfill(6) if clash else payload.sku
        payload = payload.model_copy(update={"barcode": new_barcode})

    # SKU uniqueness is intentionally NOT enforced: `sku` is a product-type that may
    # repeat across physical lots. Physical-lot uniqueness is carried by `barcode`
    # (below) and the immutable `entity_id`. See the 2026-06-17 sku/batch plan.

    # Barcode uniqueness
    if payload.barcode:
        existing_barcode = (await session.execute(
            select(Projection).where(
                Projection.company_id == company_id,
                Projection.entity_type == "item",
                Projection.state["barcode"].as_string() == payload.barcode,
            )
        )).scalars().first()
        if existing_barcode:
            raise HTTPException(status_code=409, detail=f"Barcode '{payload.barcode}' already exists")

    entity_id = f"item:{uuid.uuid4()}"
    data = payload.model_dump(exclude_none=True)
    if payload.location_id is not None:
        data["location_id"] = str(payload.location_id)

    # Amount fields must be non-negative on create. ItemCreate is extra="allow", so
    # weight/pieces/gross_weight are otherwise unvalidated. Creation is not gated by
    # edit_inventory_amounts (a create defines the item, not a hand-edit); value only.
    for _amt in AMOUNT_ITEM_KEYS & set(data):
        _amt_val = data.get(_amt)
        if _amt_val is not None and float(_amt_val) < 0:
            raise HTTPException(status_code=422, detail=f"{_amt} cannot be negative")

    # Apply category defaults for purchase_unit and weight_unit if not explicitly provided
    if payload.category:
        try:
            from celerp_verticals.routes import _all_categories  # type: ignore
            _cats = _all_categories()
            _cat = _cats.get(payload.category)
            if _cat:
                if payload.purchase_unit is None and _cat.get("default_purchase_unit"):
                    data["purchase_unit"] = _cat["default_purchase_unit"]
                if payload.purchase_conversion_factor is None:
                    data["purchase_conversion_factor"] = 1
                if data.get("weight_unit") is None and _cat.get("default_weight_unit"):
                    data["weight_unit"] = _cat["default_weight_unit"]
        except ImportError:
            pass

    # Ensure status is set (not part of ItemCreate model but required for projections).
    # Manual creation starts as draft: the item stays authorable (amounts and costs
    # editable by anyone with edit_inventory) until "Make available" commits it into
    # circulating stock. System flows (split, merge, import, receive) pass status
    # explicitly and stay available - they derive from stock already in circulation.
    data.setdefault("status", "draft")

    # Strip price fields from create event data - they go via pricing events.
    # Any key ending in _price is treated as a pricing field. cost_total is also a pricing field.
    price_fields = {k: data.pop(k) for k in list(data) if k.endswith("_price") and data[k] is not None}
    # Derived lists are computed at read time; a derived column riding along in an imported
    # or exported payload is dropped rather than stored.
    _derived_keys = derived_price_keys((await get_price_config(session, company_id))[0])
    price_fields = {k: v for k, v in price_fields.items() if k not in _derived_keys}
    if "cost_total" in data and data["cost_total"] is not None:
        # cost_total takes precedence over cost_price if both supplied
        price_fields["cost_total"] = data.pop("cost_total")
        price_fields.pop("cost_price", None)  # discard cost_price if cost_total provided
    elif "cost_total" in data:
        data.pop("cost_total")

    entry = await emit_event(
        session,
        company_id=company_id,
        entity_id=entity_id,
        entity_type="item",
        event_type="item.created",
        data=data,
        actor_id=user.id,
        location_id=payload.location_id,
        source="api",
        idempotency_key=payload.idempotency_key or str(uuid.uuid4()),
        metadata_={},
    )

    # Emit pricing events for any prices supplied inline
    for price_type, price_val in price_fields.items():
        await emit_event(
            session,
            company_id=company_id,
            entity_id=entity_id,
            entity_type="item",
            event_type="item.pricing.set",
            data={"price_type": price_type, "new_price": price_val},
            actor_id=user.id,
            location_id=None,
            source="api",
            idempotency_key=str(uuid.uuid4()),
            metadata_={},
        )

    await session.commit()
    return {"event_id": entry.id, "id": entry.entity_id}


def draft_cost_carveout(is_draft: bool, role: str, settings: dict) -> bool:
    """While an item is draft, its creator authors cost with edit_inventory alone;
    the set_inventory_prices gate re-arms at commit. Shared by the three cost surfaces
    (post_item at creation, patch_item, set_item_price) so they cannot drift out of sync."""
    return is_draft and role_has_permission(settings, role, "edit_inventory")


def is_cost_price_type(price_type: str) -> bool:
    """True when a set_item_price price_type addresses the cost list, in either the
    primitive-key form (cost_price/cost_total) or a list-name-derived key (e.g.
    landed_price for a 'Landed' cost list)."""
    if price_type in COST_ITEM_KEYS:
        return True
    name = price_type[:-len("_price")] if price_type.endswith("_price") else price_type
    return is_cost_list_name(name)


@router.patch("/{entity_id}")
async def patch_item(entity_id: str, payload: ItemPatch, company_id=Depends(get_current_company_id), _: None = require_permission("edit_inventory"), user=Depends(get_current_user), role: str = Depends(get_current_role), settings: dict = Depends(get_current_company_settings), session: AsyncSession = Depends(get_session)) -> dict:
    # Guard: restricted fields require a role at the schema-configured floor.
    from celerp.services.field_schema import get_effective_field_schema
    field_schema = await get_effective_field_schema(session, company_id)
    restricted = {f["key"] for f in field_schema if f.get("visible_to_roles") and ROLE_LEVELS.get(role, 0) < min(ROLE_LEVELS.get(r, 0) for r in f["visible_to_roles"])}
    # Draft carve-out: the circulating-stock gates (cost + amount permissions)
    # attach when the item is committed to available, not at creation. While the
    # CURRENT status is draft, anyone with edit_inventory finishes authoring the
    # item freely; the status is re-read here on every patch, so an edit landing
    # after another user commits the item is gated like any available item.
    _proj = await session.get(Projection, {"company_id": company_id, "entity_id": entity_id})
    _is_draft = str(((_proj.state if _proj else {}) or {}).get("status") or "").lower() == "draft"
    # Cost fields are gated by set_inventory_prices, not by the schema role floor:
    # a granted operator edits cost, an ungranted manager still cannot.
    restricted -= COST_ITEM_KEYS
    if not draft_cost_carveout(_is_draft, role, settings) and not role_has_permission(settings, role, "set_inventory_prices"):
        restricted |= COST_ITEM_KEYS
    # Amount fields (quantity/weight/pieces/gross_weight) and the sell unit are
    # gated by edit_inventory_amounts, mirroring the cost gate above. sell_by is
    # included because changing it rewrites quantity, so it carries the same
    # authority; the gate fires only on a real change (blocked = changed & restricted).
    restricted -= AMOUNT_EDIT_GATED_KEYS
    if not _is_draft and not role_has_permission(settings, role, "edit_inventory_amounts"):
        restricted |= AMOUNT_EDIT_GATED_KEYS
    changed_keys = set(payload.fields_changed.keys())
    if "status" in changed_keys:
        _new_status = (payload.fields_changed["status"] or {}).get("new")
        await reject_draft_status_change_via_generic_path(session, company_id, entity_id, _new_status)
        await assert_status_change_allowed(session, company_id, entity_id, _new_status, role, settings)
    blocked = changed_keys & restricted
    if blocked:
        raise HTTPException(status_code=403, detail=f"Role '{role}' cannot modify restricted fields: {sorted(blocked)}")

    # Derived price lists are computed from the base price list; their keys are never stored.
    # Both the conventional key ("trade_price") and the raw list name ("Trade") are blocked:
    # resolve_price honors a direct-name key first, so storing one would shadow the formula.
    _price_lists, _base_name, _ = await get_price_config(session, company_id)
    _derived = derived_price_keys(_price_lists)
    derived_blocked = {k for k in changed_keys if k in _derived or price_key(k) in _derived}
    if derived_blocked:
        raise HTTPException(
            status_code=422,
            detail=f"{sorted(derived_blocked)} are computed from the '{_base_name}' price list; "
                   f"edit the base price, or change the factor in Settings",
        )

    # Normalize "clear" gestures: an empty-string new value means "unset the field" -> None (issue #202).
    # Without this, an optional field can't be returned to None — barcode rejects "" (digit check), cost
    # crashes on float(""), and prices/category/text store "" instead of clearing. Required fields keep
    # their own handling. The projection handler then removes the key when new is None.
    _CLEAR_PROTECTED = {"name", "sell_by", "quantity"}
    for _f, _fc in payload.fields_changed.items():
        if _f not in _CLEAR_PROTECTED and isinstance(_fc, dict) and _fc.get("new") == "":
            _fc["new"] = None

    # Price values must be finite numbers: a non-numeric value stored on a base list would
    # make every derived read treat that item as unpriced, and NaN/Infinity break the
    # Decimal arithmetic downstream.
    for _f, _fc in payload.fields_changed.items():
        if (_f.endswith("_price") or _f == "cost_total") and isinstance(_fc, dict):
            _new = _fc.get("new")
            if _new is not None and coerce_price(_new) is None:
                raise HTTPException(status_code=422, detail=f"'{_f}' must be a number")

    # Validate sell_by change
    if "sell_by" in changed_keys:
        new_sell_by = (payload.fields_changed["sell_by"] or {}).get("new")
        if new_sell_by:
            units = await _get_company_units(session, company_id)
            unit_map = {u["name"]: u for u in units}
            if new_sell_by not in unit_map:
                raise HTTPException(status_code=422, detail=f"sell_by '{new_sell_by}' is not a valid unit name")

    # Validate quantity change against current sell_by unit.
    # Also sync derived weight/pieces field: if sell_by is a weight unit,
    # weight tracks quantity directly; if sell_by is a pieces unit, pieces tracks it.
    if "quantity" in changed_keys:
        new_qty_raw = (payload.fields_changed["quantity"] or {}).get("new")
        if new_qty_raw is not None:
            row = await session.get(Projection, {"company_id": company_id, "entity_id": entity_id})
            if row:
                current_sell_by = row.state.get("sell_by")
                units = await _get_company_units(session, company_id)
                unit_map = {u["name"]: u for u in units}
                if current_sell_by and current_sell_by in unit_map:
                    validate_quantity(float(new_qty_raw), unit_map[current_sell_by]["decimals"])
                    new_qty = float(new_qty_raw)
                    if is_weight_unit(current_sell_by, unit_map):
                        payload.fields_changed["weight"] = {
                            "old": row.state.get("weight"),
                            "new": new_qty,
                        }
                    elif is_pieces_unit(current_sell_by, unit_map):
                        old_pieces = (row.state.get("attributes") or {}).get("pieces")
                        payload.fields_changed["pieces"] = {
                            "old": old_pieces,
                            "new": int(round(new_qty)),
                        }

    # SKU uniqueness is intentionally NOT enforced on patch: `sku` is a product-type
    # that may repeat across physical lots (barcode/entity_id carry lot identity).

    # Validate barcode format + uniqueness if changing
    if "barcode" in changed_keys:
        new_barcode = (payload.fields_changed["barcode"] or {}).get("new")
        if new_barcode is not None:
            if not str(new_barcode).isdigit():
                raise HTTPException(status_code=422, detail="Barcode must contain digits only")
            existing_barcode = (await session.execute(
                select(Projection).where(
                    Projection.company_id == company_id,
                    Projection.entity_type == "item",
                    Projection.state["barcode"].as_string() == str(new_barcode),
                    Projection.entity_id != entity_id,
                )
            )).scalars().first()
            if existing_barcode:
                raise HTTPException(status_code=409, detail=f"Barcode '{new_barcode}' already exists")

    # Validate inventory_type if changing
    if "inventory_type" in changed_keys:
        new_inv_type = (payload.fields_changed["inventory_type"] or {}).get("new")
        if new_inv_type not in VALID_INVENTORY_TYPES:
            raise HTTPException(status_code=422, detail=f"inventory_type must be one of {sorted(VALID_INVENTORY_TYPES)}")

    # Validate landed_cost_kind if changing
    if "landed_cost_kind" in changed_keys:
        new_kind = (payload.fields_changed["landed_cost_kind"] or {}).get("new")
        if new_kind is not None and new_kind not in LANDED_COST_KINDS:
            raise HTTPException(status_code=422, detail=f"landed_cost_kind must be one of {sorted(LANDED_COST_KINDS)}")

    # Validate weight is non-negative
    if "weight" in changed_keys:
        new_weight = (payload.fields_changed["weight"] or {}).get("new")
        if new_weight is not None and float(new_weight) < 0:
            raise HTTPException(status_code=422, detail="Weight cannot be negative")

    # The other amount fields are non-negative too (weight handled above).
    for _amt in ("quantity", "pieces", "gross_weight"):
        if _amt in changed_keys:
            _amt_new = (payload.fields_changed[_amt] or {}).get("new")
            if _amt_new is not None and float(_amt_new) < 0:
                raise HTTPException(status_code=422, detail=f"{_amt} cannot be negative")

    # sell_by sync: when sell_by changes unit type, pull the companion field into quantity.
    # quantity always means "how many sell_by units" — so switching piece→carat should set
    # quantity = stored weight (the existing carat value), not carry over the piece count.
    # weight and pieces are independent fields and must never be overwritten here.
    if "sell_by" in changed_keys:
        new_sell_by = (payload.fields_changed["sell_by"] or {}).get("new")
        if new_sell_by:
            _sync_row = await session.get(Projection, {"company_id": company_id, "entity_id": entity_id})
            if _sync_row:
                _sync_units = await _get_company_units(session, company_id)
                _sync_unit_map = {u["name"]: u for u in _sync_units}
                old_qty = _sync_row.state.get("quantity")
                if is_weight_unit(new_sell_by, _sync_unit_map):
                    new_qty = _sync_row.state.get("weight")   # may be None — quantity is unknown until user sets it
                    payload.fields_changed["quantity"] = {"old": old_qty, "new": new_qty}
                elif is_pieces_unit(new_sell_by, _sync_unit_map):
                    raw_pieces = (_sync_row.state.get("attributes") or {}).get("pieces")
                    new_qty = int(raw_pieces) if raw_pieces is not None else None
                    payload.fields_changed["quantity"] = {"old": old_qty, "new": new_qty}

    entry = await emit_event(
        session,
        company_id=company_id,
        entity_id=entity_id,
        entity_type="item",
        event_type="item.updated",
        data=payload.model_dump(exclude_none=True),
        actor_id=user.id,
        location_id=None,
        source="api",
        idempotency_key=payload.idempotency_key or str(uuid.uuid4()),
        metadata_={},
    )
    await session.commit()
    return {"event_id": entry.id}


class BulkStatusBody(BaseModel):
    entity_ids: list[str]
    status: str


class BulkTransferBody(BaseModel):
    entity_ids: list[str]
    to_location_id: uuid.UUID


class BulkDeleteBody(BaseModel):
    entity_ids: list[str]


@router.post("/bulk/status")
async def bulk_set_status(payload: BulkStatusBody, company_id=Depends(get_current_company_id), _: None = require_permission("adjust_inventory"), user=Depends(get_current_user), role: str = Depends(get_current_role), settings: dict = Depends(get_current_company_settings), session: AsyncSession = Depends(get_session)) -> dict:
    if not payload.entity_ids:
        raise HTTPException(status_code=422, detail="entity_ids must not be empty")
    # Validated per item BEFORE any event is emitted: one blocked item rejects the
    # whole bulk with the reason, nothing is half-applied (the session never commits).
    for entity_id in payload.entity_ids:
        await reject_draft_status_change_via_generic_path(session, company_id, entity_id, payload.status)
        await assert_status_change_allowed(session, company_id, entity_id, payload.status, role, settings)
    event_ids = []
    for entity_id in payload.entity_ids:
        entry = await emit_event(
            session,
            company_id=company_id,
            entity_id=entity_id,
            entity_type="item",
            event_type="item.status.set",
            data={"new_status": payload.status},
            actor_id=user.id,
            location_id=None,
            source="api",
            idempotency_key=str(uuid.uuid4()),
            metadata_={},
        )
        event_ids.append(entry.id)
    await session.commit()
    return {"updated": len(event_ids), "event_ids": event_ids}


class MakeAvailableBody(BaseModel):
    entity_ids: list[str]


class RevertToDraftBody(BaseModel):
    entity_ids: list[str]
    reason: str | None = None


@router.post("/bulk/make-available")
async def bulk_make_available(payload: MakeAvailableBody, company_id=Depends(get_current_company_id), _: None = require_permission("edit_inventory"), user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    """Commit one or more drafts into stock. Same authority as authoring the draft (edit_inventory) - no extra permission."""
    if not payload.entity_ids:
        raise HTTPException(status_code=422, detail="entity_ids must not be empty")
    for entity_id in payload.entity_ids:
        await assert_make_available_allowed(session, company_id, entity_id)
    event_ids = []
    for entity_id in payload.entity_ids:
        entry = await emit_event(
            session,
            company_id=company_id,
            entity_id=entity_id,
            entity_type="item",
            event_type="item.status.set",
            data={"new_status": "available"},
            actor_id=user.id,
            location_id=None,
            source="api",
            idempotency_key=str(uuid.uuid4()),
            metadata_={},
        )
        event_ids.append(entry.id)
    await session.commit()
    return {"updated": len(event_ids), "event_ids": event_ids}


@router.post("/bulk/revert-to-draft")
async def bulk_revert_to_draft(payload: RevertToDraftBody, company_id=Depends(get_current_company_id), _: None = require_permission("edit_inventory"), user=Depends(get_current_user), role: str = Depends(get_current_role), settings: dict = Depends(get_current_company_settings), session: AsyncSession = Depends(get_session)) -> dict:
    """assert_status_change_allowed does the real gating (revert_items_to_draft + clean history)."""
    if not payload.entity_ids:
        raise HTTPException(status_code=422, detail="entity_ids must not be empty")
    for entity_id in payload.entity_ids:
        await assert_status_change_allowed(session, company_id, entity_id, "draft", role, settings)
    event_ids = []
    for entity_id in payload.entity_ids:
        entry = await emit_event(
            session,
            company_id=company_id,
            entity_id=entity_id,
            entity_type="item",
            event_type="item.status.set",
            data={"new_status": "draft", "reason": payload.reason},
            actor_id=user.id,
            location_id=None,
            source="api",
            idempotency_key=str(uuid.uuid4()),
            metadata_={},
        )
        event_ids.append(entry.id)
    await session.commit()
    return {"updated": len(event_ids), "event_ids": event_ids}


class BulkShopifySyncBody(BaseModel):
    entity_ids: list[str]
    enable: bool = True


@router.post("/bulk/shopify-sync")
async def bulk_shopify_sync(payload: BulkShopifySyncBody, company_id=Depends(get_current_company_id), _: None = require_permission("adjust_inventory"), user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    """Opt the selected items into (or out of) outbound Shopify sync by emitting
    shop.sync.enabled/disabled, which sets is_sync_to_shopify on each item's projection."""
    if not payload.entity_ids:
        raise HTTPException(status_code=422, detail="entity_ids must not be empty")
    event_type = "shop.sync.enabled" if payload.enable else "shop.sync.disabled"
    event_ids = []
    for entity_id in payload.entity_ids:
        entry = await emit_event(
            session,
            company_id=company_id,
            entity_id=entity_id,
            entity_type="item",
            event_type=event_type,
            data={},
            actor_id=user.id,
            location_id=None,
            source="api",
            idempotency_key=str(uuid.uuid4()),
            metadata_={},
        )
        event_ids.append(entry.id)
    await session.commit()
    return {"updated": len(event_ids), "enabled": payload.enable}


async def _build_transfer_data(session, company_id, entity_id: str, to_location_id, loc_map: dict | None = None) -> dict:
    """item.transferred payload with from/to ids + resolved names for 'from -> to' history.

    The source location is the item's current ``location_id`` (read before the event lands).
    ``loc_map`` (id -> name) can be passed to avoid re-querying in a bulk loop.
    """
    from celerp.models.company import Location
    if loc_map is None:
        loc_rows = (await session.execute(select(Location).where(Location.company_id == company_id))).scalars().all()
        loc_map = {str(r.id): r.name for r in loc_rows}
    proj = await session.get(Projection, {"company_id": company_id, "entity_id": entity_id})
    raw_from = proj.state.get("location_id") if proj else None
    from_id = str(raw_from) if raw_from else None
    to_id = str(to_location_id)
    return {
        "to_location_id": to_id,
        "to_location_name": loc_map.get(to_id),
        "from_location_id": from_id,
        "from_location_name": loc_map.get(from_id) if from_id else None,
    }


@router.post("/bulk/transfer")
async def bulk_transfer(payload: BulkTransferBody, company_id=Depends(get_current_company_id), _: None = require_permission("edit_inventory"), user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    if not payload.entity_ids:
        raise HTTPException(status_code=422, detail="entity_ids must not be empty")
    from celerp.models.company import Location
    loc_rows = (await session.execute(select(Location).where(Location.company_id == company_id))).scalars().all()
    loc_map = {str(r.id): r.name for r in loc_rows}
    event_ids = []
    for entity_id in payload.entity_ids:
        entry = await emit_event(
            session,
            company_id=company_id,
            entity_id=entity_id,
            entity_type="item",
            event_type="item.transferred",
            data=await _build_transfer_data(session, company_id, entity_id, payload.to_location_id, loc_map),
            actor_id=user.id,
            location_id=payload.to_location_id,
            source="api",
            idempotency_key=str(uuid.uuid4()),
            metadata_={},
        )
        event_ids.append(entry.id)
    await session.commit()
    return {"updated": len(event_ids), "event_ids": event_ids}


@router.post("/bulk/delete")
async def bulk_delete(payload: BulkDeleteBody, company_id=Depends(get_current_company_id), _: None = require_permission("adjust_inventory"), user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    if not payload.entity_ids:
        raise HTTPException(status_code=422, detail="entity_ids must not be empty")
    import sqlalchemy as _sa
    from celerp.models.projections import Projection as _Proj
    from celerp.models.ledger import LedgerEntry as _LE
    # Hard delete: remove projection rows and all ledger events for these items.
    # This is the correct behaviour for a user-initiated "Delete" action —
    # the item should vanish from the catalog entirely (hard delete, no event trail).
    await session.execute(
        _sa.delete(_Proj).where(
            _Proj.company_id == company_id,
            _Proj.entity_id.in_(payload.entity_ids),
        )
    )
    await session.execute(
        _sa.delete(_LE).where(
            _LE.company_id == company_id,
            _LE.entity_id.in_(payload.entity_ids),
        )
    )
    await session.commit()
    return {"deleted": len(payload.entity_ids)}


class BulkExpireBody(BaseModel):
    entity_ids: list[str]


@router.post("/bulk/expire")
async def bulk_expire(payload: BulkExpireBody, company_id=Depends(get_current_company_id), _: None = require_permission("adjust_inventory"), user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    if not payload.entity_ids:
        raise HTTPException(status_code=422, detail="entity_ids must not be empty")
    for eid in payload.entity_ids:
        await assert_not_draft(session, company_id, eid, "expire")
    for eid in payload.entity_ids:
        await emit_event(
            session,
            company_id=company_id,
            entity_id=eid,
            entity_type="item",
            event_type="item.expired",
            data={},
            actor_id=user.id,
            location_id=None,
            source="api",
            idempotency_key=str(uuid.uuid4()),
            metadata_={},
        )
    await session.commit()
    return {"expired": len(payload.entity_ids)}



@router.post("/{entity_id}/transfer")
async def transfer_item(entity_id: str, payload: TransferBody, company_id=Depends(get_current_company_id), _: None = require_permission("edit_inventory"), user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    entry = await emit_event(
        session,
        company_id=company_id,
        entity_id=entity_id,
        entity_type="item",
        event_type="item.transferred",
        data=await _build_transfer_data(session, company_id, entity_id, payload.to_location_id),
        actor_id=user.id,
        location_id=payload.to_location_id,
        source="api",
        idempotency_key=payload.idempotency_key or str(uuid.uuid4()),
        metadata_={},
    )
    await session.commit()
    return {"event_id": entry.id}


@router.get("/{entity_id}/split-preview")
async def split_preview(
    entity_id: str,
    child_sku: str | None = None,
    company_id=Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    parent = await session.get(Projection, {"company_id": company_id, "entity_id": entity_id})
    if parent is None or not is_item_available(parent.state):
        raise HTTPException(status_code=404, detail="Item not found or unavailable")

    parent_qty = float(parent.state.get("quantity") or 0)
    if parent_qty <= 0:
        raise HTTPException(status_code=422, detail="parent qty must be > 0")

    parent_sku = parent.state.get("sku", "")
    parent_sell_by = parent.state.get("sell_by") or "piece"
    parent_weight = _read_float(parent.state, "weight")
    parent_pieces = _read_pieces(parent.state)

    units = await _get_company_units(session, company_id)
    unit_map = {u["name"]: u for u in units}
    unit_cfg = unit_map.get(parent_sell_by) or {}
    decimals = unit_cfg.get("decimals", 0)
    sell_by_label = unit_cfg.get("label", parent_sell_by)

    # For weight-unit items, qty IS the weight — mirror like pieces for piece-unit items.
    if is_weight_unit(parent_sell_by, unit_map):
        parent_weight = parent_qty

    # When sell_by is itself a weight unit, weight_unit may be unset on the item.
    # Default to the sell_by unit so the UI labels weights correctly.
    parent_weight_unit = parent.state.get("weight_unit") or (parent_sell_by if is_weight_unit(parent_sell_by, unit_map) else "gram")
    weight_unit_cfg = unit_map.get(parent_weight_unit) or {}
    weight_decimals = weight_unit_cfg.get("decimals", 2)

    if not child_sku:
        # Child keeps the parent SKU (same product; distinct lot by barcode/entity_id).
        child_sku = parent_sku

    weight_unit_names = [u["name"] for u in units if u.get("unit_type") == "weight"]

    result: dict = {
        "parent_sku": parent_sku,
        "parent_name": parent.state.get("name", parent_sku),
        "parent_qty": parent_qty,
        "child_sku": child_sku,
        "sell_by": parent_sell_by,
        "sell_by_label": sell_by_label,
        "sell_by_type": "weight" if is_weight_unit(parent_sell_by, unit_map) else ("pieces" if is_pieces_unit(parent_sell_by, unit_map) else "other"),
        "weight_unit": parent_weight_unit,
        "weight_unit_label": weight_unit_cfg.get("label", parent_weight_unit),
        "unit_decimals": decimals,
        "weight_decimals": weight_decimals,
        "has_weight": parent_weight is not None,
        "has_pieces": parent_pieces is not None,
        "cannot_split": (decimals == 0 and parent_qty <= 1),
        "weight_unit_names": weight_unit_names,
    }

    if parent_weight is not None:
        result["parent_weight"] = parent_weight
    if parent_pieces is not None:
        result["parent_pieces"] = int(parent_pieces)

    return result


@router.post("/{entity_id}/split")
async def split_item(entity_id: str, payload: SplitBody, company_id=Depends(get_current_company_id), _: None = require_permission("edit_inventory"), role: str = Depends(get_current_role), settings: dict = Depends(get_current_company_settings), user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    # Fetch parent
    parent = await session.get(Projection, {"company_id": company_id, "entity_id": entity_id})
    if parent is None or not is_item_available(parent.state):
        raise HTTPException(status_code=404, detail="Item not found or unavailable")

    # Allow splitting ONLY when explicitly enabled (== True). A missing/None value
    # (e.g. older imports that never set the field) must NOT bypass this gate.
    if parent.state.get("allow_splitting") is not True:
        raise HTTPException(
            status_code=422,
            detail="Allow splitting is set to No for this item. Change Allow Splitting to Yes in the item details to enable splitting.",
        )

    parent_qty = float(parent.state.get("quantity") or 0)
    parent_sell_by = parent.state.get("sell_by") or "piece"
    parent_location_id = parent.state.get("location_id")
    parent_attrs = dict(parent.state.get("attributes") or {})

    # Price fields to preserve on children via pricing events (cost is split proportionally)
    parent_prices = {k: parent.state[k] for k in parent.state if k.endswith("_price") and parent.state[k] is not None and k != "cost_price"}
    parent_cost_total = float(parent.state.get("cost_total") or 0) or (
        float(parent.state.get("cost_price") or 0) * parent_qty
    )

    units = await _get_company_units(session, company_id)
    unit_map = {u["name"]: u for u in units}
    unit_cfg = unit_map.get(parent_sell_by)
    decimals = unit_cfg["decimals"] if unit_cfg else 0

    # Pieces are PARTITIONED across a split, never copied wholesale (issue #223):
    # a child gets pieces only from an explicit per-child count, or, for a
    # piece-unit item, its own quantity. Never inherited from the mother.
    parent_is_pieces_unit = is_pieces_unit(parent_sell_by, unit_map)
    parent_attrs_wo_pieces = {k: v for k, v in parent_attrs.items() if k != "pieces"}

    parent_weight: float | None = _read_float(parent.state, "weight")
    parent_is_weight_unit = is_weight_unit(parent_sell_by, unit_map)
    # No weight was ever recorded (e.g. a CSV import that never synced it from
    # quantity): fall back to qty, same as split_preview, so the mother-weight
    # column the preview showed doesn't demand edit_inventory_amounts to confirm
    # the exact value it already displayed. A genuinely stored weight (which may
    # legitimately differ from qty) is never overridden.
    parent_weight_is_derived = parent_weight is None and parent_is_weight_unit
    if parent_weight_is_derived:
        parent_weight = parent_qty
    parent_weight_unit = parent.state.get("weight_unit") or (parent_sell_by if parent_is_weight_unit else "gram")
    weight_unit_cfg = unit_map.get(parent_weight_unit) or {}
    weight_decimals = weight_unit_cfg.get("decimals", 2)

    children = payload.children
    if len(children) < 1:
        raise HTTPException(status_code=422, detail="Split requires at least 1 child")

    # Validate each child quantity and weight
    for child in children:
        validate_quantity(child.quantity, decimals)
        if child.weight is not None and child.weight < 0:
            raise HTTPException(status_code=422, detail="Child weight cannot be negative")

    # Validate total <= parent qty (100% consumption allowed - parent will be archived)
    total_child_qty = sum(c.quantity for c in children)
    if round(total_child_qty, 10) > round(parent_qty, 10):
        raise HTTPException(
            status_code=422,
            detail=f"Child quantities ({total_child_qty}) exceed parent quantity ({parent_qty})",
        )

    # Normalise: top-level pieces field → attributes so all downstream reads are uniform
    for child in children:
        if child.pieces is not None:
            child.attributes = {**child.attributes, "pieces": child.pieces}

    # Pieces conservation: if parent has pieces, validate and compute mother
    _pieces_float = _read_pieces(parent.state)
    parent_pieces: int | None = _to_int_pieces(_pieces_float) if _pieces_float is not None else None
    if parent_pieces is not None:
        total_child_pieces = sum(_to_int_pieces(c.attributes.get("pieces", 0)) for c in children)
        if total_child_pieces > parent_pieces:
            raise HTTPException(
                status_code=422,
                detail=f"Total child pieces ({total_child_pieces}) must not exceed parent pieces ({parent_pieces})",
            )

    # A split child is the SAME product as its parent, so it keeps the parent SKU unless
    # the caller explicitly names a different one — this is the single source of truth for
    # that rule (split_preview only *suggests* it; the UI sends no SKU). Lots are told apart
    # by their own unique barcode / entity_id; SKUs repeat across lots, so children may
    # share the parent's SKU and each other's — no uniqueness or parent-difference guard.
    parent_sku = parent.state.get("sku")
    child_skus = [c.sku or parent_sku for c in children]

    # Create child items
    child_eids: list[str] = []
    child_qty_list: list[float] = []
    next_barcode_seq = await _next_seq(session, company_id)  # single DB scan; incremented in-memory per child

    # Pre-compute child cost_totals using unit cost invariant: cost_price is the same for
    # parent and child, so child_cost_total = (parent_cost_total / parent_qty) * child_qty.
    # This is correct for partial splits; no remainder redistribution needed.
    _child_cost_totals: list[float | None]
    if parent_cost_total and parent_qty:
        _D_unit_cost = Decimal(str(parent_cost_total)) / Decimal(str(parent_qty))
        _child_cost_totals = [
            float((_D_unit_cost * Decimal(str(c.quantity))).quantize(Decimal("0.0000000001")))
            for c in children
        ]
    else:
        _child_cost_totals = [None] * len(children)

    def _child_weight(c) -> float | None:
        if c.weight is not None:
            return c.weight
        return c.quantity if parent_is_weight_unit else None

    # Per-child history descriptors with sequential mother deltas (one row per child).
    children_detail: list[dict] = []
    running_qty = parent_qty
    running_pieces = parent_pieces
    running_weight = parent_weight
    running_cost = parent_cost_total

    for i, child in enumerate(children):
        child_eid = f"item:{uuid.uuid4()}"
        child_eids.append(child_eid)
        child_qty_list.append(child.quantity)
        # Copy-all-then-override: inherit every parent field; reset only identity/qty/cost/status.
        child_data: dict = {
            k: v for k, v in parent.state.items() if k not in _CHILD_RESET_FIELDS
        }
        # Pieces are never inherited from the mother: an explicit per-child count
        # (already merged into child.attributes) or, for a piece-unit item, the
        # child's own quantity. Otherwise the child carries no pieces.
        _child_attrs = {**parent_attrs_wo_pieces, **child.attributes}
        if parent_is_pieces_unit:
            _child_attrs["pieces"] = _to_int_pieces(child.quantity)
        child_data.update({
            "sku": child_skus[i],
            "name": parent.state.get("name", child_skus[i]),
            "quantity": child.quantity,
            "status": "available",
            "attributes": _child_attrs,
            "barcode": child.barcode if child.barcode is not None else str(next_barcode_seq).zfill(6),
        })
        if child.weight is not None:
            child_data["weight"] = child.weight
        next_barcode_seq += 1
        await emit_event(
            session,
            company_id=company_id,
            entity_id=child_eid,
            entity_type="item",
            event_type="item.created",
            data=child_data,
            actor_id=user.id,
            location_id=_parse_uuid(parent_location_id),
            source="api",
            idempotency_key=str(uuid.uuid4()),
            metadata_={"parent_id": entity_id},
        )

        # Origin marker on the child: "Split from <mother>" — the child's first history entry.
        ch_pieces = _to_int_pieces(child.attributes.get("pieces", 0)) if parent_pieces is not None else None
        ch_weight = _child_weight(child) if parent_weight is not None else None
        origin = await emit_event(
            session,
            company_id=company_id,
            entity_id=child_eid,
            entity_type="item",
            event_type="item.split_from",
            data={
                "parent_id": entity_id,
                "parent_sku": parent_sku or "",
                "qty": child.quantity,
                "pieces": ch_pieces,
                "weight": ch_weight,
            },
            actor_id=user.id,
            location_id=_parse_uuid(parent_location_id),
            source="api",
            idempotency_key=str(uuid.uuid4()),
            metadata_={"reason": "from_split"},
        )

        # Accumulate the mother's sequential delta for this child (one history row per child).
        detail: dict = {
            "child_id": child_eid,
            "child_sku": child.sku,
            "origin_event_id": origin.id,
            "qty_before": running_qty,
            "qty_after": round(running_qty - child.quantity, 10),
        }
        running_qty = detail["qty_after"]
        if parent_pieces is not None:
            detail["pieces_before"] = running_pieces
            running_pieces = (running_pieces or 0) - (ch_pieces or 0)
            detail["pieces_after"] = running_pieces
        if parent_weight is not None:
            detail["weight_before"] = running_weight
            running_weight = round((running_weight or 0) - (ch_weight or 0), weight_decimals)
            detail["weight_after"] = running_weight
        if parent_cost_total and _child_cost_totals[i] is not None:
            detail["cost_before"] = running_cost
            running_cost = round(running_cost - _child_cost_totals[i], 10)
            detail["cost_after"] = running_cost
        children_detail.append(detail)

        # Preserve prices from parent via pricing events (excluding cost - set proportionally below)
        for price_type, price_val in parent_prices.items():
            await emit_event(
                session,
                company_id=company_id,
                entity_id=child_eid,
                entity_type="item",
                event_type="item.pricing.set",
                data={"price_type": price_type, "new_price": price_val},
                actor_id=user.id,
                location_id=None,
                source="api",
                idempotency_key=str(uuid.uuid4()),
                metadata_={"reason": "from_split"},
            )
        # Assign proportional cost_total to child (pre-computed with Decimal; remainder in last child)
        if _child_cost_totals[i] is not None:
            await emit_event(
                session,
                company_id=company_id,
                entity_id=child_eid,
                entity_type="item",
                event_type="item.pricing.set",
                data={"price_type": "cost_total", "new_price": _child_cost_totals[i]},
                actor_id=user.id,
                location_id=None,
                source="api",
                idempotency_key=str(uuid.uuid4()),
                metadata_={"reason": "from_split"},
            )

    total_child_weight = sum(w for c in payload.children if (w := _child_weight(c)) is not None)
    derived_parent_qty = round(parent_qty - total_child_qty, 10)
    derived_mother_weight = (
        round(parent_weight - total_child_weight, weight_decimals) if parent_weight is not None else None
    )
    # A hand-set mother amount (a re-weigh) that diverges from the server-derived
    # remainder is a gated amount edit (edit_inventory_amounts). The derived pass-through
    # (the bulk preview posts the derived value) and the split itself stay on
    # edit_inventory, so an operator can always split without the amount permission.
    if payload.mother_qty is not None:
        if payload.mother_qty < 0:
            raise HTTPException(status_code=422, detail="Mother quantity cannot be negative")
        if round(payload.mother_qty, 10) != derived_parent_qty and not role_has_permission(settings, role, "edit_inventory_amounts"):
            raise HTTPException(status_code=403, detail=f"Role '{role}' cannot hand-set the mother quantity: requires the edit_inventory_amounts permission")
    if payload.mother_weight is not None:
        if payload.mother_weight < 0:
            raise HTTPException(status_code=422, detail="Mother weight cannot be negative")
        if (derived_mother_weight is None or round(payload.mother_weight, weight_decimals) != derived_mother_weight) and not role_has_permission(settings, role, "edit_inventory_amounts"):
            raise HTTPException(status_code=403, detail=f"Role '{role}' cannot hand-set the mother weight: requires the edit_inventory_amounts permission")
    new_parent_qty = payload.mother_qty if payload.mother_qty is not None else derived_parent_qty
    # Clamp sub-epsilon residuals from float subtraction to an exact zero (derived branch
    # only; a submitted negative override was rejected above, never silently zeroed).
    if payload.mother_qty is None and new_parent_qty < 0:
        new_parent_qty = 0.0
    await emit_event(
        session,
        company_id=company_id,
        entity_id=entity_id,
        entity_type="item",
        event_type="item.quantity.adjusted",
        data={"new_qty": new_parent_qty},
        actor_id=user.id,
        location_id=None,
        source="api",
        idempotency_key=str(uuid.uuid4()),
        metadata_={"reason": "split_parent"},
    )

    # Update parent cost_total (reduce by sum of child cost_totals; pre-computed values guarantee conservation)
    if parent_cost_total and parent_qty:
        total_child_cost = sum(c for c in _child_cost_totals if c is not None)
        parent_remaining_cost = max(0.0, round(parent_cost_total - total_child_cost, 10))
        await emit_event(
            session,
            company_id=company_id,
            entity_id=entity_id,
            entity_type="item",
            event_type="item.pricing.set",
            data={"price_type": "cost_total", "new_price": parent_remaining_cost},
            actor_id=user.id,
            location_id=None,
            source="api",
            idempotency_key=str(uuid.uuid4()),
            metadata_={"reason": "split_parent"},
        )

    # Apply mother parcel overrides: weight computed server-side, pieces computed server-side
    computed_mother_pieces: int | None = None
    clear_mother_pieces = False
    if parent_pieces is not None:
        if parent_is_pieces_unit:
            # Pieces track quantity for a piece-unit item.
            computed_mother_pieces = _to_int_pieces(new_parent_qty)
        elif any(c.attributes.get("pieces") is not None for c in children):
            total_child_pieces = sum(_to_int_pieces(c.attributes.get("pieces", 0)) for c in children)
            computed_mother_pieces = parent_pieces - total_child_pieces
        else:
            # No per-pile counts were given, so how the pieces divide is unknown.
            # Clear the mother's count rather than keeping the full total, which
            # would duplicate it against children that carry none (issue #223).
            clear_mother_pieces = True

    computed_mother_weight: float | None = None
    if payload.mother_weight is not None:
        # User explicitly re-weighed the mother parcel — use that value directly.
        computed_mother_weight = round(payload.mother_weight, weight_decimals)
    elif parent_weight is not None:
        computed_mother_weight = derived_mother_weight

    if computed_mother_weight is not None or computed_mother_pieces is not None or clear_mother_pieces:
        fields_changed: dict[str, dict] = {}
        if computed_mother_weight is not None:
            fields_changed["weight"] = {"old": parent.state.get("weight"), "new": computed_mother_weight}
        if computed_mother_pieces is not None or clear_mother_pieces:
            new_attrs = dict(parent_attrs)
            if clear_mother_pieces:
                new_attrs.pop("pieces", None)
            else:
                new_attrs["pieces"] = computed_mother_pieces
            fields_changed["attributes"] = {"old": parent_attrs, "new": new_attrs}
        await emit_event(
            session,
            company_id=company_id,
            entity_id=entity_id,
            entity_type="item",
            event_type="item.updated",
            data={"fields_changed": fields_changed},
            actor_id=user.id,
            location_id=None,
            source="api",
            idempotency_key=str(uuid.uuid4()),
            metadata_={"reason": "split_parent"},
        )

    # If parent quantity is now 0, mark as archived (consumed by split)
    if new_parent_qty == 0:
        await emit_event(
            session,
            company_id=company_id,
            entity_id=entity_id,
            entity_type="item",
            event_type="item.status.set",
            data={"new_status": "archived"},
            actor_id=user.id,
            location_id=None,
            source="api",
            idempotency_key=str(uuid.uuid4()),
            metadata_={"reason": "consumed_by_split"},
        )

    # Delta: weight expected to remain minus weight measured. Asserted only when a real
    # measurement exists (full consumption, measured 0, or an operator re-weigh) and
    # every child carries a weight; otherwise None (honest degradation, no false anomaly).
    if new_parent_qty == 0:
        measured_remaining: float | None = 0.0
    elif payload.mother_weight is not None:
        measured_remaining = round(payload.mother_weight, weight_decimals)
    else:
        measured_remaining = None
    if parent_weight is not None and measured_remaining is not None and all(c.weight is not None for c in payload.children):
        split_delta: float | None = round(parent_weight - total_child_weight - measured_remaining, weight_decimals)
    else:
        split_delta = None

    # Emit item.split for history
    entry = await emit_event(
        session,
        company_id=company_id,
        entity_id=entity_id,
        entity_type="item",
        event_type="item.split",
        data={
            "child_ids": child_eids,
            "child_skus": child_skus,
            "quantities": child_qty_list,
            "parent_sku": parent_sku or "",
            "children_detail": children_detail,
            "delta": split_delta,
            "weight_unit": parent_weight_unit,
        },
        actor_id=user.id,
        location_id=None,
        source="api",
        idempotency_key=payload.idempotency_key or str(uuid.uuid4()),
        metadata_={},
    )

    await session.commit()
    return {
        "event_id": entry.id,
        "children": [{"id": eid, "sku": sku} for eid, sku in zip(child_eids, child_skus)],
    }


async def split_off_child(session: AsyncSession, *, company_id, user_id, parent_proj: Projection,
                          child_qty: float, child_weight: float | None = None,
                          child_pieces: int | None = None) -> tuple[str, str]:
    """Split one child of ``child_qty`` off ``parent_proj`` → ``(child_eid, child_sku)``.

    The child keeps the parent SKU (same product; a distinct lot by barcode / entity_id)
    and is the split-off portion; the mother keeps the remainder. Cost splits
    proportionally by quantity.
    Weight and pieces come ONLY from the explicit args — no proportional fallback,
    no auto-derivation; the mother keeps ``parent - child`` for each.

    Invariants (raise ValueError if violated):
      - parcel has weight (weight-unit sell_by OR a weight attribute)
            -> child_weight is required
      - sell_by is a weight unit  -> child_weight must equal child_qty
      - parcel has pieces (piece-unit sell_by OR a pieces attribute)
            -> child_pieces is required
      - sell_by is a pieces unit  -> child_pieces must equal child_qty

    Does NOT commit — the caller owns the transaction.
    """
    parent = parent_proj
    entity_id = parent.entity_id
    parent_sku = parent.state.get("sku", "")
    parent_qty = float(parent.state.get("quantity") or 0)
    parent_attrs = dict(parent.state.get("attributes") or {})

    units = await _get_company_units(session, company_id)
    unit_map = {u["name"]: u for u in units}
    sell_by = parent.state.get("sell_by") or ""
    weight_type = is_weight_unit(sell_by, unit_map)
    pieces_type = is_pieces_unit(sell_by, unit_map)
    parent_weight = _read_float(parent.state, "weight")
    parent_pieces = _read_pieces(parent.state)

    # --- validate (no omission, no fallback) ---
    if (weight_type or parent_weight is not None) and child_weight is None:
        raise ValueError("child_weight is required: this item is weight-tracked")
    if weight_type and child_weight is not None and abs(child_weight - child_qty) > 1e-9:
        raise ValueError("for weight-sold items child_weight must equal child_qty")
    if (pieces_type or parent_pieces is not None) and child_pieces is None:
        raise ValueError("child_pieces is required: this item is piece-tracked")
    if pieces_type and child_pieces is not None and abs(child_pieces - child_qty) > 1e-9:
        raise ValueError("for piece-sold items child_pieces must equal child_qty")

    # The split child is the same product as the parent: it KEEPS the parent SKU and is
    # distinguished only by its own unique barcode / entity_id (SKUs repeat across lots).
    child_sku = parent_sku

    # Cost: proportional by quantity (unit-cost invariant).
    parent_cost_total = float(parent.state.get("cost_total") or 0) or (
        float(parent.state.get("cost_price") or 0) * parent_qty
    )
    child_cost_total: float | None = None
    if parent_cost_total and parent_qty:
        unit_cost = Decimal(str(parent_cost_total)) / Decimal(str(parent_qty))
        child_cost_total = float((unit_cost * Decimal(str(child_qty))).quantize(Decimal("0.0000000001")))

    ch_pieces = _to_int_pieces(child_pieces) if child_pieces is not None else None
    parent_prices = {
        k: parent.state[k] for k in parent.state
        if k.endswith("_price") and parent.state[k] is not None and k != "cost_price"
    }

    # --- create the child ---
    child_eid = f"item:{uuid.uuid4()}"
    next_barcode_seq = await _next_seq(session, company_id)
    child_attrs = dict(parent_attrs)
    if ch_pieces is not None:
        child_attrs["pieces"] = ch_pieces
    child_data = {k: v for k, v in parent.state.items() if k not in _CHILD_RESET_FIELDS}
    child_data.update({
        "sku": child_sku,
        "name": parent.state.get("name", child_sku),
        "quantity": child_qty,
        "status": "available",
        "attributes": child_attrs,
        "barcode": str(next_barcode_seq).zfill(6),
    })
    if child_weight is not None:
        child_data["weight"] = child_weight
    await emit_event(session, company_id=company_id, entity_id=child_eid, entity_type="item",
                     event_type="item.created", data=child_data, actor_id=user_id,
                     location_id=_parse_uuid(parent.state.get("location_id")), source="fulfill_split",
                     idempotency_key=str(uuid.uuid4()), metadata_={"parent_id": entity_id})
    # Origin marker on the child: "Split from <mother>" — the child's first history entry.
    origin = await emit_event(
        session, company_id=company_id, entity_id=child_eid, entity_type="item",
        event_type="item.split_from",
        data={"parent_id": entity_id, "parent_sku": parent_sku or "", "qty": child_qty,
              "pieces": ch_pieces if parent_pieces is not None else None,
              "weight": child_weight if parent_weight is not None else None},
        actor_id=user_id, location_id=_parse_uuid(parent.state.get("location_id")),
        source="fulfill_split", idempotency_key=str(uuid.uuid4()), metadata_={"reason": "from_split"})
    for price_type, price_val in parent_prices.items():
        await emit_event(session, company_id=company_id, entity_id=child_eid, entity_type="item",
                         event_type="item.pricing.set", data={"price_type": price_type, "new_price": price_val},
                         actor_id=user_id, location_id=None, source="fulfill_split",
                         idempotency_key=str(uuid.uuid4()), metadata_={"reason": "from_split"})
    if child_cost_total is not None:
        await emit_event(session, company_id=company_id, entity_id=child_eid, entity_type="item",
                         event_type="item.pricing.set", data={"price_type": "cost_total", "new_price": child_cost_total},
                         actor_id=user_id, location_id=None, source="fulfill_split",
                         idempotency_key=str(uuid.uuid4()), metadata_={"reason": "from_split"})

    # --- reduce the mother ---
    new_parent_qty = max(0.0, round(parent_qty - child_qty, 10))
    await emit_event(session, company_id=company_id, entity_id=entity_id, entity_type="item",
                     event_type="item.quantity.adjusted", data={"new_qty": new_parent_qty},
                     actor_id=user_id, location_id=None, source="fulfill_split",
                     idempotency_key=str(uuid.uuid4()), metadata_={"reason": "split_parent"})
    if child_cost_total is not None and parent_cost_total:
        await emit_event(session, company_id=company_id, entity_id=entity_id, entity_type="item",
                         event_type="item.pricing.set",
                         data={"price_type": "cost_total", "new_price": max(0.0, round(parent_cost_total - child_cost_total, 10))},
                         actor_id=user_id, location_id=None, source="fulfill_split",
                         idempotency_key=str(uuid.uuid4()), metadata_={"reason": "split_parent"})
    # Secondary measures are NOT conserved: the child keeps its (uncapped) value and
    # the mother floors at 0 (e.g. child weight 20 of a 15ct mother -> mother 0ct).
    fields_changed: dict[str, dict] = {}
    if child_weight is not None and parent_weight is not None:
        fields_changed["weight"] = {"old": parent.state.get("weight"), "new": max(0.0, round(parent_weight - child_weight, 10))}
    if ch_pieces is not None and parent_pieces is not None:
        new_attrs = dict(parent_attrs)
        new_attrs["pieces"] = max(0, _to_int_pieces(parent_pieces) - ch_pieces)
        fields_changed["attributes"] = {"old": parent_attrs, "new": new_attrs}
    if fields_changed:
        await emit_event(session, company_id=company_id, entity_id=entity_id, entity_type="item",
                         event_type="item.updated", data={"fields_changed": fields_changed},
                         actor_id=user_id, location_id=None, source="fulfill_split",
                         idempotency_key=str(uuid.uuid4()), metadata_={"reason": "split_parent"})

    # history
    child_detail: dict = {
        "child_id": child_eid, "child_sku": child_sku, "origin_event_id": origin.id,
        "qty_before": parent_qty, "qty_after": new_parent_qty,
    }
    if parent_pieces is not None and ch_pieces is not None:
        child_detail["pieces_before"] = _to_int_pieces(parent_pieces)
        child_detail["pieces_after"] = max(0, _to_int_pieces(parent_pieces) - ch_pieces)
    if parent_weight is not None and child_weight is not None:
        child_detail["weight_before"] = parent_weight
        child_detail["weight_after"] = max(0.0, round(parent_weight - child_weight, 10))
    if child_cost_total is not None and parent_cost_total:
        child_detail["cost_before"] = parent_cost_total
        child_detail["cost_after"] = max(0.0, round(parent_cost_total - child_cost_total, 10))
    await emit_event(session, company_id=company_id, entity_id=entity_id, entity_type="item",
                     event_type="item.split",
                     data={"child_ids": [child_eid], "child_skus": [child_sku], "quantities": [child_qty],
                           "parent_sku": parent_sku or "", "children_detail": [child_detail]},
                     actor_id=user_id, location_id=None, source="fulfill_split",
                     idempotency_key=str(uuid.uuid4()), metadata_={})
    return child_eid, child_sku


@router.post("/{entity_id}/transform")
async def transform_item(entity_id: str, payload: TransformBody, company_id=Depends(get_current_company_id), _: None = require_permission("edit_inventory"), role: str = Depends(get_current_role), settings: dict = Depends(get_current_company_settings), user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    # Fetch parent
    parent = await session.get(Projection, {"company_id": company_id, "entity_id": entity_id})
    if parent is None or not is_item_available(parent.state):
        raise HTTPException(status_code=404, detail="Item not found or unavailable")

    # Validate
    if payload.child_quantity <= 0:
        raise HTTPException(status_code=422, detail="child_quantity must be > 0")
    if not payload.child_category.strip():
        raise HTTPException(status_code=422, detail="child_category cannot be empty")

    # Validate child_sell_by against company unit map (consistent with split/patch flows)
    _transform_units = await _get_company_units(session, company_id)
    _transform_unit_map = {u["name"]: u for u in _transform_units}
    if payload.child_sell_by not in _transform_unit_map:
        raise HTTPException(status_code=422, detail=f"Unknown unit '{payload.child_sell_by}'")

    # Child SKU uniqueness against existing items is no longer enforced - SKUs may
    # repeat across physical lots; lot identity is carried by barcode / entity_id.

    parent_qty = float(parent.state.get("quantity") or 0)
    parent_attrs = dict(parent.state.get("attributes") or {})
    # Sell-price fields (retail/wholesale/custom price-list rates — every *_price except cost).
    # These are intentionally DROPPED on the child: a transform changes what the item is, so the
    # pre-transform sell price must not carry over (it would let the user accidentally sell the
    # transformed goods at the old price). The user sets the new sell price manually before selling.
    parent_price_keys = {k for k in parent.state if k.endswith("_price") and k != "cost_price"}
    parent_cost_total = float(parent.state.get("cost_total") or 0) or (
        float(parent.state.get("cost_price") or 0) * parent_qty
    )
    # Cost is gated by view_inventory_costs (the endpoint is the trust boundary, not the
    # hidden UI field): only a permitted caller who actually submitted a cost may override
    # it. Everyone else - restricted role, or no cost sent - preserves the parent's cost.
    effective_cost = (
        payload.child_cost_total
        if (role_has_permission(settings, role, "view_inventory_costs") and payload.child_cost_total is not None)
        else parent_cost_total
    )
    parent_location_id = parent.state.get("location_id")

    child_eid = f"item:{uuid.uuid4()}"

    # Copy-all-then-override: inherit every parent field; reset only identity/qty/cost/status.
    # Also override sell_by and category — the purpose of a transform is to change these.
    child_data: dict = {
        k: v for k, v in parent.state.items()
        if k not in _CHILD_RESET_FIELDS and k not in parent_price_keys
    }
    child_data.update({
        "sku": payload.child_sku,
        "name": (payload.child_name or "").strip() or parent.state.get("name", payload.child_sku),
        "quantity": payload.child_quantity,
        "sell_by": payload.child_sell_by,
        "category": payload.child_category,
        "status": "available",
        "attributes": {**parent_attrs},
        "barcode": str(await _next_seq(session, company_id)).zfill(6),
    })
    if payload.child_weight is not None:
        child_data["weight"] = payload.child_weight
    if payload.child_weight_unit:
        child_data["weight_unit"] = payload.child_weight_unit
    if payload.child_pieces is not None:
        child_data["attributes"] = {**child_data["attributes"], "pieces": payload.child_pieces}

    # 1. Create child
    await emit_event(
        session,
        company_id=company_id,
        entity_id=child_eid,
        entity_type="item",
        event_type="item.created",
        data=child_data,
        actor_id=user.id,
        location_id=_parse_uuid(parent_location_id),
        source="api",
        idempotency_key=str(uuid.uuid4()),
        metadata_={"parent_id": entity_id},
    )

    # 1b. Origin marker on the child: "Transformed from <mother>" — the child's first history entry.
    transform_origin = await emit_event(
        session,
        company_id=company_id,
        entity_id=child_eid,
        entity_type="item",
        event_type="item.transformed_from",
        data={
            "parent_id": entity_id,
            "parent_sku": parent.state.get("sku") or "",
            "qty": payload.child_quantity,
            "category": payload.child_category,
            "pieces": payload.child_pieces,
            "weight": payload.child_weight,
        },
        actor_id=user.id,
        location_id=_parse_uuid(parent_location_id),
        source="api",
        idempotency_key=str(uuid.uuid4()),
        metadata_={"reason": "from_transform"},
    )

    # 2. Sell prices are intentionally NOT copied — the child starts with no sell price (see the
    #    parent_price_keys note above). Only cost carries over.

    # 2b. Set child cost via item.pricing.set (consistent with split/post_item flows)
    await emit_event(
        session,
        company_id=company_id,
        entity_id=child_eid,
        entity_type="item",
        event_type="item.pricing.set",
        data={"price_type": "cost_total", "new_price": effective_cost},
        actor_id=user.id,
        location_id=None,
        source="api",
        idempotency_key=str(uuid.uuid4()),
        metadata_={"reason": "from_transform"},
    )

    # 4. Mark parent archived (consumed by transform)
    await emit_event(
        session,
        company_id=company_id,
        entity_id=entity_id,
        entity_type="item",
        event_type="item.status.set",
        data={"new_status": "archived"},
        actor_id=user.id,
        location_id=None,
        source="api",
        idempotency_key=str(uuid.uuid4()),
        metadata_={"reason": "consumed_by_transform"},
    )

    # 5. Emit transform event
    # Delta: processing/trim loss = mother weight in minus child weight out, recorded
    # only when both weights exist (history-only, not gated by edit_inventory_amounts).
    _t_parent_weight = _read_float(parent.state, "weight")
    _t_weight_unit = parent.state.get("weight_unit") or "gram"
    _t_weight_decimals = (_transform_unit_map.get(_t_weight_unit) or {}).get("decimals", 2)
    transform_delta = (
        round(_t_parent_weight - payload.child_weight, _t_weight_decimals)
        if (_t_parent_weight is not None and payload.child_weight is not None) else None
    )
    idempotency_key = payload.idempotency_key or str(uuid.uuid4())
    await emit_event(
        session,
        company_id=company_id,
        entity_id=entity_id,
        entity_type="item",
        event_type="item.transform",
        data={
            "child_id": child_eid,
            "child_sku": payload.child_sku,
            "child_category": payload.child_category,
            "parent_cost_total": parent_cost_total,
            "child_cost_total": effective_cost,
            "parent_sku": parent.state.get("sku") or "",
            "child_origin_event_id": transform_origin.id,
            # Mother is consumed by the transform (archived) → after-values are 0.
            "qty_before": parent_qty,
            "qty_after": 0,
            "pieces_before": _read_pieces(parent.state),
            "pieces_after": 0 if _read_pieces(parent.state) is not None else None,
            "weight_before": _read_float(parent.state, "weight"),
            "weight_after": 0 if _read_float(parent.state, "weight") is not None else None,
            "delta": transform_delta,
            "weight_unit": _t_weight_unit,
        },
        actor_id=user.id,
        location_id=None,
        source="api",
        idempotency_key=idempotency_key,
        metadata_={},
    )

    # 6. Auto JE
    await create_for_item_transform(
        session,
        company_id=company_id,
        user_id=user.id,
        parent_entity_id=entity_id,
        parent_cost_total=parent_cost_total,
        parent_category=parent.state.get("category", ""),
        child_category=payload.child_category,
    )

    await session.commit()
    return {"child_id": child_eid, "child_sku": payload.child_sku, "parent_sku": parent.state.get("sku", "")}



@router.post("/merge")
async def merge_items(payload: MergeBody, company_id=Depends(get_current_company_id), _: None = require_permission("edit_inventory"), role: str = Depends(get_current_role), settings: dict = Depends(get_current_company_settings), user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    if len(payload.source_entity_ids) < 2:
        raise HTTPException(status_code=422, detail="At least 2 source_entity_ids are required to merge.")

    # Fetch projections for all source items.
    source_projections: list[Projection] = []
    for sid in payload.source_entity_ids:
        proj = await session.get(Projection, {"company_id": company_id, "entity_id": sid})
        if proj is None:
            raise HTTPException(status_code=404, detail=f"Item '{sid}' not found.")
        if str((proj.state or {}).get("status") or "").lower() == "draft":
            raise HTTPException(status_code=422, detail=f"Cannot merge a draft item ({sid}); make it available first.")
        source_projections.append(proj)

    # Validate: all items must share the same category.
    categories = {str(p.state.get("category") or "").strip() for p in source_projections}
    if len(categories) > 1:
        raise HTTPException(
            status_code=422,
            detail=f"All items must belong to the same category to merge. Found: {sorted(categories)}.",
        )

    # Validate units: merging sums quantities (in the sell unit) and net weights (in the weight
    # unit), so the sources must agree on both - you cannot add grams to carats.
    sell_units = {str(p.state.get("sell_by") or "").strip() for p in source_projections}
    sell_units.discard("")
    if len(sell_units) > 1:
        raise HTTPException(
            status_code=422,
            detail=f"Items measured in different units cannot be merged. Found: {', '.join(sorted(sell_units))}.",
        )
    weight_units = {
        str(p.state.get("weight_unit") or "").strip()
        for p in source_projections if p.state.get("weight") not in (None, "")
    }
    weight_units.discard("")
    if len(weight_units) > 1:
        raise HTTPException(
            status_code=422,
            detail=f"Items with different weight units cannot be merged. Found: {', '.join(sorted(weight_units))}.",
        )

    # Resolve target projection (SKU/barcode/name/prices come from this source).
    target_proj = await session.get(Projection, {"company_id": company_id, "entity_id": payload.target_sku_from})
    if target_proj is None:
        raise HTTPException(status_code=422, detail=f"target_sku_from '{payload.target_sku_from}' not found.")
    if str((target_proj.state or {}).get("status") or "").lower() == "draft":
        raise HTTPException(status_code=422, detail=f"Cannot merge a draft item ({payload.target_sku_from}); make it available first.")

    def _get_expiry(proj: Projection) -> str | None:
        raw = proj.state.get("expires_at")
        if raw:
            return str(raw)[:10]
        attrs = proj.state.get("attributes") or {}
        raw_attr = attrs.get("expiry_date") or attrs.get("warranty_exp")
        return str(raw_attr)[:10] if raw_attr else None

    # Compute defaults.
    total_qty = sum(float(p.state.get("quantity") or 0) for p in source_projections)
    weights = [_read_float(p.state, "weight") for p in source_projections if p.state.get("weight") not in (None, "")]
    total_weight = sum(weights) if weights else None
    # Merged cost_total (issue #199): reconcile on the source TOTALS — if every source has a cost,
    # the merged cost is their sum; if ANY source has no cost, the true total is unknowable, so the
    # merged item carries NO cost (None) rather than silently counting the missing one as 0.
    def _src_cost_total(p: Projection):
        ct = p.state.get("cost_total")
        if ct not in (None, ""):
            return float(ct)
        cp = p.state.get("cost_price")
        if cp not in (None, ""):
            return float(cp) * float(p.state.get("quantity") or 0)
        return None  # unset
    _src_costs = [_src_cost_total(p) for p in source_projections]
    merged_cost_total = sum(_src_costs) if _src_costs and all(c is not None for c in _src_costs) else None

    expiry_dates = sorted(e for p in source_projections if (e := _get_expiry(p)))
    earliest_expiry = expiry_dates[0] if expiry_dates else None

    # Resolve attributes: collect all keys across sources.
    # Expiry-related attributes are handled separately (earliest wins); exclude from conflict resolution.
    _EXPIRY_ATTR_KEYS = frozenset({"expiry_date", "warranty_exp", "expires_at"})

    all_attr_keys: set[str] = set()
    for p in source_projections:
        all_attr_keys.update((p.state.get("attributes") or {}).keys())
    all_attr_keys -= _EXPIRY_ATTR_KEYS
    # `pieces` may live TOP-LEVEL (imports / POST /items) rather than under attributes, so the loop
    # above misses it. Add it whenever any source has a piece count so it's resolved (summed) below.
    if any(_read_pieces(p.state) is not None for p in source_projections):
        all_attr_keys.add("pieces")

    # Classify the merged category's fields by their SCHEMA type, not by the shape of their values.
    # A merge must NEVER sum a field or invent a value. Only genuinely numeric-typed fields
    # (number/money/rate) drop to no-value; every other conflicting field collapses to the "Mixed"
    # system value. Keying off the value shape (as before) misclassified custom attributes whose
    # values merely look numeric (free fields, or selects with numeric options) and silently dropped
    # them instead of showing "Mixed".
    from celerp.services.field_schema import get_effective_field_schema, MIXED_VALUE, _BASE_FIELDS
    _merge_category = (next(iter(categories), "") or "").strip() or None
    _schema = await get_effective_field_schema(session, company_id, category=_merge_category)
    _dropdown_keys = {f["key"] for f in _schema if f.get("type") in ("select", "status")}
    _numeric_keys = {f["key"] for f in _schema if f.get("type") in ("number", "money", "rate")}
    # A schema-defined category attribute (e.g. `type`, `grade`, `color`) may be stored TOP-LEVEL
    # rather than under `attributes` — a field edit / POST /items keeps it there (only `pieces`/cost
    # are normalized). The attributes-only scan above misses those keys, so the value is silently
    # DROPPED on merge (no `Mixed` on conflict, and the shared value lost when sources agree). Add
    # every schema attribute key so each is resolved below via a top-level-OR-`attributes` read.
    # Base/core fields (quantity, weight, status, …) and price columns are handled separately and
    # must NOT be treated as attributes.
    _base_field_keys = frozenset(f["key"] for f in _BASE_FIELDS)
    _schema_attr_keys = {
        f["key"] for f in _schema
        if f["key"] not in _base_field_keys and not f["key"].endswith("_price")
    }
    all_attr_keys |= (_schema_attr_keys - _EXPIRY_ATTR_KEYS)

    resolved_attrs: dict = {}
    for key in all_attr_keys:
        if key == "pieces":
            # `pieces` is an EXTENSIVE/additive count (unlike intensive numeric attributes such as
            # size or grade), so it is summed rather than collapsed. Coerce every source's value to
            # a number (int/float/str are equivalent). If every source has pieces set → the merged
            # item's pieces is the sum; if any source has it unset, the true total is unknowable, so
            # the merged item carries NO pieces. See issue #197.
            coerced = [_num_pieces(_read_pieces(p.state)) for p in source_projections]
            if coerced and all(v is not None for v in coerced):
                total = sum(coerced, Decimal(0))
                resolved_attrs["pieces"] = int(total) if total == total.to_integral_value() else float(total)
            # else: at least one source lacks pieces → omit the key (no value)
            continue
        # Collect raw attribute values (preserve original type for numeric fields). Read from
        # top-level OR `attributes` so a field-edited value (top-level) is seen just like a nested
        # one — this is what makes conflicts resolve to "Mixed" and agreements keep their value.
        raw_values = [_read_attr(p.state, key) for p in source_projections if _has_attr(p.state, key)]
        if not raw_values:
            continue  # no source carries this attribute in either location → nothing to resolve
        str_values = [str(v) for v in raw_values]
        unique_str_vals = set(str_values)
        if len(unique_str_vals) == 1:
            # No conflict — carry forward the original typed value.
            resolved_attrs[key] = raw_values[0]
        elif key in _dropdown_keys:
            # Dropdown field: never sum and never invent an option — differing sources collapse
            # to the system "Mixed" value (issue: merge must not create new dropdown values).
            resolved_attrs[key] = MIXED_VALUE
        elif key in _numeric_keys:
            # Numeric-typed field — summing invents a meaningless value (size 1 + 2 ≠ 3; 18K + 14K ≠ 32K),
            # and a numeric cell cannot render the "Mixed" label. The correct value is unknowable, so
            # the merged item carries NO value for it.
            continue  # omit the key → no value
        else:
            # Any other conflicting field (custom/free attribute, text, etc.) collapses to the "Mixed"
            # system value so the conflict stays visible instead of silently vanishing. An explicit
            # user override via resolved_attributes still wins.
            if payload.resolved_attributes and key in payload.resolved_attributes:
                resolved_attrs[key] = str(payload.resolved_attributes[key])
            else:
                resolved_attrs[key] = MIXED_VALUE

    # Apply user overrides.
    resulting_qty = payload.resulting_quantity if payload.resulting_quantity is not None else total_qty
    # Truncate float-summation noise to the sell unit's precision (e.g. 0.1 + 0.2 -> 0.3, not
    # 0.30000000000000004). All sources share one sell_by (validated above).
    _common_sell_by = next(iter(sell_units), "") or str(target_proj.state.get("sell_by") or "")
    _qty_dp = {u["name"]: u for u in await _get_company_units(session, company_id)}.get(_common_sell_by, {}).get("decimals")
    if _qty_dp is not None:
        resulting_qty = round(float(resulting_qty), _qty_dp)
    # A hand-set resulting_quantity that diverges from the natural summed total is a
    # gated amount edit (edit_inventory_amounts); a natural merge (no override, or an
    # override equal to the total) stays on edit_inventory.
    if payload.resulting_quantity is not None:
        if payload.resulting_quantity < 0:
            raise HTTPException(status_code=422, detail="Resulting quantity cannot be negative")
        _natural_qty = round(float(total_qty), _qty_dp) if _qty_dp is not None else float(total_qty)
        if resulting_qty != _natural_qty and not role_has_permission(settings, role, "edit_inventory_amounts"):
            raise HTTPException(status_code=403, detail=f"Role '{role}' cannot hand-set the merged quantity: requires the edit_inventory_amounts permission")
    resulting_cost = payload.resulting_cost_total if payload.resulting_cost_total is not None else merged_cost_total
    resulting_name = payload.resulting_name if payload.resulting_name is not None else str(target_proj.state.get("name") or "")

    # Update expiry_date attribute to earliest.
    if earliest_expiry:
        resolved_attrs["expiry_date"] = earliest_expiry

    # Build item.created data from target projection.
    target_state = target_proj.state
    new_entity_id = f"item:{uuid.uuid4()}"
    # The merged item is genuinely new, so its SKU can be the target's (default),
    # or a custom value the user typed (issue #190). SKU is a product-type that may
    # repeat across lots (per-lot identity is the barcode + entity_id), so no
    # uniqueness check is applied - consistent with create/rename.
    merged_sku = (payload.resulting_sku or "").strip() or str(target_state.get("sku") or "")
    create_data: dict = {
        "sku": merged_sku,
        "name": resulting_name,
        "quantity": resulting_qty,
        "sell_by": str(target_state.get("sell_by") or "piece"),
        "status": "available",
        "allow_splitting": bool(target_state.get("allow_splitting", True)),
        "attributes": resolved_attrs,
    }
    for field in ("category", "location_id", "barcode", "description", "unit", "tax_codes"):
        val = target_state.get(field)
        if val is not None:
            create_data[field] = str(val) if field == "location_id" else val

    if total_weight is not None:
        create_data["weight"] = total_weight
    weight_unit = target_state.get("weight_unit")
    if weight_unit:
        create_data["weight_unit"] = weight_unit
    # Create the new merged item.
    raw_loc = target_state.get("location_id")
    emit_location_id = uuid.UUID(str(raw_loc)) if raw_loc else None
    await emit_event(
        session,
        company_id=company_id,
        entity_id=new_entity_id,
        entity_type="item",
        event_type="item.created",
        data=create_data,
        actor_id=user.id,
        location_id=emit_location_id,
        source="api",
        idempotency_key=payload.idempotency_key or str(uuid.uuid4()),
        metadata_={"merged_from": payload.source_entity_ids},
    )

    # Carry attached files from every source onto the merged item (dedup by id; keep one hero)
    # so merging never drops attachments.
    from datetime import datetime as _dt, timezone as _tz
    _seen_files: set[str] = set()
    _hero_used = False
    for proj in source_projections:
        for f in (proj.state.get("files") or []):
            fid = f.get("id")
            if not fid or fid in _seen_files:
                continue
            _seen_files.add(fid)
            is_hero = bool(f.get("is_hero")) and not _hero_used
            if is_hero:
                _hero_used = True
            await emit_event(
                session,
                company_id=company_id,
                entity_id=new_entity_id,
                entity_type="item",
                event_type="item.file.attached",
                data={
                    "entity_id": new_entity_id,
                    "entity_type": "item",
                    "file_id": fid,
                    "filename": f.get("filename", ""),
                    "mime": f.get("mime", ""),
                    "size": f.get("size", 0),
                    "url": f.get("url", ""),
                    "document_tag": f.get("document_tag"),
                    "description": f.get("description"),
                    "uploaded_at": f.get("uploaded_at") or _dt.now(_tz.utc).isoformat(),
                    "is_hero": is_hero,
                },
                actor_id=user.id,
                location_id=None,
                source="api",
                idempotency_key=str(uuid.uuid4()),
                metadata_={"reason": "from_merge"},
            )

    # Emit pricing events for the merged money fields (issue #199). Each *_price field is a PER-UNIT
    # price, so it is reconciled on the source TOTALS (unit × qty): if every source has the price set,
    # the merged total is their sum (stored back as a unit = total / merged_qty); if ANY source lacks
    # the price, the merged item carries NO value for it (omit) rather than copying the target's price
    # or treating the missing one as 0. cost_total is already a total (computed above).
    price_fields: dict = {}
    if resulting_cost is not None:
        price_fields["cost_total"] = resulting_cost
    _price_keys = {
        k for p in source_projections for k in p.state
        if k.endswith("_price") and k != "cost_price"
    }
    _merge_qty = float(resulting_qty) or 0.0
    for pk in _price_keys:
        src_totals = []
        for p in source_projections:
            unit = p.state.get(pk)
            src_totals.append(None if unit in (None, "") else float(unit) * float(p.state.get("quantity") or 0))
        if src_totals and all(t is not None for t in src_totals):
            merged_total = sum(src_totals)
            price_fields[pk] = round(merged_total / _merge_qty, 10) if _merge_qty else merged_total
        # else: at least one source lacks this price → omit (merged item has no value for it)

    for price_type, price_val in price_fields.items():
        await emit_event(
            session,
            company_id=company_id,
            entity_id=new_entity_id,
            entity_type="item",
            event_type="item.pricing.set",
            data={"price_type": price_type, "new_price": float(price_val)},
            actor_id=user.id,
            location_id=None,
            source="api",
            idempotency_key=str(uuid.uuid4()),
            metadata_={"reason": "from_merge"},
        )

    # Emit item.merged marker on the new item for history display.
    source_skus = {p.entity_id: str(p.state.get("sku") or p.entity_id) for p in source_projections}
    await emit_event(
        session,
        company_id=company_id,
        entity_id=new_entity_id,
        entity_type="item",
        event_type="item.merged",
        data={
            "source_entity_ids": payload.source_entity_ids,
            "source_skus": source_skus,
            "resulting_qty": float(resulting_qty),
        },
        actor_id=user.id,
        location_id=None,
        source="api",
        idempotency_key=str(uuid.uuid4()),
        metadata_={},
    )

    # Deactivate all source items: qty=0, is_available=False, merged_into=new item.
    new_sku = merged_sku or new_entity_id
    for proj in source_projections:
        await emit_event(
            session,
            company_id=company_id,
            entity_id=proj.entity_id,
            entity_type="item",
            event_type="item.source_deactivated",
            data={
                "merged_into": new_entity_id,
                "merged_into_sku": new_sku,
                "original_qty": float(proj.state.get("quantity") or 0),
            },
            actor_id=user.id,
            location_id=None,
            source="api",
            idempotency_key=str(uuid.uuid4()),
            metadata_={},
        )

    await session.commit()
    return {"id": new_entity_id}


@router.post("/{entity_id}/adjust")
async def adjust_item(entity_id: str, payload: AdjustBody, company_id=Depends(get_current_company_id), _: None = require_permission("adjust_inventory"), user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    # Validate new_qty against item's sell_by unit decimals
    row = await session.get(Projection, {"company_id": company_id, "entity_id": entity_id})
    if row:
        current_sell_by = row.state.get("sell_by")
        units = await _get_company_units(session, company_id)
        unit_map = {u["name"]: u for u in units}
        if current_sell_by and current_sell_by in unit_map:
            validate_quantity(payload.new_qty, unit_map[current_sell_by]["decimals"])
    entry = await emit_event(
        session,
        company_id=company_id,
        entity_id=entity_id,
        entity_type="item",
        event_type="item.quantity.adjusted",
        data=payload.model_dump(exclude_none=True),
        actor_id=user.id,
        location_id=None,
        source="api",
        idempotency_key=payload.idempotency_key or str(uuid.uuid4()),
        metadata_={},
    )
    await session.commit()
    return {"event_id": entry.id}


@router.post("/{entity_id}/price")
async def set_item_price(entity_id: str, payload: PriceBody, company_id=Depends(get_current_company_id), user=Depends(get_current_user), role: str = Depends(get_current_role), settings: dict = Depends(get_current_company_settings), session: AsyncSession = Depends(get_session)) -> dict:
    _price_lists, _base_name, _ = await get_price_config(session, company_id)
    # Guard both the conventional key ("trade_price") and the raw list name ("Trade"):
    # resolve_price honors a direct-name key first, so storing one would shadow the formula.
    if payload.price_type in derived_price_keys(_price_lists) or price_key(payload.price_type) in derived_price_keys(_price_lists):
        raise HTTPException(
            status_code=422,
            detail=f"'{payload.price_type}' is computed from the '{_base_name}' price list; "
                   f"edit the base price, or change the factor in Settings",
        )
    # Setting a price requires set_inventory_prices, except that a draft's creator
    # (edit_inventory) authors its cost while it is still a draft - the same carve-out
    # patch_item applies, so the pricing tab's Cost card works for the person entering
    # the item. Sell prices stay gated, and the gate re-arms once the item is available.
    _proj = await session.get(Projection, {"company_id": company_id, "entity_id": entity_id})
    _is_draft = str(((_proj.state if _proj else {}) or {}).get("status") or "").lower() == "draft"
    if not (is_cost_price_type(payload.price_type) and draft_cost_carveout(_is_draft, role, settings)):
        if not role_has_permission(settings, role, "set_inventory_prices"):
            raise HTTPException(
                status_code=403,
                detail="Setting inventory prices requires the 'set_inventory_prices' permission",
            )
    entry = await emit_event(
        session,
        company_id=company_id,
        entity_id=entity_id,
        entity_type="item",
        event_type="item.pricing.set",
        data=payload.model_dump(exclude_none=True),
        actor_id=user.id,
        location_id=None,
        source="api",
        idempotency_key=payload.idempotency_key or str(uuid.uuid4()),
        metadata_={},
    )
    await session.commit()
    return {"event_id": entry.id}


@router.post("/{entity_id}/status")
async def set_item_status(entity_id: str, payload: StatusBody, company_id=Depends(get_current_company_id), _: None = require_permission("edit_inventory"), user=Depends(get_current_user), role: str = Depends(get_current_role), settings: dict = Depends(get_current_company_settings), session: AsyncSession = Depends(get_session)) -> dict:
    await reject_draft_status_change_via_generic_path(session, company_id, entity_id, payload.new_status)
    await assert_status_change_allowed(session, company_id, entity_id, payload.new_status, role, settings)
    entry = await emit_event(
        session,
        company_id=company_id,
        entity_id=entity_id,
        entity_type="item",
        event_type="item.status.set",
        data=payload.model_dump(exclude_none=True),
        actor_id=user.id,
        location_id=None,
        source="api",
        idempotency_key=payload.idempotency_key or str(uuid.uuid4()),
        metadata_={},
    )
    await session.commit()
    return {"event_id": entry.id}


@router.post("/{entity_id}/reserve")
async def reserve_item(entity_id: str, payload: ReserveBody, company_id=Depends(get_current_company_id), _: None = require_permission("edit_inventory"), user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    await assert_not_draft(session, company_id, entity_id, "reserve")
    entry = await emit_event(
        session,
        company_id=company_id,
        entity_id=entity_id,
        entity_type="item",
        event_type="item.reserved",
        data=payload.model_dump(exclude_none=True),
        actor_id=user.id,
        location_id=None,
        source="api",
        idempotency_key=payload.idempotency_key or str(uuid.uuid4()),
        metadata_={},
    )
    await session.commit()
    return {"event_id": entry.id}


@router.post("/{entity_id}/unreserve")
async def unreserve_item(entity_id: str, payload: ReserveBody, company_id=Depends(get_current_company_id), _: None = require_permission("edit_inventory"), user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    entry = await emit_event(
        session,
        company_id=company_id,
        entity_id=entity_id,
        entity_type="item",
        event_type="item.unreserved",
        data=payload.model_dump(exclude_none=True),
        actor_id=user.id,
        location_id=None,
        source="api",
        idempotency_key=payload.idempotency_key or str(uuid.uuid4()),
        metadata_={},
    )
    await session.commit()
    return {"event_id": entry.id}


@router.post("/{entity_id}/expire")
async def expire_item(entity_id: str, company_id=Depends(get_current_company_id), _: None = require_permission("adjust_inventory"), user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    await assert_not_draft(session, company_id, entity_id, "expire")
    entry = await emit_event(
        session,
        company_id=company_id,
        entity_id=entity_id,
        entity_type="item",
        event_type="item.expired",
        data={},
        actor_id=user.id,
        location_id=None,
        source="api",
        idempotency_key=str(uuid.uuid4()),
        metadata_={},
    )
    await session.commit()
    return {"event_id": entry.id}


# ── Import endpoint (CIF) ─────────────────────────────────────────────────────

class ImportRecord(BaseModel):
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
    batch_id: str | None = None


class BatchImportRequest(BaseModel):
    records: list[ImportRecord] = Field(..., max_length=500)
    filename: str | None = None
    upsert: bool = False


@router.post("/import/batch", response_model=BatchImportResult)
async def batch_import_items(
    body: BatchImportRequest,
    company_id=Depends(get_current_company_id),
    _: None = require_permission("edit_inventory"),
    role: str = Depends(get_current_role),
    settings: dict = Depends(get_current_company_settings),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> BatchImportResult:
    """Batch-import CIF item records. Idempotent on idempotency_key. Max 500 per call."""
    from sqlalchemy import delete as _delete

    from celerp_inventory.models_import_batch import ImportBatch
    from celerp.models.ledger import LedgerEntry

    # Scope keys to company to prevent cross-company idempotency collisions
    # (LedgerEntry.idempotency_key has a table-wide UNIQUE constraint with no company_id scope)
    scoped_keys = [f"{company_id}:{r.idempotency_key}" for r in body.records]
    existing = set(
        (await session.execute(
            select(LedgerEntry.idempotency_key).where(LedgerEntry.idempotency_key.in_(scoped_keys))
        )).scalars().all()
    )

    # Fetch valid unit names once for sell_by validation across all records.
    # Falls back to empty set (no validation) if units cannot be fetched.
    _units = await _get_company_units(session, company_id)
    _valid_units: frozenset[str] = frozenset(u["name"] for u in _units)
    _derived_keys = derived_price_keys((await get_price_config(session, company_id))[0])

    created = skipped = updated = 0
    errors: list[str] = []
    created_entity_ids: list[str] = []
    created_keys: list[str] = []

    for rec in body.records:
        # Strip system-managed and document-lifecycle fields — never user-settable via import.
        # status: all imported items must start as available; other statuses require linked docs.
        rec.data.pop("status", None)
        # Strip any client-supplied timestamps: created_at is set by ProjectionEngine on INSERT.
        rec.data.pop("created_at", None)
        rec.data.pop("updated_at", None)
        # Derived price lists are computed at read time; a derived column riding along in an
        # exported file must not be stored (same rule as item create).
        for _dk in _derived_keys:
            rec.data.pop(_dk, None)
        # Normalize allow_splitting to a real bool if the import provided one (CSV
        # gives strings like "Yes"/"No", and None means "unset"). Imports that omit
        # it default to no-split in the create branch below; the split guard
        # requires an explicit True, so a string/None must never read as splittable.
        if "allow_splitting" in rec.data and not isinstance(rec.data["allow_splitting"], bool):
            rec.data["allow_splitting"] = str(rec.data["allow_splitting"]).strip().lower() in ("true", "yes", "1", "y", "t")

        # Validate sell_by against company units before attempting any DB work.
        sell_by = str(rec.data.get("sell_by") or "").strip()
        if not sell_by:
            errors.append(f"Row (SKU={rec.data.get('sku', '?')}): sell_by is required")
            skipped += 1
            continue
        if _valid_units and sell_by not in _valid_units:
            errors.append(
                f"Row (SKU={rec.data.get('sku', '?')}): sell_by '{sell_by}' is not a valid unit"
            )
            skipped += 1
            continue

        # Amount fields must be non-negative: rec.data is untyped and emitted verbatim
        # as item.created / item.patched with no schema or projection validation, so this
        # is the only place a negative CSV amount is caught.
        _neg_amt = None
        for _k in AMOUNT_ITEM_KEYS & set(rec.data):
            _v = rec.data.get(_k)
            if _v in (None, ""):
                continue
            try:
                if float(_v) < 0:
                    _neg_amt = _k
                    break
            except (TypeError, ValueError):
                pass
        if _neg_amt is not None:
            errors.append(f"Row (SKU={rec.data.get('sku', '?')}): {_neg_amt} cannot be negative")
            skipped += 1
            continue

        scoped_key = f"{company_id}:{rec.idempotency_key}"
        if scoped_key in existing:
            if body.upsert:
                # Hand-editing an existing item's amount or sell unit via CSV upsert is
                # a genuine hand-edit surface, gated by edit_inventory_amounts. A create
                # (below) defines the item and stays on edit_inventory. The amount keys
                # are optional per row, so their presence already signals intent to
                # change; sell_by is required on every row (validated above), so gating
                # it on mere presence would block every upsert by an ungranted role.
                # Gate sell_by on a real CHANGE against the stored value instead.
                if not role_has_permission(settings, role, "edit_inventory_amounts"):
                    gated = set(AMOUNT_ITEM_KEYS & set(rec.data))
                    stored_proj = await session.get(Projection, {"company_id": company_id, "entity_id": rec.entity_id})
                    stored_sell_by = str((stored_proj.state.get("sell_by") if stored_proj else "") or "").strip()
                    if sell_by != stored_sell_by:
                        gated.add("sell_by")
                    if gated:
                        errors.append(f"Row (SKU={rec.data.get('sku', '?')}): editing {sorted(gated)} requires the edit_inventory_amounts permission")
                        skipped += 1
                        continue
                # Emit patch event with a upsert-specific idempotency key
                upsert_idem = f"{scoped_key}:upsert"
                upsert_existing = set(
                    (await session.execute(
                        select(LedgerEntry.idempotency_key).where(
                            LedgerEntry.idempotency_key == upsert_idem
                        )
                    )).scalars().all()
                )
                if upsert_idem in upsert_existing:
                    skipped += 1
                    continue
                try:
                    loc_id: uuid.UUID | None = None
                    raw_loc = rec.data.get("location_id")
                    if raw_loc:
                        try:
                            loc_id = uuid.UUID(str(raw_loc))
                        except ValueError:
                            pass
                    await emit_event(
                        session,
                        company_id=company_id,
                        entity_id=rec.entity_id,
                        entity_type="item",
                        event_type="item.patched",
                        data=rec.data,
                        actor_id=user.id,
                        location_id=loc_id,
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
        try:
            # Imported items default to no-split until splitting is explicitly
            # enabled per item (matches how they display; the split guard requires
            # an explicit True).
            rec.data.setdefault("allow_splitting", False)
            loc_id: uuid.UUID | None = None
            raw_loc = rec.data.get("location_id")
            if raw_loc:
                try:
                    loc_id = uuid.UUID(str(raw_loc))
                except ValueError:
                    pass
            await emit_event(
                session,
                company_id=company_id,
                entity_id=rec.entity_id,
                entity_type="item",
                event_type=rec.event_type,
                data=rec.data,
                actor_id=user.id,
                location_id=loc_id,
                source=rec.source,
                idempotency_key=scoped_key,
                metadata_={"source_ts": rec.source_ts} if rec.source_ts else {},
            )
            existing.add(scoped_key)
            created_entity_ids.append(rec.entity_id)
            created_keys.append(scoped_key)
            created += 1
        except Exception as exc:
            if len(errors) < 10:
                errors.append(f"{rec.entity_id}: {exc}")

    batch_id: str | None = None
    if created > 0:
        new_batch_id = uuid.uuid4()
        batch = ImportBatch(
            id=new_batch_id,
            company_id=company_id,
            entity_type="item",
            filename=body.filename,
            row_count=created,
            entity_ids=created_entity_ids,
            idempotency_keys=created_keys,
            status="active",
        )
        session.add(batch)
        batch_id = str(new_batch_id)

        # Auto-wipe demo items on first real import
        demo_eids = (await session.execute(
            select(LedgerEntry.entity_id).where(
                LedgerEntry.company_id == company_id,
                LedgerEntry.source == "demo",
                LedgerEntry.entity_type == "item",
            ).distinct()
        )).scalars().all()
        if demo_eids:
            await session.execute(
                _delete(Projection).where(
                    Projection.company_id == company_id,
                    Projection.entity_id.in_(demo_eids),
                )
            )
            await session.execute(
                _delete(LedgerEntry).where(
                    LedgerEntry.company_id == company_id,
                    LedgerEntry.entity_id.in_(demo_eids),
                )
            )

    await session.commit()
    return BatchImportResult(created=created, skipped=skipped, updated=updated, errors=errors, batch_id=batch_id)


# ---------------------------------------------------------------------------
# Import history + undo
# ---------------------------------------------------------------------------


@router.get("/import/batches")
async def list_import_batches(
    company_id=Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """List all import batches for this company, newest first."""
    from sqlalchemy import select as _select

    from celerp_inventory.models_import_batch import ImportBatch

    rows = (await session.execute(
        _select(ImportBatch)
        .where(ImportBatch.company_id == company_id)
        .order_by(ImportBatch.imported_at.desc())
    )).scalars().all()

    return {"batches": [
        {
            "id": str(b.id),
            "entity_type": b.entity_type,
            "filename": b.filename,
            "row_count": b.row_count,
            "status": b.status,
            "imported_at": b.imported_at.isoformat(),
            "undone_at": b.undone_at.isoformat() if b.undone_at else None,
        }
        for b in rows
    ]}


@router.post("/import/batches/{batch_id}/undo")
async def undo_import_batch(
    batch_id: str,
    company_id=Depends(get_current_company_id),
    user=Depends(get_current_user),
    _: None = require_permission("manage_company_settings"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Undo an import batch: soft-delete all created items, purge idempotency keys."""
    from datetime import datetime, timezone as _tz

    from sqlalchemy import delete as _delete
    from sqlalchemy import select as _select

    from celerp_inventory.models_import_batch import ImportBatch
    from celerp.models.ledger import LedgerEntry
    from celerp.models.projections import Projection

    try:
        batch_uuid = uuid.UUID(batch_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Import batch not found")
    batch = await session.get(ImportBatch, batch_uuid)
    if batch is None or batch.company_id != company_id:
        raise HTTPException(status_code=404, detail="Import batch not found")
    if batch.status == "undone":
        raise HTTPException(status_code=409, detail="Batch already undone")

    entity_ids = batch.entity_ids or []

    # Check for modified-since: any ledger event after the import that isn't item.created
    modified: list[str] = []
    for eid in entity_ids:
        extra = (await session.execute(
            _select(LedgerEntry.entity_id)
            .where(
                LedgerEntry.company_id == company_id,
                LedgerEntry.entity_id == eid,
                LedgerEntry.event_type != "item.created",
                LedgerEntry.ts > batch.imported_at,
            )
            .limit(1)
        )).scalar_one_or_none()
        if extra:
            modified.append(eid)

    # Delete projections for all entities in this batch
    if entity_ids:
        await session.execute(
            _delete(Projection).where(
                Projection.company_id == company_id,
                Projection.entity_id.in_(entity_ids),
            )
        )
        # Purge ledger entries so re-import works cleanly
        ikeys = batch.idempotency_keys or []
        if ikeys:
            await session.execute(
                _delete(LedgerEntry).where(LedgerEntry.idempotency_key.in_(ikeys))
            )

    batch.status = "undone"
    batch.undone_at = datetime.now(_tz.utc)
    batch.undone_by = user.id
    await session.commit()

    return {
        "ok": True,
        "removed": len(entity_ids),
        "modified_items": modified,
    }


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------

@router.get("/export/csv")
async def export_items_csv(
    company_id=Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
    q: str | None = None,
    category: str | None = None,
    status: str | None = None,
) -> StreamingResponse:
    stmt = select(Projection).where(Projection.company_id == company_id, Projection.entity_type == "item")
    rows = (await session.execute(stmt)).scalars().all()
    price_config = await get_price_config(session, company_id)
    items = [flatten_item(r.state, r.entity_id, created_at=r.created_at, updated_at=r.updated_at, price_config=price_config) for r in rows]
    if q:
        ql = q.lower()
        def _csv_matches(it: dict) -> bool:
            if ql in str(it.get("name", "")).lower():
                return True
            if ql in str(it.get("sku", "")).lower():
                return True
            if ql in str(it.get("barcode", "")).lower():
                return True
            if ql in str(it.get("description", "")).lower():
                return True
            if ql in str(it.get("category", "")).lower():
                return True
            for v in (it.get("attributes") or {}).values():
                if ql in str(v).lower():
                    return True
            return False
        items = [it for it in items if _csv_matches(it)]
    if category:
        items = [it for it in items if it.get("category") == category]
    if status:
        items = [it for it in items if it.get("status") == status]

    # Build price columns dynamically from the price config fetched above
    price_cols = [price_key(pl["name"]) for pl in price_config[0] if pl.get("name")]

    _COLS = ["id", "sku", "name", "category", "quantity", "status"] + price_cols + ["weight", "weight_unit", "pieces", "sell_by", "barcode", "hs_code", "purchase_sku", "purchase_name", "purchase_unit", "purchase_conversion_factor", "created_at", "updated_at"]

    def _fmt_ts(val) -> str:
        """Ensure timestamps are ISO 8601 UTC with Z suffix."""
        if not val:
            return ""
        s = str(val).strip()
        if not s:
            return ""
        # Already has Z or +00:00 — normalise to Z
        if s.endswith("Z"):
            return s
        if s.endswith("+00:00"):
            return s[:-6] + "Z"
        # No timezone info — assume UTC, append Z
        return s.rstrip() + "Z"

    unit_map = build_unit_map(await _get_company_units(session, company_id))

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=_COLS, extrasaction="ignore")
    writer.writeheader()
    for it in items:
        row = {c: it.get(c, "") for c in _COLS}
        # The measure the sell unit already IS derives from quantity: the stored
        # companion field is absent on fresh items and can go stale after sales.
        # Matches the inventory table's derived weight/pieces columns.
        sell_by = it.get("sell_by")
        if is_weight_unit(sell_by, unit_map):
            row["weight"] = it.get("quantity", "")
            row["weight_unit"] = sell_by
        elif is_pieces_unit(sell_by, unit_map):
            row["pieces"] = it.get("quantity", "")
        row["created_at"] = _fmt_ts(it.get("created_at"))
        row["updated_at"] = _fmt_ts(it.get("updated_at"))
        writer.writerow(row)
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=items.csv"},
    )


def setup_api_routes(app) -> None:
    # Scanning module disabled until properly finished
    # from celerp_inventory.routes_scanning import router as scanning_router
    from celerp_inventory.routes_attachments import router as attachments_router
    # attachments_router first: its specific sub-paths (e.g. /files/{id}) must
    # be registered before the catch-all /{entity_id} route in the main router.
    app.include_router(attachments_router, prefix="/items", tags=["attachments"])
    app.include_router(router, prefix="/items", tags=["items"])

