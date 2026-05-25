# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

from __future__ import annotations

import csv
import io
import uuid
from datetime import datetime, timezone

from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.db import get_session
from celerp.events.engine import emit_event
from celerp.models.projections import Projection
from celerp.services.auth import get_current_company_id, get_current_user, get_current_role, require_admin, require_manager, ROLE_LEVELS
from celerp.services.auto_je import create_for_item_transform
from celerp.services.units import DEFAULT_UNITS, validate_quantity, build_unit_map, is_weight_unit, is_pieces_unit

router = APIRouter(dependencies=[Depends(get_current_user)])


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

VALID_INVENTORY_TYPES: frozenset[str] = frozenset({"stocked", "non_stocked", "service"})

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
    return float(raw) if raw is not None else None


def _flatten_item(state: dict, entity_id: str, location_id: str | None = None, location_name: str | None = None) -> dict:
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
    qty = float(flat.get("quantity") or 0)
    if flat.get("cost_total") is not None:
        flat["cost_price"] = round(float(flat["cost_total"]) / qty, 10) if qty else 0.0
    elif flat.get("cost_price") is not None:
        flat["cost_total"] = round(float(flat["cost_price"]) * qty, 2)
    # else: both remain absent (item has no cost set)
    return flat


def _apply_field_visibility(items: list[dict], role: str, field_schema: list[dict]) -> list[dict]:
    """Strip fields from item dicts that the caller's role is not allowed to see.

    A field is restricted if its visible_to_roles list is non-empty AND the caller's
    ROLE_LEVELS level is below the minimum level of any role in that list.
    Empty visible_to_roles means visible to all.
    """
    caller_level = ROLE_LEVELS.get(role, 0)
    restricted = {
        f["key"]
        for f in field_schema
        if f.get("visible_to_roles") and caller_level < min(
            ROLE_LEVELS.get(r, 0) for r in f["visible_to_roles"]
        )
    }
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
    inventory_type: str = "stocked"  # stocked | non_stocked | service


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
    barcode: str | None = None  # auto-assigned from shared sequence if omitted
    attributes: dict = Field(default_factory=dict)


class SplitBody(BaseModel):
    children: list[SplitChild]
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


@router.get("")
async def list_items(
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
                      location_name=loc_map.get(str(r.location_id)) if r.location_id else None)
        for r in rows
    ]

    # Status filtering: default excludes hidden statuses; "all" skips filtering;
    # "archived" expands to include merged/expired (and legacy disposed events).
    if status == "all":
        pass  # no filter
    elif status == "archived":
        result = [r for r in result if str(r.get("status") or "").lower() in _ARCHIVED_GROUP]
    elif status:
        result = [r for r in result if str(r.get("status") or "").lower() == status.lower()]
    else:
        result = [r for r in result if str(r.get("status") or "").lower() not in _HIDDEN_STATUSES]

    if category:
        result = [r for r in result if str(r.get("category") or "") == category]

    if inventory_type:
        result = [r for r in result if (r.get("inventory_type") or "stocked") == inventory_type]

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

    total = len(result)
    return {"items": result[offset: offset + limit], "total": total}


