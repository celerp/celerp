# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""celerp-fixed-assets - Fixed asset register and depreciation for Celerp.

Every asset change is a movement posted through the module journal interface
(celerp.services.module_journals), so the register and the general ledger agree.
"""

PLUGIN_MANIFEST = {
    # ── Identity ──────────────────────────────────────────────────────────────
    "name": "celerp-fixed-assets",
    "version": "0.1.0",
    "display_name": "Fixed assets",
    "description": "Fixed asset register, depreciation runs, disposals and revaluations.",
    "license": "MIT",
    "author": "Celerp",
    "api_routes": "celerp_fixed_assets.routes",
    "ui_routes": "celerp_fixed_assets.ui_routes",
    # Assets are registered from bill lines, so the documents module is required too.
    "depends_on": ["celerp-accounting", "celerp-docs"],

    # ── Extension slots ───────────────────────────────────────────────────────
    "slots": {
        "nav": {
            "group": "Finance",
            "key": "fixed_assets",
            "icon": "🏢",
            "label": "Fixed assets",
            "label_key": "nav.fixed_assets",
            "href": "/fixed-assets",
            "order": 53,
            "permission": "view_fixed_assets",
            "settings_href": "/settings/fixed-assets",
        },
        "projection_handler": [
            {"prefix": "fa.", "handler": "celerp_fixed_assets.projections:apply_fixed_asset_event"},
        ],
        # A demo company's sample data includes a few asset bills to register.
        "demo_records": {"handler": "celerp_fixed_assets.demo:seed_demo_assets_hook"},
        # Each company's run for last month, prepared or posted once its run day has come.
        "background_loop": {"handler": "celerp_fixed_assets.runs:loop"},
        # Invoices that sell an asset and credit notes that credit its sale.
        "doc_asset_lines": {"handler": "celerp_fixed_assets.sales:on_asset_lines"},
        # An asset sale's invoice and credit notes link back to the sale.
        "doc_detail_actions": [{"render": "celerp_fixed_assets.ui_routes:doc_sale_actions",
                                "permission": "view_fixed_assets"}],
    },

    # ── UI translation catalogs ───────────────────────────────────────────────
    "locales": {
        "en": {"file": "celerp_fixed_assets/locales/en.json"},
        "am": {"file": "celerp_fixed_assets/locales/am.json"},
        "ar": {"file": "celerp_fixed_assets/locales/ar.json", "rtl": True},
        "de": {"file": "celerp_fixed_assets/locales/de.json"},
        "es": {"file": "celerp_fixed_assets/locales/es.json"},
        "fr": {"file": "celerp_fixed_assets/locales/fr.json"},
        "id": {"file": "celerp_fixed_assets/locales/id.json"},
        "it": {"file": "celerp_fixed_assets/locales/it.json"},
        "ja": {"file": "celerp_fixed_assets/locales/ja.json"},
        "pt": {"file": "celerp_fixed_assets/locales/pt.json"},
        "th": {"file": "celerp_fixed_assets/locales/th.json"},
        "vi": {"file": "celerp_fixed_assets/locales/vi.json"},
    },
}
