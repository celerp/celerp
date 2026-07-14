# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""celerp-docs module: document lifecycle (invoices, POs, quotations, credit notes, etc.) and lists."""

PLUGIN_MANIFEST = {
    "name": "celerp-docs",
    "version": "1.0.0",
    "display_name": "Documents",
    "description": "Document lifecycle: invoices, purchase orders, quotations, credit notes, and lists.",
    "license": "MIT",
    "author": "Celerp",
    "api_routes": "celerp_docs.api_setup",
    "ui_routes": "celerp_docs.ui_routes",
    "depends_on": ["celerp-inventory", "celerp-contacts"],
    "slots": {
        "nav": [
            {"group": "Sales Documents", "key": "invoices", "href": "/docs?type=invoice", "label": "Invoices", "label_key": "nav.invoices", "order": 20, "settings_href": "/settings/sales", "min_role": "operator"},
            {"group": "Sales Documents", "key": "credit-notes", "href": "/docs?type=credit_note", "label": "Credit Notes", "label_key": "nav.credit_notes", "order": 20.8, "min_role": "operator"},
            {"group": "Sales Documents", "key": "memos", "href": "/docs?type=memo", "label": "Consignment Out", "label_key": "nav.consignment_out", "order": 20.5, "min_role": "operator"},
            {"group": "Sales Documents", "key": "lists", "href": "/lists", "label": "Lists / Quotations", "label_key": "nav.lists_quotations", "order": 21, "min_role": "operator"},
            {"group": "Purchasing Documents", "key": "purchase-orders", "href": "/docs?type=purchase_order", "label": "Purchase Orders", "label_key": "nav.purchase_orders", "order": 26, "settings_href": "/settings/purchasing", "min_role": "operator"},
            {"group": "Purchasing Documents", "key": "vendor-bills", "href": "/docs?type=bill", "label": "Vendor Bills", "label_key": "nav.vendor_bills", "order": 26.5, "min_role": "operator"},
            {"group": "Purchasing Documents", "key": "consignment-in", "href": "/docs?type=consignment_in", "label": "Consignment In", "label_key": "nav.consignment_in", "order": 27, "min_role": "operator"},
            {"group": "Finance", "key": "payments", "href": "/payments", "label": "Payments", "label_key": "nav.payments", "order": 51.5, "min_role": "manager"},
        ],
        "projection_handler": [
            {"prefix": "doc.", "handler": "celerp_docs.doc_projections:apply_documents_event"},
            {"prefix": "list.", "handler": "celerp_docs.doc_projections:apply_documents_event"},
        ],
        "send_to_targets": [
            {"label": "Invoice", "doc_type": "invoice", "statuses": ["draft", "awaiting_payment"]},
            {"label": "Consignment Out", "doc_type": "memo", "statuses": ["draft"]},
            {"label": "List/Quotation", "doc_type": "list", "statuses": ["draft", "sent"]},
            {"label": "Shipping Document", "doc_type": "shipping_doc", "statuses": ["draft", "sent"]},
            {"label": "Purchase Order", "doc_type": "purchase_order", "statuses": ["draft"]},
        ],
    },
    "migrations": None,
    "requires": [],
    "default_enabled": True,
}