@router.get("/valuation")
async def get_valuation(
    category: str | None = None,
    status: str | None = None,
    company_id=Depends(get_current_company_id),
    _: None = Depends(require_manager),
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
                    # Cost uses stored cost_total (lot total), else fallback to unit price * qty
                    tc = state.get("cost_total")
                    if tc is not None:
                        price_totals[pl_name] += Decimal(str(tc))
                    elif state.get(key) is not None:
                        price_totals[pl_name] += Decimal(str(state[key])) * Decimal(str(qty))
                else:
                    v = state.get(key)
                    if v is not None:
                        price_totals[pl_name] += Decimal(str(v)) * Decimal(str(qty))
            except Exception:
                pass

    return {
        "item_count": active_item_count,
        "active_item_count": active_item_count,
        "price_totals": {k: float(v) for k, v in price_totals.items()},
        # Backward-compatible keys for existing UI
        "cost_total": float(price_totals.get("Cost", 0)),
        "wholesale_total": float(price_totals.get("Wholesale", 0)),
        "retail_total": float(price_totals.get("Retail", 0)),
        "category_counts": dict(sorted(category_counts.items(), key=lambda x: -x[1])),
        # total_scoped_count is always active_item_count - used for the "All" tab
        # (some items may have no category and won't appear in category_counts)
        "total_scoped_count": active_item_count,
        "count_by_status": count_by_status,
    }


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
                         location_name=loc_name)
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
        raise HTTPException(status_code=403, detail=f"Role '{role}' cannot set cost_price")

    if payload.inventory_type not in VALID_INVENTORY_TYPES:
        raise HTTPException(status_code=422, detail=f"inventory_type must be one of {sorted(VALID_INVENTORY_TYPES)}")

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

    # Ensure timestamps are always set on creation
    now_iso = datetime.now(timezone.utc).isoformat()
    data.setdefault("created_at", now_iso)
    data.setdefault("updated_at", now_iso)

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

    # Validate quantity change against current sell_by unit
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

    # Validate weight is non-negative
    if "weight" in changed_keys:
        new_weight = (payload.fields_changed["weight"] or {}).get("new")
        if new_weight is not None and float(new_weight) < 0:
            raise HTTPException(status_code=422, detail="Weight cannot be negative")

    # Always stamp updated_at so the projection reflects the mutation time.
    payload.fields_changed["updated_at"] = {"old": None, "new": datetime.now(timezone.utc).isoformat()}

    # sell_by sync: when sell_by changes unit type, sync quantity → weight or pieces so
    # the independent stored fields stay consistent with what the derived display showed.
    # Without this, switching piece→carat→edit qty→piece restores stale weight/pieces value.
    if "sell_by" in changed_keys:
        new_sell_by = (payload.fields_changed["sell_by"] or {}).get("new")
        if new_sell_by:
            _sync_row = await session.get(Projection, {"company_id": company_id, "entity_id": entity_id})
            if _sync_row:
                _sync_units = await _get_company_units(session, company_id)
                _sync_unit_map = {u["name"]: u for u in _sync_units}
                qty = _sync_row.state.get("quantity")
                if qty is not None:
                    if is_weight_unit(new_sell_by, _sync_unit_map):
                        payload.fields_changed["weight"] = {"old": _sync_row.state.get("weight"), "new": float(qty)}
                        payload.fields_changed["weight_unit"] = {"old": _sync_row.state.get("weight_unit"), "new": new_sell_by}
                    elif is_pieces_unit(new_sell_by, _sync_unit_map):
                        payload.fields_changed["pieces"] = {"old": (_sync_row.state.get("attributes") or {}).get("pieces"), "new": int(round(float(qty)))}

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
            data={"to_location_id": str(payload.to_location_id), "updated_at": datetime.now(timezone.utc).isoformat()},
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
        data={"to_location_id": str(payload.to_location_id), "updated_at": datetime.now(timezone.utc).isoformat()},
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
    if parent is None or not parent.state.get("is_available", True):
        raise HTTPException(status_code=404, detail="Item not found or unavailable")

    parent_qty = float(parent.state.get("quantity") or 0)
    if parent_qty <= 0:
        raise HTTPException(status_code=422, detail="parent qty must be > 0")

    parent_sku = parent.state.get("sku", "")
    parent_sell_by = parent.state.get("sell_by") or "piece"
    parent_weight_raw = parent.state.get("weight")
    parent_weight = float(parent_weight_raw) if parent_weight_raw is not None else None
    parent_pieces = _read_pieces(parent.state)

    units = await _get_company_units(session, company_id)
    unit_map = {u["name"]: u for u in units}
    unit_cfg = unit_map.get(parent_sell_by) or {}
    decimals = unit_cfg.get("decimals", 0)
    sell_by_label = unit_cfg.get("label", parent_sell_by)

    parent_weight_unit = parent.state.get("weight_unit") or "gram"
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

    result: dict = {
        "parent_sku": parent_sku,
        "parent_name": parent.state.get("name", parent_sku),
        "parent_qty": parent_qty,
        "child_sku": child_sku,
        "sell_by": parent_sell_by,
        "sell_by_label": sell_by_label,
        "unit_decimals": decimals,
        "weight_decimals": weight_decimals,
        "has_weight": parent_weight is not None,
        "has_pieces": parent_pieces is not None,
        "cannot_split": (decimals == 0 and parent_qty <= 1),
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
    if parent is None or not parent.state.get("is_available", True):
        raise HTTPException(status_code=404, detail="Item not found or unavailable")

    if not parent.state.get("allow_splitting", True):
        raise HTTPException(
            status_code=422,
            detail="Allow splitting is set to No for this item. Change Allow Splitting to Yes in the item details to enable splitting.",
        )

    parent_qty = float(parent.state.get("quantity") or 0)
    parent_sell_by = parent.state.get("sell_by") or "piece"
    parent_category = parent.state.get("category")
    parent_location_id = parent.state.get("location_id")
    parent_attrs = dict(parent.state.get("attributes") or {})

    # Fields to preserve on children (everything except identity/qty/cost - cost split proportionally)
    parent_prices = {k: parent.state[k] for k in parent.state if k.endswith("_price") and parent.state[k] is not None and k != "cost_price"}
    parent_cost_total = float(parent.state.get("cost_total") or 0) or (
        float(parent.state.get("cost_price") or 0) * parent_qty
    )
    parent_description = parent.state.get("description")
    parent_status = parent.state.get("status")
    parent_tax_codes = parent.state.get("tax_codes")
    parent_expires_at = parent.state.get("expires_at")

    units = await _get_company_units(session, company_id)
    unit_map = {u["name"]: u for u in units}
    unit_cfg = unit_map.get(parent_sell_by)
    decimals = unit_cfg["decimals"] if unit_cfg else 0

    parent_weight_raw = parent.state.get("weight")
    parent_weight: float | None = float(parent_weight_raw) if parent_weight_raw is not None else None
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

    # Validate total <= parent qty
    total_child_qty = sum(c.quantity for c in children)
    if round(total_child_qty, 10) >= round(parent_qty, 10):
        raise HTTPException(
            status_code=422,
            detail=f"Child quantities ({total_child_qty}) exceed or equal parent quantity ({parent_qty})",
        )

    # Pieces conservation: if parent has pieces, validate and compute mother
    _pieces_float = _read_pieces(parent.state)
    parent_pieces: int | None = _to_int_pieces(_pieces_float) if _pieces_float is not None else None
    if parent_pieces is not None:
        total_child_pieces = sum(_to_int_pieces(c.attributes.get("pieces", 0)) for c in children)
        if total_child_pieces >= parent_pieces:
            raise HTTPException(
                status_code=422,
                detail=f"Total child pieces ({total_child_pieces}) must be less than parent pieces ({parent_pieces})",
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
    for child in children:
        child_eid = f"item:{uuid.uuid4()}"
        child_eids.append(child_eid)
        child_qty_list.append(child.quantity)
        merged_attrs = {**parent_attrs, **child.attributes}
        now_iso = datetime.now(timezone.utc).isoformat()
        child_data: dict = {
            "sku": child.sku,
            "name": parent.state.get("name", child.sku),
            "quantity": child.quantity,
            "sell_by": parent_sell_by,
            "allow_splitting": bool(parent.state.get("allow_splitting", True)),
            "attributes": merged_attrs,
            "created_at": now_iso,
            "updated_at": now_iso,
        }
        if child.weight is not None:
            child_data["weight"] = child.weight
        if parent_category:
            child_data["category"] = parent_category
        if parent_location_id:
            child_data["location_id"] = parent_location_id
        if parent_description:
            child_data["description"] = parent_description
        if parent_status:
            child_data["status"] = parent_status
        parent_weight_unit = parent.state.get("weight_unit")
        if parent_weight_unit:
            child_data["weight_unit"] = parent_weight_unit
        if parent_tax_codes:
            child_data["tax_codes"] = parent_tax_codes
        if parent_expires_at:
            child_data["expires_at"] = parent_expires_at
        # Auto-assign barcode from shared sequence (or use caller-supplied override)
        child_data["barcode"] = child.barcode if child.barcode is not None else str(next_barcode_seq).zfill(6)
        next_barcode_seq += 1
        await emit_event(
            session,
            company_id=company_id,
            entity_id=child_eid,
            entity_type="item",
            event_type="item.created",
            data=child_data,
            actor_id=user.id,
            location_id=uuid.UUID(parent_location_id) if parent_location_id else None,
            source="api",
            idempotency_key=str(uuid.uuid4()),
            metadata_={"parent_id": entity_id},
        )

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
                metadata_={},
            )
        # Assign proportional cost_total to child
        if parent_cost_total and parent_qty:
            child_cost_total = round(parent_cost_total * (child.quantity / parent_qty), 10)
            await emit_event(
                session,
                company_id=company_id,
                entity_id=child_eid,
                entity_type="item",
                event_type="item.pricing.set",
                data={"price_type": "cost_total", "new_price": child_cost_total},
                actor_id=user.id,
                location_id=None,
                source="api",
                idempotency_key=str(uuid.uuid4()),
                metadata_={},
            )

    # Reduce parent quantity
    new_parent_qty = parent_qty - total_child_qty
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

    # Update parent cost_total (Q1=A: reduce by sum of child cost_totals)
    if parent_cost_total and parent_qty:
        total_child_cost = sum(
            round(parent_cost_total * (c.quantity / parent_qty), 10) for c in children
        )
        parent_remaining_cost = round(parent_cost_total - total_child_cost, 10)
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
    if parent_weight is not None:
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


@router.post("/{entity_id}/transform")
async def transform_item(entity_id: str, payload: TransformBody, company_id=Depends(get_current_company_id), user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    # Fetch parent
    parent = await session.get(Projection, {"company_id": company_id, "entity_id": entity_id})
    if parent is None or not parent.state.get("is_available", True):
        raise HTTPException(status_code=404, detail="Item not found or unavailable")

    # Validate
    if payload.child_quantity <= 0:
        raise HTTPException(status_code=422, detail="child_quantity must be > 0")
    if not payload.child_category.strip():
        raise HTTPException(status_code=422, detail="child_category cannot be empty")

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
    now_iso = datetime.now(timezone.utc).isoformat()

    child_data: dict = {
        "sku": payload.child_sku,
        "name": parent.state.get("name", payload.child_sku),
        "quantity": payload.child_quantity,
        "sell_by": payload.child_sell_by,
        "category": payload.child_category,
        "allow_splitting": True,
        "created_at": now_iso,
        "updated_at": now_iso,
        "attributes": {**parent_attrs},
    }
    if payload.child_weight is not None:
        child_data["weight"] = payload.child_weight
    child_weight_unit = payload.child_weight_unit or parent.state.get("weight_unit")
    if child_weight_unit:
        child_data["weight_unit"] = child_weight_unit
    if payload.child_pieces is not None:
        child_data["attributes"] = {**child_data["attributes"], "pieces": payload.child_pieces}
    if parent_location_id:
        child_data["location_id"] = parent_location_id
    child_data["cost_total"] = payload.child_cost_total
    child_data["barcode"] = str(await _next_seq(session, company_id)).zfill(6)

    # 1. Create child
    await emit_event(
        session,
        company_id=company_id,
        entity_id=child_eid,
        entity_type="item",
        event_type="item.created",
        data=child_data,
        actor_id=user.id,
        location_id=uuid.UUID(parent_location_id) if parent_location_id else None,
        source="api",
        idempotency_key=str(uuid.uuid4()),
        metadata_={"parent_id": entity_id},
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
            metadata_={},
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
    weights = [float(p.state["weight"]) for p in source_projections if p.state.get("weight") is not None]
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
    now_iso = datetime.now(timezone.utc).isoformat()
    create_data: dict = {
        "sku": str(target_state.get("sku") or ""),
        "name": resulting_name,
        "quantity": resulting_qty,
        "sell_by": str(target_state.get("sell_by") or "piece"),
        "status": "available",
        "allow_splitting": bool(target_state.get("allow_splitting", True)),
        "attributes": resolved_attrs,
        "created_at": now_iso,
        "updated_at": now_iso,
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
            metadata_={},
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
        # created_at/updated_at: strip user values and backfill with current UTC time.
        # (batch_import bypasses post_item so we set them here rather than relying on setdefault.)
        _now_iso = datetime.now(timezone.utc).isoformat()
        rec.data.pop("created_at", None)
        rec.data.pop("updated_at", None)
        rec.data["created_at"] = _now_iso
        rec.data["updated_at"] = _now_iso

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
    items = [_flatten_item(r.state, r.entity_id) for r in rows]
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

