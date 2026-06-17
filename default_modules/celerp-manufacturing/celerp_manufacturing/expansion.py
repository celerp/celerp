# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Recipe expansion — turn demand into manufacturing-order inputs/outputs and JIT summaries.

Pure functions (no DB); callers inject a ``lookup(item_id) -> item_state`` resolver.
- ``expand_recipe``  : single-level — one finished item + build qty → order inputs + outputs.
                       Sub-assemblies stay a single input line (consumed as stock by the order).
- ``explode_demand`` : recursive — aggregate raw-material + sub-assembly demand across many
                       document lines, for the combined components (JIT) summary.
"""
from __future__ import annotations

from typing import Callable

from .costing import MAX_RECIPE_DEPTH, RecipeError

ItemLookup = Callable[[str], dict | None]


def is_manufacturable(item_state: dict | None) -> bool:
    recipe = (item_state or {}).get("recipe") or {}
    return bool(recipe.get("components"))


def mfg_idem_key(source_doc_id: str, source_line_id: str, fulfill_cycle: int = 0) -> str:
    """Deterministic idempotency key for an order created from a document line.

    Same (doc, line, cycle) → same key, so re-invoking "Create manufacturing order(s)"
    never double-creates. The fulfill_cycle lets a re-opened/re-fulfilled doc legitimately
    create a fresh order.
    """
    return f"mfg-from-doc:{source_doc_id}:{source_line_id}:{fulfill_cycle}"


def expand_recipe(item_state: dict, build_qty: float) -> tuple[list[dict], list[dict]]:
    """One finished item + build quantity → (inputs, expected_outputs) for a manufacturing order.

    Single-level: each component (including a sub-assembly) becomes one input line scaled by
    build_qty / output_qty. Raises RecipeError if the item is not manufacturable (caller must gate).
    """
    recipe = (item_state or {}).get("recipe") or {}
    components = recipe.get("components") or []
    if not components:
        raise RecipeError("item has no recipe to expand")
    factor = float(build_qty) / (float(recipe.get("output_qty") or 1) or 1)
    inputs = [
        {"item_id": c["item_id"], "quantity": round(float(c.get("quantity") or 0) * factor, 6)}
        for c in components if c.get("item_id")
    ]
    expected_outputs = [{
        "sku": item_state.get("sku", ""),
        "name": item_state.get("name", ""),
        "quantity": float(build_qty),
        "category": item_state.get("category"),
    }]
    return inputs, expected_outputs


def explode_demand(lines: list[tuple[str, float]], lookup: ItemLookup) -> dict:
    """Recursively explode document-line demand into total component requirements (JIT summary).

    ``lines`` is [(item_id, qty), …]. Returns
    ``{"sub_assemblies": {item_id: qty}, "raw_materials": {item_id: qty}}`` aggregated across
    all lines, fully exploded to leaf raw materials (with sub-assembly subtotals). A leaf is any
    component that is not itself manufacturable. Cycle- and depth-guarded.
    """
    sub: dict[str, float] = {}
    raw: dict[str, float] = {}

    def _walk(item_id: str, qty: float, path: frozenset[str], depth: int) -> None:
        if depth > MAX_RECIPE_DEPTH:
            raise RecipeError("recipe nesting exceeds max depth")
        state = lookup(item_id)
        recipe = (state or {}).get("recipe") or {}
        components = recipe.get("components") or []
        if not components:
            raw[item_id] = raw.get(item_id, 0.0) + qty
            return
        sub[item_id] = sub.get(item_id, 0.0) + qty
        factor = qty / (float(recipe.get("output_qty") or 1) or 1)
        for c in components:
            cid = c.get("item_id")
            if not cid:
                continue
            if cid in path:
                raise RecipeError(f"recipe cycle detected at {cid}")
            _walk(cid, float(c.get("quantity") or 0) * factor, path | {cid}, depth + 1)

    for item_id, qty in lines:
        if is_manufacturable(lookup(item_id)):
            _walk(item_id, float(qty), frozenset({item_id}), 0)

    return {
        "sub_assemblies": {k: round(v, 6) for k, v in sub.items()},
        "raw_materials": {k: round(v, 6) for k, v in raw.items()},
    }
