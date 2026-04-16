# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

import numpy as np
from numba import njit


@njit
def compute_weighted_average_cost(quantities: np.ndarray, costs: np.ndarray) -> float:  # pragma: no cover
    total_qty = 0.0
    total_cost = 0.0
    for i in range(len(quantities)):
        total_qty += quantities[i]
        total_cost += quantities[i] * costs[i]
    if total_qty == 0.0:
        return 0.0
    return total_cost / total_qty


@njit
def compute_fifo_cost(quantities: np.ndarray, costs: np.ndarray, sell_qty: float) -> float:  # pragma: no cover
    remaining = sell_qty
    total = 0.0
    for i in range(len(quantities)):
        if remaining <= 0.0:
            break
        take = quantities[i] if quantities[i] < remaining else remaining
        total += take * costs[i]
        remaining -= take
    return total


@njit
def compute_inventory_valuation(quantities: np.ndarray, costs: np.ndarray) -> float:  # pragma: no cover
    total = 0.0
    for i in range(len(quantities)):
        total += quantities[i] * costs[i]
    return total
