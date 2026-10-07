# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Shared constants for the docs module."""

from celerp.services.doc_balance import MEMO_LIVE_STATUSES

# Per-doc-type allowlist: maps doc_type → set of statuses where fulfill-lines is permitted.
# Only doc types listed here support the fulfill-lines / revert-lines endpoints.
# Adding a new status requires an explicit decision per doc type (true-predicate design).
# Inbound doc types (bill, consignment_in) are intentionally excluded: receiving goods
# is handled by POST /receive (creates parcels). fulfill-lines is outbound-only.
# UI counterpart: ui/routes/documents.py _fin_show_fulfill — keep in sync manually (different package).
FULFILLABLE_STATUSES: dict[str, frozenset[str]] = {
    "memo":    MEMO_LIVE_STATUSES,
    "invoice": frozenset({"sent", "final", "partial", "paid", "awaiting_payment"}),
}

# Per-doc-type allowlist for the ledger-neutral reserve-lines action ("Set as reserved" and
# "Set as available" on a reserved line). Reserve never draws stock or posts COGS, so it is
# permitted on the same customer-facing outbound docs and live statuses as Set-as-shipped - it is
# a distinct named map (the reserve wrapper gates on it) that today mirrors the fulfillable set.
# List docs reserve via a separate list-type predicate in routes.py, never through this map.
RESERVABLE_DOC_STATUSES: dict[str, frozenset[str]] = dict(FULFILLABLE_STATUSES)

# Item statuses that indicate a line item has been fulfilled.
# Used by revert-to-draft guard (Fix 1) and line-delete guard (Fix 3).
FULFILLED_ITEM_STATUSES: frozenset[str] = frozenset({"sold", "memo_out"})

# Doc types where goods are received via POST /receive (creates inventory parcels).
# These docs must NOT use fulfill-lines / revert-lines — those endpoints are outbound-only.
# Revert-to-draft for these types allows additional statuses (received, partially_received).
INBOUND_DOC_TYPES: frozenset[str] = frozenset({"consignment_in", "bill"})

# Doc types that are subscription templates (not fulfillable, not part of normal doc counters).
# These are recurring template docs - they should never show a fulfill button.
TEMPLATE_DOC_TYPES: frozenset[str] = frozenset({"subscription_invoice", "subscription_po"})

# Document types whose line prices are the company's own selling prices, so a
# price that differs from its reference needs set_sales_doc_prices. Purchase and
# receiving documents carry the supplier's prices and are never gated by it; the
# list-side counterpart is list_behavior.is_money_list.
SALES_PRICED_DOC_TYPES: frozenset[str] = frozenset({
    "invoice", "proforma", "quotation", "credit_note", "memo", "subscription_invoice",
})

# Purchase-side document types: their contact is a vendor (vendor or both), every
# other document's contact is a customer (customer or both).
VENDOR_DOC_TYPES: frozenset[str] = frozenset({"purchase_order", "bill", "consignment_in", "subscription_po"})

# Older records named their counterparty customer_id/customer_name (transfers: receiver).
# Writes take them as the contact fields, and replay folds stored ones the same way.
LEGACY_CONTACT_FIELDS: dict[str, str] = {
    "customer_id": "contact_id", "customer_name": "contact_name", "receiver": "contact_name",
}

# State that only lifecycle operations write: finalize, send, payment, receive,
# fulfil, convert, close and void, plus the record identity the ledger assigns.
# Ordinary creation never carries any of it, since a new document or list is an
# unpaid draft, and import-upsert never rewrites it. Only the snapshot import
# routes bring in an issued record, behind their own permission checks.
LIFECYCLE_OWNED_FIELDS: frozenset[str] = frozenset({
    "status", "finalized", "amount_paid", "amount_outstanding", "payments",
    "sent_to", "sent_via", "finalized_at", "sent_at", "issued_at", "accepted_at",
    "received_items", "received_item_ids", "returned_items", "return_received_items",
    "fulfilled_items", "fulfillment_status", "fulfilled_at", "fulfilled_by", "fulfill_cycle",
    "converted_to", "converted_to_type", "source_po_ref", "source_proforma_ref", "linked",
    "result", "close_reason", "void_reason", "revert_count", "files",
    "pre_close_status", "pre_void_status", "pre_void_fulfillment", "pre_receipt_status",
    "entity_type", "company_id", "doc_number",
})


# Document types that can be shared by link and imported by the recipient. One
# set serves the Share button, the share API, the share page's Import link and
# the importer, so nothing can be shared that the other side cannot import. A
# quotation list travels as a quotation.
SHAREABLE_DOC_TYPES: frozenset[str] = frozenset({
    "invoice", "quotation", "proforma", "purchase_order",
    "credit_note", "bill", "memo", "consignment_in",
})


def share_doc_type(entity_type: str, state: dict) -> str | None:
    """The doc_type a document or list is shared and imported as."""
    if entity_type == "list":
        return "quotation" if state.get("list_type") in ("quote", "quotation") else None
    return state.get("doc_type")


def is_shareable(entity_type: str, state: dict) -> bool:
    return share_doc_type(entity_type, state) in SHAREABLE_DOC_TYPES


# Doc types where Send and Mark as Sent must be hidden entirely.
# Bills and consignment_in are internal receiving documents - never sent to external parties.
# Purchase orders are outbound to vendors and DO need send/mark-as-sent.
# Production orders are internal demand (invoice-to-self) - never sent to a customer.
NO_SEND_DOC_TYPES: frozenset[str] = frozenset({"bill", "consignment_in", "purchase_order", "production_order"})

# Internal demand documents: not a sale/purchase, so excluded from revenue/AR/AP/sales accounting.
# A production order is "invoice-to-self" demand that feeds the manufacturing queue.
NON_FINANCIAL_DOC_TYPES: frozenset[str] = frozenset({"production_order"})

# Statuses where Send is suppressed even for sendable doc types. A closed memo is
# settled paperwork: re-sending it would silently un-close it, so Send is hidden.
NO_SEND_STATUSES: frozenset[str] = frozenset({"paid", "void", "closed"})

# Account classes a write-off may post to: expense for spoilage, samples and shrinkage, equity for
# owner drawings and family use. Never cogs: cost of sales belongs to sold stock alone. Shared by the
# API check and the UI picker so the two never diverge.
WRITEOFF_ACCOUNT_TYPES: frozenset[str] = frozenset({"expense", "equity"})
