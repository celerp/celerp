# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""FIFO/FEFO pick algorithm — pure function, zero side effects.

Input:  line_items  — from doc, each has {sku, quantity}
        inventory   — available items [{entity_id, sku, quantity, created_at, expires_at, cost_price}]
Output: pick_plan   — [{item_id, sku, pick_qty, cost_price, action: "full"|"split"}]
                      (a split child keeps the parent SKU; lots differ by barcode/entity_id)
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


@dataclass
class PickResult:
    picks: list[PickLine] = field(default_factory=list)
    unfulfilled: list[dict[str, Any]] = field(default_factory=list)  # [{sku, short_qty}]
    strategy: str = "fifo"


def _matches_sku(item_sku: str, line_sku: str) -> bool:
    """Exact match, or a LEGACY digit-suffixed split child ('ITEM-001.1' matches
    'ITEM-001'). New split children keep the parent SKU, so the suffix case only covers
    pre-existing '.N' lots; a distinct product like 'ITEM-001.PRO' is NOT matched."""
    if item_sku == line_sku:
        return True
    if item_sku.startswith(f"{line_sku}."):
        return item_sku[len(line_sku) + 1:].isdigit()
    return False


VALID_PICK_METHODS = ("fifo", "fefo", "lifo")


def _detect_strategy(inventory: list[dict]) -> str:
    """FEFO if any item has expires_at, otherwise FIFO. Used as the auto fallback
    when no explicit strategy is configured."""
    return "fefo" if any(item.get("expires_at") for item in inventory) else "fifo"


def resolve_pick_method(item_state: dict | None, company_settings: dict | None) -> str:
    """Effective stock-cutting order for a product: the item's own ``pick_method``
    (when set and not "default"), else the company ``inventory_method``, else "fifo".

    This decides only the DRAW ORDER across lots (which physical lots are consumed
    first). COGS is always the actual cost of the specific lots drawn (specific
    identification by lot), so the order naturally yields FIFO-cost / LIFO-cost.
    """
    item_state = item_state or {}
    company_settings = company_settings or {}
    for candidate in (item_state.get("pick_method"), company_settings.get("inventory_method")):
        c = str(candidate or "").strip().lower()
        if c in VALID_PICK_METHODS:
            return c
    return "fifo"


def _sorted_inventory(inventory: list[dict], strategy: str) -> list[dict]:
    """Order lots for draw-down by the chosen strategy.

    - fifo: oldest received first (created_at ascending)
    - lifo: newest received first (created_at descending)
    - fefo: soonest to expire first (expires_at ascending, created_at tiebreak);
      lots with no expiry fall to the end, so FEFO degrades to FIFO when nothing expires
    """
    if strategy == "lifo":
        return sorted(inventory, key=lambda it: it.get("created_at") or "", reverse=True)
    if strategy == "fefo":
        return sorted(inventory, key=lambda it: (it.get("expires_at") or "9999-99-99", it.get("created_at") or ""))
    return sorted(inventory, key=lambda it: it.get("created_at") or "")  # fifo


def compute_pick_plan(
    line_items: list[dict],
    inventory: list[dict],
    strategy: str | None = None,
) -> PickResult:
    """Compute a pick plan for the given line items against available inventory.

    Pure function — no side effects, no DB access, no event emission.

    Args:
        line_items: [{sku, quantity, sell_by?, ...}] from the document.
        inventory:  [{entity_id, sku, quantity, created_at, expires_at, cost_price}]
                    Only items with quantity > 0 should be passed.
        strategy:   explicit draw order ("fifo"|"fefo"|"lifo"); when None, auto-detect
                    (FEFO if anything expires, else FIFO).

    Returns:
        PickResult with picks, unfulfilled shortfalls, and strategy used.
    """
    strategy = strategy if strategy in VALID_PICK_METHODS else _detect_strategy(inventory)

    # Build a mutable copy of inventory keyed by entity_id for tracking remaining qty
    remaining: dict[str, float] = {item["entity_id"]: float(item.get("quantity") or 0) for item in inventory}

    # Pre-sort inventory once
    sorted_inv = _sorted_inventory(inventory, strategy)

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

            # Prefer per-unit cost_price when available; fall back to cost_total / qty.
            if item.get("cost_price") is not None:
                cost = float(item["cost_price"])
            else:
                cost_total = float(item.get("cost_total") or 0)
                item_qty = float(item.get("quantity") or 1)
                cost = cost_total / item_qty if item_qty else 0.0
            allow_split = item.get("allow_splitting", True)

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
            elif allow_split:
                # Partial pick — split off `needed`; the child KEEPS the parent SKU
                # (same product, distinct lot by barcode/entity_id).
                picks.append(PickLine(
                    item_id=eid,
                    sku=item.get("sku", ""),
                    pick_qty=needed,
                    cost_price=cost,
                    action="split",
                ))
                remaining[eid] -= needed
                needed = 0
            # else: splitting not allowed — skip this item, keep looking for another

        if needed > 1e-9:
            unfulfilled.append({"sku": line_sku, "short_qty": round(needed, 6)})

    return PickResult(picks=picks, unfulfilled=unfulfilled, strategy=strategy)
