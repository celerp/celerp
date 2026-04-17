# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Extended compute tests — covers edge cases missing from the basic suite.

Coverage gaps addressed:
  - valuation.py lines 10-17, 22-30, 35-38 (zero-qty, empty arrays, partial FIFO)
  - aggregations.py lines 15-18 (empty array, float coercion)
"""

from __future__ import annotations

import numpy as np
import pytest

from celerp.compute.valuation import (
    compute_fifo_cost,
    compute_inventory_valuation,
    compute_weighted_average_cost,
)
from celerp.compute.aggregations import group_sum, sum_by_period


# ── valuation.py edge cases ───────────────────────────────────────────────────

class TestWeightedAverageCost:
    def test_basic(self):
        result = compute_weighted_average_cost(
            np.array([2.0, 1.0]), np.array([10.0, 20.0])
        )
        assert abs(result - 40.0 / 3.0) < 1e-9

    def test_zero_total_quantity_returns_zero(self):
        """All zero quantities → division avoided → returns 0."""
        result = compute_weighted_average_cost(
            np.array([0.0, 0.0]), np.array([10.0, 20.0])
        )
        assert result == 0.0

    def test_single_lot(self):
        result = compute_weighted_average_cost(
            np.array([5.0]), np.array([8.0])
        )
        assert abs(result - 8.0) < 1e-9

    def test_equal_weights(self):
        result = compute_weighted_average_cost(
            np.array([1.0, 1.0, 1.0]), np.array([3.0, 6.0, 9.0])
        )
        assert abs(result - 6.0) < 1e-9

    def test_large_values(self):
        """No overflow on large quantities × costs."""
        result = compute_weighted_average_cost(
            np.array([1_000_000.0, 1_000_000.0]),
            np.array([999.99, 1000.01]),
        )
        assert abs(result - 1000.0) < 0.01


class TestFifoCost:
    def test_basic(self):
        result = compute_fifo_cost(
            np.array([2.0, 1.0]), np.array([10.0, 20.0]), 2.5
        )
        assert abs(result - 30.0) < 1e-9

    def test_sell_exactly_one_lot(self):
        result = compute_fifo_cost(
            np.array([3.0, 5.0]), np.array([10.0, 20.0]), 3.0
        )
        assert abs(result - 30.0) < 1e-9

    def test_sell_zero_quantity(self):
        """Selling 0 units → cost is 0."""
        result = compute_fifo_cost(
            np.array([5.0, 3.0]), np.array([10.0, 20.0]), 0.0
        )
        assert result == 0.0

    def test_sell_all_lots(self):
        result = compute_fifo_cost(
            np.array([2.0, 3.0]), np.array([10.0, 20.0]), 5.0
        )
        assert abs(result - (20.0 + 60.0)) < 1e-9

    def test_partial_second_lot(self):
        """Sell into second lot partially."""
        result = compute_fifo_cost(
            np.array([2.0, 10.0]), np.array([5.0, 15.0]), 4.0
        )
        # 2 units @ 5 + 2 units @ 15 = 10 + 30 = 40
        assert abs(result - 40.0) < 1e-9

    def test_sell_more_than_available_stops_at_stock(self):
        """FIFO stops when stock runs out — does not error on over-sell."""
        result = compute_fifo_cost(
            np.array([1.0, 1.0]), np.array([10.0, 20.0]), 999.0
        )
        # Only 2 units exist
        assert abs(result - 30.0) < 1e-9


class TestInventoryValuation:
    def test_basic(self):
        result = compute_inventory_valuation(
            np.array([2.0, 3.0]), np.array([10.0, 5.0])
        )
        assert abs(result - 35.0) < 1e-9

    def test_zero_quantities(self):
        result = compute_inventory_valuation(
            np.array([0.0, 0.0]), np.array([50.0, 100.0])
        )
        assert result == 0.0

    def test_single_item(self):
        result = compute_inventory_valuation(
            np.array([7.0]), np.array([3.5])
        )
        assert abs(result - 24.5) < 1e-9

    def test_zero_cost_items(self):
        """Free items contribute nothing to valuation."""
        result = compute_inventory_valuation(
            np.array([100.0, 5.0]), np.array([0.0, 10.0])
        )
        assert abs(result - 50.0) < 1e-9


# ── aggregations.py edge cases ────────────────────────────────────────────────

class TestAggregations:
    def test_group_sum_basic(self):
        rows = [{"k": "a", "v": 1}, {"k": "a", "v": 2}, {"k": "b", "v": 3}]
        out = group_sum(rows, key="k", value="v")
        assert out == {"a": 3.0, "b": 3.0}

    def test_group_sum_empty(self):
        assert group_sum([], key="k", value="v") == {}

    def test_group_sum_single_key(self):
        rows = [{"k": "x", "v": 99}]
        assert group_sum(rows, key="k", value="v") == {"x": 99.0}

    def test_group_sum_float_values(self):
        rows = [{"k": "a", "v": 1.5}, {"k": "a", "v": 2.5}]
        out = group_sum(rows, key="k", value="v")
        assert abs(out["a"] - 4.0) < 1e-9

    def test_sum_by_period_empty(self):
        """sum_by_period on empty array must return 0."""
        result = float(sum_by_period(np.array([], dtype=np.float64)))
        assert result == 0.0

    def test_sum_by_period_single(self):
        result = float(sum_by_period(np.array([42.0], dtype=np.float64)))
        assert abs(result - 42.0) < 1e-9

    def test_sum_by_period_multiple(self):
        result = float(sum_by_period(np.array([1.0, 2.0, 3.0], dtype=np.float64)))
        assert abs(result - 6.0) < 1e-9
