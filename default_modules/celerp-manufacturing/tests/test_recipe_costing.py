# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Pure unit tests for the recipe cost roll-up (no DB)."""
from __future__ import annotations

import pytest

from celerp_manufacturing.costing import (
    MAX_RECIPE_DEPTH,
    RecipeError,
    roll_up_cost,
    unit_cost,
)


def _recipe(components=None, labor=None, overhead=None, output_qty=1):
    return {
        "output_qty": output_qty,
        "components": components or [],
        "labor": labor or [],
        "overhead": overhead or [],
    }


# --- leaf unit cost ---------------------------------------------------------

def test_leaf_unit_cost_from_lot_total() -> None:
    assert unit_cost({"cost_total": 10.0, "quantity": 5}, {}.get) == 2.0


def test_leaf_unit_cost_from_cost_price_when_no_total() -> None:
    assert unit_cost({"cost_price": 3.5}, {}.get) == 3.5


def test_cost_unpriced_component_contributes_zero() -> None:
    # Documented deterministic rule: an unpriced raw contributes 0 until priced.
    bd = roll_up_cost(_recipe([{"item_id": "item:x", "quantity": 4}]), {"item:x": {}}.get)
    assert bd["materials_cost"] == 0.0
    assert bd["unit_cost"] == 0.0


# --- summands in isolation --------------------------------------------------

def test_cost_materials_only() -> None:
    g = {"item:x": {"cost_total": 10.0, "quantity": 5}}  # unit 2.0
    bd = roll_up_cost(_recipe([{"item_id": "item:x", "quantity": 3}]), g.get)
    assert bd == {"materials_cost": 6.0, "labor_cost": 0.0, "overhead_cost": 0.0, "unit_cost": 6.0}


def test_cost_with_labor() -> None:
    bd = roll_up_cost(_recipe(labor=[{"operation": "A", "hours": 2, "rate": 40}]), {}.get)
    assert bd["labor_cost"] == 80.0 and bd["unit_cost"] == 80.0


def test_cost_with_overhead() -> None:
    bd = roll_up_cost(_recipe(overhead=[{"description": "pkg", "amount": 1.2}, {"description": "tool", "amount": 0.8}]), {}.get)
    assert bd["overhead_cost"] == 2.0 and bd["unit_cost"] == 2.0


def test_cost_fixed_labor() -> None:
    bd = roll_up_cost(_recipe(labor=[{"operation": "Setup", "kind": "fixed", "amount": 25}]), {}.get)
    assert bd["labor_cost"] == 25.0


def test_cost_daily_labor_is_rate_based() -> None:
    # Daily behaves like Hourly: cost = quantity (days) x rate (per day).
    bd = roll_up_cost(_recipe(labor=[{"operation": "Onsite", "kind": "daily", "hours": 3, "rate": 200}]), {}.get)
    assert bd["labor_cost"] == 600.0


def test_cost_mixed_hourly_and_fixed_labor() -> None:
    bd = roll_up_cost(_recipe(labor=[
        {"operation": "Assemble", "kind": "hourly", "hours": 2, "rate": 10},
        {"operation": "Setup", "kind": "fixed", "amount": 15},
    ]), {}.get)
    assert bd["labor_cost"] == 35.0  # 2*10 + 15


def test_cost_labor_sums_all_sources() -> None:
    bd = roll_up_cost(_recipe(labor=[
        {"operation": "A", "hours": 1, "rate": 10, "source": "manual"},
        {"operation": "B", "hours": 1, "rate": 5, "source": "auto:rule1"},
    ]), {}.get)
    assert bd["labor_cost"] == 15.0


# --- output_qty division ----------------------------------------------------

def test_cost_divided_by_output_qty() -> None:
    g = {"item:x": {"cost_total": 100.0, "quantity": 1}}  # unit 100
    bd = roll_up_cost(_recipe([{"item_id": "item:x", "quantity": 1}], output_qty=10), g.get)
    assert bd["unit_cost"] == 10.0  # 100 / 10


def test_cost_output_qty_defaults_to_one() -> None:
    bd = roll_up_cost({"components": [], "labor": [{"hours": 1, "rate": 7}]}, {}.get)
    assert bd["unit_cost"] == 7.0


# --- nesting ----------------------------------------------------------------

def test_cost_nested_subassembly_uses_rolled_cost() -> None:
    g = {
        "item:raw": {"cost_total": 10.0, "quantity": 2},  # unit 5
        "item:sub": {"recipe": _recipe([{"item_id": "item:raw", "quantity": 2}], labor=[{"hours": 1, "rate": 1}])},  # 2*5 + 1 = 11
    }
    bd = roll_up_cost(_recipe([{"item_id": "item:sub", "quantity": 3}]), g.get)
    assert bd["materials_cost"] == 33.0  # 3 * 11


# --- guards -----------------------------------------------------------------

def test_cost_cycle_raises() -> None:
    g = {
        "A": {"recipe": _recipe([{"item_id": "B", "quantity": 1}])},
        "B": {"recipe": _recipe([{"item_id": "A", "quantity": 1}])},
    }
    with pytest.raises(RecipeError):
        roll_up_cost(g["A"]["recipe"], g.get, _path=frozenset({"A"}))


def test_cost_self_cycle_raises() -> None:
    g = {"A": {"recipe": _recipe([{"item_id": "A", "quantity": 1}])}}
    with pytest.raises(RecipeError):
        roll_up_cost(g["A"]["recipe"], g.get, _path=frozenset({"A"}))


def test_cost_depth_cap_raises() -> None:
    n = MAX_RECIPE_DEPTH + 5
    g = {f"i{i}": {"recipe": _recipe([{"item_id": f"i{i + 1}", "quantity": 1}])} for i in range(n)}
    g[f"i{n}"] = {"cost_total": 1.0, "quantity": 1}
    with pytest.raises(RecipeError):
        roll_up_cost(g["i0"]["recipe"], g.get)
