# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""celerp-contacts — Contact management module for Celerp.

Provides:
- Contact CRUD, tags, notes, people, addresses
- Credit memo support
- Import/export
"""

PLUGIN_MANIFEST = {
    "name": "celerp-contacts",
    "version": "1.0.0",
    "display_name": "Contacts",
    "description": "Contact management, memos, notes, tags, import/export.",
    "license": "MIT",
    "author": "Celerp",
    "api_routes": "celerp_contacts.routes",
    "ui_routes": "celerp_contacts.ui_routes",
    "depends_on": ["celerp-inventory"],
    "slots": {
        "nav": [
            {"group": "Contacts", "key": "customers", "href": "/contacts/customers", "label": "Customers", "label_key": "nav.customers", "order": 40, "settings_href": "/settings/contacts", "min_role": "operator"},
            {"group": "Contacts", "key": "vendors", "href": "/contacts/vendors", "label": "Vendors", "label_key": "nav.vendors", "order": 41, "min_role": "operator"},
        ],
        "projection_handler": [
            {"prefix": "crm.contact.", "handler": "celerp_contacts.projections:apply_contact_event"},
        ],
        # Auto-run on every startup (incl. after a version upgrade): collapse legacy companies that
        # were seeded with two separate customer+vendor self-contacts into one both+is_self record.
        "on_modules_ready": {"handler": "celerp_contacts.migrations:backfill_self_contacts_hook"},
        "send_to_targets": [],
    },
    "migrations": None,
    "requires": [],
}
