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
            {"group": "Sales Documents", "key": "invoices", "href": "/docs?type=invoice", "help": "docs.sales-documents", "label": "Invoices", "label_key": "nav.invoices", "order": 20, "settings_href": "/settings/sales", "settings_help": "docs.settings", "permission": "view_documents"},
            {"group": "Sales Documents", "key": "credit-notes", "href": "/docs?type=credit_note", "help": "docs.sales-documents", "label": "Credit Memos", "label_key": "nav.credit_notes", "order": 20.8, "permission": "view_documents"},
            {"group": "Sales Documents", "key": "memos", "href": "/docs?type=memo", "help": "docs.memos", "label": "Consignment Out", "label_key": "nav.consignment_out", "order": 20.5, "permission": "view_documents"},
            {"group": "Sales Documents", "key": "lists", "href": "/lists", "help": "docs.lists-quotations", "label": "Quotations", "label_key": "nav.lists_quotations", "order": 21, "permission": "view_documents"},
            {"group": "Purchasing Documents", "key": "received", "href": "/docs/received", "help": "docs.received", "label": "Shared Documents", "label_key": "nav.received_documents", "order": 25.5, "permission": "view_documents"},
            {"group": "Purchasing Documents", "key": "purchase-orders", "href": "/docs?type=purchase_order", "help": "docs.purchasing-documents", "label": "Purchase Orders", "label_key": "nav.purchase_orders", "order": 26, "settings_href": "/settings/purchasing", "settings_help": "docs.settings", "permission": "view_documents"},
            {"group": "Purchasing Documents", "key": "vendor-bills", "href": "/docs?type=bill", "help": "docs.purchasing-documents", "label": "Vendor Bills", "label_key": "nav.vendor_bills", "order": 26.5, "permission": "view_documents"},
            {"group": "Purchasing Documents", "key": "supplier-credit-notes", "href": "/docs?type=debit_note", "help": "docs.purchasing-documents", "label": "Vendor Credit Memos", "label_key": "nav.supplier_credit_notes", "order": 26.8, "permission": "view_documents"},
            {"group": "Purchasing Documents", "key": "consignment-in", "href": "/docs?type=consignment_in", "help": "docs.purchasing-documents", "label": "Consignment In", "label_key": "nav.consignment_in", "order": 27, "permission": "view_documents"},
            {"group": "Finance", "key": "payments", "href": "/payments", "help": "docs.payments", "label": "Online Payments", "label_key": "nav.payments", "order": 51.5, "permission": "view_payments"},
        ],
        "on_modules_ready": [
            {"handler": "celerp_docs.received_legacy:move_legacy_imports_hook"},
            {"handler": "celerp_docs.legacy_receipts:record_legacy_receipts_hook"},
            {"handler": "celerp_docs.historical_lots:link_historical_lots_hook"},
            {"handler": "celerp_docs.imported_cutover:imported_cutover_hook"},
            {"handler": "celerp_docs.customs_freeze:customs_freeze_hook"},
            {"handler": "celerp_docs.legacy_credit_notes:legacy_credit_notes_hook"},
        ],
        "background_loop": {"handler": "celerp_docs.periodic:loop"},
        "projection_handler": [
            {"prefix": "doc.", "handler": "celerp_docs.doc_projections:apply_documents_event", "permission": "view_documents"},
            {"prefix": "list.", "handler": "celerp_docs.doc_projections:apply_documents_event", "permission": "view_documents"},
            {"prefix": "received_doc.", "handler": "celerp_docs.received:apply_received_event", "permission": "view_documents"},
        ],
        "send_to_targets": [
            {"label_key": "settings.doc_type_invoice", "doc_type": "invoice", "statuses": ["draft", "awaiting_payment"]},
            {"label_key": "settings.doc_type_memo", "doc_type": "memo", "statuses": ["draft"]},
            {"label_key": "enum.doc_type.list", "doc_type": "list", "statuses": ["draft", "sent"]},
            {"label_key": "settings_sales.doc_type_shipping_doc", "doc_type": "shipping_doc", "statuses": ["draft", "sent"]},
            {"label_key": "settings.doc_type_purchase_order", "doc_type": "purchase_order", "statuses": ["draft"]},
        ],
        "search_provider": {
            "handler": "celerp_docs.search:global_search",
            "result_key": "items",
            "permission": "view_documents",
        },
    },
    "migrations": None,
    "default_enabled": True,
    "help": {"dir": "celerp_docs/help"},
}
