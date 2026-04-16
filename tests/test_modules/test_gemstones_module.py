# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1
"""Tests for default_modules/celerp-verticals.

Covers:
- PLUGIN_MANIFEST structure
- Preset JSON loading (gemstones)
- celerp-verticals not in DEFAULT_ENABLED (opt-in)
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

VERTICALS_DIR = Path(__file__).parent.parent.parent / "default_modules" / "celerp-verticals"


def _import_verticals_pkg():
    pkg_dir = VERTICALS_DIR / "celerp_verticals"
    spec = importlib.util.spec_from_file_location(
        "celerp_verticals_root",
        pkg_dir / "__init__.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestVerticalsManifest:
    def test_manifest_exists(self):
        mod = _import_verticals_pkg()
        assert hasattr(mod, "PLUGIN_MANIFEST")

    def test_required_identity_fields(self):
        mod = _import_verticals_pkg()
        m = mod.PLUGIN_MANIFEST
        assert m["name"] == "celerp-verticals"
        assert m["version"]
        assert m["display_name"]
        assert m["license"] == "BSL-1.1"
        assert m["author"]

    def test_has_api_routes(self):
        mod = _import_verticals_pkg()
        assert mod.PLUGIN_MANIFEST["api_routes"] == "celerp_verticals.routes"

    def test_no_migrations(self):
        mod = _import_verticals_pkg()
        assert mod.PLUGIN_MANIFEST["migrations"] is None


class TestGemstonesPreset:
    @pytest.fixture()
    def preset(self):
        import json
        preset_path = VERTICALS_DIR / "celerp_verticals" / "presets" / "gemstones.json"
        return json.loads(preset_path.read_text())

    def test_preset_name(self, preset):
        assert preset["name"] == "gemstones"

    def test_preset_has_categories_list(self, preset):
        """Thin-format preset: categories is a list of slug strings."""
        cats = preset["categories"]
        assert isinstance(cats, list)
        assert len(cats) >= 5
        assert "diamond" in cats
        assert "ruby" in cats
        assert "sapphire" in cats
        assert "emerald" in cats
        assert "jewelry" in cats

    def test_diamond_category_file_exists(self):
        """Diamond category JSON must exist in categories/ dir."""
        import json
        cat_path = VERTICALS_DIR / "celerp_verticals" / "categories" / "diamond.json"
        assert cat_path.exists()
        data = json.loads(cat_path.read_text())
        keys = {f["key"] for f in data["fields"]}
        # Core grading fields must be present
        assert "carat_weight" in keys or "carat" in keys or "weight_ct" in keys
        assert len(keys) >= 4

    def test_all_category_files_exist(self, preset):
        """Every slug listed in the preset must have a corresponding .json file."""
        cats_dir = VERTICALS_DIR / "celerp_verticals" / "categories"
        for slug in preset["categories"]:
            assert (cats_dir / f"{slug}.json").exists(), f"Missing category file: {slug}.json"


class TestHardcodedStoneFieldsRemoved:
    """Verify gemstone-specific fields were removed from core _SUGGESTION_FIELDS."""

    def test_stone_type_not_in_suggestion_fields(self):
        from celerp_inventory.routes import _SUGGESTION_FIELDS
        assert "stone_type" not in _SUGGESTION_FIELDS

    def test_stone_color_not_in_suggestion_fields(self):
        from celerp_inventory.routes import _SUGGESTION_FIELDS
        assert "stone_color" not in _SUGGESTION_FIELDS

    def test_core_fields_still_present(self):
        from celerp_inventory.routes import _SUGGESTION_FIELDS
        assert "category" in _SUGGESTION_FIELDS
        assert "status" in _SUGGESTION_FIELDS
