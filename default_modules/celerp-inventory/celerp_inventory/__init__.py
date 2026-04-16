# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1
"""celerp-inventory module: item management, field schemas, location tracking, bulk operations."""

PLUGIN_MANIFEST = {
    "name": "celerp-inventory",
    "version": "1.0.0",
    "display_name": "Inventory",
    "description": "Item management, field schemas, location tracking, bulk operations.",
    "license": "BSL-1.1",
    "author": "Celerp",
    "api_routes": "celerp_inventory.routes",
    "ui_routes": "celerp_inventory.ui_routes",
    "depends_on": [],
    "soft_depends": [],
    "slots": {
        "nav": {"group": "Inventory", "key": "inventory", "href": "/inventory", "label": "Inventory", "order": 30, "min_role": "operator"},
        "projection_handler": {"prefix": "item.", "handler": "celerp_inventory.projections:apply_item_event"},
    },
    "migrations": None,
    "requires": [],
}
