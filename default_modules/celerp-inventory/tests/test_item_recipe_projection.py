# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Pure projection tests for the item.recipe.set event (full-replace semantics)."""
from __future__ import annotations

from celerp_inventory.projections import apply_item_event


def _recipe(components):
    return {"output_qty": 1, "components": components, "labor": [], "overhead": []}


def test_recipe_set_stores_full_recipe() -> None:
    recipe = _recipe([{"item_id": "item:x", "sku": "X", "quantity": 2, "unit": "pieces"}])
    state = apply_item_event({"sku": "FG", "quantity": 0}, "item.recipe.set", {"recipe": recipe})
    assert state["recipe"] == recipe


def test_recipe_set_full_replace_not_merge() -> None:
    first = _recipe([
        {"item_id": "item:a", "quantity": 1},
        {"item_id": "item:b", "quantity": 1},
    ])
    state = apply_item_event({"sku": "FG"}, "item.recipe.set", {"recipe": first})
    second = _recipe([{"item_id": "item:c", "quantity": 5}])
    state = apply_item_event(state, "item.recipe.set", {"recipe": second})
    # Determinism: the recipe is replaced wholesale, never merged.
    assert state["recipe"]["components"] == [{"item_id": "item:c", "quantity": 5}]


def test_recipe_set_empty_clears() -> None:
    state = apply_item_event({"sku": "FG"}, "item.recipe.set", {"recipe": _recipe([{"item_id": "item:a", "quantity": 1}])})
    state = apply_item_event(state, "item.recipe.set", {"recipe": _recipe([])})
    assert state["recipe"]["components"] == []


def test_recipe_set_preserves_other_state() -> None:
    base = {"sku": "FG", "name": "Widget", "quantity": 7, "cost_total": 100.0, "status": "available"}
    state = apply_item_event(base, "item.recipe.set", {"recipe": _recipe([{"item_id": "item:a", "quantity": 1}])})
    for key, val in base.items():
        assert state[key] == val
