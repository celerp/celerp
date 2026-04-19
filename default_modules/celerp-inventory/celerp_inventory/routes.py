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

router = APIRouter(dependencies=[Depends(get_current_user)])


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

_DEFAULT_UNITS: list[dict] = [
    {"name": "piece", "label": "Piece", "decimals": 0},
    {"name": "carat", "label": "Carat (ct)", "decimals": 2},
    {"name": "gram", "label": "Gram (g)", "decimals": 2},
    {"name": "kg", "label": "Kilogram (kg)", "decimals": 3},
    {"name": "oz", "label": "Ounce (oz)", "decimals": 2},
    {"name": "lb", "label": "Pound (lb)", "decimals": 2},
    {"name": "liter", "label": "Liter (L)", "decimals": 2},
    {"name": "meter", "label": "Meter (m)", "decimals": 2},
]


async def _get_company_units(session: AsyncSession, company_id) -> list[dict]:
    """Return the company's units config (falls back to default seed)."""
    from celerp.models.company import Company
    company = await session.get(Company, company_id)
    if company:
        units = (company.settings or {}).get("units")
        if units:
            return units
    return _DEFAULT_UNITS


def validate_quantity(qty: float, decimals: int) -> None:
    """Raise HTTPException(422) if qty has more decimal places than allowed.

    Uses round-trip via Decimal to avoid float arithmetic artifacts (e.g. 2.55*100=254.999...).
    """
    from decimal import Decimal, ROUND_HALF_UP
    d = Decimal(str(qty))
    quantizer = Decimal(10) ** -decimals
    rounded = d.quantize(quantizer, rounding=ROUND_HALF_UP)
    if d != rounded:
        raise HTTPException(
            status_code=422,
            detail=f"Quantity {qty} exceeds allowed precision ({decimals} decimal places)",
        )


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

    sku: str
    name: str
    sell_by: str                           # required - must be a valid unit name from company settings
    quantity: float = 0
    category: str | None = None
    location_id: uuid.UUID | None = None
    cost_price: float | None = None
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
    attributes: dict = Field(default_factory=dict)
    idempotency_key: str | None = None


class ItemPatch(BaseModel):
    fields_changed: dict[str, dict] = Field(default_factory=dict)
    idempotency_key: str | None = None


class TransferBody(BaseModel):
    to_location_id: uuid.UUID
    idempotency_key: str | None = None


class SplitChild(BaseModel):
    sku: str
    quantity: float
    attributes: dict = Field(default_factory=dict)


class SplitBody(BaseModel):
    children: list[SplitChild]
    idempotency_key: str | None = None


class MergeBody(BaseModel):
    source_entity_ids: list[str]
    target_sku_from: str                       # entity_id of the source whose SKU/barcode to use
    resulting_quantity: float | None = None    # optional override (default = sum)
    resulting_cost_price: float | None = None  # optional override (default = weighted avg)
    resulting_name: str | None = None          # optional override (default = target's name)
    resolved_attributes: dict | None = None    # user picks for conflicting string attributes
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
_ARCHIVED_GROUP = frozenset({"archived", "merged", "expired", "disposed"})


