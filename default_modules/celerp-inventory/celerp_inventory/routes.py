# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT

from __future__ import annotations

import hashlib
import hmac
import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.db import get_session
from celerp.events.engine import emit_event, find_event_by_idempotency
from celerp.events.schemas import reject_comma_sku
from celerp.importers.tabular import MAX_CELLS, MAX_ROWS
from celerp.inventory_codes import (
    PHYSICAL_CODE_RESOLVE_EXCLUDED_STATUSES,
    normalize_rfid_epc,
    validate_barcode,
    validate_gtin,
    validate_rfid_epc,
)
from celerp.models.projections import Projection
from .services import (
    VALID_INVENTORY_TYPES,
    BatchImportRequest,
    BatchImportResult,
    adjust_item_quantity,
    ImportRejected,
    allocate_internal_codes,
    apply_source_semantics,
    build_item_import_spec,
    commit_import_batch,
    import_items,
    lot_fields,
    import_preview_hash,
    is_item_field_key,
    item_price_mutex_groups,
    preview_import_rows,
    source_header_semantics,
)
from celerp.accounting_roles import LOT_ACCOUNT_FIELD, ON_BOOKS_FIELD
from celerp.services.company_lock import lock_projections
from celerp.services.item_erasure import erase_items, mentioned_elsewhere
from celerp.services.lot_origin import RECORDED, RETIRED, STOCK_TYPES, in_stock, is_authoring_event, record_kept_stock
from celerp.services.physical_codes import code_in_use, lock_item_code_namespace
from celerp.services.auth import get_current_company_id, get_current_user, get_current_role, ROLE_LEVELS
from celerp.services.business_time import business_date_at
from celerp.services.cost_visibility import COST_ITEM_KEYS, apply_field_visibility, restricted_field_keys
from celerp.services.csv_export import csv_stream, resolve_export_cols
from celerp.services.demo import demo_item_ids
from celerp.services.field_schema import AMOUNT_EDIT_GATED_KEYS, AMOUNT_ITEM_KEYS, DEFAULT_ITEM_SCHEMA, NUMERIC_SCHEMA_TYPES
from celerp.services.permissions import (
    assert_role_permission,
    get_current_company_settings,
    locked_authority,
    reject_price_change,
    require_permission,
    role_has_permission,
)
from celerp.services.pricing import (
    coerce_price,
    derived_price_keys,
    get_price_config,
    inject_derived_prices,
    is_cost_list_name,
    is_price_item_key,
    price_key,
    price_keys_in,
    stored_price,
)
from celerp.services.units import validate_quantity, build_unit_map, get_company_units, is_weight_unit, is_pieces_unit, LANDED_COST_KINDS
from celerp.services.vertical_presets import category_item_defaults
from celerp.services.line_measures import splitting_allowed
from celerp.services.money import round_basis, round_money, to_decimal, to_stored_float
from celerp.schemas.numbers import FiniteFloat
from celerp_inventory.projections import _is_image_mime, is_core_item_key, is_item_available, thumbnail_file_id

router = APIRouter(dependencies=[Depends(get_current_user)])


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

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


# Companion keys whose visibility follows a schema field they mirror, so field-level
# visibility strips them with their source. qty_each is derived from quantity and
# pieces (a role denied either source cannot recover it from the ratio); location_id
# is a non-schema mirror of location_name (a denied role must not recover the location
# through the id it would resolve via /companies/me/locations); the image ids follow the
# image field (thumbnail), so a role denied the image is not handed a way to fetch it. The rule lives here
# (one place) and is applied by apply_item_visibility at every item read; the
# visibility service itself holds no inventory field names.
DERIVED_FIELD_DEPS: dict[str, tuple[str, ...]] = {
    "qty_each": ("quantity", "pieces"),
    "location_id": ("location_name",),
    "thumbnail_file_id": ("thumbnail",),
    "preview_image_id": ("thumbnail",),
}


def apply_item_visibility(
    items: list[dict], role: str, field_schema: list[dict], can_see_costs: bool,
    can_author_drafts: bool = False,
) -> list[dict]:
    """Strip what the caller may not see from flattened item dicts.

    Field visibility with the companion keys above, plus the image entries of the file
    lists: a role denied the image field gets the item's other files but none of its
    images, so no image id or URL reaches it through ``files`` or ``attachments``.
    """
    out = apply_field_visibility(
        items, role, field_schema, can_see_costs,
        can_author_drafts=can_author_drafts, derived_field_deps=DERIVED_FIELD_DEPS,
    )
    if "thumbnail" not in restricted_field_keys(role, field_schema):
        return out
    return [
        {**item, **{
            key: [f for f in item[key] if not _is_image_file(f)]
            for key in ("files", "attachments") if isinstance(item.get(key), list)
        }}
        for item in out
    ]


def _is_image_file(entry) -> bool:
    return isinstance(entry, dict) and (
        _is_image_mime(str(entry.get("mime") or "")) or entry.get("type") == "image"
    )


def flatten_item(state: dict, entity_id: str, location_id: str | None = None, location_name: str | None = None, created_at: object | None = None, updated_at: object | None = None, price_config: tuple[list[dict], str, str] | None = None, unit_map: dict[str, dict] | None = None) -> dict:
    """Flatten attributes dict to top-level so schema-driven UI sees all fields.

    When ``price_config`` (``(price_lists, base_price_list, currency)`` from
    ``get_price_config``) is given, derived price lists are computed onto the result after
    the cost roll-up, so a Cost base prices from the same unit cost every other consumer sees.
    """
    flat = dict(state)
    flat.pop("_catalog_sku_aliases", None)
    flat["id"] = entity_id
    flat["thumbnail_file_id"] = thumbnail_file_id(state)
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
    # Per-piece measure. For a piece-denominated lot the quantity IS the piece count
    # (celerp syncs pieces to quantity for pieces units), so each piece is 1; the
    # value is 0 for an empty lot (guarded, no divide-by-zero). For a weight/carat lot
    # the per-piece measure is quantity spread over the stones (a 6 ct box of 4 is 1.5
    # ct each); a 1-piece or pieces-missing lot falls back to the full quantity. unit_map
    # is only supplied by the callers that surface qty_each to the client.
    if unit_map is not None and is_pieces_unit(flat.get("sell_by"), unit_map):
        flat["qty_each"] = 1 if qty else 0
    else:
        pieces = _read_pieces(flat) or 0
        flat["qty_each"] = round(qty / pieces, 10) if pieces > 1 else qty
    _recipe_unit = _recipe_standard_unit_cost(flat)
    if _recipe_unit is not None:
        # Recipe-backed item: derive cost from the rolled standard (single source of truth); never
        # let a lingering build-lot cost_total silently override it. See _recipe_standard_unit_cost.
        flat["cost_price"] = _recipe_unit
        flat["cost_total"] = round_basis(_recipe_unit * qty) if qty else 0.0
    elif flat.get("cost_total") is not None:
        flat["cost_price"] = round(float(flat["cost_total"]) / qty, 10) if qty else 0.0
    elif flat.get("cost_price") is not None:
        flat["cost_total"] = round_basis(float(flat["cost_price"]) * qty)
    # else: both remain absent (item has no cost set)
    if price_config is not None:
        inject_derived_prices(flat, *price_config)
    return flat


class ItemCreate(BaseModel):
    model_config = {"extra": "allow"}  # Accept dynamic price fields (e.g. vip_price)

    sku: str | None = None
    name: str
    sell_by: str | None = None             # a company unit; omitted only when the category supplies one
    quantity: FiniteFloat = 0
    category: str | None = None
    location_id: uuid.UUID | None = None
    cost_price: FiniteFloat | None = None  # legacy alias; prefer cost_total
    cost_total: FiniteFloat | None = None
    wholesale_price: FiniteFloat | None = None
    retail_price: FiniteFloat | None = None
    description: str | None = None
    unit: str | None = None
    barcode: str | None = None             # digits only if provided
    auto_barcode: bool = False             # duplicate/clone: mint a fresh unique barcode from the shared sequence, never inherit one
    gtin: str | None = None                # product GTIN/UPC/EAN (digits, {8,12,13,14}); identifies a product, not a lot; not unique
    rfid_epc: str | None = None            # RFID/EPC physical-tag code; company-unique, normalized upper-case
    hs_code: str | None = None             # Harmonized System code for trade/customs
    tax_codes: list[str] = Field(default_factory=list)
    purchase_sku: str | None = None        # vendor's SKU / part number
    purchase_name: str | None = None       # vendor's product name
    purchase_unit: str | None = None       # unit vendor sells in (e.g. "case", "box")
    purchase_conversion_factor: FiniteFloat | None = None  # sell units per purchase unit (e.g. 24 pcs/case)
    allow_splitting: bool = True
    attributes: dict = Field(default_factory=dict)
    idempotency_key: str | None = None
    # stocked | component | non_stocked | service | freight; omitted means the
    # category's default, else stocked
    inventory_type: str | None = None
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
    quantity: FiniteFloat
    weight: FiniteFloat | None = None
    pieces: int | None = None   # complement for weight-unit items (independent of weight)
    barcode: str | None = None  # auto-assigned from shared sequence if omitted
    attributes: dict = Field(default_factory=dict)


class SplitBody(BaseModel):
    children: list[SplitChild]
    mother_qty: FiniteFloat | None = None    # explicit mother qty override (used when user re-weighed mother)
    mother_weight: FiniteFloat | None = None # explicit mother weight override
    idempotency_key: str | None = None


class MergeBody(BaseModel):
    source_entity_ids: list[str]
    target_sku_from: str                       # entity_id of the source whose SKU/barcode to use
    resulting_quantity: FiniteFloat | None = None    # optional override (default = sum)
    resulting_cost_total: FiniteFloat | None = None  # must equal the sources' cost; a merge never revalues
    resulting_name: str | None = None          # optional override (default = target's name)
    resulting_sku: str | None = None           # optional custom SKU (default = target's SKU); issue #190
    resolved_attributes: dict | None = None    # user picks for conflicting string attributes
    idempotency_key: str | None = None
    plan_fingerprint: str | None = None        # required to confirm: from the preview the user confirmed; refused if the items changed since


class TransformBody(BaseModel):
    child_sku: str
    child_category: str
    child_sell_by: str
    child_quantity: FiniteFloat
    child_name: str | None = None
    child_weight: FiniteFloat | None = None
    child_weight_unit: str | None = None
    child_pieces: int | None = None
    child_cost_total: FiniteFloat | None = None  # final cost override (needs set_inventory_prices); None preserves parent cost
    idempotency_key: str | None = None


class AdjustBody(BaseModel):
    new_qty: FiniteFloat
    idempotency_key: str | None = None


class PriceBody(BaseModel):
    price_type: str
    new_price: FiniteFloat
    idempotency_key: str | None = None


class StatusBody(BaseModel):
    new_status: str
    idempotency_key: str | None = None


class ReserveBody(BaseModel):
    quantity: FiniteFloat
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

# Statuses a generic status edit cannot set: each records an outcome its own action
# books (Expire is administrative but has its own action and permission).
_ACTION_OWNED_STATUSES: dict[str, str] = {
    "sold": "An item is sold by fulfilling its invoice, not a direct status edit.",
    "merged": "An item is merged through the Merge action, not a direct status edit.",
    "expired": "Use the Expire action to expire an item, not a direct status edit.",
}

# Why stock that has left the books cannot come back through a status edit, by the
# status it left them in; each names the action that undoes it.
_LEFT_THE_BOOKS: dict[str, str] = {
    "disposed": "This stock was written off; use Undo write-off to bring it back.",
    "merged": "This item was merged into another; use Undo merge to bring it back.",
    "sold": "This item was sold; reverse its fulfilment to bring it back.",
    "fulfilled": "This item was sold; reverse its fulfilment to bring it back.",
    "void": "This item was deleted.",
    "deleted": "This item was deleted.",
}
_GAVE_UP_ITS_STOCK = ("This item holds no stock on the books: it was sold, or its stock went into other items "
                      "(a split, transform or merge), or its receipt or return was undone, so a status edit "
                      "cannot bring it back.")
# A sold item can still be archived to tidy the catalog; it stays off the books.
_ARCHIVABLE_AFTER_SALE = frozenset({"sold", "fulfilled"})


def _kept(status: str | None) -> dict:
    """What an Archive or Expire event says: the company keeps the stock on its books."""
    return {ON_BOOKS_FIELD: True} if str(status or "").lower() in RETIRED else {}


