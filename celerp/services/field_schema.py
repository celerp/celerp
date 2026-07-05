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

# Reserved system value for a field whose sources disagreed and cannot be reconciled (e.g. a merge
# of items holding different values for a dropdown or custom attribute). There is ONE canonical form,
# "Mixed", stored and displayed verbatim — no separate lowercase sentinel vs capitalized label.
MIXED_VALUE = "Mixed"

# Default price lists (used when company has none configured)
_DEFAULT_PRICE_LISTS: list[dict] = [
    {"name": "Wholesale"},
    {"name": "Retail"},
]

# Base schema fields (without price columns - those are injected dynamically)
_BASE_FIELDS: list[dict] = [
    {"key": "sku",               "label": "SKU",               "type": "text",   "editable": True,  "required": True,  "options": [],                                            "visible_to_roles": [],               "position": 0,  "show_in_table": True},
    {"key": "name",              "label": "Name",              "type": "text",   "editable": True,  "required": True,  "options": [],                                            "visible_to_roles": [],               "position": 1,  "show_in_table": True},
    {"key": "category",          "label": "Category",          "type": "text",   "editable": True,  "required": False, "options": [],                                            "visible_to_roles": [],               "position": 2,  "show_in_table": True},
    {"key": "quantity",          "label": "Qty",               "type": "number", "editable": True,  "required": False, "options": [],                                            "visible_to_roles": [],               "position": 3,   "show_in_table": True,  "tooltip_key": "field.tooltip.quantity"},
    {"key": "sell_by",           "label": "Sell Unit",         "type": "text",   "editable": True,  "required": False, "options": [],                                            "visible_to_roles": [],               "position": 3.05,"show_in_table": False, "tooltip_key": "field.tooltip.sell_by"},
    {"key": "weight",            "label": "Net Weight",        "type": "number", "editable": True,  "required": False, "options": [],                                            "visible_to_roles": [],               "position": 3.02, "show_in_table": True,  "tooltip_key": "field.tooltip.weight"},
    {"key": "weight_unit",       "label": "Net Weight Unit",   "type": "text",   "editable": True,  "required": False, "options": [],                                            "visible_to_roles": [],               "position": 3.15,"show_in_table": False, "tooltip_key": "field.tooltip.weight_unit"},
    {"key": "pieces",            "label": "Pieces",            "type": "number", "editable": True,  "required": False, "options": [],                                            "visible_to_roles": [],               "position": 3.06,"show_in_table": True,  "tooltip_key": "field.tooltip.pieces"},
    {"key": "reorder_point",     "label": "Reorder Point",     "type": "number", "editable": True,  "required": False, "options": [],                                            "visible_to_roles": [],               "position": 3.7, "show_in_table": False, "tooltip_key": "field.tooltip.reorder_point"},
    {"key": "reorder_qty",       "label": "Reorder Qty",       "type": "number", "editable": True,  "required": False, "options": [],                                            "visible_to_roles": [],               "position": 3.72,"show_in_table": False, "tooltip_key": "field.tooltip.reorder_qty"},
    {"key": "gross_weight",      "label": "Gross Weight",      "type": "number", "editable": True,  "required": False, "options": [],                                            "visible_to_roles": [],               "position": 3.25,"show_in_table": False, "tooltip_key": "field.tooltip.gross_weight"},
    {"key": "gross_weight_unit", "label": "Gross Weight Unit", "type": "text",   "editable": True,  "required": False, "options": [],                                            "visible_to_roles": [],               "position": 3.3, "show_in_table": False, "tooltip_key": "field.tooltip.gross_weight_unit"},
    {"key": "inventory_type",    "label": "Inventory Type",    "type": "select", "editable": True,  "required": False, "options": ["stocked", "component", "non_stocked", "service", "freight"], "visible_to_roles": [],               "position": 4.2, "show_in_table": False, "tooltip_key": "field.tooltip.inventory_type"},
    {"key": "allow_splitting",   "label": "Allow Splitting",   "type": "bool",   "editable": True,  "required": False, "options": [],                                            "visible_to_roles": [],               "position": 4.5, "show_in_table": False, "tooltip_key": "field.tooltip.allow_splitting"},
    {"key": "location_name",     "label": "Location",          "type": "text",   "editable": False, "required": False, "options": [],                                            "visible_to_roles": [],               "position": 5,  "show_in_table": True,  "tooltip_key": "field.tooltip.location_name"},
    # Price columns are injected dynamically at position 6+ by _inject_price_columns()
    {"key": "status",            "label": "Status",            "type": "status", "editable": True,  "required": False, "options": ["available", "reserved", "sold"], "visible_to_roles": [],               "position": 100, "show_in_table": True},
    {"key": "short_description", "label": "Short Description", "type": "text",   "editable": True,  "required": False, "options": [],                                            "visible_to_roles": [],               "position": 101, "show_in_table": False},
    {"key": "description",       "label": "Description",       "type": "text",   "editable": True,  "required": False, "options": [],                                            "visible_to_roles": [],               "position": 102, "show_in_table": False},
    {"key": "notes",             "label": "Notes",             "type": "text",   "editable": True,  "required": False, "options": [],                                            "visible_to_roles": [],               "position": 103, "show_in_table": False},
    {"key": "barcode",           "label": "Barcode",           "type": "text",   "editable": True,  "required": False, "options": [],                                            "visible_to_roles": [],               "position": 104, "show_in_table": True,  "tooltip_key": "field.tooltip.barcode"},
    {"key": "hs_code",           "label": "HS Code",           "type": "text",   "editable": True,  "required": False, "options": [],                                            "visible_to_roles": [],               "position": 104.5, "show_in_table": False, "tooltip_key": "field.tooltip.hs_code"},
    {"key": "batch_no",          "label": "Batch / Lot No",    "type": "text",   "editable": True,  "required": False, "options": [],                                            "visible_to_roles": [],               "position": 104.55,"show_in_table": False, "tooltip_key": "field.tooltip.batch_no"},
    {"key": "purchase_sku",              "label": "Purchase SKU",              "type": "text",   "editable": True,  "required": False, "options": [],  "visible_to_roles": [],  "position": 104.6, "show_in_table": False, "tooltip_key": "field.tooltip.purchase_sku"},
    {"key": "purchase_name",             "label": "Purchase Name",             "type": "text",   "editable": True,  "required": False, "options": [],  "visible_to_roles": [],  "position": 104.7, "show_in_table": False, "tooltip_key": "field.tooltip.purchase_name"},
    {"key": "purchase_unit",             "label": "Purchase Unit",             "type": "text",   "editable": True,  "required": False, "options": [],  "visible_to_roles": [],  "position": 104.8, "show_in_table": True,  "tooltip_key": "field.tooltip.purchase_unit"},
    {"key": "purchase_conversion_factor","label": "Conversion Factor",         "type": "number", "editable": True,  "required": False, "options": [],  "visible_to_roles": [],  "position": 104.9, "show_in_table": False, "tooltip_key": "field.tooltip.purchase_conversion_factor"},
    {"key": "created_at",        "label": "Created",           "type": "date",   "editable": False, "required": False, "options": [],                                            "visible_to_roles": [],               "position": 107, "show_in_table": False},
    {"key": "updated_at",        "label": "Updated",           "type": "date",   "editable": False, "required": False, "options": [],                                            "visible_to_roles": [],               "position": 108, "show_in_table": False},
]

