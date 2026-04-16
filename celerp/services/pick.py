# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

"""FIFO/FEFO pick algorithm — pure function, zero side effects.

Input:  line_items  — from doc, each has {sku, quantity}
        inventory   — available items [{entity_id, sku, quantity, created_at, expires_at, cost_price}]
Output: pick_plan   — [{item_id, sku, pick_qty, cost_price, action: "full"|"split", split_sku}]
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class PickLine:
    item_id: str
    sku: str
    pick_qty: float
    cost_price: float
    action: str          # "full" | "split"
    split_sku: str = ""  # populated only when action="split"


@dataclass
class PickResult:
    picks: list[PickLine] = field(default_factory=list)
    unfulfilled: list[dict[str, Any]] = field(default_factory=list)  # [{sku, short_qty}]
    strategy: str = "fifo"


def _matches_sku(item_sku: str, line_sku: str) -> bool:
    """Exact match OR child SKU (e.g. 'ITEM-001.1' matches parent 'ITEM-001')."""
    return item_sku == line_sku or item_sku.startswith(f"{line_sku}.")


def _detect_strategy(inventory: list[dict]) -> str:
    """FEFO if any item has expires_at, otherwise FIFO."""
    return "fefo" if any(item.get("expires_at") for item in inventory) else "fifo"


def _sort_key(item: dict, strategy: str):
    if strategy == "fefo":
        return item.get("expires_at") or "9999-99-99", item.get("created_at") or ""
    return item.get("created_at") or ""


def compute_pick_plan(
    line_items: list[dict],
    inventory: list[dict],
) -> PickResult:
    """Compute a pick plan for the given line items against available inventory.

    Pure function — no side effects, no DB access, no event emission.

    Args:
        line_items: [{sku, quantity, sell_by?, ...}] from the document.
        inventory:  [{entity_id, sku, quantity, created_at, expires_at, cost_price}]
                    Only items with quantity > 0 should be passed.

    Returns:
        PickResult with picks, unfulfilled shortfalls, and strategy used.
    """
    strategy = _detect_strategy(inventory)

    # Build a mutable copy of inventory keyed by entity_id for tracking remaining qty
    remaining: dict[str, float] = {item["entity_id"]: float(item.get("quantity") or 0) for item in inventory}

    # Pre-sort inventory once
    sorted_inv = sorted(inventory, key=lambda it: _sort_key(it, strategy))

    picks: list[PickLine] = []
    unfulfilled: list[dict[str, Any]] = []

    for line in line_items:
        line_sku = line.get("sku") or ""
        line_qty = float(line.get("quantity") or 0)

        # Service items skip physical fulfillment
        sell_by = line.get("sell_by") or ""
        if sell_by in ("service", "hour"):
            continue

        if line_qty <= 0 or not line_sku:
            continue

        needed = line_qty

        # Find matching inventory items (exact + child prefix)
        matching = [item for item in sorted_inv if _matches_sku(item.get("sku", ""), line_sku)]

        for item in matching:
            if needed <= 1e-9:
                break
            eid = item["entity_id"]
            avail = remaining.get(eid, 0)
            if avail <= 1e-9:
                continue

            cost = float(item.get("cost_price") or 0)

            if avail <= needed + 1e-9:
                # Take entire item
                picks.append(PickLine(
                    item_id=eid,
                    sku=item.get("sku", ""),
                    pick_qty=avail,
                    cost_price=cost,
                    action="full",
                ))
                needed -= avail
                remaining[eid] = 0
            else:
                # Partial — need to split
                picks.append(PickLine(
                    item_id=eid,
                    sku=item.get("sku", ""),
                    pick_qty=needed,
                    cost_price=cost,
                    action="split",
                    split_sku=f"{item.get('sku', '')}.{_next_child_suffix(item.get('sku', ''), inventory)}",
                ))
                remaining[eid] -= needed
                needed = 0

        if needed > 1e-9:
            unfulfilled.append({"sku": line_sku, "short_qty": round(needed, 6)})

    return PickResult(picks=picks, unfulfilled=unfulfilled, strategy=strategy)


def _next_child_suffix(parent_sku: str, inventory: list[dict]) -> int:
    """Determine the next available child suffix number for a parent SKU."""
    max_suffix = 0
    prefix = f"{parent_sku}."
    for item in inventory:
        item_sku = item.get("sku", "")
        if item_sku.startswith(prefix):
            tail = item_sku[len(prefix):]
            # Only consider direct children (no further dots)
            if "." not in tail:
                try:
                    n = int(tail)
                    max_suffix = max(max_suffix, n)
                except ValueError:
                    pass
    return max_suffix + 1
