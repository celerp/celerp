# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Pure landed-cost allocation engine (P3): value-basis, per-kind, per-unit, exact reconciliation."""
from __future__ import annotations

from celerp.services.landed_cost import allocate_landed_cost


def _landed_total(per_unit: dict[str, float], qty_by_key: dict[str, float]) -> float:
    return round(sum(per_unit[k].get(kind, 0) * qty_by_key[k]
                     for k in per_unit for kind in per_unit[k]), 2)


def test_value_basis_split():
    goods = [{"key": "A", "value": 100, "qty": 10}, {"key": "B", "value": 300, "qty": 3}]
    res = allocate_landed_cost(goods, [{"kind": "freight", "amount": 40}])
    # A gets 100/400 = 25%, B gets 75% -> A 10 (unit 1.0), B 30 (unit 10.0)
    assert round(res["A"]["freight"] * 10, 2) == 10.0
    assert round(res["B"]["freight"] * 3, 2) == 30.0


def test_reconciles_to_pool_exactly_with_rounding():
    # 10 across 3 equal lines -> 3.33/3.33/3.34, sums to 10.00 exactly.
    goods = [{"key": k, "value": 100, "qty": 1} for k in ("A", "B", "C")]
    res = allocate_landed_cost(goods, [{"kind": "duty", "amount": 10}])
    total = _landed_total(res, {"A": 1, "B": 1, "C": 1})
    assert total == 10.00


def test_multiple_kinds_independent():
    goods = [{"key": "A", "value": 100, "qty": 10}, {"key": "B", "value": 100, "qty": 10}]
    res = allocate_landed_cost(goods, [{"kind": "freight", "amount": 20}, {"kind": "duty", "amount": 10}])
    assert round(res["A"]["freight"] * 10, 2) == 10.0 and round(res["A"]["duty"] * 10, 2) == 5.0


def test_zero_value_falls_back_to_quantity():
    goods = [{"key": "A", "value": 0, "qty": 3}, {"key": "B", "value": 0, "qty": 1}]
    res = allocate_landed_cost(goods, [{"kind": "freight", "amount": 8}])
    # by quantity: A 3/4 -> 6, B 1/4 -> 2
    assert round(res["A"]["freight"] * 3, 2) == 6.0 and round(res["B"]["freight"] * 1, 2) == 2.0


def test_empty_inputs():
    assert allocate_landed_cost([], [{"kind": "freight", "amount": 10}]) == {}
    goods = [{"key": "A", "value": 100, "qty": 1}]
    assert allocate_landed_cost(goods, []) == {"A": {}}
