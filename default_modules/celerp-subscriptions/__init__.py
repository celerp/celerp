# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1
"""celerp-subscriptions — Subscriptions module for Celerp.

Provides:
- Recurring invoice and purchase order generation
- Pause, resume, and manual generate controls
"""

PLUGIN_MANIFEST = {
    "name": "celerp-subscriptions",
    "version": "1.0.0",
    "display_name": "Subscriptions",
    "description": "Recurring invoice and purchase order generation with pause/resume/generate.",
    "license": "BSL-1.1",
    "author": "Celerp",
    "api_routes": "celerp_subscriptions.routes",
    "ui_routes": "celerp_subscriptions.ui_routes",
    "depends_on": ["celerp-docs"],
    "slots": {
        "nav": {"group": "Sales", "key": "subscriptions", "href": "/subscriptions", "label": "Subscriptions", "label_key": "nav.subscriptions", "order": 25, "min_role": "operator"},
        "projection_handler": {"prefix": "sub.", "handler": "celerp_subscriptions.projection_handler:apply_subscription_event"},
    },
    "migrations": None,
    "requires": [],
}
