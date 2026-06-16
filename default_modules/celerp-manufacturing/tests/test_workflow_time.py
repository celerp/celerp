# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Pure tests for the single workflow time-conversion point (canonical minutes)."""
from __future__ import annotations

from celerp.events.schemas import workflow_step_minutes


def test_minutes_passthrough() -> None:
    assert workflow_step_minutes(15, "min") == 15.0


def test_hours_to_minutes() -> None:
    assert workflow_step_minutes(12, "hr") == 720.0


def test_days_to_minutes() -> None:
    assert workflow_step_minutes(2, "day") == 2880.0


def test_blank_is_zero() -> None:
    assert workflow_step_minutes(None, "hr") == 0.0
    assert workflow_step_minutes("", "day") == 0.0


def test_unknown_unit_falls_back_to_minutes() -> None:
    # Defensive: an unexpected unit is treated as minutes, never crashes.
    assert workflow_step_minutes(5, "weeks") == 5.0
