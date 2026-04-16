# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1
"""Shared constants for the docs module."""

# Statuses that allow physical fulfillment
FULFILLABLE_STATUSES: frozenset[str] = frozenset({"final", "sent", "awaiting_payment", "partial"})
