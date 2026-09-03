# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Kernel service: effective item field schema resolution.

Extracted from routers/companies.py so celerp-inventory module can import
it without a cross-module router dependency.
"""
from __future__ import annotations

import uuid as _uuid

from sqlalchemy.ext.asyncio import AsyncSession

from celerp.models.company import Company
from celerp.services.pricing import is_cost_list_name, is_derived, price_key

# Reserved system value for a field whose sources disagreed and cannot be reconciled (e.g. a merge
# of items holding different values for a dropdown or custom attribute). There is ONE canonical form,
# "Mixed", stored and displayed verbatim — no separate lowercase sentinel vs capitalized label.
MIXED_VALUE = "Mixed"

# Item amount fields hand-edited on the item surface, gated by the
# edit_inventory_amounts permission. One source of truth, mirroring the
# COST_ITEM_KEYS precedent in celerp.services.cost_visibility.
AMOUNT_ITEM_KEYS: frozenset[str] = frozenset({"quantity", "weight", "pieces", "gross_weight"})

# Fields whose edit is gated by edit_inventory_amounts. Superset of the numeric
# amount keys with sell_by added: changing the sell unit rewrites quantity, so it
# carries the same authority as editing an amount directly. Kept distinct from
# AMOUNT_ITEM_KEYS, which also drives the non-negative numeric validator on create
# and import - sell_by is a unit string and must never reach that float() path.
AMOUNT_EDIT_GATED_KEYS: frozenset[str] = AMOUNT_ITEM_KEYS | {"sell_by"}

# Schema-field keys for the canonical cost price list, stripped from /me/item-schema
# for callers without view_inventory_costs. Distinct from cost_visibility.COST_ITEM_KEYS,
# which names the item-DATA keys (cost_total, not the virtual schema key cost_price_total).
COST_SCHEMA_KEYS: frozenset[str] = frozenset({"cost_price", "cost_price_total"})

# Default price lists (used when company has none configured)
_DEFAULT_PRICE_LISTS: list[dict] = [
    {"name": "Wholesale"},
    {"name": "Retail"},
]

# Base schema fields (without price columns - those are injected dynamically)
_BASE_FIELDS: list[dict] = [
    {"key": "sku",               "label": "SKU",               "type": "text",   "editable": True,  "required": True,  "options": [],                                            "visible_to_roles": [],               "position": 0,  "show_in_table": True, "label_key": "field.label.sku"},
    {"key": "name",              "label": "Name",              "type": "text",   "editable": True,  "required": True,  "options": [],                                            "visible_to_roles": [],               "position": 1,  "show_in_table": True, "label_key": "field.label.name"},
    {"key": "category",          "label": "Category",          "type": "text",   "editable": True,  "required": False, "options": [],                                            "visible_to_roles": [],               "position": 2,  "show_in_table": True, "label_key": "field.label.category"},
    {"key": "quantity",          "label": "Qty",               "type": "number", "editable": True,  "required": False, "options": [],                                            "visible_to_roles": [],               "position": 3,   "show_in_table": True,  "tooltip_key": "field.tooltip.quantity", "label_key": "field.label.quantity"},
    {"key": "sell_by",           "label": "Sell Unit",         "type": "text",   "editable": True,  "required": False, "options": [],                                            "visible_to_roles": [],               "position": 3.05,"show_in_table": False, "tooltip_key": "field.tooltip.sell_by", "label_key": "field.label.sell_by"},
    {"key": "weight",            "label": "Net Weight",        "type": "number", "editable": True,  "required": False, "options": [],                                            "visible_to_roles": [],               "position": 3.02, "show_in_table": True,  "tooltip_key": "field.tooltip.weight", "label_key": "field.label.weight"},
    {"key": "weight_unit",       "label": "Net Weight Unit",   "type": "text",   "editable": True,  "required": False, "options": [],                                            "visible_to_roles": [],               "position": 3.15,"show_in_table": False, "tooltip_key": "field.tooltip.weight_unit", "label_key": "field.label.weight_unit"},
    {"key": "pieces",            "label": "Pieces",            "type": "number", "editable": True,  "required": False, "options": [],                                            "visible_to_roles": [],               "position": 3.06,"show_in_table": True,  "tooltip_key": "field.tooltip.pieces", "label_key": "field.label.pieces"},
    {"key": "reorder_point",     "label": "Reorder Point",     "type": "number", "editable": True,  "required": False, "options": [],                                            "visible_to_roles": [],               "position": 3.7, "show_in_table": False, "tooltip_key": "field.tooltip.reorder_point", "label_key": "field.label.reorder_point"},
    {"key": "reorder_qty",       "label": "Reorder Qty",       "type": "number", "editable": True,  "required": False, "options": [],                                            "visible_to_roles": [],               "position": 3.72,"show_in_table": False, "tooltip_key": "field.tooltip.reorder_qty", "label_key": "field.label.reorder_qty"},
    {"key": "gross_weight",      "label": "Gross Weight",      "type": "number", "editable": True,  "required": False, "options": [],                                            "visible_to_roles": [],               "position": 3.25,"show_in_table": False, "tooltip_key": "field.tooltip.gross_weight", "label_key": "field.label.gross_weight"},
    {"key": "gross_weight_unit", "label": "Gross Weight Unit", "type": "text",   "editable": True,  "required": False, "options": [],                                            "visible_to_roles": [],               "position": 3.3, "show_in_table": False, "tooltip_key": "field.tooltip.gross_weight_unit", "label_key": "field.label.gross_weight_unit"},
    {"key": "inventory_type",    "label": "Inventory Type",    "type": "select", "editable": True,  "required": False, "options": ["stocked", "component", "non_stocked", "service", "freight"], "visible_to_roles": [],               "position": 4.2, "show_in_table": False, "tooltip_key": "field.tooltip.inventory_type", "label_key": "field.label.inventory_type"},
    {"key": "allow_splitting",   "label": "Allow Splitting",   "type": "bool",   "editable": True,  "required": False, "options": [],                                            "visible_to_roles": [],               "position": 4.5, "show_in_table": False, "tooltip_key": "field.tooltip.allow_splitting", "label_key": "field.label.allow_splitting"},
    {"key": "pick_method",       "label": "Stock Cutting",     "type": "select", "editable": True,  "required": False, "options": ["default", "fifo", "fefo", "lifo"], "visible_to_roles": [],               "position": 4.6, "show_in_table": False, "tooltip_key": "field.tooltip.pick_method", "label_key": "field.label.pick_method"},
    {"key": "location_name",     "label": "Location",          "type": "text",   "editable": False, "required": False, "options": [],                                            "visible_to_roles": [],               "position": 5,  "show_in_table": True,  "tooltip_key": "field.tooltip.location_name", "label_key": "field.label.location_name"},
    # Price columns are injected dynamically at position 6+ by _inject_price_columns()
    # Read-only: status changes only through dedicated actions (Make Available, Revert
    # to Draft), never a free-form dropdown edit - same convention as document status.
    {"key": "status",            "label": "Status",            "type": "status", "editable": False, "required": False, "options": [], "visible_to_roles": [],               "position": 100, "show_in_table": True, "label_key": "field.label.status"},
    {"key": "short_description", "label": "Short Description", "type": "text",   "editable": True,  "required": False, "options": [],                                            "visible_to_roles": [],               "position": 101, "show_in_table": False, "label_key": "field.label.short_description"},
    {"key": "description",       "label": "Description",       "type": "text",   "editable": True,  "required": False, "options": [],                                            "visible_to_roles": [],               "position": 102, "show_in_table": False, "label_key": "field.label.description"},
    {"key": "notes",             "label": "Notes",             "type": "text",   "editable": True,  "required": False, "options": [],                                            "visible_to_roles": [],               "position": 103, "show_in_table": False, "label_key": "field.label.notes"},
    {"key": "barcode",           "label": "Barcode",           "type": "text",   "editable": True,  "required": False, "options": [],                                            "visible_to_roles": [],               "position": 104, "show_in_table": True,  "tooltip_key": "field.tooltip.barcode", "label_key": "field.label.barcode"},
    {"key": "hs_code",           "label": "HS Code",           "type": "text",   "editable": True,  "required": False, "options": [],                                            "visible_to_roles": [],               "position": 104.5, "show_in_table": False, "tooltip_key": "field.tooltip.hs_code", "label_key": "field.label.hs_code"},
    {"key": "country_of_origin", "label": "Country of Origin", "type": "text",   "editable": True,  "required": False, "options": [],                                            "visible_to_roles": [],               "position": 104.52,"show_in_table": False, "tooltip_key": "field.tooltip.country_of_origin", "label_key": "field.label.country_of_origin"},
    {"key": "preferred_supplier","label": "Preferred Supplier","type": "text",   "editable": True,  "required": False, "options": [],                                            "visible_to_roles": [],               "position": 104.54,"show_in_table": False, "tooltip_key": "field.tooltip.preferred_supplier", "label_key": "field.label.preferred_supplier"},
    {"key": "batch_no",          "label": "Batch / Lot No",    "type": "text",   "editable": True,  "required": False, "options": [],                                            "visible_to_roles": [],               "position": 104.55,"show_in_table": False, "tooltip_key": "field.tooltip.batch_no", "label_key": "field.label.batch_no"},
    {"key": "purchase_sku",              "label": "Purchase SKU",              "type": "text",   "editable": True,  "required": False, "options": [],  "visible_to_roles": [],  "position": 104.6, "show_in_table": False, "tooltip_key": "field.tooltip.purchase_sku", "label_key": "field.label.purchase_sku"},
    {"key": "purchase_name",             "label": "Purchase Name",             "type": "text",   "editable": True,  "required": False, "options": [],  "visible_to_roles": [],  "position": 104.7, "show_in_table": False, "tooltip_key": "field.tooltip.purchase_name", "label_key": "field.label.purchase_name"},
    {"key": "purchase_unit",             "label": "Purchase Unit",             "type": "text",   "editable": True,  "required": False, "options": [],  "visible_to_roles": [],  "position": 104.8, "show_in_table": True,  "tooltip_key": "field.tooltip.purchase_unit", "label_key": "field.label.purchase_unit"},
    {"key": "purchase_conversion_factor","label": "Conversion Factor",         "type": "number", "editable": True,  "required": False, "options": [],  "visible_to_roles": [],  "position": 104.9, "show_in_table": False, "tooltip_key": "field.tooltip.purchase_conversion_factor", "label_key": "field.label.purchase_conversion_factor"},
    {"key": "created_at",        "label": "Created",           "type": "date",   "editable": False, "required": False, "options": [],                                            "visible_to_roles": [],               "position": 107, "show_in_table": False, "label_key": "field.label.created_at"},
    {"key": "updated_at",        "label": "Updated",           "type": "date",   "editable": False, "required": False, "options": [],                                            "visible_to_roles": [],               "position": 108, "show_in_table": False, "label_key": "field.label.updated_at"},
]

# Built-in fields keyed for O(1) lookup when rehydrating stripped metadata.
_BASE_FIELD_BY_KEY: dict[str, dict] = {f["key"]: f for f in _BASE_FIELDS}


def _rehydrate_builtin_metadata(schema: list[dict]) -> list[dict]:
    """Reattach canonical label_key/tooltip_key to built-in fields.

    The item schema PATCH model does not carry label_key or tooltip_key, so a
    company that customizes its schema stores built-in fields stripped of both.
    Reattach them from _BASE_FIELDS, keyed strictly on built-in membership so
    custom fields and dynamic price columns are never touched. label_key is
    restored only when the stored label still equals the canonical English label
    (a user-renamed built-in keeps its literal label and is never translated);
    tooltip_key is restored for every built-in key, since there is no
    custom-tooltip edit path. Idempotent: a field that already carries a key is
    left as-is."""
    out: list[dict] = []
    for f in schema:
        base = _BASE_FIELD_BY_KEY.get(f.get("key"))
        if base is None:
            out.append(f)
            continue
        updated = dict(f)
        if ("label_key" not in updated and base.get("label_key")
                and updated.get("label") == base.get("label")):
            updated["label_key"] = base["label_key"]
        if "tooltip_key" not in updated and base.get("tooltip_key"):
            updated["tooltip_key"] = base["tooltip_key"]
        out.append(updated)
    return out


def _inject_price_columns(base: list[dict], price_lists: list[dict]) -> list[dict]:
    """Insert a money column for each price list after position 5 (location).

    For every cost-type price list (e.g. "Cost"), a paired virtual column
    ``<key>_total`` is injected immediately after it (position + 0.01).
    This column displays ``unit_price × quantity`` and is never stored.
    """
    existing_keys = {f["key"] for f in base}
    price_cols = []
    for i, pl in enumerate(price_lists):
        name = pl.get("name", "")
        key = price_key(name)
        if key in existing_keys:
            continue  # already present (stored schema round-trip)
        # "Cost" price lists are restricted to admin/manager
        restricted = is_cost_list_name(name)
        pos = 6 + i
        price_cols.append({
            "key": key,
            "label": name,
            "type": "rate",  # a unit price is a rate (may carry > currency precision); total stays money
            "editable": not is_derived(pl),  # derived lists are computed, never edited per item
            "required": False,
            "options": [],
            "visible_to_roles": ["admin", "manager"] if restricted else [],
            "position": pos,
            "show_in_table": True,
        })
        if restricted:
            # Virtual total column: always paired with the cost price column
            price_cols.append({
                "key": f"{key}_total",
                "label": f"{name} (Total)",
                "type": "money",
                "editable": True,
                "required": False,
                "options": [],
                "visible_to_roles": ["admin", "manager"],
                "position": pos + 0.01,
                "show_in_table": True,
                "virtual": True,           # never stored; computed at render time
                "paired_with": key,        # always moves with this field
            })
    return sorted(base + price_cols, key=lambda f: f.get("position", 999))


def cost_columns(price_lists: list[dict]) -> list[dict]:
    """The cost schema columns (cost_price and its virtual total) generated for the given
    price lists, empty if none is a cost list. Used to re-inject the cost fields that the
    company-scoped /me/item-schema strips, for a draft whose cost is item-visible - the
    single source the schema endpoint would have produced, never a hand-built copy."""
    return [f for f in _inject_price_columns([], price_lists) if f.get("key") in COST_SCHEMA_KEYS]


# The schema field types whose stored values are numeric, so a field of one of these
# types is range/exact scope-searchable and classified numeric on merge. Single source
# for that classification, consumed by the inventory search and merge paths.
NUMERIC_SCHEMA_TYPES: frozenset[str] = frozenset({"number", "money", "rate", "weight"})

# Field keys a company can never hide through a visible_to_roles override. status drives
# lifecycle membership filtering (sold/archived/draft) that runs BEFORE field visibility
# in list_items, so a role-restricted status would be a membership oracle: probing
# ?status=sold against the default view discloses the hidden value. Forcing an empty
# visible_to_roles keeps that pre-visibility filter safe.
NON_HIDEABLE_FIELD_KEYS: frozenset[str] = frozenset({"status"})

# Backward-compatible constant: base fields + default price columns
DEFAULT_ITEM_SCHEMA: list[dict] = _inject_price_columns(_BASE_FIELDS, _DEFAULT_PRICE_LISTS)


async def get_effective_field_schema(
    session: AsyncSession, company_id, category: str | None = None
) -> list[dict]:
    """Return the effective item field schema for the given company/category.

    Price columns are dynamically generated from the company's configured
    price lists (settings["price_lists"]), not hardcoded.

    If the stored schema is missing default fields (e.g. after a partial PATCH),
    missing defaults are appended so clients always see the full set.
    """
    co = await session.get(
        Company,
        _uuid.UUID(str(company_id)) if isinstance(company_id, str) else company_id,
    )
    if co is None:
        return DEFAULT_ITEM_SCHEMA
    settings = co.settings or {}

    # Build base schema: stored fields + any missing _BASE_FIELDS defaults
    stored: list[dict] = settings.get("item_schema") or _BASE_FIELDS
    price_lists: list[dict] = settings.get("price_lists") or _DEFAULT_PRICE_LISTS
    # Inject price columns (idempotent - skips already-present keys)
    stored_with_prices = _inject_price_columns(stored, price_lists)
    # Append any _BASE_FIELDS defaults missing from stored schema (no duplicates)
    stored_keys = {f["key"] for f in stored_with_prices}
    full_defaults = _inject_price_columns(_BASE_FIELDS, price_lists)
    base_schema = stored_with_prices + [f for f in full_defaults if f["key"] not in stored_keys]

    # Price-column editability is config-driven only: a derived list's column is read-only,
    # a manual list's is editable. This must win over any stored schema round-trip captured
    # while the list's derived state was different.
    editable_by_key = {price_key(pl.get("name", "")): not is_derived(pl) for pl in price_lists}
    base_schema = [
        {**f, "editable": editable_by_key[f["key"]]} if f["key"] in editable_by_key else f
        for f in base_schema
    ]

    # Status is never a free-form dropdown, regardless of a stored schema round-trip
    # captured before this became a hard rule.
    base_schema = [
        {**f, "editable": False, "options": []} if f["key"] == "status" else f
        for f in base_schema
    ]

    if category:
        cat_schemas: dict[str, list[dict]] = settings.get("category_schemas") or {}
        cat_fields: list[dict] = cat_schemas.get(category) or []
        if cat_fields:
            keys_in_cat = {f["key"] for f in cat_fields}
            merged = [f for f in base_schema if f["key"] not in keys_in_cat]
            merged.extend(cat_fields)
            base_schema = merged

    # Non-hideable fields carry an empty visible_to_roles no matter what a stored base or
    # category override set. Applied at the single exit so both paths are covered.
    base_schema = [
        {**f, "visible_to_roles": []} if f["key"] in NON_HIDEABLE_FIELD_KEYS else f
        for f in base_schema
    ]
    return _rehydrate_builtin_metadata(base_schema)
