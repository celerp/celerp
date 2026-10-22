# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Shared constants for the docs module."""

from collections.abc import Callable
from decimal import Decimal

from celerp.services.doc_balance import MEMO_LIVE_STATUSES
from celerp.services.list_behavior import SCAN_RUNS_FIELD

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

# Per-doc-type allowlist for revert-lines (Set as available on shipped lines): the statuses
# in which goods this document shipped may be taken back. Draft, void, closed and converted
# documents are terminal for it. A distinct named map, today equal to the fulfillable set.
REVERTIBLE_STATUSES: dict[str, frozenset[str]] = dict(FULFILLABLE_STATUSES)

# Doc types where goods are received via POST /receive (creates inventory parcels).
# These docs must NOT use fulfill-lines / revert-lines — those endpoints are outbound-only.
# Revert-to-draft for these types allows additional statuses (received, partially_received).
INBOUND_DOC_TYPES: frozenset[str] = frozenset({"consignment_in", "bill"})

# Per-doc-type allowlist of the statuses POST /receive takes goods in. An issued purchase
# document receives in every status its payments and earlier receipts or returns can move it
# to; a void, closed or converted one receives nothing. A purchase order may receive while
# still a draft (its receipt books its own entry), and so may a consignment (consigned goods
# are not owned, so their receipt books nothing); a draft bill has not been issued, so it
# receives nothing. The finalized view offers Receive Goods from the same map.
_RECEIVING_STATUSES = frozenset({
    "final", "sent", "awaiting_payment", "partial", "paid",
    "received", "partially_received", "partial_returned", "returned",
})
RECEIVABLE_STATUSES: dict[str, frozenset[str]] = {
    "bill": _RECEIVING_STATUSES,
    "consignment_in": _RECEIVING_STATUSES | {"draft"},
    "purchase_order": _RECEIVING_STATUSES | {"draft"},
}

# Statuses each document type sends goods back to its supplier in (POST /return-items): once
# goods have come in on it, and until all of them have gone back. A bill or purchase order
# paid in part or in full returns goods too: what was paid for them becomes a supplier credit.
_RETURN_OPEN = frozenset({"received", "partially_received", "partial_returned", "awaiting_payment"})
SUPPLIER_RETURN_STATUSES: dict[str, frozenset[str]] = {
    "consignment_in": _RETURN_OPEN,
    "bill": _RETURN_OPEN | {"partial", "paid"},
    "purchase_order": _RETURN_OPEN | {"partial", "paid"},
}

# Per-doc-type statuses POST /docs/{id}/convert takes a document in. A quotation converts in
# every status but QUOTATION_SPENT_STATUSES.
CONVERTIBLE_STATUSES: dict[str, frozenset[str]] = {
    "memo": frozenset({"final", "sent", "received", "partially_received", "converted"}),
    "consignment_in": frozenset({"final", "sent", "received", "partially_received", "partial_returned", "returned"}),
}
QUOTATION_SPENT_STATUSES = frozenset({"void", "converted"})


def sent_out_by(item_state: dict, doc_id: str) -> bool:
    """Whether document ``doc_id`` sent these goods out (sold or out on memo): it
    fulfilled them, or holds them as their status document."""
    return item_state.get("status") in ("sold", "memo_out") and (
        doc_id in (item_state.get("fulfilled_for_docs") or []) or item_state.get("status_doc_id") == doc_id)


