# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1
"""Tests for celerp.modules.slots"""
import pytest
from celerp.modules import slots


@pytest.fixture(autouse=True)
def clear_slots():
    """Isolate each test: start with clean slot registry."""
    slots.clear()
    yield
    slots.clear()


class TestSlotRegistry:
    def test_get_empty_slot_returns_empty_list(self):
        assert slots.get("nav") == []

    def test_register_and_get(self):
        slots.register("nav", {"label": "Labels", "href": "/labels", "order": 10})
        result = slots.get("nav")
        assert len(result) == 1
        assert result[0]["label"] == "Labels"

    def test_multiple_contributions_same_slot(self):
        slots.register("bulk_action", {"label": "Print", "form_action": "/print"})
        slots.register("bulk_action", {"label": "Export", "form_action": "/export"})
        result = slots.get("bulk_action")
        assert len(result) == 2
        labels = {r["label"] for r in result}
        assert labels == {"Print", "Export"}

    def test_different_slots_are_independent(self):
        slots.register("nav", {"label": "A"})
        slots.register("bulk_action", {"label": "B"})
        assert len(slots.get("nav")) == 1
        assert len(slots.get("bulk_action")) == 1
        assert slots.get("settings_tab") == []

    def test_get_returns_copy_not_reference(self):
        slots.register("nav", {"label": "X"})
        result = slots.get("nav")
        result.append({"label": "injected"})
        # Original should be unaffected
        assert len(slots.get("nav")) == 1

    def test_all_slots_snapshot(self):
        slots.register("nav", {"label": "A"})
        slots.register("nav", {"label": "B"})
        slots.register("item_action", {"label": "C"})
        snapshot = slots.all_slots()
        assert len(snapshot["nav"]) == 2
        assert len(snapshot["item_action"]) == 1

    def test_clear_removes_all(self):
        slots.register("nav", {"label": "A"})
        slots.register("bulk_action", {"label": "B"})
        slots.clear()
        assert slots.get("nav") == []
        assert slots.get("bulk_action") == []
