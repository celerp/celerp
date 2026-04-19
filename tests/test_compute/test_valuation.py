# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
import numpy as np

from celerp.compute.valuation import compute_fifo_cost, compute_inventory_valuation, compute_weighted_average_cost


def test_weighted_average_cost() -> None:
    assert compute_weighted_average_cost(np.array([2.0, 1.0]), np.array([10.0, 20.0])) == 40.0 / 3.0


def test_fifo_cost() -> None:
    assert compute_fifo_cost(np.array([2.0, 1.0]), np.array([10.0, 20.0]), 2.5) == 30.0


def test_inventory_valuation() -> None:
    assert compute_inventory_valuation(np.array([2.0, 3.0]), np.array([10.0, 5.0])) == 35.0
