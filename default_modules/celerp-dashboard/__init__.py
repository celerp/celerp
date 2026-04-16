# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1
"""celerp-dashboard — Dashboard KPI module for Celerp."""

PLUGIN_MANIFEST = {
    "name": "celerp-dashboard",
    "version": "1.0.0",
    "display_name": "Dashboard",
    "description": "KPI overview: inventory value, AR, document counts, and recent activity.",
    "license": "BSL-1.1",
    "author": "Celerp",
    "api_routes": "celerp_dashboard.setup",
    "ui_routes": None,
    "depends_on": [],
    "slots": {
        "nav": {"group": None, "key": "dashboard", "href": "/dashboard", "label": "Dashboard", "label_key": "nav.dashboard", "order": 1},
    },
    "migrations": None,
}
