# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Recipe expansion — turn demand into manufacturing-order inputs/outputs and JIT summaries.

Pure functions (no DB); callers inject a ``lookup(item_id) -> item_state`` resolver.
- ``expand_recipe``  : single-level — one finished item + build qty → order inputs.
- ``output_line``    : the output a run expects, taken from the product it makes.
                       Sub-assemblies stay a single input line (consumed as stock by the order).
- ``explode_demand`` : recursive — aggregate raw-material + sub-assembly demand across many
                       document lines, for the combined components (JIT) summary.
"""
from __future__ import annotations

from typing import Callable

from celerp_inventory.projections import is_manufacturable

from .costing import MAX_RECIPE_DEPTH, RecipeError, component_quantity, output_quantity

ItemLookup = Callable[[str], dict | None]


def for_product(item_state: dict | None, exc: RecipeError) -> RecipeError:
    """A recipe refusal naming the product whose recipe it is, as the user knows it (its SKU)."""
    return RecipeError(f"{(item_state or {}).get('sku') or 'This product'}: {exc}")


def mfg_idem_key(source_doc_id: str, item_id: str, operation: str) -> str:
    """The identity of the run one request makes for one demand line: the product ``item_id``
    for the document ``source_doc_id`` (blank when made to stock), within the user's action
    ``operation``. The same action sent again names the same run, so it is never made twice;
    a new action may make another run for the same line when the line is short again.
    """
    return f"mfg-from-doc:{source_doc_id}:{item_id}:{operation}"


def merge_inputs(inputs) -> list[dict]:
    """One line per component, in first-seen order: a component listed twice needs both amounts."""
    merged: dict[str, dict] = {}
    for line in inputs:
        key = line["item_id"]
        if key in merged:
            merged[key]["quantity"] = round(merged[key]["quantity"] + float(line["quantity"]), 6)
        else:
            merged[key] = {**line, "quantity": float(line["quantity"])}
    return list(merged.values())


def output_line(item_state: dict, quantity: float) -> dict:
    """The output a run expects: ``quantity`` of the product it makes, named by the product."""
    return {"sku": item_state.get("sku", ""), "name": item_state.get("name", ""), "quantity": float(quantity),
            "category": item_state.get("category")}


def expand_recipe(item_state: dict, build_qty: float, lookup: ItemLookup) -> list[dict]:
    """One finished item + build quantity → the inputs of a manufacturing order.

    Single-level: each component (including a sub-assembly) becomes one input line scaled by
    build_qty / output_qty. Raises RecipeError if the item is not manufacturable (caller must gate).
    ``lookup`` resolves a component's state, so a refusal can name it by its SKU.
    """
    recipe = (item_state or {}).get("recipe") or {}
    components = recipe.get("components") or []
    if not components:
        raise RecipeError("item has no recipe to expand")
    factor = float(build_qty) / output_quantity(recipe)
    return merge_inputs(
        {"item_id": c["item_id"], "quantity": round(component_quantity(c, lookup) * factor, 6)}
        for c in components if c.get("item_id")
    )


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
        factor = qty / output_quantity(recipe)
        for c in components:
            cid = c.get("item_id")
            if not cid:
                continue
            if cid in path:
                raise RecipeError(f"recipe cycle detected at {cid}")
            _walk(cid, component_quantity(c, lookup) * factor, path | {cid}, depth + 1)

    for item_id, qty in lines:
        state = lookup(item_id)
        if is_manufacturable(state):
            try:
                _walk(item_id, float(qty), frozenset({item_id}), 0)
            except RecipeError as exc:
                raise for_product(state, exc) from None

    return {
        "sub_assemblies": {k: round(v, 6) for k, v in sub.items()},
        "raw_materials": {k: round(v, 6) for k, v in raw.items()},
    }