async def assert_status_change_allowed(
    session: AsyncSession, company_id, entity_id: str, new_status: str,
    role: str, settings: dict,
) -> None:
    """Function-level validation shared by every item-status write path (single,
    bulk, and PATCH). Unknown values are rejected with the allowed list. A status
    edit is administrative: it never records an outcome another action books
    (written off, sold, merged, expired), and stock that has left the books (written
    off, sold, merged, or used up by a split or transform) comes back only through the
    action that undoes it, judged on the locked row. A draft item's amounts and costs
    are freely editable, so an item that has circulated must never quietly become one
    again: reverting a committed item to draft requires the revert_items_to_draft
    permission AND a clean history, and every rejection names its reason instead of
    hiding the control.
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
    if ns in _ACTION_OWNED_STATUSES:
        raise HTTPException(status_code=422, detail=_ACTION_OWNED_STATUSES[ns])
    if ns != "draft":
        row = (await lock_projections(session, company_id, [entity_id])).get(entity_id)
        state = (row.state if row else {}) or {}
        current = str(state.get("status") or "").lower()
        if ns == "archived" and current in _ARCHIVABLE_AFTER_SALE:
            return
        if current not in ("", "draft", ns) and not in_stock(state):
            raise HTTPException(status_code=422, detail=_LEFT_THE_BOOKS.get(current, _GAVE_UP_ITS_STOCK))
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
    circulated = sorted(e for e in event_types if not is_authoring_event(e))
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


async def assert_expirable(session: AsyncSession, company_id, entity_id: str) -> None:
    """Expire retires stock the company still owns, so the lot must hold stock on the
    books when its row lock is taken."""
    await assert_not_draft(session, company_id, entity_id, "expire")
    row = (await lock_projections(session, company_id, [entity_id])).get(entity_id)
    state = (row.state if row else {}) or {}
    if row is not None and not in_stock(state):
        raise HTTPException(
            status_code=409,
            detail=f"Only stock on hand can be expired; this item is {state.get('status') or 'unknown'}.",
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
# The closed allowlist of user-facing text fields the free-text search covers. It is
# every core field a user reads on an item: identity, descriptions, unit-of-measure
# codes, inventory type, and the status doc number rows are traced by. "Core" here is
# the projection's write-side storage class, NOT a synonym for "internal", so the list
# is chosen by what the user actually sees, not by that classification. Core fields
# left off are genuine bookkeeping (idempotency_key, id/lineage refs like parent_id and
# status_doc_id, internal classifications) and are deliberately unsearchable (#306);
# numeric columns match only through the explicit numeric path.
_SEARCH_FIELDS = ("name", "sku", "barcode", "gtin", "rfid_epc", "description",
                  "category", "short_description", "notes", "hs_code", "batch_no",
                  "purchase_name", "purchase_sku", "location_name",
                  "sell_by", "unit", "weight_unit", "gross_weight_unit",
                  "purchase_unit", "inventory_type", "status_doc_number", "lot")
_NUMERIC_FIELDS = ("quantity", "weight", "pieces")
# The continuous per-item measures that must never become column-filter funnels: a facet
# over quantity would list every distinct amount. A custom numeric category attribute
# (a `number`-typed attribute like `length`) is NOT a measure and keeps its funnel.
_NUMERIC_MEASURE_KEYS = frozenset(_NUMERIC_FIELDS) | {"qty_each"}
# A range is PURE number-dash-number only, so a hyphenated SKU (SHOT274-005) stays literal.
_RANGE_RE = re.compile(r"^(\d+(?:\.\d+)?)-(\d+(?:\.\d+)?)$")
# A field-scoped term starts with an identifier and a colon: `stone_type: demantoid`,
# `qty: 1-2`. A digit-led token (12:30) or a hyphenated SKU has no leading identifier,
# so scoping never captures it and the unscoped path stays unchanged.
_SCOPE_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*)$")
# Friendly names for the fields a user is likely to type. Every alias is documented in
# the search help panel; the canonical per-piece field name is qty_each.
_FIELD_ALIASES = {
    "qty": "quantity", "ct": "quantity", "carat": "quantity",
    "pcs": "pieces",
    "ct_each": "qty_each", "per_pc": "qty_each",
}


def searchable_field_sets(schema: list[dict]) -> tuple[frozenset[str], frozenset[str]]:
    """Derive (numeric, text) scoped-search field sets from an effective field schema.

    numeric = _NUMERIC_FIELDS plus every schema key of a numeric type (number/money/rate/weight)
    plus the synthetic qty_each; text = _SEARCH_FIELDS plus the remaining (non-numeric)
    schema keys. Both exclude cost/price keys so a price column never becomes
    scope-searchable. The two sets are disjoint (a schema key is numeric when its type is
    numeric, else text, and the two folded-in constants share no member), so the coercion
    test is unambiguous.
    """
    numeric = set(_NUMERIC_FIELDS)
    numeric.add("qty_each")
    text = set(_SEARCH_FIELDS)
    for f in schema:
        key = f.get("key")
        if not key or is_price_item_key(key):
            continue
        if f.get("type") in NUMERIC_SCHEMA_TYPES:
            numeric.add(key)
        else:
            text.add(key)
    numeric -= {k for k in numeric if is_price_item_key(k)}
    text -= numeric
    text -= {k for k in text if is_price_item_key(k)}
    return frozenset(numeric), frozenset(text)


# Module-level defaults keep the pure-dict grammar callable with no DB: derived once
# from the default item schema unioned with the two hardcoded constants. list_items
# passes per-category sets that override these; every other caller (and the pure-dict
# grammar tests) uses these base sets.
_DEFAULT_NUMERIC_FIELDS, _DEFAULT_TEXT_FIELDS = searchable_field_sets(DEFAULT_ITEM_SCHEMA)


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
    # Named fields aside, the only other searchable values are DYNAMIC category
    # attributes, which flatten to the top level. Skipping every core key
    # (projections.is_core_item_key marks the closed core set) is what keeps internal
    # bookkeeping (idempotency_key, id/lineage refs) out of search (#306) while still
    # matching user-defined attribute values; numeric columns match only via the
    # explicit numeric path, never by substring.
    for k, v in record.items():
        if is_core_item_key(k) or k in _NUMERIC_FIELDS:
            continue
        if isinstance(v, str) and term in v.lower():
            return k
    return None


def _as_number(v: object) -> float | None:
    """Coerce a value to float, or None if it is not numeric. Shared by the scoped
    numeric-equality check so a query and a stored value compare as numbers."""
    try:
        return float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _term_match_reason(
    record: dict, term: str,
    numeric_fields: frozenset[str] = _DEFAULT_NUMERIC_FIELDS,
    text_fields: frozenset[str] = _DEFAULT_TEXT_FIELDS,
) -> tuple[str, str] | None:
    """One AND-term: the (field, matched text) behind the hit, or None. The matched
    text is the term itself for substring hits and the whole number for numeric
    range/exact hits, so the UI can embolden exactly what matched.

    A term of the form `field: value` scopes the match to one named field (an alias
    resolves to its canonical field). Resolution is gated to user-facing fields - a
    core/bookkeeping key stays unsearchable (#306) even when named explicitly - and a
    scoped value may be a range (over that one field) or a substring. A term with no
    leading `identifier:` falls through to the unscoped behavior unchanged.

    numeric_fields / text_fields are the effective per-category searchable field sets
    (searchable_field_sets); they drive both resolution and whether a scoped value is
    coerced to a number. They default to the module-level sets so the grammar stays
    callable with no DB."""
    scope = _SCOPE_RE.match(term)
    if scope:
        raw = scope.group(1)
        field = _FIELD_ALIASES.get(raw, raw)
        value = scope.group(2).strip()
        # Scope only when the prefix RESOLVES to a real user-facing field: a known
        # alias, a searchable or numeric field, or a dynamic attribute present on this
        # record that is not a core/bookkeeping key (#306). An unresolved prefix (or an
        # empty value) is not a scope - fall through so a literal `foo:bar` in the text
        # is still found instead of dropping the search.
        resolved = bool(value) and (
            raw in _FIELD_ALIASES
            or field in numeric_fields
            or field in text_fields
            or (field in record and not is_core_item_key(field))
        )
        if resolved:
            # Textual identifier fields (sku, barcode, hs_code, batch_no, lot...) are
            # matched as strings only - never coerced to numbers - so leading zeros and
            # other identity survive: `sku: 001` must not match a stored "1", and a
            # barcode `00123` must not match "123". Numeric coercion (range and exact
            # equality) applies to every field that is not a known text field: genuine
            # numeric columns, number-typed category fields, and dynamic attributes with
            # no schema entry (which are in neither set).
            numeric_ok = field not in text_fields
            rng = _RANGE_RE.match(value)
            if numeric_ok and rng:
                lo, hi = float(rng.group(1)), float(rng.group(2))
                if lo <= hi:
                    try:
                        n = float(record.get(field))
                    except (TypeError, ValueError):
                        return None
                    if lo <= n <= hi:
                        return field, format(n, "g")
                    return None
                # lo > hi is not a usable range; fall through to a scoped value match.
            stored = record.get(field)
            if numeric_ok:
                qnum, snum = _as_number(value), _as_number(stored)
                if qnum is not None and snum is not None:
                    # Numeric-coercible on both sides: exact equality, never substring, so
                    # `qty: 1` does not match 10 and `grade: 3` does not match 30.
                    return (field, value) if qnum == snum else None
            if value.lower() in str(stored if stored is not None else "").lower():
                return field, value
            return None
        # Unresolved prefix or empty scoped value: fall through to the unscoped path.
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


def query_match_reasons(
    record: dict, q: str,
    numeric_fields: frozenset[str] = _DEFAULT_NUMERIC_FIELDS,
    text_fields: frozenset[str] = _DEFAULT_TEXT_FIELDS,
) -> list[tuple[str, str]] | None:
    """Match a flattened item dict against the search grammar. `,` ORs groups, `&`
    ANDs the terms within a group; empty terms and empty groups are dropped.
    Returns the first matching group's (field, matched text) pairs - one per
    AND-term, deduped, order preserved - or None when no group matches.

    numeric_fields / text_fields are the effective per-category searchable field sets
    threaded to _term_match_reason; they default to the module-level sets."""
    for group in q.split(","):
        terms = [t.strip().lower() for t in group.split("&") if t.strip()]
        if not terms:
            continue
        reasons = [_term_match_reason(record, term, numeric_fields, text_fields) for term in terms]
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


@dataclass
class ItemListFilters:
    """The item list's filters, shared by the list and its CSV export so both see one set."""
    q: str | None = None
    sku: str | None = None
    skus: str | None = None  # comma-separated exact SKU list
    barcode: str | None = None
    gtin: str | None = None
    rfid_epc: str | None = None
    status: str | None = None
    category: str | None = None
    inventory_type: str | None = None
    location_id: str | None = None
    source: str | None = None
    filter: str | None = None
    on_memo_to: str | None = None
    consigned_from: str | None = None
    sort: str | None = None
    dir: str = "desc"


def _attr_filters(request: Request) -> list[tuple[str, set[str]]]:
    """Category-attribute column filters: ?attr.<key>=v1,v2 keeps items whose (flattened)
    attribute value is in the chosen set. Multiple attribute filters AND together."""
    out: list[tuple[str, set[str]]] = []
    for qk, qv in request.query_params.multi_items():
        if not qk.startswith("attr.") or not qv:
            continue
        wanted = {x.strip() for x in qv.split(",") if x.strip()}
        if wanted:
            out.append((qk[len("attr."):], wanted))
    return out


def _list_value(flat: dict, price_list: str, currency: str) -> Decimal | None:
    """A flattened item's value on one price list in the company currency, or None when
    it cannot be valued. Totals sum these, so each row is money before it is added.

    Cost values at the lot total (recipe standard x qty when recipe-backed), else the
    cost list's unit price x quantity; every other list (derived lists included, which
    flatten_item resolves) is its unit price x quantity."""
    lot_cost = coerce_price(flat.get("cost_total"))
    if is_cost_list_name(price_list) and lot_cost is not None:
        return round_money(lot_cost, currency)
    unit = stored_price(flat, price_list)
    qty = coerce_price(flat.get("quantity"))
    if unit is None or qty is None:
        return None
    return round_money(to_decimal(unit) * to_decimal(qty), currency)


def _unit_total(total: Decimal, unit: str, unit_map: dict[str, dict]) -> float:
    """A summed amount at its unit's configured precision; a unit with no configured
    precision (a legacy name) is reported exactly as summed."""
    decimals = (unit_map.get(unit) or {}).get("decimals")
    if decimals is None:
        return float(total)
    return float(total.quantize(Decimal(10) ** -int(decimals), rounding=ROUND_HALF_UP))


def result_aggregates(
    result: list[dict], price_lists: list[dict], can_see_costs: bool, currency: str, unit_map: dict[str, dict],
) -> dict:
    """Totals of exactly the rows in ``result`` (the filtered, visibility-stripped set).

    Amounts are grouped by their own unit and never added across units; a row whose
    amount or price the role cannot see is left out of that total, and price totals
    count the rows they leave out (price_missing) instead of reading them as zero.
    Cost lists are omitted entirely for a role without view_inventory_costs. Price totals
    sum each row's value in the company currency, as the store-wide valuation does.
    Quantities and weights sum exactly and report at each unit's precision."""
    quantity_by_unit: dict[str, Decimal] = {}
    weight_by_unit: dict[str, Decimal] = {}
    pieces_total: Decimal | None = None
    names = [pl.get("name", "") for pl in price_lists
             if can_see_costs or not is_cost_list_name(pl.get("name", ""))]
    price_totals = {name: Decimal(0) for name in names}
    price_missing = {name: 0 for name in names}
    for r in result:
        # coerce_price reads any stored amount as a finite number or None.
        qty = coerce_price(r.get("quantity"))
        if qty is not None:
            unit = str(r.get("sell_by") or "")
            quantity_by_unit[unit] = quantity_by_unit.get(unit, Decimal(0)) + to_decimal(qty)
        weight = coerce_price(r.get("weight"))
        if weight is not None:
            unit = str(r.get("weight_unit") or "")
            weight_by_unit[unit] = weight_by_unit.get(unit, Decimal(0)) + to_decimal(weight)
        pieces = coerce_price(r.get("pieces"))
        if pieces is not None:
            pieces_total = (pieces_total or Decimal(0)) + to_decimal(pieces)
        for name in names:
            value = _list_value(r, name, currency)
            if value is None:
                price_missing[name] += 1
            else:
                price_totals[name] += value
    return {
        "item_count": len(result),
        "quantity_by_unit": {k: _unit_total(v, k, unit_map) for k, v in quantity_by_unit.items()},
        "weight_by_unit": {k: _unit_total(v, k, unit_map) for k, v in weight_by_unit.items()},
        "pieces_total": None if pieces_total is None else float(pieces_total),
        "price_totals": {k: to_stored_float(v) for k, v in price_totals.items()},
        "price_missing": price_missing,
    }


async def query_items(
    session: AsyncSession, company_id, role: str, f: ItemListFilters, attr_filters: list[tuple[str, set[str]]],
) -> dict:
    """The filtered, ordered item set for ``f`` with its facets and totals, before pagination.
    Shared by list_items (which pages ``items``) and the CSV export (which never does)."""
    from celerp.services.reorder import is_below_reorder
    from celerp.models.company import Company
    from .search import (
        apply_item_order,
        apply_query_match,
        flatten_item_rows,
        load_item_rows,
        strip_field_visibility,
    )
    # Load + flatten is the shared front of the search pipeline (single-sourced in
    # celerp_inventory.search). The projection set is read once here and reused:
    # the holdings/sold scopes below need the raw rows and their state, and the
    # flatten runs over that same snapshot rather than issuing a second full load.
    company = await session.get(Company, company_id)
    settings = (company.settings if company else {}) or {}
    # Memo/consignment membership and sold prices come from documents, so they follow the
    # document permission: a contact scope is refused without it, and a sold row carries
    # no price.
    can_see_docs = role_has_permission(settings, role, "view_documents")
    base_currency = settings.get("currency") or "USD"
    holding_scoped = bool(f.on_memo_to or f.consigned_from)
    if holding_scoped:
        assert_role_permission(settings, role, "view_documents")
    rows = await load_item_rows(session, company_id)
    result = await flatten_item_rows(session, company_id, rows)

    # Status filtering: default excludes hidden statuses; "all" skips filtering; "archived" expands
    # to include merged/expired; a comma-separated value matches any (column-filter multi-select).
    status_set = {s.strip().lower() for s in f.status.split(",") if s.strip()} if (f.status and "," in f.status) else None
    if f.status == "all":
        pass  # no filter
    elif status_set:
        result = [r for r in result if str(r.get("status") or "").lower() in status_set]
    elif f.status == "archived":
        result = [r for r in result if str(r.get("status") or "").lower() in _ARCHIVED_GROUP]
    elif f.status:
        result = [r for r in result if str(r.get("status") or "").lower() == f.status.lower()]
    else:
        result = [r for r in result if str(r.get("status") or "").lower() not in _HIDDEN_STATUSES]

    # Contact-scoped holdings: memo out to a customer, or consignment in from a supplier.
    # Membership is derived from that contact's docs (celerp.services.holdings), and is
    # authoritative: it narrows result on its own. The per-item scope value (quoted memo
    # price / consignment cost) is attached after cost-visibility gating, below.
    scope_value: dict[str, float | None] = {}
    if holding_scoped:
        from celerp.services.holdings import consignment_holdings, memo_holdings
        items_state = [(r.entity_id, r.state) for r in rows]
        scope_doc_type = "memo" if f.on_memo_to else "consignment_in"
        scope_contact = f.on_memo_to or f.consigned_from
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
            memo_holdings(items_state, issued, base_currency) if f.on_memo_to
            else consignment_holdings(items_state, issued, base_currency)
        )
        result = [r for r in result if r.get("id") in scope_value]

    # Sold price: when the sold view is active, price each sold item from the line of
    # the document that sold it (status_doc_id). A realized sale price is not a cost, so
    # (like the memo value below) it is not gated by view_inventory_costs. Computed here
    # over the loaded rows; attached to the result dicts after visibility rebuild.
    sold_scoped = can_see_docs and "sold" in (status_set or {str(f.status).lower()} if f.status else set())
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
                base_currency,
            )

    # Connector source: items linked to a platform this company is connected to; a link
    # left from an earlier connection is history. Powers the connector detail "View N
    # synced products" link. Channel links are never a schema field, so they carry no
    # visible_to_roles floor and are safe to filter here; category/inventory_type/
    # location_id ARE schema fields a role may be denied, so their filters run after
    # apply_field_visibility (below) to avoid a membership oracle.
    from celerp.connectors import ownership
    try:
        _connected = await ownership.connected_connector_platforms(session, company_id)
    except ownership.ConnectorOwnershipError:
        _connected = set()
    if f.source:
        from celerp_inventory.services import external_link_for_state
        _platform = f.source.strip().lower()
        result = [
            r for r in result
            if _platform in _connected and external_link_for_state(r, _platform)
        ]

    # Apply visible_to_roles filtering from the effective field schema BEFORE any
    # membership-affecting step (category/inventory_type/location filters, facets, attr.*
    # filter, sku/barcode, low_stock, and search matching). Were a hidden field allowed to
    # steer membership, an item's mere
    # presence in the result set would disclose that field's value - searching `qty: 5`
    # and getting the row back reveals the hidden quantity, and a low_stock or attr.*
    # hit is the same oracle. apply_field_visibility drops stripped keys from the dict,
    # so every step below operates only over fields the requesting role may see. The
    # effective schema is resolved PER the item's category, mirroring the detail
    # endpoint, so a category-scoped restriction is honored and a null/unresolved
    # category falls back to the base item schema (identical disclosure to detail).
    can_see_costs = role_has_permission(settings, role, "view_inventory_costs")
    # Per-category strip + searchable field sets are the shared visibility phase
    # (single-sourced in celerp_inventory.search). item_field_sets drives the q-search
    # loop below. The helper resolves the effective schema per the item's category,
    # mirroring the detail endpoint, so a category-scoped restriction is honored and a
    # null/unresolved category falls back to the base item schema.
    result, item_field_sets = await strip_field_visibility(session, company_id, role, result)

    # category / inventory_type / location_id are schema fields a role may be denied, so
    # they filter over the visibility-stripped dicts: a denied role sees the key absent
    # (None), the value never matches, and membership cannot disclose the hidden value -
    # the same oracle closure applied to q, attr.*, and low_stock above.
    if f.category:
        cats = {c.strip() for c in f.category.split(",") if c.strip()}
        result = [r for r in result if str(r.get("category") or "") in cats]
    if f.inventory_type:
        types = {it.strip() for it in f.inventory_type.split(",") if it.strip()}
        result = [r for r in result if "inventory_type" in r and r.get("inventory_type") in types]
    if f.location_id:
        locs = {loc.strip() for loc in f.location_id.split(",") if loc.strip()}
        result = [r for r in result if str(r.get("location_id") or "") in locs]

    # Distinct attribute values for the column-filter funnels, over the status/category/type/location
    # scope and BEFORE attribute filters are applied (so every available value stays selectable).
    # Built from the visibility-stripped dicts: an attribute is a non-core, non-measure key on the
    # flattened item, so a role-hidden attribute (stripped above) never appears in a facet value.
    # A custom numeric attribute (a `number`-typed `length`) IS facetable; only the continuous
    # per-item measures (_NUMERIC_MEASURE_KEYS) are excluded.
    _FACET_MAX = 500
    facet_sets: dict[str, set] = {}
    for r in result:
        for akey, aval in r.items():
            if is_core_item_key(akey) or akey in _NUMERIC_MEASURE_KEYS or aval in (None, ""):
                continue
            s = facet_sets.setdefault(akey, set())
            if len(s) < _FACET_MAX:
                s.add(str(aval))
    attribute_facets = {k: sorted(s) for k, s in facet_sets.items() if s}

    # Category-attribute column filters AND together (see _attr_filters).
    for akey, wanted in attr_filters:
        result = [r for r in result if str(r.get(akey) if r.get(akey) is not None else "") in wanted]

    if f.sku:
        result = [r for r in result if str(r.get("sku", "")) == f.sku]

    if f.skus:
        sku_set = {s.strip() for s in f.skus.split(",") if s.strip()}
        result = [r for r in result if str(r.get("sku", "")) in sku_set]

    if f.barcode:
        result = [r for r in result if str(r.get("barcode", "")) == f.barcode]

    if f.gtin:
        result = [r for r in result if str(r.get("gtin", "")) == f.gtin]

    if f.rfid_epc:
        # EPC is stored normalized (upper-cased); normalize the filter so a lower-case
        # query matches the stored value.
        _epc = normalize_rfid_epc(f.rfid_epc)
        result = [r for r in result if str(r.get("rfid_epc", "")) == _epc]

    # Semantic "low stock" filter: at or below reorder point (backs the dashboard
    # cards' /inventory?filter=low_stock link and the reorder alert action_url).
    if f.filter == "low_stock":
        # Drafts are not stock: an unfinished item must not raise a reorder alarm.
        # Guarded on a visible quantity: is_below_reorder reads quantity defaulting a
        # missing value to 0, so a stripped (role-hidden) quantity would falsely include
        # the item - excluding it keeps low_stock from being an oracle over the hidden
        # quantity / reorder point.
        result = [r for r in result
                  if "quantity" in r and is_below_reorder(r)
                  and str(r.get("status") or "").lower() != "draft"]

    if f.q:
        # Shared q-filter + q_match attachment (single-sourced in celerp_inventory.search):
        # comma = OR groups, & = AND terms, lo-hi = numeric range, bare number =
        # numeric-exact OR text, else text substring. Each item is matched against its own
        # category's numeric/text field sets, so a number-typed category field resolves and
        # a text-typed one is not coerced. Reasons are computed over the visibility-filtered
        # dict, so every cited field is one the role may see; no post-filter is needed.
        result = apply_query_match(result, f.q, item_field_sets)

    # Attach the per-item scope value AFTER visibility (so it survives any dict rebuild).
    # The consignment value is cost, so it is gated by view_inventory_costs exactly like
    # every other cost figure; the memo value is a quoted sale price and is not gated.
    gate_cost = bool(f.consigned_from) and not can_see_costs
    if holding_scoped:
        for r in result:
            r["holding_value"] = None if gate_cost else scope_value.get(r.get("id"))

    # Attach the realized sale price to each sold row (not a cost, so not cost-gated).
    sold_result = [r for r in result if str(r.get("status") or "").lower() == "sold"] if sold_scoped else []
    for r in sold_result:
        r["sold_price"] = sold_price.get(r.get("id"))

    # Totals of the final filtered set, so they describe exactly the rows `total` counts,
    # on every page and for any combination of filters.
    aggregates = result_aggregates(
        result, (await get_price_config(session, company_id))[0], can_see_costs, base_currency,
        build_unit_map(await get_company_units(session, company_id)))

    # Ordering (FEFO / user column sort / default) is single-sourced in
    # celerp_inventory.search so the list and the global-search bar stay in
    # lockstep. Applied AFTER all filtering so pagination is globally correct.
    apply_item_order(
        result,
        inventory_method=(company.settings or {}).get("inventory_method") if company else None,
        sort=f.sort,
        direction=f.dir,
        status=f.status,
    )

    from celerp_inventory.services import build_channel_states
    _channel_states = build_channel_states(rows, connected_platforms=_connected)
    for _item in result:
        _item["_channel_state"] = _channel_states.get(_item.get("id"), {})

    resp: dict = {"items": result, "total": len(result), "attribute_facets": attribute_facets,
                  "aggregates": aggregates}
    if holding_scoped and not gate_cost:
        # Total over the whole scoped set (post-filter, pre-pagination) so the contact
        # card reads it directly and reconciles with the list at the same value basis; items
        # with no resolvable value are counted, never estimated.
        from celerp.services.holdings import value_total
        resp["value_total"], resp["value_total_missing"] = value_total(
            (scope_value.get(r.get("id")) for r in result), base_currency)
    if sold_scoped:
        # Realized value over the WHOLE filtered set (pre-pagination) so the sold view's
        # Total card reads the same figure on every page; rows without a resolvable
        # selling line are counted so the UI can say how many the total leaves out.
        from celerp.services.holdings import sold_value_total
        resp["sold_total"], resp["sold_total_missing"] = sold_value_total(sold_result, sold_price, base_currency)
    return resp


