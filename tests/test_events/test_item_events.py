# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

from __future__ import annotations

import pytest

from celerp_inventory.projections import apply_item_event


def test_item_all_events() -> None:
    state = apply_item_event({}, "item.created", {"sku": "S", "name": "A", "quantity": 1})
    assert state["is_available"] is True

    state = apply_item_event(state, "item.updated", {"fields_changed": {"name": {"old": "A", "new": "B"}}})
    assert state["name"] == "B"

    state = apply_item_event(state, "item.pricing.set", {"price_type": "price", "new_price": 10})
    assert state["price"] == 10

    state = apply_item_event(state, "item.status.set", {"new_status": "active"})
    assert state["status"] == "active"

    state = apply_item_event(state, "item.transferred", {"to_location_id": "loc:1"})
    assert state["location_id"] == "loc:1"

    state = apply_item_event(state, "item.quantity.adjusted", {"new_qty": 5})
    assert state["quantity"] == 5

    state = apply_item_event(state, "item.produced", {"quantity_produced": 3})
    assert state["quantity"] == 8

    state = apply_item_event(state, "item.consumed", {"quantity_consumed": 2})
    assert state["quantity"] == 6

    state = apply_item_event(state, "item.reserved", {"quantity": 1.5})
    assert state["reserved_quantity"] == 1.5

    state = apply_item_event(state, "item.unreserved", {"quantity": 10})
    assert state["reserved_quantity"] == 0

    state2 = apply_item_event(state, "item.split", {"child_ids": ["x", "y"], "child_skus": ["SKU-A", "SKU-B"], "quantities": [1.0, 1.0]})
    # Parent stays available after split (qty reduced via item.quantity.adjusted)
    assert state2.get("is_available") is not False
    assert state2["children"] == ["x", "y"]

    state3 = apply_item_event(state, "item.merged", {"source_entity_ids": ["a"]})
    # item.merged is a no-op marker; quantity unchanged
    assert state3["quantity"] == 6

    state4 = apply_item_event(state, "item.expired", {})
    assert state4["is_available"] is False and state4["is_expired"] is True

    state5 = apply_item_event(state, "item.disposed", {})
    assert state5["is_available"] is False and state5["is_expired"] is False


def test_item_snapshot_is_like_create() -> None:
    state = apply_item_event({}, "item.snapshot", {"sku": "S", "name": "A", "quantity": 1})
    assert state["is_available"] is True


def test_item_unknown_raises() -> None:
    with pytest.raises(ValueError):
        apply_item_event({}, "item.nope", {})


# ---------------------------------------------------------------------------
# Expiry wiring
# ---------------------------------------------------------------------------

def test_expiry_date_attribute_syncs_to_expires_at_on_create() -> None:
    state = apply_item_event({}, "item.created", {
        "sku": "RICE-001", "name": "Jasmine Rice", "quantity": 500,
        "attributes": {"lot_no": "B-001", "expiry_date": "2026-06-15"},
    })
    assert state["expires_at"] == "2026-06-15"


def test_warranty_exp_attribute_syncs_to_expires_at_on_create() -> None:
    state = apply_item_event({}, "item.created", {
        "sku": "LAPTOP-X1", "name": "ThinkPad X1",
        "attributes": {"warranty_exp": "2028-12-31"},
    })
    assert state["expires_at"] == "2028-12-31"


def test_expiry_date_update_resyncs_expires_at() -> None:
    state = apply_item_event({}, "item.created", {
        "sku": "RICE-001", "name": "Rice",
        "attributes": {"expiry_date": "2026-06-15"},
    })
    assert state["expires_at"] == "2026-06-15"
    state = apply_item_event(state, "item.updated", {
        "fields_changed": {"attributes": {"new": {"expiry_date": "2026-09-01"}}},
    })
    assert state["expires_at"] == "2026-09-01"


def test_no_expiry_attribute_leaves_expires_at_absent() -> None:
    state = apply_item_event({}, "item.created", {"sku": "WIDGET", "name": "Widget"})
    assert "expires_at" not in state


# ---------------------------------------------------------------------------
# Merge projection
# ---------------------------------------------------------------------------

def test_merged_is_noop_marker() -> None:
    """item.merged is a no-op marker; it must not mutate the state."""
    state = {"sku": "RICE-001", "quantity": 300, "attributes": {}}
    result = apply_item_event(state, "item.merged", {
        "source_entity_ids": ["item:a", "item:b"],
    })
    assert result["quantity"] == 300
    assert "expires_at" not in result


def test_source_deactivated_zeros_qty_and_marks_unavailable() -> None:
    state = {"sku": "RICE-001", "quantity": 300, "is_available": True}
    result = apply_item_event(state, "item.source_deactivated", {"merged_into": "item:target-1"})
    assert result["is_available"] is False
    assert result["quantity"] == 0
    assert result["merged_into"] == "item:target-1"



def test_migrate_old_sell_by_weight_to_carat():
    """Old sell_by=weight + weight_unit=ct should migrate to sell_by=carat."""
    state = apply_item_event({}, "item.created", {
        "sku": "MIG-1", "name": "Emerald", "quantity": 1, "sell_by": "weight",
        "weight": 3.2, "weight_unit": "ct", "attributes": {},
    })
    assert state["sell_by"] == "carat"
    assert state["quantity"] == 3.2
    assert "weight" not in state
    assert "weight_unit" not in state


def test_migrate_old_sell_by_weight_parcel_moves_pieces():
    """Parcel with qty>1 and weight should move qty to attributes.pieces."""
    state = apply_item_event({}, "item.created", {
        "sku": "MIG-2", "name": "Parcel", "quantity": 20, "sell_by": "weight",
        "weight": 50.0, "weight_unit": "ct", "attributes": {},
    })
    assert state["sell_by"] == "carat"
    assert state["quantity"] == 50.0
    assert state["attributes"]["pieces"] == 20
    assert "weight" not in state


def test_new_sell_by_unit_no_migration():
    """New items with sell_by already a unit name should not be migrated."""
    state = apply_item_event({}, "item.created", {
        "sku": "NEW-1", "name": "Ring", "quantity": 5, "sell_by": "piece",
        "attributes": {},
    })
    assert state["sell_by"] == "piece"
    assert state["quantity"] == 5
    assert "pieces" not in state.get("attributes", {})


def test_split_does_not_mark_unavailable():
    """item.split event should NOT set is_available to False."""
    state = apply_item_event(
        {"sku": "SP-1", "quantity": 10, "is_available": True},
        "item.split",
        {"child_ids": ["item:c1", "item:c2"], "child_skus": ["C1", "C2"], "quantities": [3.0, 3.0]},
    )
    assert state["is_available"] is True
    assert state["children"] == ["item:c1", "item:c2"]
    assert state["child_skus"] == ["C1", "C2"]
