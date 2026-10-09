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

def as_lot(entity_id: str, created_at, state: dict, claim: str | None) -> dict:
    """The lot dict the planners read, from an item's id, creation time and state.
    ``claim`` is the document-level demand claim of the drawing owner."""
    return {
        "entity_id": entity_id,
        "quantity": float(state.get("quantity") or 0),
        "created_at": created_at.isoformat() if hasattr(created_at, "isoformat") else (created_at or ""),
        "expires_at": state.get("expires_at"),
        "state": state,
        "claim": claim,
    }


def doc_bound_lots(line_items: list[dict]) -> set[str]:
    """The lots a document's lines reference directly.

    A lot bound by one line is that line's stock: it is never drawn as another
    line's spanning sibling, so one physical lot cannot satisfy two lines and a
    document allocates the same way whatever order its lines are processed in.
    """
    return {str(line_item_id(li)) for li in line_items if line_item_id(li)}

def _draw(lots: list[dict], needed: float, remaining: dict[str, float] | None,
          skipped: list[dict] | None = None):
    """Take ``needed`` units from ``lots`` in the given order. ``remaining`` holds the
    quantity each lot still has after earlier draws planned in the same operation (a lot
    absent from it has its full quantity) and is updated in place, so lines planned one
    after another never take the same stock twice.

    A lot whose Allow Splitting is off is taken whole or not at all: one that would be
    only partly drawn is passed over (and added to ``skipped`` when given), so the draw
    moves on to the next lot instead of cutting it."""
    draws: list[tuple[dict, float, bool]] = []
    left = float(needed)
    seen: set[str] = set()
    for lot in lots:
        if left <= 1e-9:
            break
        eid = lot.get("entity_id")
        if eid in seen:
            continue
        seen.add(eid)
        whole = float(lot.get("quantity") or 0)
        avail = whole if remaining is None else remaining.get(eid, whole)
        if avail <= 1e-9:
            continue
        take = min(left, avail)
        if take < whole - 1e-9 and not splitting_allowed(lot.get("state", lot)):
            if skipped is not None:
                skipped.append(lot)
            continue
        draws.append((lot, take, abs(take - avail) <= 1e-9))
        if remaining is not None:
            remaining[eid] = avail - take
        left -= take
    return draws, max(0.0, left)


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
    return _draw([primary] + _sorted_inventory(siblings, method), needed, None)


def attribute_holds(
    line_items: list[dict], held: dict[str, dict],
) -> tuple[dict[int, list[str]], list[str], dict[str, list[int]]]:
    """Which line of a document or List holds each lot it has reserved.

    ``held`` maps each lot the owner holds to its state. A lot stamped with a line id
    belongs to that line while the line exists; once the line is gone it is an orphan.
    An unstamped (older) hold belongs to the one line that binds it, or, bound by no
    line, to the one line of the same product (an older reservation took sibling lots
    without recording the line). Matching several lines either way it is ambiguous and
    no line may treat it as its own; matching none it is an orphan. Returns
    ``(by_line, orphans, ambiguous)``: line index to its lots, unattributable lots, and
    ambiguous lots to the indices of the lines they match.
    """
    index_of = {li.get("line_id"): i for i, li in enumerate(line_items) if li.get("line_id")}
    binders: dict[str, list[int]] = {}
    carriers: dict[str, list[int]] = {}
    for i, li in enumerate(line_items):
        eid = line_item_id(li)
        if eid:
            binders.setdefault(str(eid), []).append(i)
            sku = str(li.get("sku") or "").strip()
            if sku:
                carriers.setdefault(sku, []).append(i)
    by_line: dict[int, list[str]] = {}
    orphans: list[str] = []
    ambiguous: dict[str, list[int]] = {}
    for eid, st in held.items():
        stamp = (st or {}).get("status_line_entity_id")
        if stamp:
            idx = index_of.get(stamp)
            if idx is None:
                orphans.append(eid)
            else:
                by_line.setdefault(idx, []).append(eid)
            continue
        lines = binders.get(eid) or carriers.get(str((st or {}).get("sku") or "").strip(), [])
        if len(lines) == 1:
            by_line.setdefault(lines[0], []).append(eid)
        elif lines:
            ambiguous[eid] = lines
        else:
            orphans.append(eid)
    return by_line, orphans, ambiguous


def line_draw_sources(
    line_items: list[dict], index: int, lots: dict[str, dict], attributed: dict[int, list[str]],
    method: str,
) -> tuple[list[dict], dict | None, list[dict]]:
    """The lots line ``index`` may draw from: ``(own, primary, free)``.

    ``lots`` are the vetted candidates (the caller has locked and read them), each a
    lot dict whose ``claim`` is the document-level demand claim: "free" for stock
    anyone may take, "reserved" for this owner's own hold, None otherwise. Document-level
    eligibility is narrowed to the line: ``own`` is only the holds attributed to this
    line (its bound lot first, then pick order), ``primary`` is the bound lot when it is free, and
    ``free`` is the free stock of the same product except lots another line binds.
    """
    li = line_items[index]
    bound = str(line_item_id(li) or "")
    bound_lot = lots.get(bound)
    own_ids = attributed.get(index, [])
    held = [lots[e] for e in own_ids if e in lots]
    own = [lot for lot in held if lot["entity_id"] == bound] + _sorted_inventory(
        [lot for lot in held if lot["entity_id"] != bound], method)
    primary = bound_lot if bound_lot is not None and bound_lot.get("claim") == "free" else None
    sku = str(((bound_lot or {}).get("state") or {}).get("sku") or li.get("sku") or "").strip()
    others = doc_bound_lots([x for i, x in enumerate(line_items) if i != index]) - {bound}
    free = [lot for eid, lot in lots.items()
            if lot.get("claim") == "free" and eid != bound and eid not in others
            and str((lot.get("state") or {}).get("sku") or "").strip() == sku]
    return own, primary, free


def plan_line_draws(
    needed: float, *, own: list[dict], primary: dict | None, free: list[dict],
    method: str, remaining: dict[str, float], span: bool = True, skipped: list[dict] | None = None,
) -> tuple[list[tuple[dict, float, bool]], float]:
    """Allocate a line's ``needed`` quantity: its own holds in line_draw_sources order,
    then its free bound lot, then - when the product may span lots - free lots
    of the same product in pick order. ``remaining`` is shared by every line planned in
    one operation. Returns ``(draws, shortfall)`` as plan_lot_draws does; a hold left
    out of the draws (or only partly drawn) is more than the line now needs. A lot that
    may not be split and would be only partly drawn is passed over (see _draw) and
    recorded in ``skipped`` when given."""
    order = list(own)
    if primary is not None:
        order.append(primary)
    if span:
        order.extend(_sorted_inventory(free, method))
    return _draw(order, needed, remaining, skipped)
