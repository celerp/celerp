# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""celerp-labels — Label/barcode printing module for Celerp.

This is the reference module for the Celerp module system. It demonstrates:
- PLUGIN_MANIFEST structure
- API + UI route registration
- Slot contributions (nav, bulk_action, item_action, settings_tab)
- DB migrations via Alembic
- pip dependency declaration via requirements.txt

Module authors: see https://celerp.com/docs/modules for the full guide.
"""

PLUGIN_MANIFEST = {
    # ── Identity ──────────────────────────────────────────────────────────────
    "name": "celerp-labels",
    "version": "0.1.0",
    "display_name": "Label Printing",
    "description": "Print barcode/QR labels for inventory items and documents.",
    "license": "MIT",
    "author": "Celerp",

    # ── Routes ────────────────────────────────────────────────────────────────
    # Each module is a Python package in DATA_DIR/modules/<name>/
    # Route modules are imported relative to that package.
    "api_routes": "celerp_labels.routes",
    "ui_routes": "celerp_labels.ui_routes",
    "depends_on": ["celerp-inventory"],

    # ── Extension slots ───────────────────────────────────────────────────────
    "slots": {
        "nav": {
            "group": "Inventory",
            "key": "labels",
            "icon": "🏷",
            "label": "Labels",
            "label_key": "nav.labels",
            "href": "/settings/labels",
            "order": 32,
            "min_role": "operator",
        },
        "bulk_action": {
            "label": "Print Labels",
            "form_action": "/labels/print-bulk",
            "icon": "🖨",
        },
        "item_action": None,
        "settings_tab": {
            "label": "Labels",
            "href": "/settings/labels",
            "order": 60,
        },
    },

    # ── DB migrations ─────────────────────────────────────────────────────────
    # Path to an Alembic migrations directory inside this package.
    # The loader passes this to ALEMBIC_VERSION_LOCATIONS.
    "migrations": "celerp_labels.migrations",

    # ── Python dependencies ───────────────────────────────────────────────────
    # Informational; actual install via requirements.txt in this directory.
    "requires": ["reportlab>=4.0", "python-barcode", "qrcode"],
}
