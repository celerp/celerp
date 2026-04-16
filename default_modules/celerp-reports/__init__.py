# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

PLUGIN_MANIFEST = {
    "name": "celerp-reports",
    "version": "1.0.0",
    "display_name": "Reports",
    "description": "Financial and operational reports.",
    "license": "BSL-1.1",
    "author": "Celerp",
    "depends_on": ["celerp-accounting", "celerp-docs", "celerp-inventory"],
    "api_routes": "celerp_reports.api_setup",
    "ui_routes": "celerp_reports.ui_routes",
    "slots": {
        "nav": {"group": "Finance", "key": "reports", "href": "/reports", "label": "Reports", "label_key": "nav.reports", "order": 51, "min_role": "manager"},
    },
}
