# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1
"""celerp-inventory — Inventory module for Celerp.

Provides:
- Item management and field schemas
- Location tracking and stock transfers
- Bulk operations and barcode scanning
"""

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
        "nav": [
            {"group": "Inventory", "key": "inventory", "href": "/inventory", "label": "Inventory", "label_key": "nav.inventory", "order": 30, "settings_href": "/settings/inventory", "min_role": "operator"},
            {"group": "Inventory", "key": "inventory_sold", "href": "/inventory?status=sold", "label": "Sold Inventory", "label_key": "nav.sold_inventory", "order": 31, "min_role": "operator"},
            {"group": "Inventory", "key": "inventory_archived", "href": "/inventory?status=archived", "label": "Archived Inventory", "label_key": "nav.archived_inventory", "order": 32, "min_role": "operator"},
        ],
        "projection_handler": [
            {"prefix": "item.", "handler": "celerp_inventory.projections:apply_item_event"},
            # {"prefix": "scan.", "handler": "celerp.projections.handlers.scanning:apply_scanning_event"},  # Scanning module disabled until properly finished
        ],
    },
    "migrations": None,
    "requires": [],
}
