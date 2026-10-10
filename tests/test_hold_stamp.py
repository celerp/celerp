# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A reserved lot records which line of its document holds it.

The line stamp lives only as long as the reservation: it is written with a reserved
status that names its document and line, and cleared by every other status change. It
is app-owned (no create, edit or import may set it), and a new lot split off a held lot
never inherits it."""
from __future__ import annotations

import pytest

from line_actions_support import h, lot  # noqa: F401

LINE = "6f1c2b9e-1d1a-4c55-9e57-0f3b1a2c4d5e"


def _apply(state, event_type, data):
    from celerp_inventory.projections import apply_item_event
    return apply_item_event(state, event_type, data)


def _reserved(line=LINE):
    data = {"new_status": "reserved", "source_doc_id": "doc:INV-1", "doc_number": "INV-1"}
    if line:
        data["source_line_entity_id"] = line
    return _apply({"status": "available", "quantity": 5}, "item.status.set", data)


def test_reserve_stamps_owner_and_line():
    st = _reserved()
    assert st["status_doc_id"] == "doc:INV-1"
    assert st["status_line_entity_id"] == LINE


def test_reserve_without_line_leaves_no_line_stamp():
    assert "status_line_entity_id" not in _reserved(line=None)


def test_release_clears_line_stamp():
    st = _apply(_reserved(), "item.status.set", {"new_status": "available"})
    assert "status_line_entity_id" not in st and "status_doc_id" not in st


def test_restamp_to_new_owner_without_line_clears_old_line():
    st = _apply(_reserved(), "item.status.set",
                {"new_status": "reserved", "source_doc_id": "doc:INV-2", "doc_number": "INV-2"})
    assert st["status_doc_id"] == "doc:INV-2"
    assert "status_line_entity_id" not in st


def test_shipment_clears_line_stamp():
    st = _apply(_reserved(), "item.fulfilled",
                {"source_doc_id": "doc:INV-1", "doc_number": "INV-1", "quantity_fulfilled": 5,
                 "source_line_entity_id": LINE})
    assert st["status"] == "sold"
    assert "status_line_entity_id" not in st


def test_manual_status_edit_clears_line_stamp():
    st = _apply(_reserved(), "item.updated", {"fields_changed": {"status": {"old": "reserved", "new": "available"}}})
    assert "status_line_entity_id" not in st


def test_line_stamp_is_core_and_app_owned():
    from celerp.services.field_schema import SYSTEM_ITEM_KEYS
    from celerp_inventory.projections import CORE_ITEM_KEYS
    assert "status_line_entity_id" in CORE_ITEM_KEYS
    assert "status_line_entity_id" in SYSTEM_ITEM_KEYS


@pytest.mark.asyncio
async def test_item_create_cannot_set_line_stamp(client, h):
    r = await client.post("/items", headers=h, json={
        "sku": "STAMP-1", "name": "x", "quantity": 1, "sell_by": "piece", "status": "available",
        "status_line_entity_id": LINE})
    assert r.status_code == 422, r.text


def test_split_child_does_not_inherit_hold_stamp():
    from celerp_inventory.services import lot_fields
    child = lot_fields({"sku": "P", "status": "reserved", "status_doc_id": "doc:INV-1",
                        "status_doc_number": "INV-1", "status_line_entity_id": LINE})
    assert not {"status_doc_id", "status_doc_number", "status_line_entity_id"} & set(child)
