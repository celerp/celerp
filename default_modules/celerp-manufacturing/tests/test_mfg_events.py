# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT

from __future__ import annotations

import pytest

from celerp_manufacturing.projection_handler import apply_manufacturing_event


def test_mfg_flow() -> None:
    state = apply_manufacturing_event(
        {}, "mfg.order.created",
        {"product_sku": "S", "quantity": 1, "inputs": [{"item_id": "item:g", "quantity": 5}]},
    )
    assert state["status"] == "planned" and state["is_in_production"] is False
    assert state["received_qty"] == 0.0 and state["inputs"][0]["issued_qty"] == 0.0

    # Issuing components auto-advances Planned -> In Progress and tracks issued_qty.
    state = apply_manufacturing_event(state, "mfg.order.issued", {"items": [{"item_id": "item:g", "quantity": 5}]})
    assert state["status"] == "in_progress" and state["is_in_production"] is True
    assert state["inputs"][0]["issued_qty"] == 5.0

    state = apply_manufacturing_event(state, "mfg.order.on_hold", {"reason": "wait for stones"})
    assert state["status"] == "on_hold" and state["is_in_production"] is False and state["hold_reason"] == "wait for stones"

    state = apply_manufacturing_event(state, "mfg.order.resumed", {})
    assert state["status"] == "in_progress" and state["is_in_production"] is True and "hold_reason" not in state

    # Receiving finished goods accumulates received_qty.
    state = apply_manufacturing_event(state, "mfg.order.received", {"quantity": 1})
    assert state["received_qty"] == 1.0

    state = apply_manufacturing_event(state, "mfg.order.completed", {})
    assert state["status"] == "completed" and state["is_in_production"] is False

    state = apply_manufacturing_event(state, "mfg.order.cancelled", {"reason": "x"})
    assert state["status"] == "cancelled"


def test_issue_auto_advances_from_on_hold() -> None:
    state = apply_manufacturing_event({}, "mfg.order.created", {"inputs": [{"item_id": "item:g", "quantity": 2}]})
    state = apply_manufacturing_event(state, "mfg.order.on_hold", {"reason": "paused"})
    state = apply_manufacturing_event(state, "mfg.order.issued", {"items": [{"item_id": "item:g", "quantity": 2}]})
    assert state["status"] == "in_progress" and "hold_reason" not in state


def test_mfg_unknown_raises() -> None:
    with pytest.raises(ValueError):
        apply_manufacturing_event({}, "mfg.nope", {})


def test_an_item_issued_twice_in_one_event_counts_both_times() -> None:
    state = apply_manufacturing_event({}, "mfg.order.created", {"inputs": [{"item_id": "item:g", "quantity": 5}]})
    state = apply_manufacturing_event(state, "mfg.order.issued", {"items": [
        {"item_id": "item:g", "quantity": 2}, {"item_id": "item:g", "quantity": 3}], "value": "10"})
    assert state["inputs"][0]["issued_qty"] == 5.0
    assert state["wip_issued"] == "10"


def test_movement_values_accumulate_as_exact_decimals() -> None:
    state = apply_manufacturing_event({}, "mfg.order.created", {"inputs": [{"item_id": "item:g", "quantity": 3}]})
    for _ in range(3):
        state = apply_manufacturing_event(state, "mfg.order.issued", {
            "items": [{"item_id": "item:g", "quantity": 1}], "value": "0.10", "wip_account_code": "1130-WIP"})
    assert (state["wip_issued"], state["wip_account_code"]) == ("0.30", "1130-WIP")
    state = apply_manufacturing_event(state, "mfg.order.received", {"quantity": 1, "lot_item_id": "item:l", "value": "0.20"})
    assert state["wip_transferred"] == "0.20"
    assert state["receipts"] == [{"lot_item_id": "item:l", "quantity": 1.0, "value": "0.20"}]
    state = apply_manufacturing_event(state, "mfg.order.completed", {"transferred": "0.25", "wasted": "0.05"})
    assert (state["wip_transferred"], state["wip_wasted"]) == ("0.25", "0.05")
    assert "wip_untracked" not in state


def test_movement_from_an_older_release_marks_the_run_untracked() -> None:
    state = apply_manufacturing_event({}, "mfg.order.created", {"inputs": [{"item_id": "item:g", "quantity": 1}]})
    state = apply_manufacturing_event(state, "mfg.order.issued", {"items": [{"item_id": "item:g", "quantity": 1}]})
    assert state["wip_untracked"] is True
    state = apply_manufacturing_event(state, "mfg.order.wip_opened", {"issued": "4", "transferred": "0"})
    assert "wip_untracked" not in state and state["wip_issued"] == "4"
    state = apply_manufacturing_event({}, "mfg.order.received", {"quantity": 1, "lot_item_id": "item:l"})
    assert state["wip_untracked"] is True


def test_a_created_event_cannot_carry_a_runs_accounting_facts() -> None:
    state = apply_manufacturing_event({}, "mfg.order.created", {
        "inputs": [], "wip_issued": "999", "wip_transferred": "1", "wip_account_code": "9999",
        "receipts": [{"lot_item_id": "x"}], "wip_untracked": False, "wip_unresolved": "x", "wip_wasted": "1"})
    assert not {"wip_issued", "wip_transferred", "wip_account_code", "receipts", "wip_untracked",
                "wip_unresolved", "wip_wasted"} & set(state)


def test_an_unresolved_run_records_why() -> None:
    state = apply_manufacturing_event({}, "mfg.order.wip_unresolved", {"reason": "history incomplete"})
    assert state["wip_unresolved"] == "history incomplete"
