# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT

import uuid
from dataclasses import dataclass
from types import SimpleNamespace

from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.events.engine import emit_event
from celerp.inventory_codes import (
    BarcodeConflictError,
    RfidEpcConflictError,
    normalize_rfid_epc,
    validate_barcode,
    validate_rfid_epc,
)
from celerp.models.company import Company, Location
from celerp.models.projections import Projection
from celerp.importers.tabular import CsvImportSpec
from celerp.services.field_schema import AMOUNT_ITEM_KEYS
from celerp.services.money import to_stored_float, unit_price_from_total
from celerp.services.permissions import role_has_permission
from celerp.services.pricing import derived_price_keys, get_price_config, is_derived, price_key
from celerp.services.units import (
    build_unit_map,
    get_company_units,
    is_pieces_unit,
    is_weight_unit,
)

# Internally assigned SKUs/barcodes are short zero-padded sequences; imported
# EAN-13/GTIN-14 barcodes (13-14 digits) are excluded from the sequence scan so
# they are never re-used as the next internal code.
_MAX_SEQ_DIGITS = 9
_SEQ_WIDTH = 6


async def lock_item_code_namespace(session: AsyncSession, company_id) -> None:
    """Serialize SKU/barcode allocation for a company.

    Two concurrent creates each read the same max sequence and mint the same next
    code; the barcode unique index then rejects the loser with a 409. Taking a row
    lock on the company here makes the second allocator wait for the first to
    commit, so it reads the updated max and mints the next code instead of colliding.
    The lock is held until the caller's transaction commits or rolls back; every
    allocation and barcode check in that request must run after this call.

    The mode is FOR NO KEY UPDATE, not FOR UPDATE. Every ledger insert takes an
    implicit foreign-key KEY SHARE lock on its company row and holds it to commit,
    so a plain FOR UPDATE here would have to upgrade past that share lock: two
    transactions that have each already emitted an event for the company both hold
    KEY SHARE and then block on each other's row lock, which PostgreSQL breaks by
    aborting one with a deadlock (40P01). FOR NO KEY UPDATE does not conflict with
    KEY SHARE, so the upgrade never happens, while it still conflicts with another
    FOR NO KEY UPDATE, keeping barcode allocators serialized for every module.
    """
    await session.execute(
        select(Company.id).where(Company.id == company_id).with_for_update(key_share=True)
    )