@router.get("", openapi_extra={"x-celerp-agent": True})
async def list_items(
    request: Request,
    company_id=Depends(get_current_company_id),
    _: None = require_permission("view_inventory"),
    session: AsyncSession = Depends(get_session),
    role: str = Depends(get_current_role),
    filters: ItemListFilters = Depends(),
    limit: int = 50,
    offset: int = 0,
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
    resp = await query_items(session, company_id, role, filters, _attr_filters(request))
    resp["items"] = resp["items"][offset: offset + limit]
    return resp


@router.get("/valuation", openapi_extra={"x-celerp-agent": True})
async def get_valuation(
    category: str | None = None,
    status: str | None = None,
    on_memo_to: str | None = None,
    consigned_from: str | None = None,
    company_id=Depends(get_current_company_id),
    _: None = require_permission("view_inventory"),
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

    currency = settings.get("currency") or "USD"
    holding_scope: set[str] | None = None
    if on_memo_to or consigned_from:
        assert_role_permission(settings, role, "view_documents")
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
            memo_holdings(items_state, issued, currency) if on_memo_to
            else consignment_holdings(items_state, issued, currency)
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

        # Consigned-in goods are borrowed, not owned, so they stay out of stock value. Under a
        # holdings scope the scope alone decides membership, so the cards count what the list shows.
        if holding_scope is None and (row.consignment_flag == "in" or state.get("consignment_flag") == "in"):
            continue

        # Only goods the company holds have physical value; services and non-stocked do not.
        if (state.get("inventory_type") or "stocked") not in STOCK_TYPES:
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
        # Value from the flattened item so cost (recipe standard / lot total) and derived
        # lists price identically to every other consumer of item state.
        flat = flatten_item(state, row.entity_id, price_config=_price_config)
        for pl in _price_lists:
            value = _list_value(flat, pl.get("name", ""), currency)
            if value is not None:
                price_totals[pl.get("name", "")] += value

    _cost_pl_names = {pl.get("name", "") for pl in _price_lists if is_cost_list_name(pl.get("name", ""))}
    show_cost = role_has_permission(settings, role, "view_inventory_costs")

    price_totals_out = {
        k: to_stored_float(v) for k, v in price_totals.items()
        if show_cost or k not in _cost_pl_names
    }

    result: dict = {
        "item_count": active_item_count,
        "active_item_count": active_item_count,
        "price_totals": price_totals_out,
        # Backward-compatible keys for existing UI
        "wholesale_total": to_stored_float(price_totals.get("Wholesale", Decimal(0))),
        "retail_total": to_stored_float(price_totals.get("Retail", Decimal(0))),
        "category_counts": dict(sorted(category_counts.items(), key=lambda x: -x[1])),
        # total_scoped_count backs the "All" tab: everything the scoped list shows,
        # which includes drafts even though they carry no stock value yet
        # (some items may have no category and won't appear in category_counts)
        "total_scoped_count": active_item_count + draft_count,
        "count_by_status": count_by_status,
    }
    if show_cost:
        result["cost_total"] = to_stored_float(price_totals.get("Cost", Decimal(0)))
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
    demo_eids = set(await demo_item_ids(session, company_id))
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


# Upper bound on a single bulk-metadata request. A detail list can carry a few
# thousand lines; 5000 covers the largest real lists with margin while keeping one
# request bounded (mirrors the bounded-list precedent on ImportRecord).
MAX_ITEMS_METADATA = 5000


class ItemsMetadataBody(BaseModel):
    entity_ids: list[str] = Field(..., min_length=1, max_length=MAX_ITEMS_METADATA)


@router.post("/metadata")
async def items_metadata(payload: ItemsMetadataBody, company_id=Depends(get_current_company_id), role: str = Depends(get_current_role), settings: dict = Depends(get_current_company_settings), session: AsyncSession = Depends(get_session)) -> dict:
    """Bulk item-metadata read: one entry per requested id, keyed by entity_id.

    Returns the same visibility-filtered flat dict GET /items/{entity_id} returns
    per item, minus the sold_price enrichment (list/doc/audit renderers never read
    it). This is a read gated by the router-level authentication; company_id is
    derived server-side from the JWT, never from the body, and the query is scoped
    to that company so it cannot read another company's items. Field/cost
    visibility is applied per the item's OWN category, exactly as the per-item
    route does, so restricted fields never leak. Unknown ids are simply absent from
    the result (no error, no fabricated entry).
    """
    from celerp.models.company import Location
    from celerp.services.field_schema import get_effective_field_schema

    # pydantic rejects an empty or over-max list with 422 before this runs; dedup
    # so a repeated id resolves once.
    ids = list(dict.fromkeys(payload.entity_ids))

    rows = (await session.execute(
        select(Projection).where(
            Projection.company_id == company_id,
            Projection.entity_type == "item",
            Projection.entity_id.in_(ids),
        )
    )).scalars().all()
    if not rows:
        return {"items": {}}

    loc_ids = {r.location_id for r in rows if r.location_id}
    loc_names: dict = {}
    if loc_ids:
        locs = (await session.execute(
            select(Location).where(Location.id.in_(loc_ids))
        )).scalars().all()
        loc_names = {loc.id: loc.name for loc in locs}

    price_config = await get_price_config(session, company_id)
    # Units are company-wide, so build the map once for the whole batch. It feeds
    # flatten_item's qty_each derivation, exactly as GET /items/{id} does per item.
    units = await _get_company_units(session, company_id)
    unit_map = {u["name"]: u for u in units}
    can_see_costs = role_has_permission(settings, role, "view_inventory_costs")
    can_author_drafts = role_has_permission(settings, role, "edit_inventory")

    flats: list[dict] = []
    for row in rows:
        flat = flatten_item(
            row.state, row.entity_id,
            location_id=str(row.location_id) if row.location_id else None,
            location_name=loc_names.get(row.location_id) if row.location_id else None,
            created_at=row.created_at,
            updated_at=row.updated_at,
            price_config=price_config,
            unit_map=unit_map,
        )
        flats.append(flat)

    # Group by the item's own category and apply the per-category effective schema
    # once per distinct category, exactly as GET /items/{id} does with a single
    # item. A single shared schema (or category=None) would leak a field restricted
    # in one category but visible in another.
    by_category: dict = {}
    for flat in flats:
        by_category.setdefault(flat.get("category"), []).append(flat)

    result: dict = {}
    for category, group in by_category.items():
        field_schema = await get_effective_field_schema(session, company_id, category=category)
        filtered = apply_item_visibility(
            group, role, field_schema, can_see_costs, can_author_drafts=can_author_drafts,
        )
        for flat in filtered:
            result[flat["id"]] = flat

    return {"items": result}


# ── Import routes ─────────────────────────────────────────────────────────────
# Declared before GET /{entity_id} so "import" is never captured as an entity id.
# One writer (services.write_import_batch), three transports: the browser
# importer (/import/rows) and the agent commit (/import/commit) through
# services.import_items, and the raw event batch (/import/batch) through
# services.commit_import_batch.


def _bounded_rows(rows: list[dict]) -> list[dict]:
    """Hold mapped rows to the same cell budget as a parsed upload."""
    if sum(len(r) for r in rows) > MAX_CELLS:
        raise ValueError(f"Too many cells: the limit is {MAX_CELLS}")
    return rows


def _rows_preview_hash(rows: list[dict], upsert: bool, idempotency_key: str | None, semantic_fingerprint: str) -> str:
    """Binds the rows, the update-existing choice, the operation key, and what
    the rows meant when previewed."""
    return import_preview_hash({
        "rows": rows, "upsert": upsert, "idempotency_key": idempotency_key,
        "semantic_fingerprint": semantic_fingerprint,
    })


def _validation_failed(errors: list[dict]) -> HTTPException:
    return HTTPException(status_code=422, detail={"code": "validation_failed", "errors": errors})


def _preview_stale() -> HTTPException:
    return HTTPException(status_code=409, detail={"code": "preview_stale"})


async def _import_authority(session, company_id, user_id) -> tuple[str, dict]:
    """The importer's role and the company settings, read and held under the
    company lock before an import commit plans anything; a permission lost since
    the request was authorized is refused here."""
    return await locked_authority(session, company_id, user_id, ("import_export_data", "edit_inventory"))


async def _write_import(session, company_id, user_id, role: str, settings: dict, rows: list[dict], **kwargs) -> BatchImportResult:
    """Run import_items, answering its row rejections as 422 validation_failed."""
    try:
        return await import_items(session, company_id, user_id, role, settings, rows, **kwargs)
    except ImportRejected as exc:
        raise _validation_failed(exc.errors)


class InventoryImportRows(BaseModel):
    # The writer commits in batches of 500 internally; the envelope carries the
    # whole import so it is previewed and committed as one operation.
    rows: list[dict] = Field(..., max_length=MAX_ROWS)
    upsert: bool = False
    filename: str | None = None
    idempotency_key: str | None = None
    preview_hash: str | None = Field(None, min_length=64, max_length=64)

    @field_validator("rows")
    @classmethod
    def _check_rows(cls, rows: list[dict]) -> list[dict]:
        return _bounded_rows(rows)


class InventoryImportRowsPreviewRequest(BaseModel):
    rows: list[dict] = Field(..., max_length=MAX_ROWS)
    upsert: bool = False
    idempotency_key: str | None = None

    @field_validator("rows")
    @classmethod
    def _check_rows(cls, rows: list[dict]) -> list[dict]:
        return _bounded_rows(rows)


class InventoryImportRowsPreview(BaseModel):
    errors: list[dict]
    locations_to_create: list[str]
    preview_hash: str


@router.post(
    "/import/rows/preview", response_model=InventoryImportRowsPreview,
    dependencies=[require_permission("import_export_data")],
)
async def import_rows_preview(
    body: InventoryImportRowsPreviewRequest,
    company_id=Depends(get_current_company_id),
    role: str = Depends(get_current_role),
    settings: dict = Depends(get_current_company_settings),
    session: AsyncSession = Depends(get_session),
) -> InventoryImportRowsPreview:
    """Semantic preview of mapped browser rows; nothing is written.

    The returned hash binds the rows, the update-existing choice, the operation
    key, and what the rows mean now, and /import/rows refuses a commit whose
    hash no longer matches.
    """
    plan = await preview_import_rows(
        session, company_id, role, settings, body.rows,
        upsert=body.upsert, idempotency_key=body.idempotency_key,
    )
    return InventoryImportRowsPreview(
        errors=plan.errors,
        locations_to_create=plan.locations_to_create,
        preview_hash=_rows_preview_hash(body.rows, body.upsert, body.idempotency_key, plan.semantic_fingerprint),
    )


@router.post(
    "/import/rows", response_model=BatchImportResult,
    dependencies=[require_permission("import_export_data"), require_permission("edit_inventory")],
)
async def import_rows(
    body: InventoryImportRows,
    company_id=Depends(get_current_company_id),
    role: str = Depends(get_current_role),
    settings: dict = Depends(get_current_company_settings),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> BatchImportResult:
    """Commit already-mapped import rows (the browser importer's transport).

    Rows arrive mapped and fixed by the UI. The shared committer owns location
    resolution and creation, unit and quantity derivation, monetary conversion,
    idempotency, and the category-schema follow-up. Unmarked: this is the browser
    transport, not an agent capability (the agent commits through /import/commit).

    Every commit runs the semantic preflight once, and any row error is refused
    with 422 before anything is written. With ``preview_hash`` the commit is also
    bound to /import/rows/preview: the preview is recomputed, a changed hash,
    including rows that now mean something else, is refused with 409, and the
    rows are written from that recomputed plan. The company lock is taken before
    that preview and held through the write.
    """
    role, settings = await _import_authority(session, company_id, user.id)
    plan = None
    if body.preview_hash is not None:
        plan = await preview_import_rows(
            session, company_id, role, settings, body.rows,
            upsert=body.upsert, idempotency_key=body.idempotency_key,
        )
        if _rows_preview_hash(body.rows, body.upsert, body.idempotency_key, plan.semantic_fingerprint) != body.preview_hash:
            raise _preview_stale()
        if plan.errors:
            raise _validation_failed(plan.errors)
    return await _write_import(
        session, company_id, user.id, role, settings, body.rows,
        upsert=body.upsert, filename=body.filename, idempotency_key=body.idempotency_key,
        plan=plan,
    )


# Agent import: preview then commit. The agent uploads a CSV/xlsx, previews the
# suggested mapping and any row errors, then commits by echoing the preview hash
# so a file that changed between the two calls is refused rather than silently
# imported under a stale mapping.

_AI_FILE_ID_RE = re.compile(r"^ai_up_[0-9a-f]{32}$")


class InventoryImportPreview(BaseModel):
    file_id: str
    sheet: str | None
    upsert: bool
    columns: list[str]
    mapping: dict[str, str]
    unmapped_required: list[str]
    row_count: int
    sample: list[dict]
    errors: list[dict]
    locations_to_create: list[str] = Field(default_factory=list)
    preview_hash: str


class InventoryImportPreviewRequest(BaseModel):
    file_id: str = Field(..., min_length=1, max_length=64)
    sheet: str | None = Field(None, max_length=64)
    upsert: bool = False
    mapping: dict[str, str] | None = None


async def _build_item_preview(
    session, company_id, user_id, role: str, settings: dict, *,
    file_id: str, sheet: str | None, upsert: bool, mapping: dict[str, str] | None = None,
    idempotency_key: str | None = None,
) -> dict:
    """Load an uploaded file, map and validate it, and run the import preflight.

    Returns the preview payload plus the mapped rows, the flat error list, the
    original filename, the preview hash, and the semantic fingerprint.
    Recomputed identically by preview and commit so the hash pins the exact
    bytes, sheet, mapping, row count, and what the rows would write.
    ``idempotency_key`` is the commit's operation key (None when previewing).

    Raises 404 when the file id is malformed, missing, or owned by another
    company; 422 when the bytes cannot be read as a table.
    """

    from celerp.ai.files import load_file
    from celerp.importers.tabular import (
        TabularError,
        normalize_and_validate_mapping,
        read_table,
        remap_rows,
        suggest_mapping,
    )
    from celerp.services.field_schema import all_category_schemas, union_category_attr_keys

    if not _AI_FILE_ID_RE.match(file_id):
        raise HTTPException(status_code=404, detail="File not found")
    try:
        data, meta = load_file(file_id, company_id, user_id)
    except (FileNotFoundError, PermissionError):
        raise HTTPException(status_code=404, detail="File not found")

    filename = meta.get("filename") or file_id
    try:
        cols, rows = read_table(data, filename, sheet=sheet)
    except TabularError as exc:
        detail: dict = {"code": "unreadable_file", "message": str(exc)}
        if exc.sheets:
            detail["sheets"] = exc.sheets
        raise HTTPException(status_code=422, detail=detail)

    price_lists, _default_list, _currency = await get_price_config(session, company_id)
    spec = build_item_import_spec(price_lists)
    # The same suggestion the browser mapper renders; the caller's mapping
    # overrides it column by column.
    category_attrs = union_category_attr_keys(all_category_schemas(settings))
    resolved = normalize_and_validate_mapping(
        cols, suggest_mapping(cols, spec.cols, category_attrs), mapping,
        allowed_targets=spec.cols,
        required_targets=spec.required,
        allowed_category_attrs=category_attrs,
        is_reserved_field=is_item_field_key,
        mutex_groups=item_price_mutex_groups(price_lists),
    )
    mapping = resolved.mapping
    semantics = source_header_semantics(mapping, settings.get("currency") or "USD")
    # Rows are only previewed under a mapping that can be applied.
    mapped_rows: list[dict] = []
    plan = None
    errors = resolved.errors + semantics.errors
    if resolved.applicable:
        _new_cols, mapped_rows = remap_rows(cols, rows, mapping)
        mapped_rows = apply_source_semantics(mapped_rows, semantics)
        plan = await preview_import_rows(
            session, company_id, role, settings, mapped_rows,
            upsert=upsert, idempotency_key=idempotency_key,
        )
        errors += plan.errors
    errors = errors[:50]

    unmapped_required = sorted(r for r in spec.required if r not in set(mapping.values()))
    row_count = len(rows)
    preview_hash = import_preview_hash({
        "file_id": file_id,
        "sheet": sheet,
        "upsert": upsert,
        "mapping": mapping,
        "row_count": row_count,
        "file_sha256": hashlib.sha256(data).hexdigest(),
        "semantic_fingerprint": plan.semantic_fingerprint if plan else None,
    })

    return {
        "payload": InventoryImportPreview(
            file_id=file_id, sheet=sheet, upsert=upsert, columns=cols,
            mapping=mapping, unmapped_required=unmapped_required, row_count=row_count,
            sample=mapped_rows[:5], errors=errors,
            locations_to_create=plan.locations_to_create if plan else [], preview_hash=preview_hash,
        ),
        "errors": errors,
        "mapped_rows": mapped_rows,
        "filename": filename,
        "preview_hash": preview_hash,
        "plan": plan,
    }


@router.get(
    "/import/preview", response_model=InventoryImportPreview,
    dependencies=[require_permission("import_export_data")],
)
async def import_preview(
    file_id: str = Query(..., min_length=1, max_length=64),
    sheet: str | None = Query(None, max_length=64),
    upsert: bool = Query(False),
    company_id=Depends(get_current_company_id),
    role: str = Depends(get_current_role),
    settings: dict = Depends(get_current_company_settings),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> InventoryImportPreview:
    """Preview an uploaded item import for the browser UI."""
    result = await _build_item_preview(
        session, company_id, user.id, role, settings,
        file_id=file_id, sheet=sheet, upsert=upsert,
    )
    return result["payload"]


@router.post(
    "/import/preview", response_model=InventoryImportPreview,
    openapi_extra={"x-celerp-agent": True, "x-celerp-agent-confirm": False},
    dependencies=[require_permission("import_export_data")],
)
async def import_preview_agent(
    body: InventoryImportPreviewRequest,
    company_id=Depends(get_current_company_id),
    role: str = Depends(get_current_role),
    settings: dict = Depends(get_current_company_settings),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> InventoryImportPreview:
    """Preview an uploaded catalog with an optional caller-corrected mapping."""
    result = await _build_item_preview(
        session, company_id, user.id, role, settings, file_id=body.file_id,
        sheet=body.sheet, upsert=body.upsert, mapping=body.mapping,
    )
    return result["payload"]


class InventoryImportCommit(BaseModel):
    file_id: str = Field(..., min_length=1, max_length=64)
    sheet: str | None = Field(None, max_length=64)
    upsert: bool = False
    mapping: dict[str, str] | None = None
    preview_hash: str = Field(..., min_length=64, max_length=64)


@router.post(
    "/import/commit", response_model=BatchImportResult,
    openapi_extra={"x-celerp-agent": True, "x-celerp-agent-idempotent": True},
    dependencies=[require_permission("import_export_data"), require_permission("edit_inventory")],
)
async def import_commit(
    body: InventoryImportCommit,
    company_id=Depends(get_current_company_id),
    role: str = Depends(get_current_role),
    settings: dict = Depends(get_current_company_settings),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> BatchImportResult:
    """Commit an item import previewed via /import/preview.

    Recomputes the preview from the stored bytes; a hash mismatch means the file,
    its mapping, or what its rows would write changed since the preview, refused
    with 409 rather than imported under stale assumptions. Any row validation
    error is refused with 422 and the error list; otherwise the shared committer
    writes the rows from that recomputed plan. The company lock is taken before
    that preview and held through the write.
    """
    role, settings = await _import_authority(session, company_id, user.id)
    operation_key = f"preview:{body.preview_hash}"
    result = await _build_item_preview(
        session, company_id, user.id, role, settings, file_id=body.file_id,
        sheet=body.sheet, upsert=body.upsert, mapping=body.mapping,
        idempotency_key=operation_key,
    )
    if result["preview_hash"] != body.preview_hash:
        raise _preview_stale()
    if result["errors"]:
        raise _validation_failed(result["errors"])
    return await _write_import(
        session, company_id, user.id, role, settings, result["mapped_rows"],
        upsert=body.upsert, filename=result["filename"], idempotency_key=operation_key,
        plan=result["plan"],
    )


@router.get("/{entity_id}", dependencies=[require_permission("view_inventory")], openapi_extra={"x-celerp-agent": True})
async def get_item(entity_id: str, company_id=Depends(get_current_company_id), role: str = Depends(get_current_role), settings: dict = Depends(get_current_company_settings), session: AsyncSession = Depends(get_session)) -> dict:
    from celerp.models.company import Location
    from celerp.services.field_schema import get_effective_field_schema
    row = await session.get(Projection, {"company_id": company_id, "entity_id": entity_id})
    if row is None or row.entity_type != "item":
        raise HTTPException(status_code=404, detail="Not found")
    loc_name: str | None = None
    if row.location_id:
        loc = (await session.execute(
            select(Location).where(
                Location.id == row.location_id, Location.company_id == company_id,
            )
        )).scalar_one_or_none()
        loc_name = loc.name if loc else None
    units = await _get_company_units(session, company_id)
    unit_map = {u["name"]: u for u in units}
    flat = flatten_item(row.state, row.entity_id,
                         location_id=str(row.location_id) if row.location_id else None,
                         location_name=loc_name,
                         created_at=row.created_at,
                         updated_at=row.updated_at,
                         price_config=await get_price_config(session, company_id),
                         unit_map=unit_map)
    field_schema = await get_effective_field_schema(session, company_id, category=flat.get("category"))
    can_see_costs = role_has_permission(settings, role, "view_inventory_costs")
    filtered = apply_item_visibility(
        [flat], role, field_schema, can_see_costs,
        can_author_drafts=role_has_permission(settings, role, "edit_inventory"),
    )
    result = filtered[0]
    if (str(row.state.get("status") or "").lower() == "sold" and row.state.get("status_doc_id")
            and role_has_permission(settings, role, "view_documents")):
        from celerp.services.holdings import sold_prices
        sold_doc = await session.get(Projection, {"company_id": company_id, "entity_id": str(row.state["status_doc_id"])})
        if sold_doc is not None:
            result["sold_price"] = sold_prices(
                [(row.entity_id, row.state)], [(sold_doc.entity_id, sold_doc.state)],
                settings.get("currency") or "USD",
            ).get(row.entity_id)
    return result


@router.get("/{entity_id}/reorder-suggestion", dependencies=[require_permission("view_inventory")], openapi_extra={"x-celerp-agent": True})
async def get_reorder_suggestion(entity_id: str, company_id=Depends(get_current_company_id), session: AsyncSession = Depends(get_session)) -> dict:
    """Suggested reorder_point / reorder_qty from trailing outbound velocity.

    Read-only assist for the item detail / bulk dialog - the stored fields stay the
    single source of truth. Returns nulls when there is no outbound history (never
    a fabricated number). See celerp.services.reorder.suggest_reorder.
    """
    from celerp.services.reorder import suggest_reorder
    row = await session.get(Projection, {"company_id": company_id, "entity_id": entity_id})
    if row is None or row.entity_type != "item":
        raise HTTPException(status_code=404, detail="Not found")
    return await suggest_reorder(session, company_id, entity_id)


class ResolveResult:
    """Result of resolving a scanned/typed code to item(s).

    ``kind`` is one of: "barcode" or "rfid_epc" (each matched a unique physical-lot
    identifier), "gtin" or "sku" (each matched a product identifier, which may map to
    N physical lots), or "none". ``matches`` is the list of matching item Projections
    (0, 1, or - for a product identifier - N). ``ambiguous`` is True only when a
    product identifier matched more than one lot: the caller must disambiguate (scan a
    physical code or pick a lot), never silently pick one.
    """
    __slots__ = ("kind", "matches")

    # A physical identifier resolves to exactly one physical lot; a product identifier
    # (gtin/sku) may legitimately map to many lots.
    _PHYSICAL_KINDS = frozenset({"barcode", "rfid_epc"})
    _PRODUCT_KINDS = frozenset({"gtin", "sku"})

    def __init__(self, kind: str, matches: list):
        self.kind = kind
        self.matches = matches

    @property
    def ambiguous(self) -> bool:
        return self.kind in self._PRODUCT_KINDS and len(self.matches) > 1

    @property
    def duplicate_physical(self) -> bool:
        """True when a code resolves to more than one distinct physical item. Barcode and
        RFID EPC are ONE physical namespace, so this covers both a single field matching
        two lots (legacy rows written before a per-company unique index) AND a value held
        as one item's barcode and another item's rfid_epc (a cross-field collision no
        single-field index catches). Distinct from ``ambiguous`` (the product-identifier
        concept): a duplicate physical code must be reported, never silently resolved to
        one lot."""
        return self.kind in self._PHYSICAL_KINDS and len(self.matches) > 1

    @property
    def one(self):
        """The single match, or None when there are zero or (ambiguously) many."""
        return self.matches[0] if len(self.matches) == 1 else None


def _resolve_from_candidates(barcode_matches, rfid_matches, gtin_matches, sku_matches) -> "ResolveResult":
    """Choose a ResolveResult from the per-field candidate lists, enforcing the shared
    physical namespace. Barcode and RFID EPC are one namespace: a code matching EITHER
    field is a physical match, gathered BEFORE any product identifier is considered. The
    physical union is deduped by ``entity_id`` (a single item carrying both a barcode and
    an EPC is ONE item, not a duplicate). If the union spans more than one distinct item
    the resolver fails closed (``duplicate_physical`` True, ``one`` None), never silently
    picking one; a single physical item resolves (kind "barcode" when a barcode matched,
    else "rfid_epc"). Only with NO physical match do the product identifiers resolve -
    gtin then sku - each to its N lots. Shared by both the single and the batch resolver
    so they disambiguate identically."""
    physical: dict = {}
    for r in barcode_matches:
        physical.setdefault(r.entity_id, r)
    for r in rfid_matches:
        physical.setdefault(r.entity_id, r)
    if physical:
        kind = "barcode" if barcode_matches else "rfid_epc"
        return ResolveResult(kind, list(physical.values()))
    if gtin_matches:
        return ResolveResult("gtin", gtin_matches)
    if sku_matches:
        return ResolveResult("sku", sku_matches)
    return ResolveResult("none", [])


def duplicate_barcode_detail(code: str) -> str:
    """The single operator-facing message for a physical code that resolves to more than
    one lot, shared by every scan surface so the wording is sourced in one place."""
    return f"{code}: more than one inventory item has this physical code"


# Statuses whose items are retained for history but are no longer a current physical
# lot, so they must not participate in operational resolution. A `merged` source keeps
# its original barcode AND sku (item.source_deactivated sets status only), so an
# unfiltered candidate set counts it as live: by barcode it falsely trips
# `duplicate_physical`, and by sku (a numeric sku may equal a barcode) it re-enters
# resolution through the fallback. Scope is `merged` ONLY: reserved/memo_out/sold/
# archived/expired must still resolve. PHYSICAL_CODE_RESOLVE_EXCLUDED_STATUSES is
# applied to every identifier candidate set, in both resolvers, and shared with
# Doctor's physical-code conflict report.


async def resolve_item_by_code(session: AsyncSession, company_id, code: str) -> ResolveResult:
    """Canonical code -> item(s) resolver. A physical identifier (barcode, RFID EPC) wins
    and resolves to one lot; a product identifier (GTIN, SKU) may resolve to N.

    This is the single disambiguation rule shared by every scan/lookup surface so they
    behave identically. Physical identifiers (barcode + RFID EPC) share one namespace and
    are gathered together before the product identifiers (GTIN, SKU) are considered: a
    value held as one item's barcode and another's rfid_epc spans two physical items and
    fails closed rather than resolving barcode-first. An RFID reader emitting an EPC feeds
    this same path as a barcode scanner; the EPC is normalized (trimmed + upper-cased)
    before compare so a scan resolves regardless of the reader's case. A physical code
    matching more than one distinct item is reported via ``duplicate_physical``
    (fail-closed), never silently resolved to one.
    """
    code = (code or "").strip()
    if not code:
        return ResolveResult("none", [])
    epc_code = normalize_rfid_epc(code)
    rows = (await session.execute(
        select(Projection).where(
            Projection.company_id == company_id,
            Projection.entity_type == "item",
        )
    )).scalars().all()

    def _live(r) -> bool:
        return str((r.state or {}).get("status") or "").lower() not in PHYSICAL_CODE_RESOLVE_EXCLUDED_STATUSES

    def _by(key, wanted):
        return [r for r in rows if str((r.state or {}).get(key) or "") == wanted and _live(r)]

    return _resolve_from_candidates(
        _by("barcode", code),
        _by("rfid_epc", epc_code),
        _by("gtin", code),
        _by("sku", code),
    )


async def resolve_items_by_codes(session: AsyncSession, company_id, codes) -> dict[str, "ResolveResult"]:
    """Batch form of :func:`resolve_item_by_code`: resolve many codes against ONE inventory load.

    A per-code caller (a 200-code scan run) would otherwise load every item projection once per
    code. This loads them once, indexes by every identifier, and returns one ResolveResult per
    distinct code - the SAME shared-namespace disambiguation rule as the single-code path
    (physical barcode + RFID EPC gathered before product GTIN/SKU, fail-closed on a cross-field
    or multi-lot physical collision), so callers behave identically. EPC is indexed and looked
    up in its normalized (upper-cased) form.
    """
    wanted = {(c or "").strip() for c in codes if (c or "").strip()}
    if not wanted:
        return {}
    rows = (await session.execute(
        select(Projection).where(
            Projection.company_id == company_id,
            Projection.entity_type == "item",
        )
    )).scalars().all()
    by_barcode: dict[str, list] = {}
    by_rfid_epc: dict[str, list] = {}
    by_gtin: dict[str, list] = {}
    by_sku: dict[str, list] = {}
    for r in rows:
        st = r.state or {}
        if str(st.get("status") or "").lower() in PHYSICAL_CODE_RESOLVE_EXCLUDED_STATUSES:
            continue
        bc = str(st.get("barcode") or "")
        epc = str(st.get("rfid_epc") or "")
        gtin = str(st.get("gtin") or "")
        sku = str(st.get("sku") or "")
        if bc:
            by_barcode.setdefault(bc, []).append(r)
        if epc:
            by_rfid_epc.setdefault(epc, []).append(r)
        if gtin:
            by_gtin.setdefault(gtin, []).append(r)
        if sku:
            by_sku.setdefault(sku, []).append(r)
    out: dict[str, ResolveResult] = {}
    for code in wanted:
        epc_code = normalize_rfid_epc(code)
        out[code] = _resolve_from_candidates(
            by_barcode.get(code, []),
            by_rfid_epc.get(epc_code, []),
            by_gtin.get(code, []),
            by_sku.get(code, []),
        )
    return out


def _validate_sku(sku: str | None) -> None:
    """Friendly-422 wrapper over the canonical event-boundary rule (celerp.events.schemas), so an
    interactive route surfaces a clear message instead of the raw write-time rejection."""
    try:
        reject_comma_sku(sku)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


def _validate_gtin(gtin) -> None:
    """Friendly-422 wrapper over validate_gtin: an interactive route surfaces the format
    message instead of the raw ValueError falling through to a 500."""
    try:
        validate_gtin(gtin)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


def _validate_rfid_epc(rfid_epc) -> None:
    """Friendly-422 wrapper over validate_rfid_epc (same reason as _validate_gtin)."""
    try:
        validate_rfid_epc(rfid_epc)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


async def get_item_projection(session: AsyncSession, company_id, entity_id: str) -> Projection:
    row = await session.get(Projection, {"company_id": company_id, "entity_id": entity_id})
    if row is None or row.entity_type != "item":
        raise HTTPException(status_code=404, detail="Item not found")
    return row


async def _lock_items_for_physical_mutation(session: AsyncSession, company_id, entity_ids: list[str]) -> dict[str, Projection]:
    """Lock the physical-code namespace, then freshly lock the source item rows.

    Physical restructures mint or validate codes and derive quantities/costs from
    existing item state. All such operations use Company -> Projection ordering so
    they cannot deadlock with code edits, and every calculation is based on state
    committed before this transaction acquired the row locks.
    """
    await lock_item_code_namespace(session, company_id)
    rows = await lock_projections(session, company_id, entity_ids)
    return {entity_id: row for entity_id, row in rows.items() if row.entity_type == "item"}


async def _require_company_location(session: AsyncSession, company_id, location_id) -> None:
    if location_id is None:
        return
    from celerp.models.company import Location

    try:
        parsed = location_id if isinstance(location_id, uuid.UUID) else uuid.UUID(str(location_id))
    except (TypeError, ValueError, AttributeError):
        raise HTTPException(status_code=422, detail="Invalid location_id")
    exists = (await session.execute(
        select(Location.id).where(Location.id == parsed, Location.company_id == company_id)
    )).scalar_one_or_none()
    if exists is None:
        raise HTTPException(status_code=422, detail="Location not found for this company")


@router.post("", openapi_extra={"x-celerp-agent": True, "x-celerp-agent-idempotent": True})
async def post_item(payload: ItemCreate, company_id=Depends(get_current_company_id), _: None = require_permission("edit_inventory"), user=Depends(get_current_user), role: str = Depends(get_current_role), settings: dict = Depends(get_current_company_settings), session: AsyncSession = Depends(get_session)) -> dict:
    idem_key = payload.idempotency_key or str(uuid.uuid4())
    if payload.idempotency_key:
        replay = await find_event_by_idempotency(session, company_id, idem_key)
        if replay is not None:
            if replay.event_type != "item.created":
                raise HTTPException(status_code=409, detail="Idempotency key was already used for another operation")
            return {"event_id": replay.id, "id": replay.entity_id}

    # Guard: setting a price on creation requires set_inventory_prices, except that a
    # draft's creator authors cost with edit_inventory alone (the gate re-arms at commit) -
    # the same draft_cost_carveout the pricing surfaces use, so the three stay in lockstep.
    # A new item starts as a draft. One created available is made available in the same
    # request, so its stock is booked as it enters (Make Available); any other status is
    # reached only through the action that leads to it.
    _requested_status = str((payload.model_extra or {}).get("status") or "draft").lower()
    if _requested_status not in ("draft", "available"):
        raise HTTPException(status_code=422, detail=(
            f"An item is created as draft or available, not {_requested_status}."))
    _create_draft = _requested_status == "draft"
    _price_lists = (await get_price_config(session, company_id))[0]
    _gated = price_keys_in(payload.model_dump(exclude_none=True), _price_lists)
    if draft_cost_carveout(_create_draft, role, settings):
        _gated -= COST_ITEM_KEYS
    reject_price_change(_gated, role, settings)

    # The category's defaults fill what the payload leaves out; an explicit value wins.
    category_defaults = category_item_defaults(payload.category)
    payload = payload.model_copy(update={
        "sell_by": payload.sell_by or category_defaults.get("sell_by"),
        "inventory_type": (
            payload.inventory_type if payload.inventory_type is not None
            else category_defaults.get("inventory_type", "stocked")
        ),
    })
    if payload.inventory_type not in VALID_INVENTORY_TYPES:
        raise HTTPException(status_code=422, detail=f"inventory_type must be one of {sorted(VALID_INVENTORY_TYPES)}")
    if not payload.sell_by:
        raise HTTPException(status_code=422, detail="sell_by is required unless the category has a default unit")

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

    _validate_sku(payload.sku)
    _validate_gtin(payload.gtin)
    _validate_rfid_epc(payload.rfid_epc)
    await _require_company_location(session, company_id, payload.location_id)

    # Serialize SKU/barcode allocation for this company: two concurrent creates must
    # not mint the same code. The lock is held until this request commits.
    await lock_item_code_namespace(session, company_id)
    replay = await find_event_by_idempotency(session, company_id, idem_key)
    if replay is not None:
        if replay.event_type != "item.created":
            raise HTTPException(status_code=409, detail="Idempotency key was already used for another operation")
        return {"event_id": replay.id, "id": replay.entity_id}

    # Auto-assign sequential SKU if not provided
    if not payload.sku:
        payload = payload.model_copy(update={"sku": (await allocate_internal_codes(session, company_id))[0]})

    # Duplicate/clone: mint a fresh unique barcode and discard any inherited one.
    # Mirrors the split child - a new entity needs a new unique barcode (barcode is
    # globally unique, so a copy must never carry the source's). The physical RFID/EPC
    # tag is bound to one physical unit, so a clone must never inherit it either; the
    # product GTIN is kept (a clone is the same product).
    if payload.auto_barcode:
        payload = payload.model_copy(update={
            "barcode": (await allocate_internal_codes(session, company_id))[0],
            "rfid_epc": None,
        })
    # Auto-copy SKU to barcode when barcode omitted and SKU is purely numeric.
    # SKU is a (possibly repeated) product-type, so gate the copy on the shared
    # Barcode/EPC physical namespace: if that value is already in use as another
    # item's barcode OR rfid_epc, assign a fresh sequential barcode instead so the
    # create does not 409 on the final physical check (a second item sharing a
    # numeric SKU, or a value held as another item's EPC, must not block it). A SKU
    # is a product identifier and never inherits physical-code uniqueness.
    # Single-SKU behaviour is unchanged (the first/only item still gets barcode == sku).
    elif payload.barcode is None and payload.sku.isdigit():
        if await code_in_use(session, company_id, payload.sku):
            new_barcode = (await allocate_internal_codes(session, company_id))[0]
        else:
            new_barcode = payload.sku
        payload = payload.model_copy(update={"barcode": new_barcode})

    # SKU uniqueness is intentionally NOT enforced: `sku` is a product-type that may
    # repeat across physical lots. Physical-lot uniqueness is carried by `barcode`
    # (checked at the event boundary, 409 on conflict) and the immutable `entity_id`.

    entity_id = f"item:{uuid.uuid4()}"
    data = payload.model_dump(exclude_none=True)
    data.pop("auto_barcode", None)  # request-only signal; never persisted onto the item
    if payload.location_id is not None:
        data["location_id"] = str(payload.location_id)

    # Amount fields must be non-negative on create. ItemCreate is extra="allow", so
    # weight/pieces/gross_weight are otherwise unvalidated. Creation is not gated by
    # edit_inventory_amounts (a create defines the item, not a hand-edit); value only.
    for _amt in AMOUNT_ITEM_KEYS & set(data):
        _amt_val = data.get(_amt)
        if _amt_val is not None and float(_amt_val) < 0:
            raise HTTPException(status_code=422, detail=f"{_amt} cannot be negative")

    for field in ("purchase_unit", "weight_unit"):
        if data.get(field) is None and field in category_defaults:
            data[field] = category_defaults[field]

    # Created as a draft: authorable (amounts and costs editable by anyone with
    # edit_inventory) until Make Available commits it into stock.
    data["status"] = "draft"

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
        idempotency_key=idem_key,
        metadata_={},
    )

    if getattr(entry, "was_deduped", False):
        return {"event_id": entry.id, "id": entry.entity_id}

    # Emit pricing events for any prices supplied inline. Derive their replay identity
    # from the primary command, so retries cannot create a second price change.
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
            idempotency_key=f"{idem_key}:price:{price_type}",
            metadata_={},
        )

    if not _create_draft:
        await emit_event(
            session,
            company_id=company_id,
            entity_id=entity_id,
            entity_type="item",
            event_type="item.status.set",
            data={"new_status": "available", "ts": datetime.now(timezone.utc).isoformat()},
            actor_id=user.id,
            location_id=None,
            source="api",
            idempotency_key=f"{idem_key}:make-available",
            metadata_={},
        )

    await session.commit()
    return {"event_id": entry.id, "id": entry.entity_id}


def _changed_attribute_keys(state: dict | None, fields_changed: dict) -> set[str]:
    """Keys a patch changes inside ``attributes``: the read model lifts them to the
    top level, so a price there is gated like a top-level price."""
    change = fields_changed.get("attributes")
    new = change.get("new") if isinstance(change, dict) else None
    if not isinstance(new, dict):
        return set()
    old = (state or {}).get("attributes") or {}
    return {k for k in set(new) | set(old) if new.get(k) != old.get(k)}


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


@router.patch("/{entity_id}", openapi_extra={"x-celerp-agent": True, "x-celerp-agent-idempotent": True})
async def patch_item(entity_id: str, payload: ItemPatch, company_id=Depends(get_current_company_id), _: None = require_permission("edit_inventory"), user=Depends(get_current_user), role: str = Depends(get_current_role), settings: dict = Depends(get_current_company_settings), session: AsyncSession = Depends(get_session)) -> dict:
    if payload.idempotency_key:
        replay = await find_event_by_idempotency(session, company_id, payload.idempotency_key)
        if replay is not None:
            if replay.event_type != "item.updated" or replay.entity_id != entity_id:
                raise HTTPException(status_code=409, detail="Idempotency key was already used for another operation")
            return {"event_id": replay.id}

    # Guard: restricted fields require a role at the schema-configured floor.
    from celerp.services.field_schema import get_effective_field_schema
    field_schema = await get_effective_field_schema(session, company_id)
    restricted = {f["key"] for f in field_schema if f.get("visible_to_roles") and ROLE_LEVELS.get(role, 0) < min(ROLE_LEVELS.get(r, 0) for r in f["visible_to_roles"])}
    # Draft carve-out: the circulating-stock gates (cost + amount permissions)
    # attach when the item is committed to available, not at creation. While the
    # CURRENT status is draft, anyone with edit_inventory finishes authoring the
    # item freely; the status is re-read here on every patch, so an edit landing
    # after another user commits the item is gated like any available item.
    _proj = await get_item_projection(session, company_id, entity_id)
    _is_draft = str((_proj.state or {}).get("status") or "").lower() == "draft"
    # Cost fields are gated by set_inventory_prices, not by the schema role floor:
    # a granted operator edits cost, an ungranted manager still cannot.
    restricted -= COST_ITEM_KEYS
    changed_keys = set(payload.fields_changed.keys())
    _price_lists, _base_name, _ = await get_price_config(session, company_id)
    _price_changes = {k for k in changed_keys | _changed_attribute_keys(_proj.state, payload.fields_changed) if is_price_item_key(k, _price_lists)}
    if draft_cost_carveout(_is_draft, role, settings):
        _price_changes -= COST_ITEM_KEYS
    reject_price_change(_price_changes, role, settings)
    # Amount fields (quantity/weight/pieces/gross_weight) and the sell unit are
    # gated by edit_inventory_amounts, mirroring the cost gate above. sell_by is
    # included because changing it rewrites quantity, so it carries the same
    # authority; the gate fires only on a real change (blocked = changed & restricted).
    restricted -= AMOUNT_EDIT_GATED_KEYS
    if not _is_draft and not role_has_permission(settings, role, "edit_inventory_amounts"):
        restricted |= AMOUNT_EDIT_GATED_KEYS
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
        if is_price_item_key(_f, _price_lists) and isinstance(_fc, dict):
            _new = _fc.get("new")
            if _new is not None and coerce_price(_new) is None:
                raise HTTPException(status_code=422, detail=f"'{_f}' must be a number")

    # A renamed SKU is held to the same rule as a created one: no comma (the OR operator).
    if "sku" in changed_keys:
        _validate_sku((payload.fields_changed["sku"] or {}).get("new"))

    if "location_id" in changed_keys:
        await _require_company_location(
            session, company_id, (payload.fields_changed["location_id"] or {}).get("new")
        )

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

    # Validate barcode format if changing. Uniqueness is checked at the event boundary
    # under the company code-namespace lock (409 on conflict).
    if "barcode" in changed_keys:
        new_barcode = (payload.fields_changed["barcode"] or {}).get("new")
        if new_barcode is not None:
            try:
                validate_barcode(new_barcode)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc))

    # Validate gtin format if changing (a product identifier: format only, not unique).
    if "gtin" in changed_keys:
        _validate_gtin((payload.fields_changed["gtin"] or {}).get("new"))

    # Validate rfid_epc format if changing. It shares the physical-code namespace with
    # barcode; uniqueness is checked at the event boundary on the canonicalized value.
    if "rfid_epc" in changed_keys:
        new_epc = (payload.fields_changed["rfid_epc"] or {}).get("new")
        if new_epc is not None:
            _validate_rfid_epc(new_epc)

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

    if "sku" in changed_keys:
        from celerp_inventory.services import stamp_catalog_family_members
        await stamp_catalog_family_members(
            session, company_id, entity_id, actor_id=user.id, source="api"
        )

    event = dict(
        entity_id=entity_id,
        event_type="item.updated",
        data={**payload.model_dump(exclude_none=True),
              **(_kept((payload.fields_changed.get("status") or {}).get("new")) if "status" in changed_keys else {})},
        actor_id=user.id,
        source="api",
        idempotency_key=payload.idempotency_key or str(uuid.uuid4()),
    )
    if changed_keys & COST_ITEM_KEYS:
        entry = await _restate_cost_or_409(session, company_id, **event)
    else:
        entry = await emit_event(session, company_id=company_id, entity_type="item", location_id=None, metadata_={}, **event)
    await session.commit()
    return {"event_id": entry.id}


async def _restate_cost_or_409(session: AsyncSession, company_id, **event):
    """Apply a goods-cost change with its merge and COGS consequences (see restate_item_cost)."""
    from celerp_inventory.services import CostRestatementConflict, restate_item_cost
    try:
        return await restate_item_cost(session, company_id, **event)
    except CostRestatementConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc))


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
    # Locked, then validated per item BEFORE any event is emitted: one blocked item
    # rejects the whole bulk with the reason, nothing is half-applied (the session
    # never commits).
    await _lock_selected_items(session, company_id, payload.entity_ids)
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
            data={"new_status": payload.status, **_kept(payload.status)},
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
    """Commit one or more drafts into stock. Same authority as authoring the draft (edit_inventory) - no extra permission.

    Every selected lot is locked before any is checked, so a second request for the same
    lots waits for this one and then finds them available: already available is a no-op,
    and only the drafts actually moved are returned."""
    if not payload.entity_ids:
        raise HTTPException(status_code=422, detail="entity_ids must not be empty")
    rows = await _lock_selected_items(session, company_id, payload.entity_ids)
    for entity_id in payload.entity_ids:
        await assert_make_available_allowed(session, company_id, entity_id)
    at = datetime.now(timezone.utc).isoformat()  # one business day for the whole move
    event_ids = []
    for entity_id in payload.entity_ids:
        if not _row_is_draft(rows[entity_id]):
            continue
        entry = await emit_event(
            session,
            company_id=company_id,
            entity_id=entity_id,
            entity_type="item",
            event_type="item.status.set",
            data={"new_status": "available", "ts": at},
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
    """assert_status_change_allowed does the real gating (revert_items_to_draft + clean history),
    on lots locked before any is checked: a fulfilment or reservation that reached a lot
    first is seen, and a second request for the same lots finds them drafts already, a
    no-op. Only the lots actually returned to draft are returned."""
    if not payload.entity_ids:
        raise HTTPException(status_code=422, detail="entity_ids must not be empty")
    rows = await _lock_selected_items(session, company_id, payload.entity_ids)
    for entity_id in payload.entity_ids:
        await assert_status_change_allowed(session, company_id, entity_id, "draft", role, settings)
    at = datetime.now(timezone.utc).isoformat()  # one business day for the whole move
    event_ids = []
    for entity_id in payload.entity_ids:
        if _row_is_draft(rows[entity_id]):
            continue
        entry = await emit_event(
            session,
            company_id=company_id,
            entity_id=entity_id,
            entity_type="item",
            event_type="item.status.set",
            data={"new_status": "draft", "reason": payload.reason, "ts": at},
            actor_id=user.id,
            location_id=None,
            source="api",
            idempotency_key=str(uuid.uuid4()),
            metadata_={},
        )
        event_ids.append(entry.id)
    await session.commit()
    return {"updated": len(event_ids), "event_ids": event_ids}


async def _lock_selected_items(session: AsyncSession, company_id, entity_ids: list[str]) -> dict[str, Projection]:
    """Lock every selected item before any is checked; an id that is not an item is 404."""
    rows = await lock_projections(session, company_id, entity_ids)
    missing = sorted({e for e in entity_ids if e not in rows or rows[e].entity_type != "item"})
    if missing:
        raise HTTPException(status_code=404, detail=f"Item not found: {', '.join(missing)}")
    return rows


def _row_is_draft(row: Projection) -> bool:
    return str((row.state or {}).get("status") or "").lower() == "draft"


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
    """Delete drafts that were a mistake, without a trace. Every selected item must be a
    draft that never became stock and is used nowhere; otherwise nothing is deleted and
    the answer names the items and the actions that fit them instead."""
    if not payload.entity_ids:
        raise HTTPException(status_code=422, detail="entity_ids must not be empty")
    rows = await _lock_selected_items(session, company_id, payload.entity_ids)
    blocked = await _not_deletable(session, company_id, rows)
    if blocked:
        skus = ", ".join(sorted(str((rows[e].state or {}).get("sku") or e) for e in blocked))
        raise HTTPException(status_code=409, detail=(
            f"Nothing was deleted. Only a draft that never became stock and is used nowhere can be deleted, "
            f"and these cannot: {skus}. Use Revert to Draft for stock made available by mistake, "
            f"Archive to retire a product, or Write Off Stock for goods that left the company."))
    await erase_items(session, company_id, rows)
    await session.commit()
    return {"deleted": len(rows)}


async def _not_deletable(session: AsyncSession, company_id, rows: dict[str, Projection]) -> set[str]:
    """The selected items that are not a draft mistake: anything not a draft now, a draft
    with an inventory account, one that was ever stock (any event leaving draft or
    recording its books) or circulated, and one another record uses."""
    from celerp.models.ledger import LedgerEntry

    blocked = {e for e, row in rows.items() if not _row_is_draft(row) or (row.state or {}).get(LOT_ACCOUNT_FIELD)}
    events = (await session.execute(select(LedgerEntry.entity_id, LedgerEntry.event_type, LedgerEntry.data).where(
        LedgerEntry.company_id == company_id, LedgerEntry.entity_id.in_(list(rows))))).all()
    for eid, event_type, data in events:
        status = str((data or {}).get("status") or "draft").lower()
        if event_type == RECORDED or not is_authoring_event(event_type) or status != "draft":
            blocked.add(eid)
    return blocked | await mentioned_elsewhere(session, company_id, {e: [e] for e in rows})


class BulkExpireBody(BaseModel):
    entity_ids: list[str]


@router.post("/bulk/expire")
async def bulk_expire(payload: BulkExpireBody, company_id=Depends(get_current_company_id), _: None = require_permission("adjust_inventory"), user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    if not payload.entity_ids:
        raise HTTPException(status_code=422, detail="entity_ids must not be empty")
    await _lock_selected_items(session, company_id, payload.entity_ids)
    for eid in payload.entity_ids:
        await assert_expirable(session, company_id, eid)
    for eid in payload.entity_ids:
        await emit_event(
            session,
            company_id=company_id,
            entity_id=eid,
            entity_type="item",
            event_type="item.expired",
            data=_kept("expired"),
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
    _price_lists = (await get_price_config(session, company_id))[0]
    for _child in payload.children:
        _validate_sku(_child.sku)
        reject_price_change(price_keys_in({"attributes": _child.attributes}, _price_lists), role, settings)
    parent = (await _lock_items_for_physical_mutation(session, company_id, [entity_id])).get(entity_id)
    if parent is None or not is_item_available(parent.state):
        raise HTTPException(status_code=404, detail="Item not found or unavailable")

    # Block splitting only when explicitly disabled. A missing/None value
    # (e.g. older imports that never set the field) is treated as splittable.
    if not splitting_allowed(parent.state):
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
    from celerp_inventory.services import normalize_sku, resolve_catalog_anchor_for_item
    catalog_anchor_id: str | None = None
    try:
        catalog_anchor_id = (
            await resolve_catalog_anchor_for_item(session, company_id, entity_id)
        ).entity_id
    except ValueError:
        pass

    # Create child items
    child_eids: list[str] = []
    child_qty_list: list[float] = []
    # Mint one fresh free barcode per child that did not supply its own. The hardened
    # allocator takes the code-namespace lock (held to commit) and skips any value already
    # held as a barcode OR rfid_epc, so a minted child barcode can never collide with an
    # existing physical tag. A caller-supplied child barcode is checked against the same
    # shared namespace at the event boundary (409 on conflict).
    _minted_barcodes = iter(
        await allocate_internal_codes(
            session, company_id, sum(1 for c in children if c.barcode is None)
        )
    )

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
        child_data: dict = lot_fields(parent.state)
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
            "barcode": child.barcode if child.barcode is not None else next(_minted_barcodes),
        })
        if (
            catalog_anchor_id
            and normalize_sku(child_skus[i]) == normalize_sku(parent_sku)
        ):
            child_data["catalog_item_id"] = catalog_anchor_id
        else:
            child_data.pop("catalog_item_id", None)
        if child.weight is not None:
            child_data["weight"] = child.weight
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
      - child_qty must not exceed the locked parent quantity
      - parcel has weight (weight-unit sell_by OR a weight attribute)
            -> child_weight is required
      - sell_by is a weight unit  -> child_weight must equal child_qty
      - parcel has pieces (piece-unit sell_by OR a pieces attribute)
            -> child_pieces is required
      - sell_by is a pieces unit  -> child_pieces must equal child_qty

    Does NOT commit — the caller owns the transaction.
    """
    # Lock and re-read the live parent projection before carving: split_off_child emits
    # the mother's new quantity as an ABSOLUTE value, so two concurrent carves of one
    # parcel must each base their decrement on the current committed quantity, not on a
    # snapshot read earlier. FOR UPDATE serializes them so the second decrements from the
    # first's result instead of overwriting it (lost update). Serializing the read alone
    # only fixes ordering; the capacity check just below is what stops a carve larger than
    # what is actually on hand, which is the other half of "no phantom stock".
    entity_id = parent_proj.entity_id
    parent = (await _lock_items_for_physical_mutation(session, company_id, [entity_id])).get(entity_id)
    if parent is None:
        raise ValueError("parent item no longer exists")
    parent_sku = parent.state.get("sku", "")
    parent_qty = float(parent.state.get("quantity") or 0)
    parent_attrs = dict(parent.state.get("attributes") or {})

    # --- validate against the locked quantity (no floor, no silent clamp) ---
    if round(child_qty - parent_qty, 10) > 0:
        raise ValueError(f"cannot split {child_qty:g} of {parent_qty:g} available")

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
    # Mint a fresh free barcode through the hardened allocator, which skips any value
    # already held as a barcode OR rfid_epc so this new physical lot never collides with
    # an existing tag.
    child_barcode = (await allocate_internal_codes(session, company_id))[0]
    child_attrs = dict(parent_attrs)
    if ch_pieces is not None:
        child_attrs["pieces"] = ch_pieces
    child_data = lot_fields(parent.state)
    from celerp_inventory.services import (
        normalize_sku as _normalize_family_sku,
        resolve_catalog_anchor_for_item as _resolve_family_anchor,
    )
    try:
        _family_anchor = await _resolve_family_anchor(
            session, company_id, entity_id
        )
    except ValueError:
        _family_anchor = None
    child_data.update({
        "sku": child_sku,
        "name": parent.state.get("name", child_sku),
        "quantity": child_qty,
        "status": "available",
        "attributes": child_attrs,
        "barcode": child_barcode,
        # Resolve a possibly-unset parent default to a concrete bool: a None parent
        # (splittable by default) must not emit a None the item.created schema rejects.
        "allow_splitting": splitting_allowed(parent.state),
    })
    if (
        _family_anchor is not None
        and _normalize_family_sku((_family_anchor.state or {}).get("sku"))
        == _normalize_family_sku(child_sku)
    ):
        child_data["catalog_item_id"] = _family_anchor.entity_id
    else:
        child_data.pop("catalog_item_id", None)
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
    _validate_sku(payload.child_sku)
    parent = (await _lock_items_for_physical_mutation(session, company_id, [entity_id])).get(entity_id)
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
    # A cost that differs from the parent's is a price write, so it takes the same
    # set_inventory_prices gate as PATCH; with no cost sent (or the parent's cost sent
    # back) the child keeps the parent's cost.
    if payload.child_cost_total is not None and payload.child_cost_total != parent_cost_total:
        reject_price_change({"cost_total"}, role, settings)
    effective_cost = payload.child_cost_total if payload.child_cost_total is not None else parent_cost_total
    parent_location_id = parent.state.get("location_id")

    child_eid = f"item:{uuid.uuid4()}"
    # Lock the code namespace so a concurrent allocator cannot mint the same barcode.
    child_barcode = (await allocate_internal_codes(session, company_id))[0]

    # Copy-all-then-override: inherit every parent field; reset only identity/qty/cost/status.
    # Also override sell_by and category — the purpose of a transform is to change these.
    child_data: dict = {k: v for k, v in lot_fields(parent.state).items() if k not in parent_price_keys}
    child_data.update({
        "sku": payload.child_sku,
        "name": (payload.child_name or "").strip() or parent.state.get("name", payload.child_sku),
        "quantity": payload.child_quantity,
        "sell_by": payload.child_sell_by,
        "category": payload.child_category,
        "status": "available",
        "attributes": {**parent_attrs},
        "barcode": child_barcode,
    })
    # A transform yields a DIFFERENT product, so no product-family identity carries.
    child_data.pop("gtin", None)
    child_data.pop("catalog_item_id", None)
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

    await session.commit()
    return {"child_id": child_eid, "child_sku": payload.child_sku, "parent_sku": parent.state.get("sku", "")}



# A merge sent with an idempotency key gets its result id from the key, so every
# delivery of the same merge names the same item and the same journal entry.
_MERGE_ID_NAMESPACE = uuid.UUID("6f1d3c52-9a1e-4c55-9d1e-2a7f5b0e8c41")

_STALE_MERGE = "The merge or its items changed since it was reviewed. Review the merge again."
_UNREVIEWED_MERGE = "Preview this merge first, then confirm it with the plan_fingerprint the preview returned."


def _merge_result_id(company_id, idempotency_key: str | None) -> str:
    if not idempotency_key:
        return f"item:{uuid.uuid4()}"
    return f"item:{uuid.uuid5(_MERGE_ID_NAMESPACE, f'{company_id}:{idempotency_key}')}"


def _check_merge_request(payload: MergeBody) -> None:
    _validate_sku(payload.resulting_sku)
    if len(payload.source_entity_ids) < 2:
        raise HTTPException(status_code=422, detail="At least 2 source_entity_ids are required to merge.")
    if len(set(payload.source_entity_ids)) != len(payload.source_entity_ids):
        raise HTTPException(status_code=422, detail="source_entity_ids must contain distinct items.")
    if payload.target_sku_from not in payload.source_entity_ids:
        raise HTTPException(status_code=422, detail="target_sku_from must identify one of the merge sources.")


def _merge_request_digest(payload: MergeBody) -> str:
    """What a merge asks for, whatever order its items are listed in. A retry under
    the same idempotency key must ask for exactly this."""
    canonical = {
        "sources": sorted(payload.source_entity_ids),
        "target": payload.target_sku_from,
        "quantity": payload.resulting_quantity,
        "cost_total": payload.resulting_cost_total,
        "name": payload.resulting_name,
        "sku": payload.resulting_sku,
        "attributes": payload.resolved_attributes or {},
    }
    return hashlib.sha256(json.dumps(canonical, sort_keys=True, default=str).encode()).hexdigest()


def _merge_fingerprint(payload: MergeBody, sources: list[Projection], reclass) -> str:
    """Names what the merge asks for, the stock its plan was made from, and where its
    value goes. Keyed, so it reveals nothing about cost to a role that cannot see cost."""
    from celerp.config import settings as app_settings

    basis = {
        "request": _merge_request_digest(payload),
        "sources": [[p.entity_id, p.state] for p in sorted(sources, key=lambda p: p.entity_id)],
        "destination": reclass.destination,
        "moves": {code: str(amount) for code, amount in reclass.moves.items()},
        "currency": reclass.currency,
    }
    message = json.dumps(basis, sort_keys=True, default=str).encode()
    return hmac.new(app_settings.jwt_secret.encode(), message, hashlib.sha256).hexdigest()


async def _merge_disclosure(session: AsyncSession, company_id, reclass, settings: dict, role: str) -> dict | None:
    """The inventory accounts a merge moves value between, named as the chart names
    them, for the person merging. The amounts are goods cost, so a role that cannot
    see cost gets the accounts only."""
    from celerp.services.posting_readiness import account_names

    disclosure = reclass.disclosure()
    if not disclosure:
        return None
    names = await account_names(session, company_id,
                                [disclosure["destination"], *(m["account"] for m in disclosure["moves"])])
    disclosure["destination_name"] = names[disclosure["destination"]]
    hide = not role_has_permission(settings, role, "view_inventory_costs")
    disclosure["moves"] = [{**m, "name": names[m["account"]], **({"amount": None} if hide else {})}
                           for m in disclosure["moves"]]
    return disclosure


@dataclass
class MergePlan:
    """Everything a merge writes, decided once from the source items as they stand."""

    sources: list[Projection]
    create_data: dict
    price_fields: dict
    merged_sku: str
    location_id: uuid.UUID | None
    reclass: object
    disclosure: dict | None
    fingerprint: str


async def _plan_merge(session: AsyncSession, company_id, payload: MergeBody, settings: dict, role: str,
                      rows: dict[str, Projection]) -> MergePlan:
    """The one merge plan, shared by the preview and the merge itself. ``rows`` are the
    source items as read by the caller; the merge passes them locked."""
    from celerp.connectors import ownership, registry
    from celerp.services.auto_je import company_currency, merge_reclassification
    from celerp_inventory.services import external_link_for_state, normalize_sku

    reject_price_change(
        price_keys_in({"attributes": payload.resolved_attributes or {}}, (await get_price_config(session, company_id))[0]),
        role, settings,
    )
    source_projections: list[Projection] = []
    for sid in payload.source_entity_ids:
        proj = rows.get(sid)
        if proj is None:
            raise HTTPException(status_code=404, detail=f"Item '{sid}' not found.")
        status = str((proj.state or {}).get("status") or "").lower()
        if status == "draft":
            raise HTTPException(status_code=422, detail=f"Cannot merge a draft item ({sid}); make it available first.")
        if status == "merged":
            raise HTTPException(status_code=409, detail=f"Item '{sid}' has already been merged.")
        if not is_item_available(proj.state or {}):
            sku = (proj.state or {}).get("sku") or sid
            raise HTTPException(status_code=409,
                                detail=f"Item '{sku}' is not on hand ({status or 'no status'}), so it cannot be merged.")
        source_projections.append(proj)

    try:
        connected = await ownership.connected_connector_platforms(session, company_id)
    except ownership.ConnectorOwnershipError:
        raise HTTPException(
            status_code=503,
            detail="Could not check this company's connectors. Nothing was merged; try again.",
        )
    live = sorted(
        registry.get(platform).display_name
        for platform in connected
        if any(external_link_for_state(proj.state or {}, platform) for proj in source_projections)
    )
    if live:
        raise HTTPException(
            status_code=409,
            detail=f"This catalog product is currently linked to {' and '.join(live)}. Merge its physical lots instead.",
        )
    explicit_catalog_ids = {
        str((proj.state or {}).get("catalog_item_id"))
        for proj in source_projections
        if (proj.state or {}).get("catalog_item_id")
    }
    if len(explicit_catalog_ids) > 1:
        raise HTTPException(
            status_code=409,
            detail="Items from different catalog products cannot be merged.",
        )
    merged_catalog_id = next(iter(explicit_catalog_ids), None)
    if merged_catalog_id is None:
        from celerp_inventory.services import resolve_catalog_anchor_for_item
        inferred_catalog_ids: set[str] = set()
        inference_failed = False
        for proj in source_projections:
            try:
                inferred = await resolve_catalog_anchor_for_item(
                    session, company_id, proj.entity_id
                )
            except ValueError:
                inference_failed = True
                break
            inferred_catalog_ids.add(inferred.entity_id)
        if not inference_failed and len(inferred_catalog_ids) == 1:
            inferred_id = next(iter(inferred_catalog_ids))
            if any(proj.entity_id != inferred_id for proj in source_projections):
                merged_catalog_id = inferred_id
    catalog_anchor = (
        await session.get(
            Projection,
            {"company_id": company_id, "entity_id": merged_catalog_id},
        )
        if merged_catalog_id else None
    )
    if merged_catalog_id and (
        catalog_anchor is None or catalog_anchor.entity_type != "item"
    ):
        raise HTTPException(
            status_code=409,
            detail="Catalog product link is invalid.",
        )
    if catalog_anchor is not None:
        anchor_sku = normalize_sku((catalog_anchor.state or {}).get("sku"))
        for proj in source_projections:
            source_catalog_id = (proj.state or {}).get("catalog_item_id")
            if (
                not source_catalog_id
                and normalize_sku((proj.state or {}).get("sku")) != anchor_sku
            ):
                raise HTTPException(
                    status_code=409,
                    detail="Items from different catalog products cannot be merged.",
                )

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
    target_proj = rows[payload.target_sku_from]

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
    # (number/money/rate/weight) drop to no-value; every other conflicting field collapses to the "Mixed"
    # system value. Keying off the value shape (as before) misclassified custom attributes whose
    # values merely look numeric (free fields, or selects with numeric options) and silently dropped
    # them instead of showing "Mixed".
    from celerp.services.field_schema import get_effective_field_schema, MIXED_VALUE, _BASE_FIELDS
    _merge_category = (next(iter(categories), "") or "").strip() or None
    _schema = await get_effective_field_schema(session, company_id, category=_merge_category)
    _dropdown_keys = {f["key"] for f in _schema if f.get("type") in ("select", "status")}
    _numeric_keys = {f["key"] for f in _schema if f.get("type") in NUMERIC_SCHEMA_TYPES}
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
    # A merge keeps the value of what it combines. Changing that value is a cost
    # correction on the merged item, never part of the merge. A role that may not
    # write prices is refused as for any price write, so its answer says nothing
    # about the cost.
    currency = await company_currency(session, company_id)
    if payload.resulting_cost_total is not None and (
        merged_cost_total is None
        or round_money(to_decimal(payload.resulting_cost_total), currency) != round_money(to_decimal(merged_cost_total), currency)
    ):
        reject_price_change({"cost_total"}, role, settings)
        raise HTTPException(
            status_code=422,
            detail="A merge keeps the cost of the items it combines. To change the merged item's cost, "
                   "merge first and then make a cost correction on the merged item.",
        )
    resulting_name = payload.resulting_name if payload.resulting_name is not None else str(target_proj.state.get("name") or "")

    # Update expiry_date attribute to earliest.
    if earliest_expiry:
        resolved_attrs["expiry_date"] = earliest_expiry

    target_state = target_proj.state
    # The merged item is genuinely new, so its SKU can be the target's (default),
    # or a custom value the user typed (issue #190). SKU is a product-type that may
    # repeat across lots (per-lot identity is the barcode + entity_id), so no
    # uniqueness check is applied - consistent with create/rename.
    merged_sku = (payload.resulting_sku or "").strip() or str(target_state.get("sku") or "")
    if catalog_anchor is not None and normalize_sku(merged_sku) != normalize_sku(
        (catalog_anchor.state or {}).get("sku")
    ):
        raise HTTPException(
            status_code=409,
            detail="A merged catalog-family lot must keep its catalog product SKU.",
        )
    create_data: dict = {
        "sku": merged_sku,
        "name": resulting_name,
        "quantity": resulting_qty,
        "sell_by": str(target_state.get("sell_by") or "piece"),
        "status": "available",
        "allow_splitting": splitting_allowed(target_state),
        "attributes": resolved_attrs,
    }
    if merged_catalog_id:
        create_data["catalog_item_id"] = merged_catalog_id
    # The merged lot keeps the surviving lot's inventory account; value held in any
    # other account moves into it with the merge.
    reclass = merge_reclassification(target_state, [p.state for p in source_projections], currency)
    create_data[LOT_ACCOUNT_FIELD] = reclass.destination

    # The merged item is the same product as the target, so carry the target's product
    # GTIN. The physical RFID/EPC tag is NOT carried: the merged item is a new physical
    # unit (the merge mints a fresh barcode), so it starts with no physical tag.
    for field in ("category", "location_id", "description", "unit", "tax_codes", "gtin"):
        val = target_state.get(field)
        if val is not None:
            create_data[field] = str(val) if field == "location_id" else val

    if total_weight is not None:
        create_data["weight"] = total_weight
    weight_unit = target_state.get("weight_unit")
    if weight_unit:
        create_data["weight_unit"] = weight_unit

    # Pricing for the merged money fields (issue #199). Each *_price field is a PER-UNIT
    # price, so it is reconciled on the source TOTALS (unit × qty): if every source has the price set,
    # the merged total is their sum (stored back as a unit = total / merged_qty); if ANY source lacks
    # the price, the merged item carries NO value for it (omit) rather than copying the target's price
    # or treating the missing one as 0. cost_total is already a total (computed above).
    price_fields: dict = {}
    if merged_cost_total is not None:
        price_fields["cost_total"] = merged_cost_total
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

    raw_loc = target_state.get("location_id")
    return MergePlan(
        sources=source_projections,
        create_data=create_data,
        price_fields=price_fields,
        merged_sku=merged_sku,
        location_id=uuid.UUID(str(raw_loc)) if raw_loc else None,
        reclass=reclass,
        disclosure=await _merge_disclosure(session, company_id, reclass, settings, role),
        fingerprint=_merge_fingerprint(payload, source_projections, reclass),
    )


@router.post("/merge/preview")
async def preview_merge(payload: MergeBody, company_id=Depends(get_current_company_id), _: None = require_permission("edit_inventory"), role: str = Depends(get_current_role), settings: dict = Depends(get_current_company_settings), session: AsyncSession = Depends(get_session)) -> dict:
    """What merging these items would do to the books, before the user confirms. Send
    the same body the merge will be confirmed with (every field but ``idempotency_key``
    and ``plan_fingerprint`` counts): the merge refuses if the request or its items
    change between this preview and the confirmation."""
    _check_merge_request(payload)
    rows = {p.entity_id: p for p in (await session.execute(select(Projection).where(
        Projection.company_id == company_id, Projection.entity_type == "item",
        Projection.entity_id.in_(payload.source_entity_ids)))).scalars()}
    plan = await _plan_merge(session, company_id, payload, settings, role, rows)
    return {"inventory_reclassification": plan.disclosure, "plan_fingerprint": plan.fingerprint}


@router.post("/merge")
async def merge_items(payload: MergeBody, company_id=Depends(get_current_company_id), _: None = require_permission("edit_inventory"), role: str = Depends(get_current_role), user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    """Merge the items into one new item. Preview the merge first with POST
    /items/merge/preview and confirm it with the ``plan_fingerprint`` that preview
    returned: a merge without one is refused, and so is one whose items changed since
    the preview. A retry of the same request under the same ``idempotency_key``
    returns the first merge's result without a new preview."""
    _check_merge_request(payload)
    from celerp.connectors import ownership
    from celerp.services.account_roles import current_settings
    from celerp.services.auto_je import create_for_merge_reclassification

    # Hold every product channel steady (connect and disconnect wait) before the
    # item locks, so the link check in the plan cannot race a connector change.
    for platform in sorted(ownership.PRODUCT_CHANNEL_PLATFORMS):
        await ownership.lock_connector_key(session, platform)
    locked_sources = await _lock_items_for_physical_mutation(session, company_id, payload.source_entity_ids)
    request_digest = _merge_request_digest(payload)
    if payload.idempotency_key:
        # A repeat delivery of a merge that already happened gets its result again. Read
        # under the item locks, so a delivery still in flight finishes first.
        replay = await find_event_by_idempotency(session, company_id, payload.idempotency_key)
        if replay is not None:
            meta = replay.metadata_ or {}
            if replay.event_type != "item.created" or meta.get("merge_request") != request_digest:
                raise HTTPException(status_code=409, detail="Idempotency key was already used for another operation")
            return {"id": replay.entity_id, "inventory_reclassification": meta.get("inventory_reclassification")}

    # Plan again under the locks, from the settings as last committed, and refuse if
    # the items are no longer what the user reviewed.
    settings = await current_settings(session, company_id)
    plan = await _plan_merge(session, company_id, payload, settings, role, locked_sources)
    if payload.plan_fingerprint is None:
        raise HTTPException(status_code=422, detail=_UNREVIEWED_MERGE)
    if not hmac.compare_digest(payload.plan_fingerprint, plan.fingerprint):
        raise HTTPException(status_code=409, detail=_STALE_MERGE)

    new_entity_id = _merge_result_id(company_id, payload.idempotency_key)
    # The merged item is a new physical lot, so it mints a FRESH barcode rather than
    # inheriting the target's: the source items are deactivated (status="merged") but
    # keep their barcodes, so copying the target's here would collide with the still
    # indexed source under uq_projection_company_item_barcode. allocate_internal_codes
    # locks the company's code namespace, so a concurrent create/split/merge cannot
    # mint the same barcode.
    create_data = {**plan.create_data, "barcode": (await allocate_internal_codes(session, company_id))[0]}
    await emit_event(
        session,
        company_id=company_id,
        entity_id=new_entity_id,
        entity_type="item",
        event_type="item.created",
        data=create_data,
        actor_id=user.id,
        location_id=plan.location_id,
        source="api",
        idempotency_key=payload.idempotency_key or f"merge:{new_entity_id}",
        metadata_={"merged_from": payload.source_entity_ids, "merge_request": request_digest,
                   "inventory_reclassification": plan.disclosure},
    )

    # Carry attached files from every source onto the merged item (dedup by id; keep one hero)
    # so merging never drops attachments.
    from datetime import datetime as _dt, timezone as _tz
    _seen_files: set[str] = set()
    _hero_used = False
    for proj in plan.sources:
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
                idempotency_key=f"merge:{new_entity_id}:file:{fid}",
                metadata_={"reason": "from_merge"},
            )


    for price_type, price_val in plan.price_fields.items():
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
            idempotency_key=f"merge:{new_entity_id}:price:{price_type}",
            metadata_={"reason": "from_merge"},
        )

    # Emit item.merged marker on the new item for history display.
    source_skus = {p.entity_id: str(p.state.get("sku") or p.entity_id) for p in plan.sources}
    await emit_event(
        session,
        company_id=company_id,
        entity_id=new_entity_id,
        entity_type="item",
        event_type="item.merged",
        data={
            "source_entity_ids": payload.source_entity_ids,
            "source_skus": source_skus,
            "resulting_qty": float(plan.create_data["quantity"]),
        },
        actor_id=user.id,
        location_id=None,
        source="api",
        idempotency_key=f"merge:{new_entity_id}:marker",
        metadata_={},
    )

    # Deactivate all source items: qty=0, is_available=False, merged_into=new item.
    new_sku = plan.merged_sku or new_entity_id
    for proj in plan.sources:
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
                "original_status": str(proj.state.get("status") or "available"),
                "original_status_doc_id": proj.state.get("status_doc_id"),
                "original_status_doc_number": proj.state.get("status_doc_number"),
            },
            actor_id=user.id,
            location_id=None,
            source="api",
            idempotency_key=f"merge:{new_entity_id}:source:{proj.entity_id}",
            metadata_={},
        )

    await create_for_merge_reclassification(
        session, company_id=company_id, user_id=user.id, merged_id=new_entity_id, merged_sku=new_sku,
        source_ids=payload.source_entity_ids, reclass=plan.reclass,
        ts=business_date_at(datetime.now(timezone.utc), settings.get("timezone")),
    )

    await session.commit()
    return {"id": new_entity_id, "inventory_reclassification": plan.disclosure}


