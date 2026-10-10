# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Pure projection tests for the cost_base/cost_landed split: cost_total = cost_base + landed,
landed carried as one absolute pool per (bill, kind), a manual cost edit re-adds freight on top
of the new base, and goods arriving with their own cost leave the pools as they are."""
from __future__ import annotations

from celerp_inventory.projections import apply_item_event


def _new(qty=10, cost_total=100):
    return apply_item_event({}, "item.created",
                            {"sku": "X", "quantity": qty, "cost_total": cost_total, "sell_by": "piece"})


def _allocate(state, *, bill="bill:1", kind="freight", amount):
    return apply_item_event(state, "item.landed_cost.allocated",
                            {"source_bill_id": bill, "kind": kind, "amount": amount})


def test_created_bootstraps_base_no_landed():
    s = _new(qty=10, cost_total=100)
    assert s["cost_base"] == 100 and s["cost_landed"] == 0 and s["cost_total"] == 100


def test_allocation_adds_its_amount():
    s = _allocate(_new(qty=10, cost_total=100), amount=20)
    assert s["landed_costs"] == {"bill:1::freight": 20} and s["cost_total"] == 120


def test_allocation_is_absolute_per_bill_and_kind():
    s = _allocate(_new(qty=10, cost_total=100), amount=20)
    s = _allocate(s, amount=50)             # the same (bill, kind) again replaces it
    assert s["cost_landed"] == 50 and s["cost_total"] == 150
    s = _allocate(s, amount=0)              # nothing left of it
    assert "landed_costs" not in s and s["cost_landed"] == 0 and s["cost_total"] == 100


def test_manual_cost_edit_then_freight_readds_on_top():
    """A manual cost edit sets the base; freight allocated later lands on top of that base."""
    s = _allocate(_new(qty=10, cost_total=100), amount=20)
    s = apply_item_event(s, "item.updated",
                         {"fields_changed": {"cost_total": {"old": 120, "new": 80}}})
    assert s["cost_base"] == 80 and s["cost_total"] == 100
    s = _allocate(s, amount=50)
    assert s["cost_base"] == 80 and s["cost_landed"] == 50 and s["cost_total"] == 130


def test_cost_price_edit_sets_base():
    s = _allocate(_new(qty=10, cost_total=100), amount=20)
    s = apply_item_event(s, "item.updated",
                         {"fields_changed": {"cost_price": {"old": 12, "new": 8}}})
    assert s["cost_base"] == 80 and s["cost_total"] == 100


def test_units_leaving_take_their_share_of_each_pool():
    s = _allocate(_new(qty=10, cost_total=100), amount=20)
    s = apply_item_event(s, "item.quantity.adjusted", {"new_qty": 5})
    assert s["cost_base"] == 50 and s["cost_landed"] == 10 and s["cost_total"] == 60


def test_goods_arriving_with_their_own_cost_leave_the_freight():
    s = _allocate(_new(qty=10, cost_total=100), amount=20)
    s = apply_item_event(s, "item.quantity.adjusted", {"new_qty": 15, "cost_base": 150})
    assert s["cost_base"] == 150 and s["cost_landed"] == 20 and s["cost_total"] == 170


def test_explicit_pools_replace_the_lots():
    s = _allocate(_new(qty=10, cost_total=100), amount=20)
    s = apply_item_event(s, "item.quantity.adjusted",
                         {"new_qty": 7, "cost_base": 70, "landed_costs": {"bill:1::freight": 14}})
    assert s["landed_costs"] == {"bill:1::freight": 14} and s["cost_total"] == 84


def test_multiple_kinds_sum():
    s = _allocate(_new(qty=10, cost_total=100), kind="freight", amount=20)
    s = _allocate(s, kind="duty", amount=10)
    assert s["cost_landed"] == 30 and s["cost_total"] == 130


def test_earlier_per_unit_record_replays_as_its_amount():
    """Earlier releases recorded landed cost per unit: the event replays as unit x quantity
    then, and a lot projected with per-unit contributions converts to pools on its next event."""
    s = apply_item_event(_new(qty=10, cost_total=100), "item.landed_cost.applied",
                         {"source_bill_id": "bill:1", "kind": "freight", "unit_amount": 2})
    assert s["landed_costs"] == {"bill:1::freight": 20} and s["cost_total"] == 120
    stored = {**_new(qty=10, cost_total=100), "landed_contributions": {"bill:1::freight": 2.0}}
    s = apply_item_event(stored, "item.quantity.adjusted", {"new_qty": 15, "cost_base": 150})
    assert "landed_contributions" not in s
    assert s["landed_costs"] == {"bill:1::freight": 20} and s["cost_total"] == 170


def test_legacy_item_without_base_unaffected():
    """An item carrying only cost_total (pre-split) round-trips unchanged on any update."""
    legacy = {"sku": "OLD", "quantity": 4, "cost_total": 40}
    s = apply_item_event(legacy, "item.updated", {"fields_changed": {"name": {"old": None, "new": "Renamed"}}})
    assert s["cost_base"] == 40 and s["cost_landed"] == 0 and s["cost_total"] == 40
