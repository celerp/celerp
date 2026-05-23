# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

from __future__ import annotations

from collections import defaultdict
from typing import Any

import numpy as np


def sum_by_period(values: np.ndarray) -> float:
    return float(np.sum(values))


def group_sum(rows: list[dict[str, Any]], *, key: str, value: str) -> dict[str, float]:
    out: dict[str, float] = defaultdict(float)
    for row in rows:
        out[str(row[key])] += float(row[value])
    return dict(out)