async def _latest_item_event(session: AsyncSession, company_id, entity_id: str):
    from celerp.models.ledger import LedgerEntry
    return (await session.execute(
        select(LedgerEntry).where(LedgerEntry.company_id == company_id, LedgerEntry.entity_id == entity_id)
        .order_by(LedgerEntry.id.desc()).limit(1)
    )).scalar_one_or_none()


@router.post("/{entity_id}/undo-merge")
async def undo_merge(entity_id: str, company_id=Depends(get_current_company_id), _: None = require_permission("edit_inventory"), user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    """Undo a merge: the merged items hold their own stock again, each on the inventory
    account it was in before, and the merge's reclassification entry is reversed.

    Only while nothing has happened to the merged item or its sources since the merge;
    after that the merge is part of what followed and stays."""
    merged = (await session.get(Projection, {"company_id": company_id, "entity_id": entity_id}))
    if merged is None or merged.entity_type != "item":
        raise HTTPException(status_code=404, detail=f"Item '{entity_id}' not found.")
    marker = await _latest_item_event(session, company_id, entity_id)
    if marker is None or marker.event_type != "item.merged":
        raise HTTPException(status_code=409, detail=(
            "This merge can no longer be undone: the merged item has changed since it was made."))
    source_ids = list(marker.data["source_entity_ids"])
    locked = await _lock_items_for_physical_mutation(session, company_id, [entity_id, *source_ids])
    marker = await _latest_item_event(session, company_id, entity_id)
    if marker is None or marker.event_type != "item.merged" or entity_id not in locked:
        raise HTTPException(status_code=409, detail=(
            "This merge can no longer be undone: the merged item has changed since it was made."))
    restores = []
    for sid in source_ids:
        last = await _latest_item_event(session, company_id, sid)
        if sid not in locked or last is None or last.event_type != "item.source_deactivated" \
                or (last.data or {}).get("merged_into") != entity_id:
            raise HTTPException(status_code=409, detail=(
                "This merge can no longer be undone: one of the merged items has changed since it was made."))
        if not (last.data or {}).get("original_status"):
            raise HTTPException(status_code=409, detail=(
                "This merge was made before merges could be undone, so the items' earlier state is not on record."))
        restores.append((sid, last.data))

    from celerp.services.auto_je import void_for_merge_reclassification
    await void_for_merge_reclassification(session, company_id=company_id, user_id=user.id, merged_id=entity_id)
    for sid, data in restores:
        await emit_event(
            session, company_id=company_id, entity_id=sid, entity_type="item", event_type="item.unmerged",
            data={"merged_into": entity_id, "restored_status": data["original_status"],
                  "source_doc_id": data.get("original_status_doc_id"),
                  "doc_number": data.get("original_status_doc_number")},
            actor_id=user.id, location_id=None, source="api",
            idempotency_key=f"merge-undo:{entity_id}:{sid}", metadata_={},
        )
    await emit_event(
        session, company_id=company_id, entity_id=entity_id, entity_type="item", event_type="item.merge_undone",
        data={"source_entity_ids": source_ids}, actor_id=user.id, location_id=None, source="api",
        idempotency_key=f"merge-undo:{entity_id}", metadata_={},
    )
    await session.commit()
    return {"id": entity_id, "restored": source_ids}


@router.post("/{entity_id}/adjust")
async def adjust_item(entity_id: str, payload: AdjustBody, company_id=Depends(get_current_company_id), _: None = require_permission("adjust_inventory"), user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    entry = await adjust_item_quantity(
        session, company_id, user.id, entity_id, payload.model_dump(exclude_none=True),
        source="api", idempotency_key=payload.idempotency_key or str(uuid.uuid4()),
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
    _proj = await get_item_projection(session, company_id, entity_id)
    _is_draft = str((_proj.state or {}).get("status") or "").lower() == "draft"
    if not (is_cost_price_type(payload.price_type) and draft_cost_carveout(_is_draft, role, settings)):
        reject_price_change({payload.price_type}, role, settings)
    event = dict(
        entity_id=entity_id,
        event_type="item.pricing.set",
        data=payload.model_dump(exclude_none=True),
        actor_id=user.id,
        source="api",
        idempotency_key=payload.idempotency_key or str(uuid.uuid4()),
    )
    if is_cost_price_type(payload.price_type):
        entry = await _restate_cost_or_409(session, company_id, **event)
    else:
        entry = await emit_event(session, company_id=company_id, entity_type="item", location_id=None, metadata_={}, **event)
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
        data={**payload.model_dump(exclude_none=True), **_kept(payload.new_status)},
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
    await assert_expirable(session, company_id, entity_id)
    entry = await emit_event(
        session,
        company_id=company_id,
        entity_id=entity_id,
        entity_type="item",
        event_type="item.expired",
        data=_kept("expired"),
        actor_id=user.id,
        location_id=None,
        source="api",
        idempotency_key=str(uuid.uuid4()),
        metadata_={},
    )
    await session.commit()
    return {"event_id": entry.id}


# ── Import endpoint (CIF) ─────────────────────────────────────────────────────
# The request/result models and the committer body live in services.py so every
# transport shares one implementation; this route is the raw-event-batch transport.


@router.post("/import/batch", response_model=BatchImportResult)
async def batch_import_items(
    body: BatchImportRequest,
    company_id=Depends(get_current_company_id),
    _: None = require_permission("import_export_data"),
    __: None = require_permission("edit_inventory"),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> BatchImportResult:
    """Batch-import CIF item records. Idempotent on idempotency_key. Max 500 per call.

    The raw-event-batch transport: records arrive already shaped by the caller.
    The writer is services.write_import_batch, shared with /import/rows and the
    agent /import/commit.
    """
    role, settings = await _import_authority(session, company_id, user.id)
    return await commit_import_batch(session, company_id, user, role, settings, body)


# ---------------------------------------------------------------------------
# Import history + undo
# ---------------------------------------------------------------------------


@router.get("/import/batches")
async def list_import_batches(
    company_id=Depends(get_current_company_id),
    _: None = require_permission("import_export_data"),
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
    _: None = require_permission("import_export_data"),
    __: None = require_permission("edit_inventory"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Undo an import: remove the items it created and take off the books exactly the
    opening stock it booked for them, in one step. Refused, with nothing changed, when an
    item was changed or used since, or its opening stock was booked another way, or the
    import's entry cannot be voided (a locked period)."""
    from datetime import datetime, timezone as _tz

    from celerp_inventory.models_import_batch import ImportBatch
    from celerp.models.ledger import LedgerEntry
    from celerp.services.auto_je import _void_je_if_posted
    from celerp.services.company_lock import lock_company

    try:
        batch_uuid = uuid.UUID(batch_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Import batch not found")
    # The company lock serialises this with any chunk still adding to the batch.
    await lock_company(session, company_id)
    batch = (await session.execute(
        select(ImportBatch).where(ImportBatch.id == batch_uuid).with_for_update()
        .execution_options(populate_existing=True))).scalar_one_or_none()
    if batch is None or batch.company_id != company_id:
        raise HTTPException(status_code=404, detail="Import batch not found")
    if batch.status == "undone":
        raise HTTPException(status_code=409, detail="Batch already undone")

    entity_ids = batch.entity_ids or []
    rows = await lock_projections(session, company_id, entity_ids)
    entries = (await session.execute(select(Projection).where(
        Projection.company_id == company_id,
        Projection.entity_id.startswith(f"je:auto:opening-stock:{batch.id}:")))).scalars().all()
    unexpected = sorted(e.entity_id for e in entries if (e.state or {}).get("status") != "posted")
    if unexpected:
        raise HTTPException(status_code=409, detail={
            "code": "import_entry_changed",
            "message": "This import cannot be undone because the entry booking its opening stock was changed.",
            "entity_ids": unexpected,
        })

    # Only what the import itself wrote may be on the items: their creation, and the
    # inventory account the import's own entry booked them into.
    created_by_import = set(batch.idempotency_keys or [])
    booked_by_import: set[str] = set()
    modified = {eid for eid, row in rows.items() if row.entity_type != "item"}
    for eid, event_type, key, meta in (await session.execute(
        select(LedgerEntry.entity_id, LedgerEntry.event_type, LedgerEntry.idempotency_key, LedgerEntry.metadata_)
        .where(LedgerEntry.company_id == company_id, LedgerEntry.entity_id.in_(entity_ids)))).all():
        if event_type == "item.created" and key in created_by_import:
            continue
        if event_type == RECORDED and (meta or {}).get("recorded_by") == "opening stock":
            booked_by_import.add(eid)
            continue
        modified.add(eid)
    modified |= {eid for eid, row in rows.items()
                 if (row.state or {}).get(LOT_ACCOUNT_FIELD) and eid not in booked_by_import}
    modified |= await mentioned_elsewhere(session, company_id, {e: [e] for e in entity_ids},
                                          besides=[e.entity_id for e in entries])
    if modified:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "import_items_modified",
                "message": "This import cannot be undone because imported items were changed or used later.",
                "entity_ids": sorted(modified),
            },
        )

    # The entry comes off first: a locked period refuses it before anything is removed.
    for entry in entries:
        await _void_je_if_posted(
            session, company_id=company_id, user_id=user.id, doc_id=str(batch.id), je_id=entry.entity_id,
            idem_key=f"import-undo:{entry.entity_id}", reason="Import undone", trigger="item.import-undone")
    await erase_items(session, company_id, entity_ids)
    batch.status = "undone"
    # Release the operation so the same source can be imported again as a new entry.
    batch.operation_key = None
    batch.undone_at = datetime.now(_tz.utc)
    batch.undone_by = user.id
    await session.commit()

    return {
        "ok": True,
        "removed": len(entity_ids),
    }


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------

def _fmt_ts(val) -> str:
    """Ensure timestamps are ISO 8601 UTC with Z suffix."""
    if not val:
        return ""
    s = str(val).strip()
    if not s:
        return ""
    if s.endswith("Z"):
        return s
    if s.endswith("+00:00"):
        return s[:-6] + "Z"
    return s.rstrip() + "Z"


@router.get(
    "/export/csv",
    dependencies=[require_permission("view_inventory"), require_permission("import_export_data")],
)
async def export_items_csv(
    request: Request,
    company_id=Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
    role: str = Depends(get_current_role),
    settings: dict = Depends(get_current_company_settings),
    filters: ItemListFilters = Depends(),
    cols: str | None = None,
) -> StreamingResponse:
    """The item list as CSV: the list's filters and order, every matching row (pagination never
    applies) and, via ``cols``, the columns the screen shows in the order it shows them."""
    from celerp.services.field_schema import get_effective_field_schema

    price_config = await get_price_config(session, company_id)
    can_see_costs = role_has_permission(settings, role, "view_inventory_costs")
    # The column universe: the default export set, the effective schema (the filtered category's
    # when exactly one is selected, else the base item schema plus every category's fields, as
    # the All view's column manager offers them) and the derived money columns.
    single_cat = filters.category if filters.category and "," not in filters.category else None
    schema = await get_effective_field_schema(session, company_id, category=single_cat)
    category_fields: list[dict] = [] if single_cat else [
        fld for fields in (settings.get("category_schemas") or {}).values() for fld in fields
    ]
    price_cols = [price_key(pl["name"]) for pl in price_config[0] if pl.get("name")]
    default_cols = ["id", "sku", "name", "category", "quantity", "status"] + price_cols + [
        "weight", "weight_unit", "pieces", "sell_by", "barcode", "gtin", "rfid_epc", "hs_code",
        "purchase_sku", "purchase_name", "purchase_unit", "purchase_conversion_factor",
        "created_at", "updated_at",
    ]
    virtual = {fld["key"]: fld["paired_with"] for fld in schema if fld.get("virtual") and fld.get("paired_with")}
    # Image fields are list-only previews with no CSV representation.
    allowed = set(default_cols) | {
        fld["key"] for fld in schema + category_fields if fld.get("type") != "image"
    } | {"holding_value", "sold_price"}
    # The columns are settled before the rows are fetched, so an unknown column costs no query.
    out_cols = resolve_export_cols(cols, default_cols, allowed)
    listed = await query_items(session, company_id, role, filters, _attr_filters(request))

    # A column the role may not see leaves the header, not just the cells: the rows were already
    # stripped by the list pipeline, so this keeps the header honest. A cost-list column such as
    # landed_price is not a key apply_field_visibility names, so it is dropped here by list name,
    # and a virtual total follows the column it pairs with.
    probe_keys = set(out_cols) | {virtual[c] for c in out_cols if c in virtual}
    visible = set(apply_item_visibility([{k: 1 for k in probe_keys}], role, schema, can_see_costs)[0])
    if not can_see_costs:
        visible -= {price_key(pl["name"]) for pl in price_config[0] if pl.get("name") and is_cost_list_name(pl["name"])}
    out_cols = [c for c in out_cols if c in visible and virtual.get(c, c) in visible]

    unit_map = build_unit_map(await _get_company_units(session, company_id))
    currency = settings.get("currency") or "USD"

    def _rows():
        for it in listed["items"]:
            row = dict(it)
            # A virtual total the row does not carry is what the table shows: the stored cost_total
            # for the cost total, else the price times the quantity, at the currency's precision.
            for total_key, price_col in virtual.items():
                if total_key in row:
                    continue
                if total_key == "cost_price_total" and row.get("cost_total") not in (None, ""):
                    row[total_key] = row["cost_total"]
                elif row.get(price_col) not in (None, ""):
                    row[total_key] = to_stored_float(round_money(
                        to_decimal(row[price_col]) * to_decimal(row.get("quantity") or 0), currency))
            # The measure the sell unit already IS derives from quantity: the stored companion
            # field is absent on fresh items and can go stale after sales. Matches the table.
            sell_by = row.get("sell_by")
            if is_weight_unit(sell_by, unit_map):
                row["weight"] = row.get("quantity", "")
                row["weight_unit"] = sell_by
            elif is_pieces_unit(sell_by, unit_map):
                row["pieces"] = row.get("quantity", "")
            row["created_at"] = _fmt_ts(row.get("created_at"))
            row["updated_at"] = _fmt_ts(row.get("updated_at"))
            yield row

    return StreamingResponse(
        csv_stream(out_cols, _rows()),
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
    from celerp.importers.sinks import register_sink
    from celerp_inventory.migration_sink import SINK
    register_sink(SINK)


async def record_kept_stock_hook(*, session: AsyncSession) -> None:
    """on_modules_ready: recognize the archived and expired stock older releases left
    every company (lot_origin.record_kept_stock). A company staged for a data migration
    is left alone until the migration finishes."""
    from celerp.models.company import Company
    from celerp.services import migrations

    for company_id in (await session.execute(select(Company.id).order_by(Company.id))).scalars().all():
        if not await migrations.is_company_migration_staged(session, company_id):
            await record_kept_stock(session, company_id)
