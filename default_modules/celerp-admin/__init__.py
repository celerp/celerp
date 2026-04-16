# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1
"""celerp-admin — Admin and diagnostics module for Celerp."""

PLUGIN_MANIFEST = {
    "name": "celerp-admin",
    "version": "1.0.0",
    "display_name": "Admin Tools",
    "description": "Ledger doctor, data integrity auditing and repair tools.",
    "license": "BSL-1.1",
    "author": "Celerp",
    "api_routes": "celerp_admin.setup",
    "ui_routes": None,
    "depends_on": [],
    "slots": {},
    "migrations": None,
}
