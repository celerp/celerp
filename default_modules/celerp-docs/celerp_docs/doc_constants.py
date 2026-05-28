# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1
"""Shared constants for the docs module."""

# Per-doc-type allowlist: maps doc_type → set of statuses where fulfill-lines is permitted.
# Only doc types listed here support the fulfill-lines / revert-lines endpoints.
# Adding a new status requires an explicit decision per doc type (true-predicate design).
# UI counterpart: ui/routes/documents.py _fin_show_fulfill — keep in sync manually (different package).
FULFILLABLE_STATUSES: dict[str, frozenset[str]] = {
    "memo":           frozenset({"sent", "final", "partial", "received", "partially_received", "partial_returned"}),
    "invoice":        frozenset({"sent", "final", "partial", "paid", "awaiting_payment"}),
    "consignment_in": frozenset({"sent", "final", "received", "partially_received"}),
}

# Item statuses that indicate a line item has been fulfilled.
# Used by revert-to-draft guard (Fix 1) and line-delete guard (Fix 3).
FULFILLED_ITEM_STATUSES: frozenset[str] = frozenset({"sold", "memo_out"})

# Doc types where fulfillment means goods *arrive* (inbound flow).
# For these, fulfilling a doc must NOT deduct inventory - items already exist
# with correct quantity from the receive step. Fulfillment just marks the doc complete.
INBOUND_DOC_TYPES: frozenset[str] = frozenset({"consignment_in"})

# Doc types that are subscription templates (not fulfillable, not part of normal doc counters).
# These are recurring template docs - they should never show a fulfill button.
TEMPLATE_DOC_TYPES: frozenset[str] = frozenset({"subscription_invoice", "subscription_po"})

# Doc types where Send and Mark as Sent must be hidden entirely.
# Bills and consignment_in are internal receiving documents - never sent to external parties.
# Purchase orders are outbound to vendors and DO need send/mark-as-sent.
NO_SEND_DOC_TYPES: frozenset[str] = frozenset({"bill", "consignment_in", "purchase_order"})

# Statuses where Send is suppressed even for sendable doc types.
NO_SEND_STATUSES: frozenset[str] = frozenset({"paid", "void"})
