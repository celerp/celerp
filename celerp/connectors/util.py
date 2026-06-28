# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Shared helpers for platform connectors."""
from __future__ import annotations


def money(v) -> float | None:
    """Parse a platform money value to float.

    Returns None only when the value is genuinely absent (missing/null/empty) or
    unparseable. A real price of 0 stays 0.0 - never collapsed to None (a free or
    zero-priced item is a valid price, not a missing one).
    """
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
