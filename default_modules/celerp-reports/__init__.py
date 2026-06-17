# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT

PLUGIN_MANIFEST = {
    "name": "celerp-reports",
    "version": "1.0.0",
    "display_name": "Reports",
    "description": "Financial and operational reports.",
    "license": "MIT",
    "author": "Celerp",
    "depends_on": ["celerp-accounting", "celerp-docs", "celerp-inventory"],
    "api_routes": "celerp_reports.api_setup",
    "ui_routes": "ui.routes.reports",
    "slots": {
        "nav": {"group": "Finance", "key": "reports", "href": "/reports", "label": "Reports", "label_key": "nav.reports", "order": 51, "min_role": "manager"},
    },
}
