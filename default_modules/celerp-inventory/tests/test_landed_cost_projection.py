# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Pure projection tests for the cost_base/cost_landed split (P3): cost_total = cost_base + landed,
landed stored per-unit and re-derivable, a manual cost edit re-adds freight on top of the new base."""
from __future__ import annotations

from celerp_inventory.projections import apply_item_event


def _new(qty=10, cost_total=100):
    return apply_item_event({}, "item.created",
                            {"sku": "X", "quantity": qty, "cost_total": cost_total, "sell_by": "piece"})


def _apply_landed(state, *, bill="bill:1", kind="freight", unit):
    return apply_item_event(state, "item.landed_cost.applied",
                            {"source_bill_id": bill, "kind": kind, "unit_amount": unit})


def test_created_bootstraps_base_no_landed():
    s = _new(qty=10, cost_total=100)
    assert s["cost_base"] == 100 and s["cost_landed"] == 0 and s["cost_total"] == 100


def test_landed_adds_per_unit_scaled_by_qty():
    s = _new(qty=10, cost_total=100)        # base 100, unit cost 10
    s = _apply_landed(s, unit=2)            # +2/unit landed -> +20 total
    assert s["cost_landed"] == 20 and s["cost_total"] == 120


def test_landed_is_absolute_and_re_derivable():
    s = _new(qty=10, cost_total=100)
    s = _apply_landed(s, unit=2)            # 120
    s = _apply_landed(s, unit=5)            # overwrite same (bill,kind) -> base 100 + 50
    assert s["cost_landed"] == 50 and s["cost_total"] == 150
    s = _apply_landed(s, unit=0)            # clear contribution
    assert s["cost_landed"] == 0 and s["cost_total"] == 100


def test_manual_cost_edit_then_freight_readds_on_top():
    """The headline requirement: a manual cost edit sets the base; a later freight change re-adds
    the import freight on top of that new base."""
    s = _new(qty=10, cost_total=100)
    s = _apply_landed(s, unit=2)            # base 100 + landed 20 = 120
    # User manually corrects the cost (cost_total edit) to 80 -> that is the new BASE.
    s = apply_item_event(s, "item.updated",
                         {"fields_changed": {"cost_total": {"old": 120, "new": 80}}})
    assert s["cost_base"] == 80 and s["cost_total"] == 100   # 80 base + 20 landed still applied
    # Freight is then updated -> re-adds on top of the new base.
    s = _apply_landed(s, unit=5)            # landed now 50
    assert s["cost_base"] == 80 and s["cost_landed"] == 50 and s["cost_total"] == 130


def test_cost_price_edit_sets_base():
    s = _new(qty=10, cost_total=100)
    s = _apply_landed(s, unit=2)            # 120
    # Editing unit cost_price to 8 -> base = 8*10 = 80; landed re-added.
    s = apply_item_event(s, "item.updated",
                         {"fields_changed": {"cost_price": {"old": 12, "new": 8}}})
    assert s["cost_base"] == 80 and s["cost_total"] == 100   # 80 + 20 landed


def test_quantity_change_scales_landed():
    s = _new(qty=10, cost_total=100)
    s = _apply_landed(s, unit=2)            # landed 20 at qty 10
    s = apply_item_event(s, "item.quantity.adjusted", {"new_qty": 5})
    # base is a lot total (unchanged); landed is per-unit -> 2*5 = 10.
    assert s["cost_landed"] == 10 and s["cost_total"] == 110


def test_multiple_kinds_sum():
    s = _new(qty=10, cost_total=100)
    s = _apply_landed(s, kind="freight", unit=2)    # +20
    s = _apply_landed(s, kind="duty", unit=1)       # +10
    assert s["cost_landed"] == 30 and s["cost_total"] == 130


def test_legacy_item_without_base_unaffected():
    """An item carrying only cost_total (pre-split) round-trips unchanged on any update."""
    legacy = {"sku": "OLD", "quantity": 4, "cost_total": 40}
    s = apply_item_event(legacy, "item.updated", {"fields_changed": {"name": {"old": None, "new": "Renamed"}}})
    assert s["cost_base"] == 40 and s["cost_landed"] == 0 and s["cost_total"] == 40
