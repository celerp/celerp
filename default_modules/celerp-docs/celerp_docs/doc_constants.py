# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1
"""Shared constants for the docs module."""

# Statuses where the Fulfill button is hidden (denylist - all others show the button)
# Revert Fulfillment has no status restriction - it shows whenever fulfillment_status == "fulfilled"
UNFULFILLABLE_STATUSES: frozenset[str] = frozenset({"draft", "void"})

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
NO_SEND_DOC_TYPES: frozenset[str] = frozenset({"bill", "consignment_in"})

# Statuses where Send is suppressed even for sendable doc types.
NO_SEND_STATUSES: frozenset[str] = frozenset({"paid", "void"})
