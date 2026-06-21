# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT

from __future__ import annotations

import csv
import io
import uuid
from datetime import datetime, timezone

from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.db import get_session
from celerp.events.engine import emit_event
from celerp.models.projections import Projection
from celerp.services.auth import get_current_company_id, get_current_user, get_current_role, require_admin, require_manager, ROLE_LEVELS
from celerp.services.auto_je import create_for_item_transform
from celerp.services.units import DEFAULT_UNITS, validate_quantity, build_unit_map, is_weight_unit, is_pieces_unit, LANDED_COST_KINDS
from celerp_inventory.projections import is_item_available

router = APIRouter(dependencies=[Depends(get_current_user)])


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

VALID_INVENTORY_TYPES: frozenset[str] = frozenset({"stocked", "component", "non_stocked", "service", "freight"})

_DEFAULT_UNITS = DEFAULT_UNITS  # backwards-compat alias for any internal callers


async def _get_company_units(session: AsyncSession, company_id) -> list[dict]:
    """Return the company's units config (falls back to default seed)."""
    from celerp.models.company import Company
    company = await session.get(Company, company_id)
    if company:
        units = (company.settings or {}).get("units")
        if units:
            return units
    return DEFAULT_UNITS


def _to_int_pieces(val) -> int:
    """Convert a pieces value to int, tolerating float strings like '25.0'."""
    return int(float(val))


def _read_pieces(state: dict) -> float | None:
    """Read pieces from item state, checking top-level then attributes (single source of truth)."""
    raw = state.get("pieces")
    if raw is None:
        raw = (state.get("attributes") or {}).get("pieces")
    return float(raw) if raw not in (None, "") else None


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


def _flatten_item(state: dict, entity_id: str, location_id: str | None = None, location_name: str | None = None, created_at: object | None = None, updated_at: object | None = None) -> dict:
    """Flatten attributes dict to top-level so schema-driven UI sees all fields."""
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
    return flat


def _apply_field_visibility(items: list[dict], role: str, field_schema: list[dict]) -> list[dict]:
    """Strip fields from item dicts that the caller's role is not allowed to see.

    Two sources of restrictions:
    1. Schema-driven: field has visible_to_roles set and caller level is below minimum.
    2. Hardcoded: cost fields (cost_price, cost_total) require manager+.
    """
    caller_level = ROLE_LEVELS.get(role, 0)
    restricted = {
        f["key"]
        for f in field_schema
        if f.get("visible_to_roles") and caller_level < min(
            ROLE_LEVELS.get(r, 0) for r in f["visible_to_roles"]
        )
    }
    if caller_level < ROLE_LEVELS["manager"]:
        restricted |= _COST_ITEM_KEYS
    if not restricted:
        return items
    return [{k: v for k, v in item.items() if k not in restricted} for item in items]


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
    sku: str
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
    child_cost_total: float  # final cost (may be user-overridden)
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
_HIDDEN_STATUSES = frozenset({"sold", "archived", "merged", "expired"})

# "Archived" tab shows all terminal/inactive statuses grouped together.
_ARCHIVED_GROUP = frozenset({"archived", "merged", "expired"})

