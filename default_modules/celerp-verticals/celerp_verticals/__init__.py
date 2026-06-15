# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""celerp-verticals: industry vertical presets (apply category schemas in one call)."""

PLUGIN_MANIFEST = {
    "name": "celerp-verticals",
    "version": "1.0.0",
    "display_name": "Industry Verticals",
    "description": "Pre-built category schema presets for industry verticals (Gems & Jewelry, etc.).",
    "license": "MIT",
    "author": "Celerp",
    "api_routes": "celerp_verticals.routes",
    "ui_routes": "celerp_verticals.ui_routes",
    "depends_on": [],
    "soft_depends": ["celerp-inventory"],
    "slots": {},
    "migrations": None,
    "requires": [],
}