@router.get("")
async def list_items(
    company_id=Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
    role: str = Depends(get_current_role),
    limit: int = 50,
    offset: int = 0,
    q: str | None = None,
    sku: str | None = None,
    barcode: str | None = None,
    status: str | None = None,
    category: str | None = None,
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
    # "archived" expands to include merged/expired/disposed.
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

    if sku:
        result = [r for r in result if str(r.get("sku", "")) == sku]

    if barcode:
        result = [r for r in result if str(r.get("barcode", "")) == barcode]

    if q:
        q_lower = q.lower()
        # Core fields to search explicitly
        _SEARCH_FIELDS = ("name", "sku", "barcode", "description", "category")
        # Keys that are never useful to search (IDs, numbers, booleans)
        _SKIP_KEYS = frozenset({"id", "entity_id", "company_id", "location_id", "quantity",
                                 "weight", "status", "created_at", "updated_at"})
        def _item_matches(r: dict) -> bool:
            for field in _SEARCH_FIELDS:
                if q_lower in str(r.get(field, "")).lower():
                    return True
            # Search nested attributes dict (for items not yet fully flattened)
            for v in (r.get("attributes") or {}).values():
                if q_lower in str(v).lower():
                    return True
            # Search all string values in the flattened item (covers flattened attributes)
            for k, v in r.items():
                if k in _SKIP_KEYS or k in _SEARCH_FIELDS or k.endswith("_price"):
                    continue
                if isinstance(v, str) and q_lower in v.lower():
                    return True
            return False
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

        # Exclude consignment_in items: they are borrowed, not owned — exclude from all valuation
        if row.consignment_flag == "in" or state.get("consignment_flag") == "in":
            continue

        # category_counts: always global over non-hidden items
        if row_status not in _HIDDEN_STATUSES:
            if row_cat:
                category_counts[row_cat] = category_counts.get(row_cat, 0) + 1

        # Apply category filter for scoped metrics
        if category and row_cat != category:
            continue

        # count_by_status: scoped to category filter, counts all non-hidden statuses
        if row_status not in _HIDDEN_STATUSES:
            count_by_status[row_status] = count_by_status.get(row_status, 0) + 1

        # Totals: scoped to category + status filters (mirrors list_items logic)
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

        active_item_count += 1
        qty = float(state.get("quantity") or 0)
        for pl in _price_lists:
            pl_name = pl.get("name", "")
            key = f"{pl_name.lower()}_price"
            try:
                if pl_name.lower() in ("cost", "cost price", "landed"):
                    # Cost uses pre-computed total_cost (qty * unit_cost), else fallback
                    tc = state.get("total_cost")
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


@router.post("")
async def post_item(payload: ItemCreate, company_id=Depends(get_current_company_id), user=Depends(get_current_user), role: str = Depends(get_current_role), session: AsyncSession = Depends(get_session)) -> dict:
    # Guard: operator/viewer cannot set cost_price on creation (manager+ required)
    if payload.cost_price is not None and ROLE_LEVELS.get(role, 0) < ROLE_LEVELS["manager"]:
        raise HTTPException(status_code=403, detail=f"Role '{role}' cannot set cost_price")

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

    # Ensure status is set (not part of ItemCreate model but required for projections)
    data.setdefault("status", "available")

    # Ensure timestamps are always set on creation
    now_iso = datetime.now(timezone.utc).isoformat()
    data.setdefault("created_at", now_iso)
    data.setdefault("updated_at", now_iso)

    # Strip price fields from create event data - they go via pricing events.
    # Any key ending in _price is treated as a pricing field.
    price_fields = {k: data.pop(k) for k in list(data) if k.endswith("_price") and data[k] is not None}

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


@router.post("/{entity_id}/split")
async def split_item(entity_id: str, payload: SplitBody, company_id=Depends(get_current_company_id), user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    # Fetch parent
    parent = await session.get(Projection, {"company_id": company_id, "entity_id": entity_id})
    if parent is None or not parent.state.get("is_available", True):
        raise HTTPException(status_code=404, detail="Item not found or unavailable")

    parent_qty = float(parent.state.get("quantity") or 0)
    parent_sell_by = parent.state.get("sell_by") or "piece"
    parent_category = parent.state.get("category")
    parent_location_id = parent.state.get("location_id")
    parent_attrs = dict(parent.state.get("attributes") or {})

    # Fields to preserve on children (everything except identity/qty)
    parent_prices = {k: parent.state[k] for k in parent.state if k.endswith("_price") and parent.state[k] is not None}
    parent_description = parent.state.get("description")
    parent_status = parent.state.get("status")
    parent_tax_codes = parent.state.get("tax_codes")
    parent_expires_at = parent.state.get("expires_at")

    units = await _get_company_units(session, company_id)
    unit_map = {u["name"]: u for u in units}
    unit_cfg = unit_map.get(parent_sell_by)
    decimals = unit_cfg["decimals"] if unit_cfg else 0

    children = payload.children
    if len(children) < 2:
        raise HTTPException(status_code=422, detail="Split requires at least 2 children")

    # Validate each child quantity
    for child in children:
        validate_quantity(child.quantity, decimals)

    # Validate total <= parent qty
    total_child_qty = sum(c.quantity for c in children)
    if round(total_child_qty, 10) >= round(parent_qty, 10):
        raise HTTPException(
            status_code=422,
            detail=f"Child quantities ({total_child_qty}) exceed or equal parent quantity ({parent_qty})",
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
            "attributes": merged_attrs,
            "created_at": now_iso,
            "updated_at": now_iso,
        }
        if parent_category:
            child_data["category"] = parent_category
        if parent_location_id:
            child_data["location_id"] = parent_location_id
        if parent_description:
            child_data["description"] = parent_description
        if parent_status:
            child_data["status"] = parent_status
        if parent_tax_codes:
            child_data["tax_codes"] = parent_tax_codes
        if parent_expires_at:
            child_data["expires_at"] = parent_expires_at
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

        # Preserve prices from parent via pricing events
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

    # If parent quantity is now 0, mark as sold
    if new_parent_qty == 0:
        await emit_event(
            session,
            company_id=company_id,
            entity_id=entity_id,
            entity_type="item",
            event_type="item.status.set",
            data={"new_status": "sold"},
            actor_id=user.id,
            location_id=None,
            source="api",
            idempotency_key=str(uuid.uuid4()),
            metadata_={},
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
    total_cost_qty = sum(
        float(p.state.get("quantity") or 0)
        for p in source_projections
        if p.state.get("cost_price") is not None
    )
    if total_cost_qty > 0:
        weighted_cost = sum(
            float(p.state.get("cost_price") or 0) * float(p.state.get("quantity") or 0)
            for p in source_projections
        ) / total_cost_qty
    else:
        weighted_cost = float(target_proj.state.get("cost_price") or 0) or None

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
        values = [str((p.state.get("attributes") or {}).get(key, "")) for p in source_projections if key in (p.state.get("attributes") or {})]
        unique_vals = set(values)
        if len(unique_vals) == 1:
            # No conflict — carry forward.
            resolved_attrs[key] = values[0]
        elif all(_is_numeric(v) for v in unique_vals):
            # Numeric conflict — sum.
            resolved_attrs[key] = str(sum(float(v) for v in values))
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
    resulting_cost = payload.resulting_cost_price if payload.resulting_cost_price is not None else weighted_cost
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
        "attributes": resolved_attrs,
        "created_at": now_iso,
        "updated_at": now_iso,
    }
    for field in ("category", "location_id", "barcode", "description", "unit", "tax_codes"):
        val = target_state.get(field)
        if val is not None:
            create_data[field] = str(val) if field == "location_id" else val

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

    # Emit pricing events for all price fields from target (or computed cost).
    price_fields: dict = {}
    if resulting_cost is not None:
        price_fields["cost_price"] = resulting_cost
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


class BulkStatusBody(BaseModel):
    entity_ids: list[str]
    status: str


class BulkTransferBody(BaseModel):
    entity_ids: list[str]
    to_location_id: uuid.UUID


class BulkDeleteBody(BaseModel):
    entity_ids: list[str]


@router.post("/bulk/status")
async def bulk_set_status(payload: BulkStatusBody, company_id=Depends(get_current_company_id), user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
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
            event_type="item.location.transferred",
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
    # the item should vanish from the catalog entirely, not just be marked disposed.
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


class BulkDisposeBody(BaseModel):
    entity_ids: list[str]
    reason: str | None = None


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


@router.post("/bulk/dispose")
async def bulk_dispose(payload: BulkDisposeBody, company_id=Depends(get_current_company_id), _: None = Depends(require_manager), user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    if not payload.entity_ids:
        raise HTTPException(status_code=422, detail="entity_ids must not be empty")
    import sqlalchemy as _sa
    from celerp.models.projections import Projection as _Proj
    from celerp.models.ledger import LedgerEntry as _LE
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
    return {"disposed": len(payload.entity_ids)}


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


@router.post("/{entity_id}/dispose")
async def dispose_item(entity_id: str, company_id=Depends(get_current_company_id), _: None = Depends(require_manager), user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    import sqlalchemy as _sa
    from celerp.models.projections import Projection as _Proj
    from celerp.models.ledger import LedgerEntry as _LE
    await session.execute(_sa.delete(_Proj).where(_Proj.company_id == company_id, _Proj.entity_id == entity_id))
    await session.execute(_sa.delete(_LE).where(_LE.company_id == company_id, _LE.entity_id == entity_id))
    await session.commit()
    return {"deleted": entity_id}


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
    records: list[ImportRecord]
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

    keys = [r.idempotency_key for r in body.records]
    existing = set(
        (await session.execute(
            select(LedgerEntry.idempotency_key).where(LedgerEntry.idempotency_key.in_(keys))
        )).scalars().all()
    )

    created = skipped = updated = 0
    errors: list[str] = []
    created_entity_ids: list[str] = []
    created_keys: list[str] = []

    for rec in body.records:
        if rec.idempotency_key in existing:
            if body.upsert:
                # Emit patch event with a upsert-specific idempotency key
                upsert_idem = f"{rec.idempotency_key}:upsert"
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
                idempotency_key=rec.idempotency_key,
                metadata_={"source_ts": rec.source_ts} if rec.source_ts else {},
            )
            existing.add(rec.idempotency_key)
            created_entity_ids.append(rec.entity_id)
            created_keys.append(rec.idempotency_key)
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

    _COLS = ["id", "sku", "name", "category", "quantity", "status"] + price_cols + ["weight", "weight_unit", "barcode", "hs_code", "purchase_sku", "purchase_name", "purchase_unit", "purchase_conversion_factor", "created_at", "updated_at"]

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
    app.include_router(router, prefix="/items", tags=["items"])
    # app.include_router(scanning_router, prefix="/scanning", tags=["scanning"])
    app.include_router(attachments_router, prefix="/items", tags=["attachments"])

