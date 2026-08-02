# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

from __future__ import annotations

import pytest

from celerp_inventory.projections import apply_item_event, is_item_available


def test_item_all_events() -> None:
    state = apply_item_event({}, "item.created", {"sku": "S", "name": "A", "quantity": 1})
    assert is_item_available(state)

    state = apply_item_event(state, "item.updated", {"fields_changed": {"name": {"old": "A", "new": "B"}}})
    assert state["name"] == "B"

    state = apply_item_event(state, "item.pricing.set", {"price_type": "price", "new_price": 10})
    assert state["price"] == 10

    state = apply_item_event(state, "item.status.set", {"new_status": "available"})
    assert state["status"] == "available"

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
    assert is_item_available(state2)
    assert state2["children"] == ["x", "y"]

    state3 = apply_item_event(state, "item.merged", {"source_entity_ids": ["a"]})
    # item.merged is a no-op marker; quantity unchanged
    assert state3["quantity"] == 6

    state4 = apply_item_event(state, "item.expired", {})
    assert not is_item_available(state4) and state4["is_expired"] is True

    state5 = apply_item_event(state, "item.status.set", {"new_status": "archived"})
    assert state5["status"] == "archived"


def test_item_snapshot_is_like_create() -> None:
    state = apply_item_event({}, "item.snapshot", {"sku": "S", "name": "A", "quantity": 1})
    assert is_item_available(state)


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


def test_source_deactivated_preserves_original_qty_and_marks_unavailable() -> None:
    state = {"sku": "RICE-001", "quantity": 300, "status": "available"}
    result = apply_item_event(state, "item.source_deactivated", {"merged_into": "item:target-1", "original_qty": 300.0})
    assert not is_item_available(result)
    assert result["quantity"] == 300.0
    assert result["status"] == "merged"
    assert result["merged_into"] == "item:target-1"


def test_source_deactivated_falls_back_to_current_qty_when_no_original_qty() -> None:
    """If original_qty is absent (old events), current quantity is preserved."""
    state = {"sku": "RICE-002", "quantity": 50.0, "status": "available"}
    result = apply_item_event(state, "item.source_deactivated", {"merged_into": "item:target-2"})
    assert result["quantity"] == 50.0
    assert not is_item_available(result)



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


def test_item_created_preserves_allow_splitting_false() -> None:
    """Projection state must preserve allow_splitting=False from create events."""
    state = apply_item_event({}, "item.created", {
        "sku": "NS-001", "name": "No Split", "quantity": 5, "sell_by": "piece",
        "allow_splitting": False,
    })
    assert state["allow_splitting"] is False


def test_item_updated_can_disable_allow_splitting() -> None:
    """Projection state must preserve allow_splitting=False from update events."""
    state = apply_item_event({}, "item.created", {
        "sku": "NS-002", "name": "No Split", "quantity": 5, "sell_by": "piece",
        "allow_splitting": True,
    })
    state = apply_item_event(state, "item.updated", {
        "fields_changed": {"allow_splitting": {"old": True, "new": False}},
    })
    assert state["allow_splitting"] is False


def test_split_does_not_mark_unavailable():
    """item.split event should NOT make parent unavailable."""
    state = apply_item_event(
        {"sku": "SP-1", "quantity": 10, "status": "available"},
        "item.split",
        {"child_ids": ["item:c1", "item:c2"], "child_skus": ["C1", "C2"], "quantities": [3.0, 3.0]},
    )
    assert is_item_available(state)
    assert state["children"] == ["item:c1", "item:c2"]
    assert state["child_skus"] == ["C1", "C2"]


