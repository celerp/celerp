# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Recipe cost roll-up.

Pure functions — no DB. Callers inject a ``lookup(item_id) -> item_state`` resolver
so the logic is fully unit-testable and stays free of I/O. A manufactured item's
*standard* unit cost is derived from its components' unit costs + labor + overhead,
recursively for nested sub-assemblies.
"""
from __future__ import annotations

from typing import Callable

from celerp.services.money import round_money, round_rate, to_decimal

MAX_RECIPE_DEPTH = 32

ItemLookup = Callable[[str], dict | None]


class RecipeError(ValueError):
    """Raised on a cyclic or too-deeply-nested recipe graph."""


def _labor_line_cost(line: dict) -> float:
    """A labor line is either a flat fixed amount or hours × rate."""
    if (line.get("kind") or "hourly") == "fixed":
        return float(line.get("amount") or 0)
    return float(line.get("hours") or 0) * float(line.get("rate") or 0)


def _leaf_unit_cost(item_state: dict) -> float:
    """Unit cost of a non-manufactured (raw / purchased) item, from its lot value.

    cost_total is the canonical lot cost; unit = total / qty. An unpriced item
    contributes 0 (documented, deterministic — no guessing) until it is priced.
    """
    cost_total = item_state.get("cost_total")
    qty = float(item_state.get("quantity") or 0)
    if cost_total is not None and qty:
        return float(cost_total) / qty
    cost_price = item_state.get("cost_price")
    return float(cost_price) if cost_price is not None else 0.0


def unit_cost(item_state: dict | None, lookup: ItemLookup, *, currency: str = "USD", _path: frozenset[str] = frozenset(), _depth: int = 0) -> float:
    """Standard unit cost of an item: its rolled recipe cost if manufacturable, else its leaf cost."""
    state = item_state or {}
    recipe = state.get("recipe")
    if not recipe or not recipe.get("components"):
        return _leaf_unit_cost(state)
    return roll_up_cost(recipe, lookup, currency=currency, _path=_path, _depth=_depth)["unit_cost"]


def roll_up_cost(recipe: dict, lookup: ItemLookup, *, currency: str = "USD", _path: frozenset[str] = frozenset(), _depth: int = 0) -> dict:
    """Roll up a recipe into a cost breakdown.

    Returns ``{materials_cost, labor_cost, overhead_cost, unit_cost}``. The *_cost fields are money
    AMOUNTS (currency precision); unit_cost is a RATE (per-output-unit, carries rate precision so it
    reconciles when multiplied back by output qty). Internal sums stay unrounded; only outputs round
    (round-once). Raises RecipeError on a cycle or nesting beyond MAX_RECIPE_DEPTH.
    """
    if _depth > MAX_RECIPE_DEPTH:
        raise RecipeError("recipe nesting exceeds max depth")

    materials = 0.0
    for comp in recipe.get("components", []):
        cid = comp["item_id"]
        if cid in _path:
            raise RecipeError(f"recipe cycle detected at {cid}")
        child_cost = unit_cost(lookup(cid), lookup, currency=currency, _path=_path | {cid}, _depth=_depth + 1)
        line = float(comp.get("quantity") or 0) * child_cost
        # Annotate the line in-place so the UI can show each component's catalog unit cost (a rate)
        # and extended cost (an amount) without re-deriving any cost logic (single source = this module).
        comp["unit_cost"] = float(round_rate(child_cost, currency))
        comp["line_cost"] = float(round_money(line, currency))
        materials += line

    labor = sum(_labor_line_cost(l) for l in recipe.get("labor", []))
    overhead = sum(float(o.get("amount") or 0) for o in recipe.get("overhead", []))
    output_qty = float(recipe.get("output_qty") or 1) or 1
    total = materials + labor + overhead
    return {
        "materials_cost": float(round_money(materials, currency)),
        "labor_cost": float(round_money(labor, currency)),
        "overhead_cost": float(round_money(overhead, currency)),
        "unit_cost": float(round_rate(to_decimal(total) / to_decimal(output_qty), currency)),
    }


def labor_hours(recipe: dict, hours_per_day: float = 8.0) -> float:
    """Total labor HOURS for one batch (output_qty units) of a recipe.

    Hourly lines contribute their hours; daily lines convert days -> hours via hours_per_day;
    fixed lines are a flat cost with no time and contribute 0 hours.
    """
    total = 0.0
    for line in recipe.get("labor", []) or []:
        kind = (line.get("kind") or "hourly")
        if kind == "fixed":
            continue
        h = float(line.get("hours") or 0)
        if kind == "daily":
            h *= float(hours_per_day or 0)
        total += h
    return total


def where_used(target_id: str, deps: dict[str, set[str]]) -> set[str]:
    """Implosion: every item whose recipe references ``target_id`` directly or transitively.

    ``deps`` maps item_id -> set of the item_ids it directly uses as components. Returns the
    set of ancestor item_ids (the items to re-cost when ``target_id``'s cost changes). Cycle-safe.
    Inverse of the explosion done by roll_up_cost — used for mark-to-market re-costing.
    """
    reverse: dict[str, set[str]] = {}
    for parent, comps in deps.items():
        for comp in comps:
            reverse.setdefault(comp, set()).add(parent)
    ancestors: set[str] = set()
    stack = [target_id]
    while stack:
        cur = stack.pop()
        for parent in reverse.get(cur, ()):
            if parent not in ancestors:
                ancestors.add(parent)
                stack.append(parent)
    return ancestors
