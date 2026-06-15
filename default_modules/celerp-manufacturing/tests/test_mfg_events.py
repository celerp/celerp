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
