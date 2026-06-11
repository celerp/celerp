# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Pure unit tests for recipe expansion (order inputs/outputs + JIT demand explosion)."""
from __future__ import annotations

import pytest

from celerp_manufacturing.costing import RecipeError
from celerp_manufacturing.expansion import (
    expand_recipe,
    explode_demand,
    is_manufacturable,
    mfg_idem_key,
)


def _item(sku, components=None, output_qty=1, **kw):
    st = {"sku": sku, "name": sku, **kw}
    if components is not None:
        st["recipe"] = {"output_qty": output_qty, "components": components, "labor": [], "overhead": []}
    return st


# --- expand_recipe (single level) ------------------------------------------

def test_expand_basic() -> None:
    item = _item("RING", [{"item_id": "GOLD", "quantity": 2}])
    inputs, outputs = expand_recipe(item, 3)
    assert inputs == [{"item_id": "GOLD", "quantity": 6.0}]
    assert outputs == [{"sku": "RING", "name": "RING", "quantity": 3.0, "category": None}]


def test_expand_output_qty_batch() -> None:
    # recipe yields 10 per batch; comp 5 per batch; build 20 → 5 * (20/10) = 10
    item = _item("WIDGET", [{"item_id": "RAW", "quantity": 5}], output_qty=10)
    inputs, _ = expand_recipe(item, 20)
    assert inputs == [{"item_id": "RAW", "quantity": 10.0}]


def test_expand_multiple_components() -> None:
    item = _item("ASM", [{"item_id": "A", "quantity": 1}, {"item_id": "B", "quantity": 3}])
    inputs, _ = expand_recipe(item, 4)
    assert inputs == [{"item_id": "A", "quantity": 4.0}, {"item_id": "B", "quantity": 12.0}]


def test_expand_nested_single_level() -> None:
    # A sub-assembly stays ONE input line (consumed as stock); expand is single-level.
    item = _item("RING", [{"item_id": "SUB", "quantity": 2}])
    inputs, _ = expand_recipe(item, 1)
    assert inputs == [{"item_id": "SUB", "quantity": 2.0}]


def test_expand_non_manufacturable_raises() -> None:
    with pytest.raises(RecipeError):
        expand_recipe(_item("RAW"), 5)


# --- explode_demand (recursive JIT) ----------------------------------------

def test_explode_demand_aggregates_across_lines() -> None:
    g = {
        "RING": _item("RING", [{"item_id": "GOLD", "quantity": 5}]),
        "PIN": _item("PIN", [{"item_id": "GOLD", "quantity": 2}]),
        "GOLD": _item("GOLD"),
    }
    out = explode_demand([("RING", 2), ("PIN", 3)], g.get)
    assert out["raw_materials"] == {"GOLD": 16.0}  # 2*5 + 3*2
    assert out["sub_assemblies"] == {"RING": 2.0, "PIN": 3.0}


def test_explode_demand_recursive_to_leaves() -> None:
    g = {
        "RING": _item("RING", [{"item_id": "SUB", "quantity": 3}]),
        "SUB": _item("SUB", [{"item_id": "GOLD", "quantity": 2}]),
        "GOLD": _item("GOLD"),
    }
    out = explode_demand([("RING", 1)], g.get)
    assert out["raw_materials"] == {"GOLD": 6.0}          # 1*3*2
    assert out["sub_assemblies"] == {"RING": 1.0, "SUB": 3.0}


def test_explode_demand_ignores_non_manufacturable_lines() -> None:
    g = {"RAW": _item("RAW")}
    assert explode_demand([("RAW", 9)], g.get) == {"sub_assemblies": {}, "raw_materials": {}}


def test_explode_demand_cycle_safe() -> None:
    g = {"A": _item("A", [{"item_id": "B", "quantity": 1}]), "B": _item("B", [{"item_id": "A", "quantity": 1}])}
    with pytest.raises(RecipeError):
        explode_demand([("A", 1)], g.get)


# --- helpers ----------------------------------------------------------------

def test_is_manufacturable() -> None:
    assert is_manufacturable(_item("R", [{"item_id": "X", "quantity": 1}]))
    assert not is_manufacturable(_item("RAW"))
    assert not is_manufacturable(None)


def test_idem_key_deterministic_and_distinct() -> None:
    assert mfg_idem_key("doc:1", "line:a") == mfg_idem_key("doc:1", "line:a")
    assert mfg_idem_key("doc:1", "line:a") != mfg_idem_key("doc:1", "line:b")
    assert mfg_idem_key("doc:1", "line:a", 0) != mfg_idem_key("doc:1", "line:a", 1)
