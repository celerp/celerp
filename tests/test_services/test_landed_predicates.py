# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Pure predicates that classify charge/non-stock lines for the freight & landed-cost feature."""
from __future__ import annotations

import pytest

from celerp.services.units import (
    LANDED_COST_KINDS,
    NON_STOCK_INVENTORY_TYPES,
    is_landed_component,
    is_non_stock_line,
)


@pytest.mark.parametrize("inv_type,sell_by,expected", [
    ("stocked", "piece", False),
    ("component", "piece", False),
    ("non_stocked", "piece", False),   # non_stocked keeps its existing fulfillment behaviour
    ("service", "piece", True),
    ("freight", "piece", True),
    (None, "piece", False),
    ("stocked", "service", True),      # service unit forces non-stock regardless of type
    ("stocked", "hour", True),
    ("freight", None, True),
])
def test_is_non_stock_line(inv_type, sell_by, expected):
    assert is_non_stock_line(inv_type, sell_by) is expected


@pytest.mark.parametrize("inv_type,expected", [
    ("freight", True),
    ("stocked", False),
    ("service", False),
    (None, False),
])
def test_is_landed_component(inv_type, expected):
    assert is_landed_component(inv_type) is expected


def test_kind_and_type_sets():
    assert LANDED_COST_KINDS == {"freight", "insurance", "duty", "import_vat"}
    assert NON_STOCK_INVENTORY_TYPES == {"service", "freight"}
