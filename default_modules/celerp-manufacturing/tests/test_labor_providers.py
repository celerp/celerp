# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Pure tests for the auto-labor provider seam (v1 ships the hook, no providers)."""
from __future__ import annotations

import pytest

from celerp_manufacturing.costing import roll_up_cost
from celerp_manufacturing.labor import (
    apply_labor_providers,
    clear_labor_providers,
    register_labor_provider,
)


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_labor_providers()
    yield
    clear_labor_providers()


def test_no_providers_returns_manual_only() -> None:
    manual = [{"operation": "Assembly", "hours": 1, "rate": 40, "source": "manual"}]
    assert apply_labor_providers([{"item_id": "X", "quantity": 1}], manual) == manual


def test_provider_lines_tagged_auto() -> None:
    register_labor_provider(lambda comps: [{"operation": "Auto-set", "hours": 0.5, "rate": 20}])
    out = apply_labor_providers([{"item_id": "GOLD", "quantity": 2}], [])
    assert len(out) == 1 and out[0]["source"] == "auto"


def test_manual_preserved_alongside_auto_and_idempotent() -> None:
    register_labor_provider(lambda comps: [{"operation": "Auto", "hours": 1, "rate": 10, "source": "auto:r1"}])
    manual = [{"operation": "Hand-finish", "hours": 2, "rate": 30, "source": "manual"}]
    once = apply_labor_providers([], manual)
    # Re-applying the function's own output must not duplicate the auto line.
    twice = apply_labor_providers([], once)
    assert once == twice
    assert [l["source"] for l in twice] == ["manual", "auto:r1"]


def test_provider_is_pure_function_of_components() -> None:
    register_labor_provider(lambda comps: [{"operation": "X", "hours": len(comps), "rate": 1}])
    comps = [{"item_id": "A", "quantity": 1}, {"item_id": "B", "quantity": 1}]
    assert apply_labor_providers(comps, []) == apply_labor_providers(comps, [])


def test_provider_lines_summed_in_cost() -> None:
    register_labor_provider(lambda comps: [{"operation": "Auto", "hours": 2, "rate": 25}])
    labor = apply_labor_providers([], [{"operation": "Manual", "hours": 1, "rate": 10, "source": "manual"}])
    bd = roll_up_cost({"output_qty": 1, "components": [], "labor": labor, "overhead": []}, {}.get)
    assert bd["labor_cost"] == 60.0  # 1*10 manual + 2*25 auto