# Fields stripped from item responses for roles below manager.
_COST_ITEM_KEYS: frozenset[str] = frozenset({"cost_price", "cost_total"})


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
    sort: str | None = None,
    dir: str = "desc",
) -> dict:
    """List items with optional filters.

    status: exact status to show (e.g. "sold", "archived", "available").
            Pass "all" to skip status filtering entirely.
            Default (None): exclude sold + archived from results.
    category: exact category to filter on.
    """
    from celerp.models.company import Company, Location
    from celerp.services.field_schema import get_effective_field_schema
    stmt = select(Projection).where(Projection.company_id == company_id, Projection.entity_type == "item")
    rows = (await session.execute(stmt)).scalars().all()

    loc_rows = (await session.execute(select(Location).where(Location.company_id == company_id))).scalars().all()
    loc_map = {str(r.id): r.name for r in loc_rows}

    result = [
        _flatten_item(r.state, r.entity_id,
                      location_id=str(r.location_id) if r.location_id else None,
                      location_name=loc_map.get(str(r.location_id)) if r.location_id else None,
                      created_at=r.created_at,
                      updated_at=r.updated_at)
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

    if category:
        cats = {c.strip() for c in category.split(",") if c.strip()}
        result = [r for r in result if str(r.get("category") or "") in cats]

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

    if q:
        # Support comma-separated OR queries (e.g. from barcode scanner multi-scan)
        terms = [t.strip().lower() for t in q.split(",") if t.strip()]
        _SEARCH_FIELDS = ("name", "sku", "barcode", "description", "category")
        _SKIP_KEYS = frozenset({"id", "entity_id", "company_id", "location_id", "quantity",
                                 "weight", "pieces", "status", "created_at", "updated_at"})
        def _item_matches_term(r: dict, term: str) -> bool:
            for field in _SEARCH_FIELDS:
                if term in str(r.get(field, "")).lower():
                    return True
            for v in (r.get("attributes") or {}).values():
                if term in str(v).lower():
                    return True
            for k, v in r.items():
                if k in _SKIP_KEYS or k in _SEARCH_FIELDS or k.endswith("_price"):
                    continue
                if isinstance(v, str) and term in v.lower():
                    return True
            return False
        def _item_matches(r: dict) -> bool:
            return any(_item_matches_term(r, term) for term in terms)
        result = [r for r in result if _item_matches(r)]

    # Apply visible_to_roles filtering from company field schema
    field_schema = await get_effective_field_schema(session, company_id, category=None)
    result = _apply_field_visibility(result, role, field_schema)

    # FEFO: when company uses fefo, sort available items by expires_at ascending (soonest first)
    # so staff always see the items that need to be picked/sold first at the top.
    company = await session.get(Company, company_id)
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
    return {"items": result[offset: offset + limit], "total": total,
            "attribute_facets": attribute_facets}


@router.get("/valuation")
async def get_valuation(
    category: str | None = None,
    status: str | None = None,
    company_id=Depends(get_current_company_id),
    role: str = Depends(get_current_role),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Aggregate inventory valuation from projections.

    Optional ?category= and ?status= filters scope totals + count_by_status to that slice.
    category_counts is always global (all active items) — used by the category tab bar.
    count_by_status is scoped to the current category/status filter — used by status cards.
    """
    rows = (
        await session.execute(
            select(Projection).where(Projection.company_id == company_id, Projection.entity_type == "item")
        )
    ).scalars().all()

    # Compute price totals dynamically per price list
    from celerp.models.company import Company as _Company
    co = await session.get(_Company, company_id)
    _settings = co.settings if co else {}
    _price_lists: list[dict] = (_settings or {}).get("price_lists") or [{"name": "Retail"}, {"name": "Wholesale"}, {"name": "Cost"}]

    price_totals: dict[str, Decimal] = {}
    for pl in _price_lists:
        price_totals[pl.get("name", "")] = Decimal(0)
    active_item_count = 0
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

        active_item_count += 1
        qty = float(state.get("quantity") or 0)
        for pl in _price_lists:
            pl_name = pl.get("name", "")
            key = f"{pl_name.lower()}_price"
            try:
                if pl_name.lower() in ("cost", "cost price", "landed"):
                    # Recipe-backed item: value at the recipe's standard unit cost (single source of
                    # truth, consistent with _flatten_item). Else stored cost_total (lot total),
                    # else fallback to unit price * qty.
                    _runit = _recipe_standard_unit_cost(state)
                    if _runit is not None:
                        price_totals[pl_name] += Decimal(str(_runit)) * Decimal(str(qty))
                    elif state.get("cost_total") is not None:
                        price_totals[pl_name] += Decimal(str(state.get("cost_total")))
                    elif state.get(key) is not None:
                        price_totals[pl_name] += Decimal(str(state[key])) * Decimal(str(qty))
                else:
                    v = state.get(key)
                    if v is not None:
                        price_totals[pl_name] += Decimal(str(v)) * Decimal(str(qty))
            except Exception:
                pass

    _cost_pl_names = {pl.get("name", "") for pl in _price_lists if pl.get("name", "").lower() in ("cost", "cost price", "landed")}
    show_cost = ROLE_LEVELS.get(role, 0) >= ROLE_LEVELS["manager"]

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
        # total_scoped_count is always active_item_count - used for the "All" tab
        # (some items may have no category and won't appear in category_counts)
        "total_scoped_count": active_item_count,
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
    for row in rows:
        if row.entity_id in demo_eids:
            continue
        flat = _flatten_item(row.state, row.entity_id)
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
async def get_item(entity_id: str, company_id=Depends(get_current_company_id), role: str = Depends(get_current_role), session: AsyncSession = Depends(get_session)) -> dict:
    from celerp.models.company import Location
    from celerp.services.field_schema import get_effective_field_schema
    row = await session.get(Projection, {"company_id": company_id, "entity_id": entity_id})
    if row is None:
        raise HTTPException(status_code=404, detail="Not found")
    loc_name: str | None = None
    if row.location_id:
        loc = await session.get(Location, row.location_id)
        loc_name = loc.name if loc else None
    flat = _flatten_item(row.state, row.entity_id,
                         location_id=str(row.location_id) if row.location_id else None,
                         location_name=loc_name,
                         created_at=row.created_at,
                         updated_at=row.updated_at)
    field_schema = await get_effective_field_schema(session, company_id, category=flat.get("category"))
    filtered = _apply_field_visibility([flat], role, field_schema)
    return filtered[0]


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


@router.post("")
async def post_item(payload: ItemCreate, company_id=Depends(get_current_company_id), user=Depends(get_current_user), role: str = Depends(get_current_role), session: AsyncSession = Depends(get_session)) -> dict:
    # Guard: operator/viewer cannot set cost fields on creation (manager+ required)
    if (payload.cost_price is not None or payload.cost_total is not None) and ROLE_LEVELS.get(role, 0) < ROLE_LEVELS["manager"]:
        raise HTTPException(status_code=403, detail=f"Role '{role}' cannot set cost fields")

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

    # Auto-copy SKU to barcode when barcode omitted and SKU is purely numeric
    if payload.barcode is None and payload.sku.isdigit():
        payload = payload.model_copy(update={"barcode": payload.sku})

    # SKU uniqueness
    existing_sku = (await session.execute(
        select(Projection).where(
            Projection.company_id == company_id,
            Projection.entity_type == "item",
            Projection.state["sku"].as_string() == payload.sku,
        )
    )).scalars().first()
    if existing_sku:
        raise HTTPException(status_code=409, detail=f"SKU '{payload.sku}' already exists")

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

    # Ensure status is set (not part of ItemCreate model but required for projections)
    data.setdefault("status", "available")

    # Strip price fields from create event data - they go via pricing events.
    # Any key ending in _price is treated as a pricing field. cost_total is also a pricing field.
    price_fields = {k: data.pop(k) for k in list(data) if k.endswith("_price") and data[k] is not None}
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


@router.patch("/{entity_id}")
async def patch_item(entity_id: str, payload: ItemPatch, company_id=Depends(get_current_company_id), user=Depends(get_current_user), role: str = Depends(get_current_role), session: AsyncSession = Depends(get_session)) -> dict:
    # Guard: restricted fields require manager+ role
    from celerp.services.field_schema import get_effective_field_schema
    field_schema = await get_effective_field_schema(session, company_id)
    restricted = {f["key"] for f in field_schema if f.get("visible_to_roles") and ROLE_LEVELS.get(role, 0) < min(ROLE_LEVELS.get(r, 0) for r in f["visible_to_roles"])}
    changed_keys = set(payload.fields_changed.keys())
    blocked = changed_keys & restricted
    if blocked:
        raise HTTPException(status_code=403, detail=f"Role '{role}' cannot modify restricted fields: {sorted(blocked)}")

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

    # Validate SKU uniqueness if changing
    if "sku" in changed_keys:
        new_sku = (payload.fields_changed["sku"] or {}).get("new")
        if new_sku:
            existing_sku = (await session.execute(
                select(Projection).where(
                    Projection.company_id == company_id,
                    Projection.entity_type == "item",
                    Projection.state["sku"].as_string() == new_sku,
                    Projection.entity_id != entity_id,
                )
            )).scalars().first()
            if existing_sku:
                raise HTTPException(status_code=409, detail=f"SKU '{new_sku}' already exists")

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
async def bulk_set_status(payload: BulkStatusBody, company_id=Depends(get_current_company_id), _: None = Depends(require_manager), user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    if not payload.entity_ids:
        raise HTTPException(status_code=422, detail="entity_ids must not be empty")
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


@router.post("/bulk/transfer")
async def bulk_transfer(payload: BulkTransferBody, company_id=Depends(get_current_company_id), user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    if not payload.entity_ids:
        raise HTTPException(status_code=422, detail="entity_ids must not be empty")
    event_ids = []
    for entity_id in payload.entity_ids:
        entry = await emit_event(
            session,
            company_id=company_id,
            entity_id=entity_id,
            entity_type="item",
            event_type="item.transferred",
            data={"to_location_id": str(payload.to_location_id)},
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
async def bulk_delete(payload: BulkDeleteBody, company_id=Depends(get_current_company_id), _: None = Depends(require_manager), user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
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
async def bulk_expire(payload: BulkExpireBody, company_id=Depends(get_current_company_id), _: None = Depends(require_manager), user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    if not payload.entity_ids:
        raise HTTPException(status_code=422, detail="entity_ids must not be empty")
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
async def transfer_item(entity_id: str, payload: TransferBody, company_id=Depends(get_current_company_id), user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    entry = await emit_event(
        session,
        company_id=company_id,
        entity_id=entity_id,
        entity_type="item",
        event_type="item.transferred",
        data={"to_location_id": str(payload.to_location_id)},
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
        prefix = f"{parent_sku}."
        existing_res = await session.execute(
            select(Projection).where(
                Projection.company_id == company_id,
                Projection.entity_type == "item",
            )
        )
        all_items = existing_res.scalars().all()
        max_suffix = 0
        for it in all_items:
            sku = str(it.state.get("sku", "") or "")
            if sku.startswith(prefix) and "." not in sku[len(prefix):]:
                try:
                    max_suffix = max(max_suffix, int(sku[len(prefix):]))
                except ValueError:
                    pass
        child_sku = f"{prefix}{max_suffix + 1}"

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
async def split_item(entity_id: str, payload: SplitBody, company_id=Depends(get_current_company_id), user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
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

    parent_weight: float | None = _read_float(parent.state, "weight")
    parent_weight_unit = parent.state.get("weight_unit") or "gram"
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

    # Validate child SKU uniqueness within batch
    child_skus = [c.sku for c in children]
    if len(child_skus) != len(set(child_skus)):
        raise HTTPException(status_code=409, detail="Duplicate SKUs within split children")

    # Validate child SKUs against existing items
    parent_sku = parent.state.get("sku")
    for child_sku in child_skus:
        if child_sku == parent_sku:
            raise HTTPException(status_code=422, detail=f"Child SKU cannot be the same as the parent SKU '{parent_sku}'. The parent keeps its original SKU.")
        existing = (await session.execute(
            select(Projection).where(
                Projection.company_id == company_id,
                Projection.entity_type == "item",
                Projection.state["sku"].as_string() == child_sku,
            )
        )).scalars().first()
        if existing:
            raise HTTPException(status_code=409, detail=f"SKU '{child_sku}' already exists")

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

    # Per-child history descriptors with sequential mother deltas (one row per child).
    children_detail: list[dict] = []
    running_qty = parent_qty
    running_pieces = parent_pieces
    running_weight = parent_weight

    for i, child in enumerate(children):
        child_eid = f"item:{uuid.uuid4()}"
        child_eids.append(child_eid)
        child_qty_list.append(child.quantity)
        # Copy-all-then-override: inherit every parent field; reset only identity/qty/cost/status.
        child_data: dict = {
            k: v for k, v in parent.state.items() if k not in _CHILD_RESET_FIELDS
        }
        child_data.update({
            "sku": child.sku,
            "name": parent.state.get("name", child.sku),
            "quantity": child.quantity,
            "status": "available",
            "attributes": {**parent_attrs, **child.attributes},
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
        ch_weight = child.weight if (parent_weight is not None and child.weight is not None) else None
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

    # Reduce parent quantity — use explicit mother_qty override when provided (user re-weighed mother).
    new_parent_qty = payload.mother_qty if payload.mother_qty is not None else round(parent_qty - total_child_qty, 10)
    # Clamp sub-epsilon residuals from float subtraction to an exact zero.
    if new_parent_qty < 0:
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
        metadata_={},
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
            metadata_={},
        )

    # Apply mother parcel overrides: weight computed server-side, pieces computed server-side
    computed_mother_pieces: int | None = None
    if parent_pieces is not None:
        total_child_pieces = sum(_to_int_pieces(c.attributes.get("pieces", 0)) for c in children)
        computed_mother_pieces = parent_pieces - total_child_pieces

    computed_mother_weight: float | None = None
    if payload.mother_weight is not None:
        # User explicitly re-weighed the mother parcel — use that value directly.
        computed_mother_weight = round(payload.mother_weight, weight_decimals)
    elif parent_weight is not None:
        total_child_weight = sum(c.weight for c in payload.children if c.weight is not None)
        computed_mother_weight = round(parent_weight - total_child_weight, weight_decimals)

    if computed_mother_weight is not None or computed_mother_pieces is not None:
        fields_changed: dict[str, dict] = {}
        if computed_mother_weight is not None:
            fields_changed["weight"] = {"old": parent.state.get("weight"), "new": computed_mother_weight}
        if computed_mother_pieces is not None:
            new_attrs = dict(parent_attrs)
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
            metadata_={},
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

    The child (SKU ``{parent_sku}.N``) is the split-off portion; the mother keeps
    the remainder under its original SKU. Cost splits proportionally by quantity.
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

    # Next free child SKU: {parent_sku}.N
    rows = (await session.execute(
        select(Projection).where(Projection.company_id == company_id, Projection.entity_type == "item")
    )).scalars().all()
    prefix = f"{parent_sku}."
    max_suffix = 0
    for it in rows:
        sku = str(it.state.get("sku", "") or "")
        if sku.startswith(prefix) and "." not in sku[len(prefix):]:
            try:
                max_suffix = max(max_suffix, int(sku[len(prefix):]))
            except ValueError:
                pass
    child_sku = f"{prefix}{max_suffix + 1}"

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
                     idempotency_key=str(uuid.uuid4()), metadata_={})
    if child_cost_total is not None and parent_cost_total:
        await emit_event(session, company_id=company_id, entity_id=entity_id, entity_type="item",
                         event_type="item.pricing.set",
                         data={"price_type": "cost_total", "new_price": max(0.0, round(parent_cost_total - child_cost_total, 10))},
                         actor_id=user_id, location_id=None, source="fulfill_split",
                         idempotency_key=str(uuid.uuid4()), metadata_={})
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
                         idempotency_key=str(uuid.uuid4()), metadata_={})

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
    await emit_event(session, company_id=company_id, entity_id=entity_id, entity_type="item",
                     event_type="item.split",
                     data={"child_ids": [child_eid], "child_skus": [child_sku], "quantities": [child_qty],
                           "parent_sku": parent_sku or "", "children_detail": [child_detail]},
                     actor_id=user_id, location_id=None, source="fulfill_split",
                     idempotency_key=str(uuid.uuid4()), metadata_={})
    return child_eid, child_sku


@router.post("/{entity_id}/transform")
async def transform_item(entity_id: str, payload: TransformBody, company_id=Depends(get_current_company_id), user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
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

    # Check child SKU uniqueness
    existing = (await session.execute(
        select(Projection).where(
            Projection.company_id == company_id,
            Projection.entity_type == "item",
            Projection.state["sku"].as_string() == payload.child_sku,
        )
    )).scalars().first()
    if existing:
        raise HTTPException(status_code=409, detail=f"SKU '{payload.child_sku}' already exists")

    parent_qty = float(parent.state.get("quantity") or 0)
    parent_attrs = dict(parent.state.get("attributes") or {})
    # Exclude cost fields: child cost is set explicitly from child_cost_total
    parent_prices = {k: parent.state[k] for k in parent.state if k.endswith("_price") and parent.state[k] is not None and k != "cost_price"}
    parent_cost_total = float(parent.state.get("cost_total") or 0) or (
        float(parent.state.get("cost_price") or 0) * parent_qty
    )
    parent_location_id = parent.state.get("location_id")

    child_eid = f"item:{uuid.uuid4()}"

    # Copy-all-then-override: inherit every parent field; reset only identity/qty/cost/status.
    # Also override sell_by and category — the purpose of a transform is to change these.
    child_data: dict = {
        k: v for k, v in parent.state.items() if k not in _CHILD_RESET_FIELDS
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

    # 2. Copy prices
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
            metadata_={"reason": "from_transform"},
        )

    # 2b. Set child cost via item.pricing.set (consistent with split/post_item flows)
    await emit_event(
        session,
        company_id=company_id,
        entity_id=child_eid,
        entity_type="item",
        event_type="item.pricing.set",
        data={"price_type": "cost_total", "new_price": payload.child_cost_total},
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
            "child_cost_total": payload.child_cost_total,
            "parent_sku": parent.state.get("sku") or "",
            "child_origin_event_id": transform_origin.id,
            # Mother is consumed by the transform (archived) → after-values are 0.
            "qty_before": parent_qty,
            "qty_after": 0,
            "pieces_before": _read_pieces(parent.state),
            "pieces_after": 0 if _read_pieces(parent.state) is not None else None,
            "weight_before": _read_float(parent.state, "weight"),
            "weight_after": 0 if _read_float(parent.state, "weight") is not None else None,
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
async def merge_items(payload: MergeBody, company_id=Depends(get_current_company_id), user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    if len(payload.source_entity_ids) < 2:
        raise HTTPException(status_code=422, detail="At least 2 source_entity_ids are required to merge.")

    # Fetch projections for all source items.
    source_projections: list[Projection] = []
    for sid in payload.source_entity_ids:
        proj = await session.get(Projection, {"company_id": company_id, "entity_id": sid})
        if proj is None:
            raise HTTPException(status_code=404, detail=f"Item '{sid}' not found.")
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
    # Compute merged cost_total: sum of all source cost_totals (Q2=Option A)
    merged_cost_total = sum(
        float(p.state.get("cost_total") or 0) or (
            float(p.state.get("cost_price") or 0) * float(p.state.get("quantity") or 0)
        )
        for p in source_projections
    ) or None

    expiry_dates = sorted(e for p in source_projections if (e := _get_expiry(p)))
    earliest_expiry = expiry_dates[0] if expiry_dates else None

    # Resolve attributes: collect all keys across sources.
    def _is_numeric(val: str) -> bool:
        try:
            float(val)
            return True
        except (TypeError, ValueError):
            return False

    # Expiry-related attributes are handled separately (earliest wins); exclude from conflict resolution.
    _EXPIRY_ATTR_KEYS = frozenset({"expiry_date", "warranty_exp", "expires_at"})

    all_attr_keys: set[str] = set()
    for p in source_projections:
        all_attr_keys.update((p.state.get("attributes") or {}).keys())
    all_attr_keys -= _EXPIRY_ATTR_KEYS

    resolved_attrs: dict = {}
    unresolved_conflicts: list[str] = []
    for key in all_attr_keys:
        # Collect raw attribute values (preserve original type for numeric fields)
        raw_values = [(p.state.get("attributes") or {}).get(key) for p in source_projections if key in (p.state.get("attributes") or {})]
        str_values = [str(v) for v in raw_values]
        unique_str_vals = set(str_values)
        if len(unique_str_vals) == 1:
            # No conflict — carry forward the original typed value.
            resolved_attrs[key] = raw_values[0]
        elif all(_is_numeric(v) for v in unique_str_vals):
            # Numeric conflict — sum. Store as number to preserve type through round-trips.
            total = sum(float(v) for v in str_values)
            resolved_attrs[key] = int(total) if total == int(total) else total
        else:
            # String conflict — require user resolution.
            if payload.resolved_attributes and key in payload.resolved_attributes:
                resolved_attrs[key] = str(payload.resolved_attributes[key])
            else:
                unresolved_conflicts.append(key)

    if unresolved_conflicts:
        raise HTTPException(
            status_code=422,
            detail=f"Attribute conflicts require resolution via resolved_attributes: {sorted(unresolved_conflicts)}.",
        )

    # Apply user overrides.
    resulting_qty = payload.resulting_quantity if payload.resulting_quantity is not None else total_qty
    resulting_cost = payload.resulting_cost_total if payload.resulting_cost_total is not None else merged_cost_total
    resulting_name = payload.resulting_name if payload.resulting_name is not None else str(target_proj.state.get("name") or "")

    # Update expiry_date attribute to earliest.
    if earliest_expiry:
        resolved_attrs["expiry_date"] = earliest_expiry

    # Build item.created data from target projection.
    target_state = target_proj.state
    new_entity_id = f"item:{uuid.uuid4()}"
    create_data: dict = {
        "sku": str(target_state.get("sku") or ""),
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

    # Emit pricing events for all price fields from target; emit cost_total (not cost_price) for cost.
    price_fields: dict = {}
    if resulting_cost is not None:
        price_fields["cost_total"] = resulting_cost
    for pf, val in target_state.items():
        if pf.endswith("_price") and pf != "cost_price" and val is not None:
            price_fields[pf] = val

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
    new_sku = str(target_state.get("sku") or new_entity_id)
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
async def adjust_item(entity_id: str, payload: AdjustBody, company_id=Depends(get_current_company_id), _: None = Depends(require_manager), user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
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
async def set_item_price(entity_id: str, payload: PriceBody, company_id=Depends(get_current_company_id), _: None = Depends(require_manager), user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
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
async def set_item_status(entity_id: str, payload: StatusBody, company_id=Depends(get_current_company_id), user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
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
async def reserve_item(entity_id: str, payload: ReserveBody, company_id=Depends(get_current_company_id), user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
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
async def unreserve_item(entity_id: str, payload: ReserveBody, company_id=Depends(get_current_company_id), user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
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
async def expire_item(entity_id: str, company_id=Depends(get_current_company_id), _: None = Depends(require_manager), user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
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

        scoped_key = f"{company_id}:{rec.idempotency_key}"
        if scoped_key in existing:
            if body.upsert:
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
    _=Depends(require_admin),
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
    items = [_flatten_item(r.state, r.entity_id, created_at=r.created_at, updated_at=r.updated_at) for r in rows]
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

    # Build price columns dynamically from company settings
    from celerp.models.company import Company
    co = await session.get(Company, company_id)
    settings = co.settings if co else {}
    price_lists: list[dict] = (settings or {}).get("price_lists") or [{"name": "Retail"}, {"name": "Wholesale"}, {"name": "Cost"}]
    price_cols = [f"{pl.get('name', '').lower()}_price" for pl in price_lists if pl.get("name")]

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

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=_COLS, extrasaction="ignore")
    writer.writeheader()
    for it in items:
        row = {c: it.get(c, "") for c in _COLS}
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

