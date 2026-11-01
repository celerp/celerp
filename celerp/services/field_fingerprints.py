# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Fingerprints of the product fields a store link last agreed on.

A store link keeps, per field, a fingerprint of the value both sides last held: the
value last imported from the store or last sent to it. A side whose value no longer
matches the fingerprint changed it since. Inbound applies a field only when the store
changed it; outbound sends a field only when Celerp changed it. A missing fingerprint
counts as changed, never as unchanged.
"""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal, InvalidOperation

# Item fields a store link carries, by their item state key.
PRODUCT_FIELDS = ("sku", "name", "description", "sale_price")
LINK_KEY = "field_fingerprints"


def _normal(value):
    if value is None:
        return ""
    if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        try:
            return str(Decimal(str(value)).normalize())
        except InvalidOperation:
            return str(value)
    if isinstance(value, str):
        return value.strip()
    return value


def fingerprint(value) -> str:
    """A short stable fingerprint of one field value."""
    raw = json.dumps(_normal(value), sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def changed_since(link: dict, field: str, value) -> bool:
    """Whether ``value`` differs from what the link last agreed on for ``field``."""
    last = (link.get(LINK_KEY) or {}).get(field)
    return last is None or last != fingerprint(value)
