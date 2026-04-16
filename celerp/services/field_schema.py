# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1
"""Kernel service: effective item field schema resolution.

Extracted from routers/companies.py so celerp-inventory module can import
it without a cross-module router dependency.
"""
from __future__ import annotations

import uuid as _uuid

from sqlalchemy.ext.asyncio import AsyncSession

from celerp.models.company import Company

# Default price lists (used when company has none configured)
_DEFAULT_PRICE_LISTS: list[dict] = [
    {"name": "Cost"},
    {"name": "Wholesale"},
    {"name": "Retail"},
]

# Base schema fields (without price columns - those are injected dynamically)
_BASE_FIELDS: list[dict] = [
    {"key": "sku",               "label": "SKU",               "type": "text",   "editable": True,  "required": True,  "options": [],                                            "visible_to_roles": [],               "position": 0,  "show_in_table": True},
    {"key": "name",              "label": "Name",              "type": "text",   "editable": True,  "required": True,  "options": [],                                            "visible_to_roles": [],               "position": 1,  "show_in_table": True},
    {"key": "category",          "label": "Category",          "type": "text",   "editable": True,  "required": False, "options": [],                                            "visible_to_roles": [],               "position": 2,  "show_in_table": True},
    {"key": "quantity",          "label": "Qty",               "type": "number", "editable": True,  "required": False, "options": [],                                            "visible_to_roles": [],               "position": 3,  "show_in_table": True},
    {"key": "sell_by",           "label": "Sell Unit",         "type": "text",   "editable": True,  "required": False, "options": [],                                            "visible_to_roles": [],               "position": 4,  "show_in_table": True},
    {"key": "allow_splitting",   "label": "Allow Splitting",   "type": "bool",   "editable": True,  "required": False, "options": [],                                            "visible_to_roles": [],               "position": 4.5, "show_in_table": False},
    {"key": "location_name",     "label": "Location",          "type": "text",   "editable": False, "required": False, "options": [],                                            "visible_to_roles": [],               "position": 5,  "show_in_table": True},
    # Price columns are injected dynamically at position 6+ by _inject_price_columns()
    {"key": "status",            "label": "Status",            "type": "status", "editable": True,  "required": False, "options": ["available", "reserved", "sold", "disposed"], "visible_to_roles": [],               "position": 100, "show_in_table": True},
    {"key": "short_description", "label": "Short Description", "type": "text",   "editable": True,  "required": False, "options": [],                                            "visible_to_roles": [],               "position": 101, "show_in_table": False},
    {"key": "description",       "label": "Description",       "type": "text",   "editable": True,  "required": False, "options": [],                                            "visible_to_roles": [],               "position": 102, "show_in_table": False},
    {"key": "notes",             "label": "Notes",             "type": "text",   "editable": True,  "required": False, "options": [],                                            "visible_to_roles": [],               "position": 103, "show_in_table": False},
    {"key": "barcode",           "label": "Barcode",           "type": "text",   "editable": True,  "required": False, "options": [],                                            "visible_to_roles": [],               "position": 104, "show_in_table": True},
    {"key": "hs_code",           "label": "HS Code",           "type": "text",   "editable": True,  "required": False, "options": [],                                            "visible_to_roles": [],               "position": 104.5, "show_in_table": False},
    {"key": "purchase_sku",              "label": "Purchase SKU",              "type": "text",   "editable": True,  "required": False, "options": [],  "visible_to_roles": [],  "position": 104.6, "show_in_table": False},
    {"key": "purchase_name",             "label": "Purchase Name",             "type": "text",   "editable": True,  "required": False, "options": [],  "visible_to_roles": [],  "position": 104.7, "show_in_table": False},
    {"key": "purchase_unit",             "label": "Purchase Unit",             "type": "text",   "editable": True,  "required": False, "options": [],  "visible_to_roles": [],  "position": 104.8, "show_in_table": False},
    {"key": "purchase_conversion_factor","label": "Conversion Factor",         "type": "number", "editable": True,  "required": False, "options": [],  "visible_to_roles": [],  "position": 104.9, "show_in_table": False},
    {"key": "weight",            "label": "Weight",            "type": "number", "editable": True,  "required": False, "options": [],                                            "visible_to_roles": [],               "position": 105, "show_in_table": False},
    {"key": "weight_unit",       "label": "Weight Unit",       "type": "text",   "editable": True,  "required": False, "options": [],                                            "visible_to_roles": [],               "position": 106, "show_in_table": False},
    {"key": "created_at",        "label": "Created",           "type": "date",   "editable": False, "required": False, "options": [],                                            "visible_to_roles": [],               "position": 107, "show_in_table": False},
    {"key": "updated_at",        "label": "Updated",           "type": "date",   "editable": False, "required": False, "options": [],                                            "visible_to_roles": [],               "position": 108, "show_in_table": False},
]

def _inject_price_columns(base: list[dict], price_lists: list[dict]) -> list[dict]:
    """Insert a money column for each price list after position 5 (location)."""
    # "Cost" price list is restricted to admin/manager
    cost_names = {"cost", "cost price", "landed", "landed cost"}
    price_cols = []
    for i, pl in enumerate(price_lists):
        name = pl.get("name", "")
        key = f"{name.lower()}_price"
        restricted = name.lower() in cost_names
        price_cols.append({
            "key": key,
            "label": name,
            "type": "money",
            "editable": True,
            "required": False,
            "options": [],
            "visible_to_roles": ["admin", "manager"] if restricted else [],
            "position": 6 + i,
            "show_in_table": True,
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
    """
    co = await session.get(
        Company,
        _uuid.UUID(str(company_id)) if isinstance(company_id, str) else company_id,
    )
    if co is None:
        return DEFAULT_ITEM_SCHEMA
    settings = co.settings or {}

    # Build base schema with dynamic price columns
    custom_base: list[dict] = settings.get("item_schema") or _BASE_FIELDS
    price_lists: list[dict] = settings.get("price_lists") or _DEFAULT_PRICE_LISTS
    base_schema = _inject_price_columns(custom_base, price_lists)

    if category:
        cat_schemas: dict[str, list[dict]] = settings.get("category_schemas") or {}
        cat_fields: list[dict] = cat_schemas.get(category) or []
        if cat_fields:
            keys_in_cat = {f["key"] for f in cat_fields}
            merged = [f for f in base_schema if f["key"] not in keys_in_cat]
            merged.extend(cat_fields)
            return merged
    return base_schema
