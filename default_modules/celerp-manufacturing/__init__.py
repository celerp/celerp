# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""celerp-manufacturing — Manufacturing orders and BOM module for Celerp.

Provides:
- Manufacturing recipes attached to inventory items (cost roll-up, where-used)
- Production run lifecycle (planned -> in_progress -> on_hold -> completed/cancelled),
  with issue (components out) / receive (finished goods in) and one-tap build
- Projection handler for the mfg.* event prefix
- Sidebar nav (the Manufacturing group)
- Auto journal entry on run completion
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
        "nav": [
            {
                "group": "Manufacturing",
                "icon": "🏭",
                "label": "Production Queue",
                "href": "/manufacturing",
                "order": 10,
                "min_role": "operator",
            },
        ],
        "projection_handler": [
            {
                "prefix": "mfg.",
                "handler": "celerp_manufacturing.projection_handler:apply_manufacturing_event",
            },
            # Historical bom.* events (the BOM entity was retired; recipes live on the item) are
            # not routed here anymore — they fall through to the engine's default merge handler on
            # replay, so projections still rebuild cleanly without a dead branch to maintain.
        ],
    },

    # ── No DB migrations needed ───────────────────────────────────────────────
    # Manufacturing data lives in core projections/ledger tables.
    # No module-owned tables.
}
