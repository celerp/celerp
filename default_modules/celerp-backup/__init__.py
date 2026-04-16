# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1
"""celerp-backup — Cloud backup module for Celerp.

Cloud-gated: requires active Celerp Cloud subscription (X-Session-Token).
"""

PLUGIN_MANIFEST = {
    "name": "celerp-backup",
    "version": "1.0.0",
    "display_name": "Cloud Backup",
    "description": "Encrypted cloud backup to Celerp-managed R2 storage. Requires Cloud subscription.",
    "license": "BSL-1.1",
    "author": "Celerp",
    "api_routes": "celerp_backup.setup",
    "ui_routes": None,
    "depends_on": [],
    "slots": {},
    "migrations": None,
}