def test_status_doc_pairing_follows_status_writers() -> None:
    """The status cell shows the document that caused the current status. Doc-driven
    status changes (fulfil, memo->invoice conversion) stamp status_doc_id/_number;
    every doc-less status change clears the pairing so it can never go stale."""
    state = apply_item_event({}, "item.created", {"sku": "S", "name": "A", "quantity": 2})
    assert "status_doc_id" not in state

    # Fulfil to a memo stamps the memo's number
    state = apply_item_event(state, "item.fulfilled", {
        "quantity_fulfilled": 2.0, "source_doc_id": "doc:MEMO-2026-0001",
        "doc_number": "MEMO-2026-0001", "doc_type": "memo"})
    assert state["status"] == "memo_out"
    assert state["status_doc_id"] == "doc:MEMO-2026-0001"
    assert state["status_doc_number"] == "MEMO-2026-0001"

    # memo->invoice conversion re-stamps with the invoice via item.status.set
    state = apply_item_event(state, "item.status.set", {
        "new_status": "sold", "source_doc_id": "doc:INV-2026-0001",
        "doc_number": "INV-2026-0001"})
    assert state["status"] == "sold"
    assert state["status_doc_id"] == "doc:INV-2026-0001"
    assert state["status_doc_number"] == "INV-2026-0001"

    # Unfulfil clears the pairing with the status
    state = apply_item_event(state, "item.fulfillment_reversed", {
        "quantity_restored": 2.0, "source_doc_id": "doc:INV-2026-0001"})
    assert state["status"] == "available"
    assert "status_doc_id" not in state and "status_doc_number" not in state

    # A doc-less manual status set clears any stamp
    state = apply_item_event(state, "item.fulfilled", {
        "quantity_fulfilled": 2.0, "source_doc_id": "doc:INV-2026-0002",
        "doc_number": "INV-2026-0002", "doc_type": "invoice"})
    state = apply_item_event(state, "item.status.set", {"new_status": "available"})
    assert "status_doc_id" not in state and "status_doc_number" not in state

    # A manual field edit of status clears too
    state = apply_item_event(state, "item.fulfilled", {
        "quantity_fulfilled": 2.0, "source_doc_id": "doc:INV-2026-0003",
        "doc_number": "INV-2026-0003", "doc_type": "invoice"})
    state = apply_item_event(state, "item.updated", {
        "fields_changed": {"status": {"old": "sold", "new": "available"}}})
    assert state["status"] == "available"
    assert "status_doc_id" not in state and "status_doc_number" not in state

    # Terminal states clear as well
    state = apply_item_event(state, "item.status.set", {
        "new_status": "sold", "source_doc_id": "doc:INV-2026-0004",
        "doc_number": "INV-2026-0004"})
    state = apply_item_event(state, "item.expired", {})
    assert "status_doc_id" not in state and "status_doc_number" not in state

    # CSV upsert that changes status without a source doc clears; one that does not
    # touch status leaves the pairing alone (and keeps it top-level, not an attribute)
    state = apply_item_event(state, "item.status.set", {
        "new_status": "sold", "source_doc_id": "doc:INV-2026-0005",
        "doc_number": "INV-2026-0005"})
    state = apply_item_event(state, "item.patched", {"notes": "recount"})
    assert state["status_doc_number"] == "INV-2026-0005"
    assert "status_doc_number" not in (state.get("attributes") or {})
    state = apply_item_event(state, "item.patched", {"status": "available"})
    assert "status_doc_id" not in state and "status_doc_number" not in state


def _sold_with_pairing() -> dict:
    state = apply_item_event({}, "item.created", {"sku": "S", "name": "A", "quantity": 1})
    return apply_item_event(state, "item.status.set", {
        "new_status": "sold", "source_doc_id": "doc:INV-2026-0005",
        "doc_number": "INV-2026-0005"})


def test_item_patched_number_without_id_drops_pairing() -> None:
    """An upsert carrying status_doc_number but no status_doc_id leaves an
    unverifiable half-pair; the pairing is dropped, not merged."""
    state = apply_item_event(_sold_with_pairing(), "item.patched",
                             {"status_doc_number": "INV-9999"})
    assert "status_doc_id" not in state and "status_doc_number" not in state

    # An empty-string id counts as absent (the CSV path feeds unfiltered values)
    state = apply_item_event(_sold_with_pairing(), "item.patched",
                             {"status_doc_id": "", "status_doc_number": "INV-9999"})
    assert "status_doc_id" not in state and "status_doc_number" not in state


def test_item_patched_id_without_number_drops_pairing() -> None:
    """The reverse half-pair (id without number) is dropped the same way."""
    state = apply_item_event(_sold_with_pairing(), "item.patched",
                             {"status_doc_id": "doc:INV-9999"})
    assert "status_doc_id" not in state and "status_doc_number" not in state
