# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Shared constants for the docs module."""

# Per-doc-type allowlist: maps doc_type → set of statuses where fulfill-lines is permitted.
# Only doc types listed here support the fulfill-lines / revert-lines endpoints.
# Adding a new status requires an explicit decision per doc type (true-predicate design).
# Inbound doc types (bill, consignment_in) are intentionally excluded: receiving goods
# is handled by POST /receive (creates parcels). fulfill-lines is outbound-only.
# UI counterpart: ui/routes/documents.py _fin_show_fulfill — keep in sync manually (different package).
FULFILLABLE_STATUSES: dict[str, frozenset[str]] = {
    "memo":    frozenset({"sent", "final", "partial", "received", "partially_received", "partial_returned"}),
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

# Doc types where Send and Mark as Sent must be hidden entirely.
# Bills and consignment_in are internal receiving documents - never sent to external parties.
# Purchase orders are outbound to vendors and DO need send/mark-as-sent.
# Production orders are internal demand (invoice-to-self) - never sent to a customer.
NO_SEND_DOC_TYPES: frozenset[str] = frozenset({"bill", "consignment_in", "purchase_order", "production_order"})

# Internal demand documents: not a sale/purchase, so excluded from revenue/AR/AP/sales accounting.
# A production order is "invoice-to-self" demand that feeds the manufacturing queue.
NON_FINANCIAL_DOC_TYPES: frozenset[str] = frozenset({"production_order"})

# Statuses where Send is suppressed even for sendable doc types.
NO_SEND_STATUSES: frozenset[str] = frozenset({"paid", "void"})
