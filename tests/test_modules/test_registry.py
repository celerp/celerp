# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Tests for celerp.modules.registry"""
from celerp.modules import registry


class TestUsesModule:
    def test_settings_predating_enablement_run_every_module(self):
        assert registry.uses_module(None, "celerp-accounting")
        assert registry.uses_module({"currency": "USD"}, "celerp-accounting")

    def test_listed_module_is_enabled_and_others_are_not(self):
        settings = {"enabled_modules": ["celerp-inventory"]}
        assert registry.uses_module(settings, "celerp-inventory")
        assert not registry.uses_module(settings, "celerp-accounting")

    def test_malformed_list_enables_nothing(self):
        assert not registry.uses_module({"enabled_modules": "celerp-accounting"}, "celerp-accounting")


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
