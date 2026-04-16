# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

from __future__ import annotations

import numpy as np

from celerp.compute.aggregations import group_sum, sum_by_period


def test_group_sum_and_sum_by_period() -> None:
    rows = [
        {"k": "a", "v": 1},
        {"k": "a", "v": 2},
        {"k": "b", "v": 3},
    ]
    out = group_sum(rows, key="k", value="v")
    assert out == {"a": 3, "b": 3}

    assert float(sum_by_period(np.array([1.0, 2.0], dtype=np.float64))) == 3.0