def quantities_still_moving(state: dict, shipped: Callable[[dict], bool]) -> list[tuple[dict, float]]:
    """Each line of a document with the quantity on it that can still be received, shipped or
    carried into a converted document. A draft and an open quotation carry their whole lines, since
    converting a quotation copies them. A document that receives carries what each line has not
    received yet; one that ships carries the lines whose goods have not gone out (``shipped``),
    and nothing once all of them have. A subscription template carries its whole lines until it
    is cancelled, since it keeps making documents from them. What came in or went out, even if it
    can still be sent back or converted (a memo bills what is out on it, a consignment buys what
    was kept), sits in its item, so a document that can only do that carries nothing. Any other
    document, a converted one among them, is history."""
    doc_type, status = state.get("doc_type"), state.get("status")
    lines = [line for line in state.get("line_items") or [] if isinstance(line, dict)]
    if (doc_type == "quotation" and status not in QUOTATION_SPENT_STATUSES) \
            or (doc_type in TEMPLATE_DOC_TYPES and status != "cancelled"):
        return [(line, line.get("quantity")) for line in lines]
    if status in RECEIVABLE_STATUSES.get(doc_type, ()):
        return [(line, left) for line in lines if (left := _unreceived(line)) > 0]
    if status in FULFILLABLE_STATUSES.get(doc_type, ()):
        if state.get("fulfillment_status") == "fulfilled":
            return []
        return [(line, line.get("quantity")) for line in lines if not shipped(line)]
    if status in (None, "draft"):
        return [(line, line.get("quantity")) for line in lines]
    return []


def _unreceived(line: dict) -> float:
    ordered, received = (Decimal(str(float(line.get(key) or 0))) for key in ("quantity", "quantity_received"))
    return float(ordered - received)


# The payment methods that record a credit note applied to a document rather than money:
# the receiving side names its source (an invoice's "credit_note", a bill's
# "supplier_credit") and the note's own side is "applied". A pair moves no cash, so it is
# never refunded or deleted, only voided as a pair.
CREDIT_APPLICATION_METHODS: frozenset[str] = frozenset({"credit_note", "applied", "supplier_credit"})

# Doc types that are subscription templates (not fulfillable, not part of normal doc counters).
# These are recurring template docs - they should never show a fulfill button.
TEMPLATE_DOC_TYPES: frozenset[str] = frozenset({"subscription_invoice", "subscription_po"})


def template_label(state: dict, entity_id: str = "") -> str:
    """A subscription template's name as the subscriptions list shows it: its reference, else its
    number, else its name, else the last part of its id. A template made with New subscription
    has a reference and no name."""
    return state.get("ref_id") or state.get("doc_number") or state.get("name") or entity_id.split(":")[-1]

# Document types whose line prices are the company's own selling prices, so a
# price that differs from its reference needs set_sales_doc_prices. Purchase and
# receiving documents carry the supplier's prices and are never gated by it; the
# list-side counterpart is list_behavior.is_money_list.
SALES_PRICED_DOC_TYPES: frozenset[str] = frozenset({
    "invoice", "proforma", "quotation", "credit_note", "memo", "subscription_invoice",
})

# Older records named their counterparty customer_id/customer_name (transfers: receiver).
# Writes take them as the contact fields, and replay folds stored ones the same way.
LEGACY_CONTACT_FIELDS: dict[str, str] = {
    "customer_id": "contact_id", "customer_name": "contact_name", "receiver": "contact_name",
}

# State that only lifecycle operations write: finalize, send, payment, credit,
# receive, fulfil, convert, close, void and scan, plus the record identity the ledger assigns.
# Ordinary creation never carries any of it, since a new document or list is an
# unpaid draft, and import-upsert never rewrites it. Only the snapshot import
# routes bring in an issued record, behind their own permission checks.
LIFECYCLE_OWNED_FIELDS: frozenset[str] = frozenset({
    "status", "finalized", "amount_paid", "amount_outstanding", "payments", "credited",
    "sent_to", "sent_via", "finalized_at", "sent_at", "issued_at", "accepted_at",
    "received_items", "received_item_ids", "returned_items", "returned_credit", "supplier_credit", "return_received_items",
    "fulfilled_items", "fulfillment_status", "fulfilled_at", "fulfilled_by", "fulfill_cycle",
    "converted_to", "converted_to_type", "source_po_ref", "source_proforma_ref", "source_memo_id", "linked",
    "result", "close_reason", "void_reason", "revert_count", "files",
    "pre_close_status", "pre_void_status", "pre_void_fulfillment", "pre_receipt_status", "pre_convert_status",
    "entity_type", "company_id", "doc_number", SCAN_RUNS_FIELD,
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
