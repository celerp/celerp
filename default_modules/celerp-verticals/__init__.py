# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1
"""celerp-verticals — Industry Verticals module for Celerp.

Provides:
- Pre-built category schema presets for industry verticals
- Gems & Jewelry and other vertical configurations
"""

PLUGIN_MANIFEST = {
    "name": "celerp-verticals",
    "version": "1.0.0",
    "display_name": "Industry Verticals",
    "description": "Pre-built category schema presets for industry verticals (Gems & Jewelry, etc.).",
    "license": "BSL-1.1",
    "author": "Celerp",
    "api_routes": "celerp_verticals.routes",
    "ui_routes": "celerp_verticals.ui_routes",
    "depends_on": ["celerp-inventory"],
    "slots": {},
    "migrations": None,
    "requires": [],
}
