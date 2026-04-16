# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1
"""Tests for celerp.modules.registry"""
from celerp.modules import registry


class TestGetEnabled:
    def test_none_settings_returns_empty(self):
        result = registry.get_enabled(None)
        assert result == set()

    def test_empty_settings_returns_empty(self):
        result = registry.get_enabled({})
        assert result == set()

    def test_explicit_list_returned_as_set(self):
        settings = {"enabled_modules": ["celerp-labels", "gemstones"]}
        result = registry.get_enabled(settings)
        assert result == {"celerp-labels", "gemstones"}

    def test_empty_list_returns_empty_set(self):
        settings = {"enabled_modules": []}
        result = registry.get_enabled(settings)
        assert result == set()

    def test_invalid_type_returns_empty(self):
        settings = {"enabled_modules": "not-a-list"}
        result = registry.get_enabled(settings)
        assert result == set()


class TestSetEnabled:
    def test_set_enabled_updates_key(self):
        updated = registry.set_enabled({}, {"mod-a", "mod-b"})
        assert set(updated["enabled_modules"]) == {"mod-a", "mod-b"}

    def test_set_enabled_returns_new_dict(self):
        original = {}
        updated = registry.set_enabled(original, {"mod-a"})
        assert "enabled_modules" not in original
        assert "enabled_modules" in updated

    def test_set_enabled_sorted(self):
        updated = registry.set_enabled({}, {"z-mod", "a-mod"})
        assert updated["enabled_modules"] == ["a-mod", "z-mod"]


class TestEnableDisable:
    def test_enable_adds_module(self):
        settings = {"enabled_modules": ["mod-a"]}
        updated = registry.enable(settings, "mod-b")
        assert "mod-b" in updated["enabled_modules"]
        assert "mod-a" in updated["enabled_modules"]

    def test_enable_idempotent(self):
        settings = {"enabled_modules": ["mod-a"]}
        updated = registry.enable(settings, "mod-a")
        assert updated["enabled_modules"].count("mod-a") == 1

    def test_disable_removes_module(self):
        settings = {"enabled_modules": ["mod-a", "mod-b"]}
        updated = registry.disable(settings, "mod-a")
        assert "mod-a" not in updated["enabled_modules"]
        assert "mod-b" in updated["enabled_modules"]

    def test_disable_nonexistent_is_noop(self):
        settings = {"enabled_modules": ["mod-a"]}
        updated = registry.disable(settings, "mod-b")
        assert updated["enabled_modules"] == ["mod-a"]
