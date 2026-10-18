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
    # A company that turns the module off keeps reading its asset records and their audit
    # links at the same addresses; nothing can be changed until it is on again.
    "readable_when_off": True,

    # ── Extension slots ───────────────────────────────────────────────────────
    "slots": {
        "nav": [
            {
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
            # Shown to whoever holds an asset as its custodian, whatever their role.
            {
                "group": None,
                "key": "my_assets",
                "icon": "🔑",
                "label": "My assets",
                "label_key": "nav.my_assets",
                "href": "/fixed-assets/mine",
                "probe_href": "/fixed-assets/mine/nav",
                "order": 95,
            },
        ],
        "label_source": {
            "entity_type": "fa_asset",
            "handler": "celerp_fixed_assets.labels_source:asset_tag_rows",
            "permission": "view_fixed_assets",
            "label_key": "fixed_assets.asset_tags",
            "keys": ["number", "tag", "name", "class", "location", "in_service", "barcode"],
            "preset": {
                "name": "Asset tag",
                "format": "50x25mm",
                "orientation": "portrait",
                "fields": [
                    {"key": "barcode", "label": "", "type": "barcode", "x": 2, "y": 2, "barcode_height": 8},
                    {"key": "barcode", "label": "", "type": "barcode_text", "x": 2, "y": 10.5},
                    {"key": "number", "label": "fixed_assets.number", "type": "text", "x": 2, "y": 14,
                     "fontSize": 7, "bold": True},
                    {"key": "name", "label": "fixed_assets.name", "type": "text", "x": 2, "y": 17.5, "fontSize": 6},
                    {"key": "location", "label": "fixed_assets.location", "type": "text", "x": 2, "y": 20.5,
                     "fontSize": 6},
                ],
                "copies": 1,
            },
        },
        "search_provider": {
            "handler": "celerp_fixed_assets.search:global_search",
            "result_key": "items",
            "permission": "view_fixed_assets",
        },
        "projection_handler": [
            {"prefix": "fa.", "handler": "celerp_fixed_assets.projections:apply_fixed_asset_event",
             "permission": "view_fixed_assets"},
        ],
        # A demo company's sample data includes a few asset bills to register.
        "demo_records": {"handler": "celerp_fixed_assets.demo:seed_demo_assets_hook"},
        # Each company's run for last month, prepared or posted once its run day has come.
        "background_loop": {"handler": "celerp_fixed_assets.runs:loop"},
        # Invoices that sell an asset and credit notes that credit its sale.
        "doc_asset_lines": {"handler": "celerp_fixed_assets.sales:on_asset_lines"},
        # An asset sale's invoice and credit notes link back to the sale; a finalized bill's
        # asset lines lead to registering them.
        "doc_detail_actions": [{"render": "celerp_fixed_assets.ui_routes:doc_sale_actions",
                                "permission": "view_fixed_assets"},
                               {"render": "celerp_fixed_assets.ui_routes:doc_register_actions",
                                "permission": "manage_accounting"}],
        # The opening asset register, imported as at the cutover.
        "import_target": {"kind": "fixed_asset_register", "href": "/fixed-assets/import",
                          "handler": "celerp_fixed_assets.imports:import_register"},
        # Sales waiting on their invoice block turning the module off; other open work is listed.
        "turn_off_preflight": {"handler": "celerp_fixed_assets.turnoff:preflight"},
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
