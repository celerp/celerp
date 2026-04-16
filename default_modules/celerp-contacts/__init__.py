# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1
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
    "license": "BSL-1.1",
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
            {"prefix": "crm.memo.", "handler": "celerp_contacts.projections:apply_contact_event"},
        ],
        "send_to_targets": [
            {"label": "Consignment Out", "doc_type": "memo", "statuses": ["out"]},
        ],
    },
    "migrations": None,
    "requires": [],
}
