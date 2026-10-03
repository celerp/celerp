# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""The one rule every writer applies to a lot's goods cost: it is never negative.

Routes, imports and migration sinks call negative_cost_error to refuse a negative
cost in their own response shape before they write anything. The event boundary
applies the same check to every item event that sets a goods cost, so a writer
that skips it still cannot store one.
"""
from __future__ import annotations

from typing import Any

GOODS_COST_KEYS = ("cost_price", "cost_total", "cost_base")

# Item events whose data carries the lot's goods cost at the top level.
_COST_SETTING_EVENTS = frozenset((
    "item.created", "item.snapshot", "item.patched", "item.quantity.adjusted", "item.cost_adjusted",
))


def lot_label(state: dict, entity_id: str) -> str:
    """How a refusal names a lot: its SKU, else its name, else its id."""
    return str(state.get("sku") or state.get("name") or entity_id)


def negative_cost_error(label: str, *costs: Any) -> str | None:
    """The refusal for any of ``costs`` below zero, naming the lot, or None.

    Blank values are no cost, and a value that is not a number is left to the
    caller's own type check."""
    for value in costs:
        if value is None or value == "":
            continue
        try:
            negative = float(value) < 0
        except (TypeError, ValueError):
            continue
        if negative:
            return f"{label}: a cost cannot be negative"
    return None


def event_goods_costs(event_type: str, data: dict) -> list:
    """The goods-cost values an item event writes."""
    if event_type == "item.updated":
        changes = data.get("fields_changed") or {}
        return [(changes.get(key) or {}).get("new") for key in GOODS_COST_KEYS if key in changes]
    if event_type == "item.pricing.set":
        return [data.get("new_price")] if data.get("price_type") in GOODS_COST_KEYS else []
    if event_type in _COST_SETTING_EVENTS:
        return [data.get(key) for key in GOODS_COST_KEYS]
    return []
