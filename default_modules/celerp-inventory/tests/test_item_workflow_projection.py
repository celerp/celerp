# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Pure projection tests for the item.workflow.set event (full-replace semantics)."""
from __future__ import annotations

from celerp_inventory.projections import apply_item_event


def _wf(steps):
    return {"steps": steps}


def test_workflow_set_stores_full_workflow() -> None:
    wf = _wf([{"id": "s1", "name": "Cut", "instructions": "Cut to size", "time_minutes": 10.0}])
    state = apply_item_event({"sku": "FG", "quantity": 0}, "item.workflow.set", {"workflow": wf})
    assert state["workflow"] == wf


def test_workflow_set_full_replace_not_merge() -> None:
    first = _wf([{"id": "a", "name": "One"}, {"id": "b", "name": "Two"}])
    state = apply_item_event({"sku": "FG"}, "item.workflow.set", {"workflow": first})
    second = _wf([{"id": "c", "name": "Three"}])
    state = apply_item_event(state, "item.workflow.set", {"workflow": second})
    # Determinism: the workflow is replaced wholesale, never merged.
    assert state["workflow"]["steps"] == [{"id": "c", "name": "Three"}]


def test_workflow_set_empty_clears() -> None:
    state = apply_item_event({"sku": "FG"}, "item.workflow.set", {"workflow": _wf([{"id": "a"}])})
    state = apply_item_event(state, "item.workflow.set", {"workflow": _wf([])})
    assert state["workflow"]["steps"] == []


def test_workflow_set_preserves_other_state() -> None:
    base = {"sku": "FG", "name": "Widget", "quantity": 7, "recipe": {"components": []}, "status": "available"}
    state = apply_item_event(base, "item.workflow.set", {"workflow": _wf([{"id": "a", "name": "Step"}])})
    for key, val in base.items():
        assert state[key] == val
