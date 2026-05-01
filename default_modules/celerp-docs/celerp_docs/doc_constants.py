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
