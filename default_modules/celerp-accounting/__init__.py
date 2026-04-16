# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

PLUGIN_MANIFEST = {
    "name": "celerp-accounting",
    "version": "1.0.0",
    "display_name": "Accounting",
    "description": "Chart of accounts, journal entries, and financial reporting.",
    "license": "BSL-1.1",
    "author": "Celerp",
    "depends_on": ["celerp-docs"],
    "api_routes": "celerp_accounting.api_setup",
    "ui_routes": "celerp_accounting.ui_routes",
    "slots": {
        "nav": [
            {"group": "Finance", "key": "accounting", "href": "/accounting", "label": "Accounting", "label_key": "nav.accounting", "order": 50, "settings_href": "/settings/accounting", "min_role": "manager"},
            {"group": "Finance", "key": "reconcile", "href": "/accounting/reconcile/start", "label": "Reconcile", "label_key": "nav.reconcile", "order": 52, "min_role": "manager"},
        ],
        "on_company_created": {"handler": "celerp_accounting.routes:seed_chart_of_accounts_hook"},
        "projection_handler": [
            {"prefix": "account.", "handler": "celerp_accounting.projections:apply_accounting_event"},
            {"prefix": "je.", "handler": "celerp_accounting.projections:apply_accounting_event"},
            {"prefix": "acc.", "handler": "celerp_accounting.projections:apply_accounting_event"},
        ],
    },
}