async def _next_seq(session: AsyncSession, company_id) -> int:
    """Return the next integer in the shared SKU/barcode sequence for a company.

    Scans integer-valued SKUs and barcodes together so the two namespaces never
    collide (a barcode assigned during a split is never re-used as a SKU on the
    next create). Only barcodes with <= _MAX_SEQ_DIGITS digits count, excluding
    imported EAN-13/GTIN-14 barcodes while covering every internally assigned one.
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
    all_vals = list(sku_vals) + [v for v in barcode_vals if v and len(v) <= _MAX_SEQ_DIGITS]
    return max((int(v) for v in all_vals if v and str(v).isdigit()), default=0) + 1


async def allocate_internal_codes(session: AsyncSession, company_id, count: int = 1) -> list[str]:
    """Lock the company's code namespace and return ``count`` fresh codes, each free.

    The lock makes the scan-then-mint atomic against concurrent allocators. Codes are
    zero-padded to the standard internal width and are guaranteed distinct within the
    returned batch. Starting from the next sequential value, any candidate already held
    as a barcode OR an rfid_epc is skipped: the two fields share one physical-code
    namespace, and the sequence scan counts integer SKUs and barcodes only, so a numeric
    EPC can equal the next sequential code. Skipping here is the single guard every
    internal-mint path inherits, so no caller re-implements the availability check.
    """
    await lock_item_code_namespace(session, company_id)
    codes: list[str] = []
    candidate = await _next_seq(session, company_id)
    while len(codes) < count:
        code = str(candidate).zfill(_SEQ_WIDTH)
        if not await _code_in_use(session, company_id, code):
            codes.append(code)
        candidate += 1
    return codes


async def _code_in_use(
    session: AsyncSession, company_id, code, *, exclude_entity_id=None
) -> bool:
    """True when ``code`` already occupies EITHER physical-code slot of another item.

    A barcode and an RFID / EPC are both physical-code identifiers drawn from one
    namespace, so a value in use as a barcode is not free to reuse as an EPC and vice
    versa. This single query over BOTH ``state ->> 'barcode'`` and
    ``state ->> 'rfid_epc'`` is the sole cross-field collision check; both writers call
    it under ``lock_item_code_namespace`` so the read-then-write is serialized.
    ``exclude_entity_id`` skips one item's own row so re-asserting an item's current
    value is not read as a self-collision.
    """
    if not code:
        return False
    value = str(code)
    query = select(Projection.entity_id).where(
        Projection.company_id == company_id,
        Projection.entity_type == "item",
        or_(
            Projection.state["barcode"].as_string() == value,
            Projection.state["rfid_epc"].as_string() == value,
        ),
    )
    if exclude_entity_id is not None:
        query = query.where(Projection.entity_id != exclude_entity_id)
    return (await session.execute(query)).first() is not None


async def assert_barcode_available(
    session: AsyncSession, company_id, barcode, *, exclude_entity_id=None
) -> None:
    """Raise BarcodeConflictError if another item in the company already holds ``barcode``.

    An empty or absent barcode is always available. This is the application-side
    check that yields a clean 409; the DB unique index is the final backstop for
    writers that bypass it. ``exclude_entity_id`` skips one item's own row so a
    barcode change that re-asserts the item's current value is not read as a
    self-collision.
    """
    if not barcode:
        return
    if await _code_in_use(session, company_id, barcode, exclude_entity_id=exclude_entity_id):
        raise BarcodeConflictError(barcode)


async def assert_rfid_epc_available(
    session: AsyncSession, company_id, rfid_epc, *, exclude_entity_id=None
) -> None:
    """Raise RfidEpcConflictError if another item in the company already holds ``rfid_epc``.

    Mirrors ``assert_barcode_available``: an empty or absent value is always available,
    the value is normalized (trimmed + upper-cased) before the check so lookup matches
    storage, and the shared ``_code_in_use`` query catches a collision against either
    physical-code slot. The DB unique index is the final backstop.
    """
    normalized = normalize_rfid_epc(rfid_epc)
    if not normalized:
        return
    if await _code_in_use(session, company_id, normalized, exclude_entity_id=exclude_entity_id):
        raise RfidEpcConflictError(normalized)


async def create_item(session, company_id: str, data: dict, actor_id: str | None = None):
    entity_id = data.get("entity_id", f"item:{uuid.uuid4()}")
    return await emit_event(
        session,
        company_id=company_id,
        entity_id=entity_id,
        entity_type="item",
        event_type="item.created",
        data=data,
        actor_id=actor_id,
        location_id=data.get("location_id"),
        source="api",
        idempotency_key=data.get("idempotency_key", str(uuid.uuid4())),
        metadata_={},
    )


def _external_ids(platform: str, idem_key: str) -> dict:
    """Recover the platform's external ids from a connector item's idempotency key.

    Inbound upserts encode the platform id in the idempotency key, so outbound
    sync recovers it from there rather than storing duplicate columns:
      shopify:{product_id}:{variant_id} -> shopify_product_id, shopify_variant_id
      woocommerce:{product_id}          -> woocommerce_product_id
    (Shopify location-level inventory needs a location id that is not captured on
    import; those items are skipped by the connector's inventory push.)
    """
    parts = (idem_key or "").split(":")
    if platform == "shopify" and len(parts) >= 3:
        return {"shopify_product_id": parts[1], "shopify_variant_id": parts[2]}
    if platform == "woocommerce" and len(parts) >= 2:
        return {"woocommerce_product_id": parts[1]}
    return {}


async def _items_with_external_id(company_id: str, platform: str, require_sync_flag: bool = False) -> list[dict]:
    """All item projections linked to `platform`, as outbound-ready dicts.

    When ``require_sync_flag`` is set, only items the user has opted into outbound sync
    (is_sync_to_shopify=True) are returned - so the catalog is never mass-pushed back to
    the store; the merchant explicitly enables each item."""
    import uuid as _uuid
    from celerp.db import SessionLocal as AsyncSessionLocal
    from celerp.models.projections import Projection
    from sqlalchemy import select

    cid = _uuid.UUID(str(company_id))
    out: list[dict] = []
    async with AsyncSessionLocal() as session:
        query = select(Projection).where(
            Projection.company_id == cid,
            Projection.entity_type == "item",
            Projection.state["idempotency_key"].as_string().like(f"{platform}:%"),
        )
        if require_sync_flag:
            query = query.where(Projection.is_sync_to_shopify.is_(True))
        rows = (await session.execute(query)).scalars().all()
        for r in rows:
            st = r.state or {}
            out.append({
                "sku": st.get("sku"),
                "name": st.get("name"),
                "description": st.get("description"),
                "sale_price": st.get("sale_price"),
                "quantity": st.get("quantity", 0),
                "files": st.get("files") or [],
                **_external_ids(platform, st.get("idempotency_key", "")),
            })
    return out


async def list_items_with_external_id(company_id: str, platform: str) -> list[dict]:
    """Items linked to a platform (have an external id), for outbound inventory push.
    Shopify outbound is opt-in per item (is_sync_to_shopify); other platforms push all
    linked items (a per-platform flag is a follow-up)."""
    return await _items_with_external_id(company_id, platform, require_sync_flag=(platform == "shopify"))


async def list_items_modified_since_last_sync(company_id: str, platform: str) -> list[dict]:
    """Items linked to a platform, for outbound product push. Shopify pushes only items
    the user opted in (is_sync_to_shopify); other platforms push all linked items.
    Outbound PUTs are idempotent and failed items re-push on the next run, so a per-item
    modified watermark is a follow-up rather than launch work."""
    return await _items_with_external_id(company_id, platform, require_sync_flag=(platform == "shopify"))


async def upsert_from_connector(company_id: str, item) -> str:
    """
    Create or update an item from a connector payload. Returns the write outcome:
    "created", "updated", or "noop" (this exact content was already applied).

    `item` must have: sku, name, idempotency_key (stable per external item).
    Optional: sale_price, quantity, cost_price, description.

    Uses a fresh DB session so the connector does not need to manage
    session lifecycle. Idempotency is enforced at the ledger level.
    """
    from celerp.db import SessionLocal as AsyncSessionLocal
    from celerp.events.engine import connector_upsert

    idem_key = item.idempotency_key
    if not idem_key:
        raise ValueError("idempotency_key required for connector upserts")

    data = {
        "sku": item.sku,
        "name": item.name,
    }
    if item.sale_price is not None:
        data["sale_price"] = item.sale_price
        data["retail_price"] = item.sale_price   # canonical selling-price field
    if item.quantity:
        data["quantity"] = item.quantity
    if getattr(item, "cost_price", None) is not None:
        data["cost_price"] = item.cost_price     # else margin/COGS/valuation read zero cost
    if getattr(item, "description", None):
        data["description"] = item.description

    async with AsyncSessionLocal() as session:
        # Derived price lists are computed from the base at read time; a store-synced price
        # must not be stored under a derived key (it would be masked on every read, then
        # resurface as a stale manual price if the factor is ever removed).
        from celerp.services.pricing import derived_price_keys, get_price_config
        derived = derived_price_keys((await get_price_config(session, company_id))[0])
        for key in derived:
            data.pop(key, None)
        outcome = await connector_upsert(
            session, company_id=company_id, entity_type="item",
            event_type="item.created", idem_key=idem_key, data=data,
        )
        await session.commit()
        return outcome


# ---------------------------------------------------------------------------
# Semantic catalog import
# ---------------------------------------------------------------------------
#
# One committer, three transports: the browser CSV importer (POST /import/rows),
# the agent commit (POST /import/commit), and the raw event batch
# (POST /import/batch) all converge on commit_import_batch below. The business
# transformation (location resolution, unit canonicalization, quantity/rate
# derivation, dynamic attributes, idempotency) lives in build_import_records so
# it is applied identically no matter which transport delivered the rows.


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


# Columns with dedicated item fields; everything else on a row is a category
# attribute. Shared with the UI mapping form (imported from here) so the split
# between core fields and attributes has one source of truth.
_CORE_ITEM_COLS: frozenset[str] = frozenset({
    "sku", "name", "category", "quantity",
    "weight", "weight_ct", "weight_unit", "gross_weight", "gross_weight_unit",
    "sell_by", "pieces", "status",
    "barcode", "hs_code", "short_description", "description", "notes", "location_name",
    "location_id", "created_at", "updated_at",
})

# Max distinct values before an attribute column is treated as free-text instead
# of a select field when a schema is inferred from the import.
_DROPDOWN_THRESHOLD = 30


def _derive_import_qty(row: dict, sell_by: str, unit_map: dict[str, dict]) -> float:
    """Derive the stock quantity from an import row.

    Priority:
    1. An explicit ``quantity`` or ``qty`` column is trusted unconditionally.
    2. Otherwise fall back to the semantic field for the unit type:
       - pieces-type (e.g. ``piece``) -> ``pieces`` column
       - weight-type (e.g. ``carat``, ``gram``) -> ``weight`` or ``weight_ct``
       - other (service, volume, length, unknown) -> 0.0

    Returns a float; never raises.
    """
    def _to_float(val) -> float | None:
        s = str(val).strip() if val is not None else ""
        if not s:
            return None
        try:
            return float(s)
        except ValueError:
            return None

    explicit = _to_float(row.get("quantity")) if "quantity" in row else _to_float(row.get("qty"))
    if explicit is not None:
        return explicit
    if is_pieces_unit(sell_by, unit_map):
        return _to_float(row.get("pieces")) or 0.0
    if is_weight_unit(sell_by, unit_map):
        return _to_float(row.get("weight")) or _to_float(row.get("weight_ct")) or 0.0
    return 0.0


def _collect_category_attributes(rows: list[dict]) -> dict[str, dict[str, list[str]]]:
    """Return {category: {col: [distinct_values]}} for all attribute columns."""
    result: dict[str, dict[str, list[str]]] = {}
    for row in rows:
        cat = str(row.get("category", "") or "").strip() or "_uncategorized"
        if cat not in result:
            result[cat] = {}
        for k, v in row.items():
            if k in _CORE_ITEM_COLS or k.endswith("_price") or k.endswith("_price_total"):
                continue
            v_str = str(v).strip() if v is not None else ""
            if not v_str:
                continue
            if k not in result[cat]:
                result[cat][k] = []
            if v_str not in result[cat][k]:
                result[cat][k].append(v_str)
    return result


def _infer_category_schemas(cat_attr_values: dict[str, dict[str, list[str]]]) -> dict[str, list[dict]]:
    """Convert collected attribute values into schema field definitions."""
    schemas: dict[str, list[dict]] = {}
    for cat, cols in cat_attr_values.items():
        if cat == "_uncategorized":
            continue
        fields = []
        for key, distinct_vals in cols.items():
            if len(distinct_vals) <= _DROPDOWN_THRESHOLD:
                ftype = "select"
                options = sorted(distinct_vals)
            else:
                ftype = "text"
                options = []
            fields.append({
                "key": key,
                "label": key.replace("_", " ").title(),
                "type": ftype,
                "options": options,
            })
        if fields:
            schemas[cat] = fields
    return schemas


# Item import columns, single-sourced here for every transport (the browser
# mapping UI and the agent preview/commit routes). Price columns are dynamic:
# one unit-price column per importable price list, plus a virtual "_total"
# column that back-calculates the unit price at confirm time.
ITEM_IMPORT_BASE_COLS = ["sku", "name", "sell_by", "category", "quantity"]
ITEM_IMPORT_TAIL_COLS = [
    "weight", "weight_unit", "gross_weight", "gross_weight_unit", "pieces",
    "barcode", "hs_code", "purchase_sku", "purchase_name", "purchase_unit",
    "purchase_conversion_factor", "short_description", "description", "notes",
    "location_name",
]


def importable_price_lists(price_lists: list[dict]) -> list[dict]:
    """Price lists whose values can be imported. Derived lists are computed from
    the base price list at read time, so the mapper never offers their columns."""
    return [pl for pl in price_lists if pl.get("name") and not is_derived(pl)]


def build_item_import_spec(price_lists: list[dict]) -> CsvImportSpec:
    """Build the item import spec with dynamic price columns from the company's
    price lists. Shared by the browser mapper and the agent preview/commit."""
    price_cols = [price_key(pl["name"]) for pl in importable_price_lists(price_lists)]
    price_total_cols = [f"{col}_total" for col in price_cols]
    type_map: dict = {"quantity": float, "weight": float, "pieces": float}
    for col in price_cols + price_total_cols:
        type_map[col] = float
    return CsvImportSpec(
        cols=ITEM_IMPORT_BASE_COLS + price_cols + price_total_cols + ITEM_IMPORT_TAIL_COLS,
        required={"name", "sell_by"},
        type_map=type_map,
    )


@dataclass
class ImportBuild:
    records: list[dict]              # ImportRecord-shaped dicts ready for the committer
    errors: list[dict]              # {"row", "field", "message"}
    locations_to_create: list[str]


async def build_import_records(
    session: AsyncSession,
    company_id,
    rows: list[dict],
    *,
    upsert: bool,
    dry_run: bool,
) -> ImportBuild:
    """Transform mapped business rows into CIF import records, server-side.

    Owns location resolution and creation, category default sell-by, unit
    canonicalization, quantity derivation, dynamic attributes, and monetary
    (price-total to unit-price) conversion. Everything is read in-session, so no
    HTTP round-trips are made regardless of transport.

    ``dry_run=True`` never creates locations; it lists the names that would be
    created in ``ImportBuild.locations_to_create`` and leaves rows that reference
    them without a resolved location. ``dry_run=False`` creates the missing
    locations first so every row resolves.
    """
    loc_rows = (await session.execute(
        select(Location).where(Location.company_id == company_id)
    )).scalars().all()
    location_map: dict[str, str] = {loc.name: str(loc.id) for loc in loc_rows}

    # Default location for rows with no location_name: the sole location if there
    # is exactly one, otherwise the one flagged is_default (None if neither holds).
    default_location_id: str | None = None
    if len(loc_rows) == 1:
        default_location_id = str(loc_rows[0].id)
    else:
        for loc in loc_rows:
            if loc.is_default:
                default_location_id = str(loc.id)
                break

    # Location names referenced by rows that do not yet exist.
    loc_names_needed: list[str] = []
    for row in rows:
        nm = str(row.get("location_name", "") or "").strip()
        if nm and nm not in location_map and nm not in loc_names_needed:
            loc_names_needed.append(nm)

    locations_to_create: list[str] = []
    if dry_run:
        locations_to_create = list(loc_names_needed)
    else:
        for nm in loc_names_needed:
            loc = Location(id=uuid.uuid4(), company_id=company_id, name=nm, type="warehouse")
            session.add(loc)
            await session.flush()
            location_map[nm] = str(loc.id)

    company = await session.get(Company, company_id)
    currency = ((company.settings or {}).get("currency") if company else None) or "USD"

    # Category default sell-by from the vertical library (same source post_item
    # uses for category defaults). Optional: absent module leaves the map empty.
    cat_sell_by: dict[str, str] = {}
    try:
        from celerp_verticals.routes import _all_categories  # type: ignore
        for cat in _all_categories().values():
            if cat.get("default_sell_by"):
                cat_sell_by[cat["name"]] = cat["default_sell_by"]
    except ImportError:
        pass

    units = await get_company_units(session, company_id)
    unit_canonical = {u["name"].lower(): u["name"] for u in units}
    unit_map = build_unit_map(units)

    records: list[dict] = []
    errors: list[dict] = []
    for i, row in enumerate(rows):
        sku = str(row.get("sku", "") or "").strip()
        name = str(row.get("name", "") or "").strip()
        loc_name = str(row.get("location_name", "") or "").strip()

        location_id = location_map.get(loc_name) if loc_name else default_location_id
        if not location_id:
            errors.append({
                "row": i + 1,
                "field": "location_name",
                "message": "No location resolved: add a location_name column or set a default location",
            })
            continue

        sell_by = (
            unit_canonical.get(str(row.get("sell_by", "") or "").strip().lower())
            or str(row.get("sell_by", "") or "").strip()
            or cat_sell_by.get(str(row.get("category", "") or "").strip())
            or ""
        )
        qty = _derive_import_qty(row, sell_by, unit_map)

        def _flt(key: str, _row: dict = row) -> float | None:
            raw = str(_row.get(key, "") or "").strip()
            if not raw:
                return None
            try:
                return float(raw)
            except ValueError:
                return None

        # Every column not in the core field set is a category attribute.
        attrs: dict = {}
        for k, v in row.items():
            if k not in _CORE_ITEM_COLS and not k.endswith("_price") and not k.endswith("_price_total") and v is not None:
                v_str = str(v).strip()
                if v_str:
                    attrs[k] = v_str

        data = {
            "sku": sku,
            "name": name,
            "quantity": qty,
            "category": str(row.get("category", "") or "").strip() or None,
            "weight": _flt("weight") or _flt("weight_ct"),
            "weight_unit": unit_canonical.get(str(row.get("weight_unit", "") or "").strip().lower()) or str(row.get("weight_unit", "") or "").strip() or None,
            "gross_weight": _flt("gross_weight"),
            "gross_weight_unit": unit_canonical.get(str(row.get("gross_weight_unit", "") or "").strip().lower()) or str(row.get("gross_weight_unit", "") or "").strip() or None,
            "pieces": _flt("pieces"),
            "sell_by": sell_by or None,
            "barcode": str(row.get("barcode", "") or "").strip() or None,
            "hs_code": str(row.get("hs_code", "") or "").strip() or None,
            "short_description": str(row.get("short_description", "") or "").strip() or None,
            "description": str(row.get("description", "") or "").strip() or None,
            "notes": str(row.get("notes", "") or "").strip() or None,
            "location_id": location_id,
            "attributes": attrs,
        }
        # status, created_at, updated_at are intentionally omitted: status is set
        # to available by the backend on creation; the timestamps are system-generated.
        #
        # _price_total columns back-calculate a unit price = total / qty at the
        # fewest decimals that reconcile the entered total to the cent (Option B),
        # the same helper as the interactive "set from total" edit. Only used when
        # the corresponding _price column is not also present.
        for col_key in row:
            if col_key.endswith("_price_total"):
                unit_key = col_key[: -len("_total")]  # e.g. cost_price_total -> cost_price
                total_val = _flt(col_key)
                if total_val is None:
                    continue
                if _flt(unit_key) is not None:
                    # Unit price already mapped; the total is redundant.
                    continue
                if unit_key == "cost_price":
                    # cost_total is the primitive; store directly (no back-calculation).
                    data["cost_total"] = total_val
                    continue
                # qty=0 or missing: treat as 1 (total = unit price for a single item).
                data[unit_key] = to_stored_float(unit_price_from_total(total_val, qty or 1, currency))
            elif col_key.endswith("_price") and _flt(col_key) is not None:
                data[col_key] = _flt(col_key)

        barcode = data["barcode"]
        idem = f"csv:item:bc:{barcode}".lower() if barcode else f"csv:item:{sku}".lower()
        data["idempotency_key"] = idem

        records.append({
            "entity_id": f"item:{uuid.uuid4()}",
            "event_type": "item.created",
            "data": data,
            "source": "csv_import",
            "idempotency_key": idem,
        })

    return ImportBuild(records=records, errors=errors, locations_to_create=locations_to_create)


async def import_items(
    session: AsyncSession,
    company_id,
    actor_id,
    role: str,
    settings: dict,
    rows: list[dict],
    *,
    upsert: bool,
    filename: str | None,
    idempotency_key: str | None,
) -> BatchImportResult:
    """Import mapped business rows through the canonical committer.

    Builds records server-side (creating any missing locations), commits them in
    chunks of 500, then auto-merges any newly discovered attribute columns into
    the company's category schemas (best-effort, gated on manage_company_settings).
    Shared by the browser importer and the agent commit path.

    Exactly-once is delivered by the per-row keys build_import_records derives from
    each row's SKU/barcode (the same keys connector reconciliation reads). When the
    caller supplies idempotency_key, it namespaces those keys so re-submitting the
    identical batch under the same key is a no-op while a distinct key is a distinct
    import; the browser importer passes None and keeps the content keys verbatim.
    """
    build = await build_import_records(session, company_id, rows, upsert=upsert, dry_run=False)

    if idempotency_key:
        for rec in build.records:
            rec["idempotency_key"] = f"{idempotency_key}:{rec['idempotency_key']}"
            rec["data"]["idempotency_key"] = rec["idempotency_key"]

    user = SimpleNamespace(id=actor_id)

    created = skipped = updated = 0
    errors: list[str] = [f"Row {e['row']}: {e['message']}" for e in build.errors]
    batch_id: str | None = None

    _CHUNK = 500
    all_records = build.records
    for i in range(0, max(len(all_records), 1), _CHUNK):
        chunk = all_records[i : i + _CHUNK]
        if not chunk:
            break
        body = BatchImportRequest(
            records=[ImportRecord(**r) for r in chunk],
            filename=filename,
            upsert=upsert,
        )
        result = await commit_import_batch(session, company_id, user, role, settings, body)
        created += result.created
        skipped += result.skipped
        updated += result.updated
        errors.extend(result.errors)
        if result.batch_id:
            batch_id = result.batch_id

    # Auto-merge discovered attribute keys into category schemas. Best-effort:
    # mutating category schemas is a settings change, so the caller's role must
    # carry manage_company_settings; without it the merge is skipped and the
    # import still succeeds.
    if build.records and role_has_permission(settings, role, "manage_company_settings"):
        inferred = _infer_category_schemas(_collect_category_attributes(rows))
        if inferred:
            await _merge_category_schemas(session, company_id, inferred)
            await session.commit()

    return BatchImportResult(
        created=created, skipped=skipped, updated=updated, errors=errors, batch_id=batch_id
    )


async def _merge_category_schemas(session: AsyncSession, company_id, incoming: dict[str, list[dict]]) -> None:
    """Append newly discovered attribute keys to the company's category schemas.

    Never overwrites an existing key (user customizations are preserved). This is
    the sole path that grows category schemas from imported attribute columns; it
    stages the change on the company row and leaves the commit to import_items.
    """
    company = await session.get(Company, company_id)
    if company is None:
        return
    settings = dict(company.settings)
    cat_schemas: dict[str, list[dict]] = dict(settings.get("category_schemas") or {})
    added = False
    for cat, new_fields in incoming.items():
        existing = cat_schemas.get(cat) or []
        existing_keys = {f["key"] for f in existing}
        max_pos = max((f.get("position", 0) for f in existing), default=-1)
        appended = []
        for nf in new_fields:
            if nf["key"] not in existing_keys:
                max_pos += 1
                appended.append({**nf, "position": max_pos, "editable": True, "required": False, "visible_to_roles": [], "show_in_table": True})
                existing_keys.add(nf["key"])
        if appended:
            cat_schemas[cat] = existing + appended
            added = True
    if added:
        settings["category_schemas"] = cat_schemas
        company.settings = settings


async def commit_import_batch(
    session: AsyncSession,
    company_id,
    user,
    role: str,
    settings: dict,
    body: BatchImportRequest,
) -> BatchImportResult:
    """Commit CIF item records. Idempotent on idempotency_key. Max 500 per call.

    The single committer behind /import/batch, /import/rows, and /import/commit.
    """
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
    _units = await get_company_units(session, company_id)
    _valid_units: frozenset[str] = frozenset(u["name"] for u in _units)
    _derived_keys = derived_price_keys((await get_price_config(session, company_id))[0])

    created = skipped = updated = 0
    errors: list[str] = []
    created_entity_ids: list[str] = []
    created_keys: list[str] = []

    for rec in body.records:
        # Strip system-managed and document-lifecycle fields - never user-settable via import.
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
        # gives strings like "Yes"/"No"). Imports that omit it leave it unset, which
        # reads as splittable via splitting_allowed; only an explicit False blocks.
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

        # Barcode and RFID EPC share one physical-code namespace: a value already held in
        # EITHER slot by another item cannot be imported into either slot of this one.
        # Interactive create/patch enforce this via assert_barcode_available /
        # assert_rfid_epc_available; the import writer emits rec.data verbatim, so without
        # this guard a row could set rfid_epc to a value another item holds as its barcode
        # (a cross-field collision no single-field unique index catches). Run the same
        # check under the company code lock so the read-then-write is serialized and
        # earlier rows in this batch are seen (emit_event flushes projections in-session).
        # exclude_entity_id is harmless on create and correct on upsert (re-asserting the
        # item's own value is not a self-collision). A colliding row is skipped, never a 500.
        _row_barcode = rec.data.get("barcode")
        _row_epc = rec.data.get("rfid_epc")
        if _row_barcode or _row_epc:
            _code_err = None
            try:
                validate_barcode(_row_barcode)
                validate_rfid_epc(_row_epc)
                await lock_item_code_namespace(session, company_id)
                await assert_barcode_available(session, company_id, _row_barcode, exclude_entity_id=rec.entity_id)
                await assert_rfid_epc_available(session, company_id, _row_epc, exclude_entity_id=rec.entity_id)
            except (ValueError, BarcodeConflictError, RfidEpcConflictError) as exc:
                _code_err = str(exc)
            if _code_err is not None:
                errors.append(f"Row (SKU={rec.data.get('sku', '?')}): {_code_err}")
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
