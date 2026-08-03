# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""celerp-ai — AI query module for Celerp.

Cloud-gated: requires an active Connect + AI subscription (X-Session-Token).
"""

PLUGIN_MANIFEST = {
    "name": "celerp-ai",
    "version": "1.0.0",
    "display_name": "AI Assistant",
    "description": "Natural language queries against ERP data. Requires a Connect + AI subscription.",
    "license": "LicenseRef-Proprietary",
    "author": "Celerp",
    "api_routes": "celerp_ai.setup",
    "ui_routes": "celerp_ai.ui_routes",
    "depends_on": [],
    "slots": {
        "nav": {"group": "AI", "key": "ai", "href": "/ai", "label": "AI Assistant", "label_key": "nav.ai_assistant", "order": 90, "settings_href": "/ai/settings", "permission": "use_ai_assistant"},
    },
    "migrations": None,
}
