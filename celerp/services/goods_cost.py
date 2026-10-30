# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""The rules every writer applies to a lot's goods cost: it is never negative, and
whether it is known is the app's own record.

Routes, imports and migration sinks call negative_cost_error to refuse a negative
cost in their own response shape before they write anything. The event boundary
applies the same check to every item event that sets a goods cost, so a writer
that skips it still cannot store one, and refuses the unknown-cost flag
(GOODS_COST_UNKNOWN) on any event that may not carry it (unknown_flag_error).
"""
from __future__ import annotations

from typing import Any

GOODS_COST_KEYS = ("cost_price", "cost_total", "cost_base")

# A lot whose value holds goods of unknown cost: the value is what the books carry,
# but no goods cost is known for it. Set only from this flag in an event's data.
GOODS_COST_UNKNOWN = "goods_cost_unknown"

# Item events whose data may carry GOODS_COST_UNKNOWN; every other item event is refused it.
UNKNOWN_FLAG_EVENTS = frozenset((
    "item.created", "item.snapshot", "item.updated", "item.pricing.set", "item.cost_adjusted",
    "item.quantity.adjusted",
))

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


def writes_goods_cost(event_type: str, data: dict) -> bool:
    """Whether an item event sets the lot's goods cost."""
    if event_type == "item.updated":
        return any(key in (data.get("fields_changed") or {}) for key in GOODS_COST_KEYS)
    if event_type == "item.pricing.set":
        return data.get("price_type") in GOODS_COST_KEYS
    return event_type in _COST_SETTING_EVENTS and any(key in data for key in GOODS_COST_KEYS)


def unknown_flag_error(event_type: str, data: dict) -> str | None:
    """The refusal for an item event carrying the unknown-cost flag it may not carry, or
    a goods cost in a patch (a patch never restates cost), else None."""
    if GOODS_COST_UNKNOWN in data and event_type not in UNKNOWN_FLAG_EVENTS:
        return f"{event_type} cannot record whether a goods cost is known"
    if event_type == "item.patched" and any(key in data for key in GOODS_COST_KEYS):
        return "a patch cannot set a goods cost"
    return None
