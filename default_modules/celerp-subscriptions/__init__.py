# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT

PLUGIN_MANIFEST = {
    "name": "celerp-subscriptions",
    "version": "2.0.0",
    "display_name": "Subscriptions",
    "description": "Recurring invoice and purchase order templates with automatic generation.",
    "license": "MIT",
    "author": "Celerp",
    "api_routes": "celerp_subscriptions.routes",
    "ui_routes": "celerp_subscriptions.ui_routes",
    "depends_on": ["celerp-docs"],
    "slots": {
        "nav": [
            {"group": "Subscriptions", "key": "subscriptions_sales", "href": "/subscriptions?direction=sales", "label": "Sales Subscriptions", "label_key": "nav.subscriptions_sales", "order": 25, "permission": "view_subscriptions"},
            {"group": "Subscriptions", "key": "subscriptions_purchasing", "href": "/subscriptions?direction=purchasing", "label": "Purchasing Subscriptions", "label_key": "nav.subscriptions_purchasing", "order": 26, "permission": "view_subscriptions"},
        ],
    },
    "migrations": None,
    "requires": [],
}
