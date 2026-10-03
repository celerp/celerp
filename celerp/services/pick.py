# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Lot draw order and allocation for sales lines - pure functions, zero side effects.

Lots are dicts carrying entity_id, sku, quantity, created_at and expires_at. The
draw order is fifo, fefo or lifo per product (resolve_pick_method); a split child
keeps the parent SKU, so lots differ by barcode/entity_id.
"""

from __future__ import annotations

from celerp.services.document_lines import line_item_id
from celerp.services.line_measures import splitting_allowed


VALID_PICK_METHODS = ("fifo", "fefo", "lifo")


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



def consolidate_sales_lots(items: list[dict], company_settings: dict) -> list[dict]:
    """Collapse splittable same-SKU lots to the canonical pick-order representative.

    The representative binds a sales line to the first stocked lot in FIFO/FEFO/LIFO
    order while exposing aggregate same-SKU quantity. Non-splittable products remain
    one option per physical unit so callers must choose the exact item.
    """
    by_sku: dict[str, list[dict]] = {}
    order: list[str] = []
    for item in items:
        sku = str(item.get("sku") or "").strip().casefold()
        if sku not in by_sku:
            by_sku[sku] = []
            order.append(sku)
        by_sku[sku].append(item)

    out: list[dict] = []
    for sku in order:
        group = by_sku[sku]
        if len(group) == 1 or not all(splitting_allowed(item) for item in group):
            out.extend(group)
            continue
        method = resolve_pick_method(group[0], company_settings)
        sorted_lots = _sorted_inventory(group, method)
        stocked = [item for item in sorted_lots if float(item.get("quantity") or 0) > 0]
        rep = dict((stocked or sorted_lots)[0])
        rep["quantity"] = sum(float(item.get("quantity") or 0) for item in group)
        out.append(rep)
    return out

def doc_bound_lots(line_items: list[dict]) -> set[str]:
    """The lots a document's lines reference directly.

    A lot bound by one line is that line's stock: it is never drawn as another
    line's spanning sibling, so one physical lot cannot satisfy two lines and a
    document allocates the same way whatever order its lines are processed in.
    """
    return {str(line_item_id(li)) for li in line_items if line_item_id(li)}

def plan_lot_draws(
    primary: dict,
    needed: float,
    siblings: list[dict],
    method: str,
) -> tuple[list[tuple[dict, float, bool]], float]:
    """Allocate ``needed`` units across lots: the primary (bound) lot first, then
    the siblings in the ``method`` draw order (fifo/fefo/lifo). Each draw is
    ``(lot, take_qty, is_full_lot)``; the second element of the result is the
    quantity no lot could cover (0.0 when stock suffices).

    Pure allocation arithmetic - eligibility (SKU match, status, reservation
    ownership) is the caller's job. Lots are dicts carrying entity_id, quantity,
    created_at and expires_at, as _sorted_inventory reads them.
    """
    draws: list[tuple[dict, float, bool]] = []
    remaining = float(needed)
    for lot in [primary] + _sorted_inventory(siblings, method):
        if remaining <= 1e-9:
            break
        avail = float(lot.get("quantity") or 0)
        if avail <= 1e-9:
            continue
        take = min(remaining, avail)
        draws.append((lot, take, abs(take - avail) <= 1e-9))
        remaining -= take
    return draws, max(0.0, remaining)
