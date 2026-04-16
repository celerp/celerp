# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

from __future__ import annotations

import pytest

from celerp_manufacturing.projection_handler import apply_manufacturing_event


def test_mfg_flow() -> None:
    state = apply_manufacturing_event({}, "mfg.order.created", {"product_sku": "S", "quantity": 1})
    assert state["status"] == "created" and state["is_in_production"] is False

    state = apply_manufacturing_event(state, "mfg.order.started", {})
    assert state["status"] == "started" and state["is_in_production"] is True

    state = apply_manufacturing_event(state, "mfg.step.completed", {"step_id": "s1"})
    assert state["steps_completed"] == ["s1"]

    state = apply_manufacturing_event(state, "mfg.step.completed", {"step_id": "s1"})
    assert state["steps_completed"] == ["s1"]

    state = apply_manufacturing_event(state, "mfg.order.completed", {})
    assert state["status"] == "completed" and state["is_in_production"] is False

    state = apply_manufacturing_event(state, "mfg.order.cancelled", {"reason": "x"})
    assert state["status"] == "cancelled"


def test_mfg_unknown_raises() -> None:
    with pytest.raises(ValueError):
        apply_manufacturing_event({}, "mfg.nope", {})
