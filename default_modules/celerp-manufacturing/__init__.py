# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""celerp-manufacturing — Manufacturing orders and BOM module for Celerp.

Provides:
- Bill of Materials (BOM) management
- Manufacturing order lifecycle (create → start → consume → complete/cancel)
- Projection handler for mfg.* and bom.* event prefixes
- Sidebar nav, settings tab, bulk/item action slots
- Auto journal entry on order completion
"""

PLUGIN_MANIFEST = {
    # ── Identity ──────────────────────────────────────────────────────────────
    "name": "celerp-manufacturing",
    "version": "0.1.0",
    "display_name": "Manufacturing",
    "description": "Manufacturing orders, BOM management, and production tracking.",
    "license": "MIT",
    "author": "Celerp",

    # ── Routes ────────────────────────────────────────────────────────────────
    "api_routes": "celerp_manufacturing.routes",
    "ui_routes": "celerp_manufacturing.ui_routes",
    "depends_on": ["celerp-inventory"],

    # ── Extension slots ───────────────────────────────────────────────────────
    "slots": {
        "nav": {
            "group": "Inventory",
            "icon": "🏭",
            "label": "Manufacturing",
            "label_key": "nav.manufacturing",
            "href": "/manufacturing",
            "order": 40,
            "min_role": "operator",
        },
        "projection_handler": [
            {
                "prefix": "mfg.",
                "handler": "celerp_manufacturing.projection_handler:apply_manufacturing_event",
            },
            {
                "prefix": "bom.",
                "handler": "celerp_manufacturing.projection_handler:apply_manufacturing_event",
            },
        ],
    },

    # ── No DB migrations needed ───────────────────────────────────────────────
    # Manufacturing data lives in core projections/ledger tables.
    # No module-owned tables.
}
