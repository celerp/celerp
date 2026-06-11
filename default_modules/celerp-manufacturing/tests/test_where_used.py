# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Pure unit tests for the where_used implosion (mark-to-market dependency lookup)."""
from __future__ import annotations

from celerp_manufacturing.costing import where_used


def test_where_used_direct() -> None:
    deps = {"A": {"GOLD"}, "B": {"SILVER"}}
    assert where_used("GOLD", deps) == {"A"}


def test_where_used_transitive() -> None:
    # GOLD -> SUB -> RING ; GOLD -> EARRING
    deps = {"RING": {"SUB"}, "SUB": {"GOLD"}, "EARRING": {"GOLD"}}
    assert where_used("GOLD", deps) == {"SUB", "RING", "EARRING"}


def test_where_used_none() -> None:
    deps = {"A": {"GOLD"}}
    assert where_used("PLATINUM", deps) == set()


def test_where_used_cycle_safe() -> None:
    deps = {"A": {"B"}, "B": {"A", "GOLD"}}
    # No infinite loop; both A and B use GOLD (B directly, A via B).
    assert where_used("GOLD", deps) == {"A", "B"}


def test_where_used_self_not_included() -> None:
    deps = {"A": {"GOLD"}}
    # The target itself is never one of its own dependents.
    assert "GOLD" not in where_used("GOLD", deps)
