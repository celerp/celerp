# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Reorder v2: preferred-supplier item field + per-supplier PO grouping."""

from __future__ import annotations

from celerp.services.reorder import group_by_supplier


def test_preferred_supplier_is_a_base_item_field():
    from celerp.services.field_schema import _BASE_FIELDS
    by_key = {f["key"]: f for f in _BASE_FIELDS}
    assert "preferred_supplier" in by_key
    assert by_key["preferred_supplier"]["editable"] is True
    assert by_key["preferred_supplier"]["required"] is False


def test_group_by_supplier_splits_by_supplier():
    items = [
        {"entity_id": "item:1", "preferred_supplier": "c:acme"},
        {"entity_id": "item:2", "preferred_supplier": "c:globex"},
        {"entity_id": "item:3", "preferred_supplier": "c:acme"},
        {"entity_id": "item:4"},  # unassigned
    ]
    groups = group_by_supplier(items)
    assert groups["c:acme"] == ["item:1", "item:3"]
    assert groups["c:globex"] == ["item:2"]
    assert groups[""] == ["item:4"]
    assert len(groups) == 3  # acme, globex, unassigned


def test_group_by_supplier_all_unassigned_is_one_group():
    items = [{"entity_id": "item:1"}, {"entity_id": "item:2", "preferred_supplier": ""}]
    groups = group_by_supplier(items)
    assert list(groups.keys()) == [""]
    assert groups[""] == ["item:1", "item:2"]


def test_group_by_supplier_single_supplier_is_one_group():
    items = [{"entity_id": "item:1", "preferred_supplier": "c:x"},
             {"entity_id": "item:2", "preferred_supplier": "c:x"}]
    groups = group_by_supplier(items)
    assert list(groups.keys()) == ["c:x"]


def test_group_by_supplier_skips_missing_ids():
    groups = group_by_supplier([{"preferred_supplier": "c:x"}, {"entity_id": "item:1"}])
    assert groups == {"": ["item:1"]}
