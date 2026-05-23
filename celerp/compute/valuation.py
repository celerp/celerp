# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

import numpy as np


def compute_weighted_average_cost(quantities: np.ndarray, costs: np.ndarray) -> float:
    total_qty = np.sum(quantities)
    if total_qty == 0.0:
        return 0.0
    return float(np.dot(quantities, costs) / total_qty)


def compute_fifo_cost(quantities: np.ndarray, costs: np.ndarray, sell_qty: float) -> float:
    remaining = sell_qty
    total = 0.0
    for i in range(len(quantities)):
        if remaining <= 0.0:
            break
        take = min(float(quantities[i]), remaining)
        total += take * float(costs[i])
        remaining -= take
    return total


def compute_inventory_valuation(quantities: np.ndarray, costs: np.ndarray) -> float:
    return float(np.dot(quantities, costs))
