# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""celerp-ai — AI query module for Celerp.

Asks the Celerp AI service (X-Session-Token): free questions for an eligible free
account with a verified email address, more with Connect + AI.
"""

PLUGIN_MANIFEST = {
    "name": "celerp-ai",
    "version": "1.0.0",
    "display_name": "AI Assistant",
    "description": "Natural language questions about your ERP data and about using Celerp. Eligible free accounts with a verified email address receive free AI questions; Connect + AI adds more.",
    "license": "LicenseRef-Proprietary",
    "author": "Celerp",
    "api_routes": "celerp_ai.setup",
    "ui_routes": "celerp_ai.ui_routes",
    "depends_on": [],
    "slots": {
        "nav": {"group": "AI", "key": "ai", "href": "/ai", "label": "AI Assistant", "label_key": "nav.ai_assistant", "order": 90, "settings_href": "/ai/settings", "permission": "use_ai_assistant"},
    },
    "migrations": None,
    "help": {"dir": "celerp_ai/help"},
}