def _inject_price_columns(base: list[dict], price_lists: list[dict]) -> list[dict]:
    """Insert a money column for each price list after position 5 (location).

    For every cost-type price list (e.g. "Cost"), a paired virtual column
    ``<key>_total`` is injected immediately after it (position + 0.01).
    This column displays ``unit_price × quantity`` and is never stored.
    """
    existing_keys = {f["key"] for f in base}
    # "Cost" price list is restricted to admin/manager
    cost_names = {"cost", "cost price", "landed", "landed cost"}
    price_cols = []
    for i, pl in enumerate(price_lists):
        name = pl.get("name", "")
        key = f"{name.lower()}_price"
        if key in existing_keys:
            continue  # already present (stored schema round-trip)
        restricted = name.lower() in cost_names
        pos = 6 + i
        price_cols.append({
            "key": key,
            "label": name,
            "type": "rate",  # a unit price is a rate (may carry > currency precision); total stays money
            "editable": True,
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

    if category:
        cat_schemas: dict[str, list[dict]] = settings.get("category_schemas") or {}
        cat_fields: list[dict] = cat_schemas.get(category) or []
        if cat_fields:
            keys_in_cat = {f["key"] for f in cat_fields}
            merged = [f for f in base_schema if f["key"] not in keys_in_cat]
            merged.extend(cat_fields)
            return merged
    return base_schema
