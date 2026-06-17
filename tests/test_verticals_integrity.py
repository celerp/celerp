# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Data-integrity tests for vertical presets, category schemas, and demo seeding.

These are pure data checks (no DB / no HTTP) over the celerp-verticals JSON
library and the demo-seed dict in celerp.services.demo. They guard three
invariants that previously broke silently:

  * Every preset's `modules` resolve to a real installed module, and every
    `categories[]` entry resolves to a category file. (Would have caught the
    dangling `celerp-warehousing` reference across all presets.)
  * Every demo item's `category` binds to a schema key that applying its preset
    produces - i.e. the stable category slug. (Catches the demo/schema key drift
    that hid every vertical's custom attribute columns.)
  * Demo items in a service category carry `inventory_type: "service"`.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from celerp.modules.audit import _installed_package_names
from celerp.services.demo import _VERTICAL_ITEMS

_VERTICALS_PKG = (
    Path(__file__).resolve().parent.parent
    / "default_modules"
    / "celerp-verticals"
    / "celerp_verticals"
)
_PRESETS_DIR = _VERTICALS_PKG / "presets"
_CATEGORIES_DIR = _VERTICALS_PKG / "categories"

# "General" is the system-wide fallback category, not a vertical schema; the
# blank preset's placeholder demo rows use it by design.
_PLACEHOLDER_CATEGORIES = {"General"}


def _load(path: Path) -> dict:
    return json.loads(path.read_text())


def _categories_by_slug() -> dict[str, dict]:
    return {_load(p)["name"]: _load(p) for p in _CATEGORIES_DIR.glob("*.json")}


def _all_presets() -> list[dict]:
    return [_load(p) for p in sorted(_PRESETS_DIR.glob("*.json"))]


def _preset_category_slugs(preset: dict, cats_by_slug: dict[str, dict]) -> set[str]:
    """The set of schema keys applying this preset would create (slugs)."""
    return {c for c in (preset.get("categories") or []) if c in cats_by_slug}


# ---------------------------------------------------------------------------
# Preset integrity
# ---------------------------------------------------------------------------

def test_preset_integrity():
    """Every preset's modules + categories resolve to real, installed targets."""
    installed_modules = _installed_package_names()
    assert installed_modules, "expected at least one installed module package"
    cats_by_slug = _categories_by_slug()
    assert cats_by_slug, "expected category files on disk"

    problems: list[str] = []
    for preset in _all_presets():
        name = preset["name"]
        for mod in preset.get("modules") or []:
            if mod not in installed_modules:
                problems.append(f"{name}: module {mod!r} is not installed")
        for cat in preset.get("categories") or []:
            if cat not in cats_by_slug:
                problems.append(f"{name}: category {cat!r} has no file")
    assert not problems, "preset integrity failures:\n" + "\n".join(problems)


def test_no_dangling_warehousing_module():
    """Regression guard: the non-existent celerp-warehousing must not reappear."""
    for preset in _all_presets():
        assert "celerp-warehousing" not in (preset.get("modules") or []), preset["name"]


# ---------------------------------------------------------------------------
# Demo -> schema binding
# ---------------------------------------------------------------------------

def test_demo_binding():
    """Every demo item's category binds to a slug its preset would create."""
    cats_by_slug = _categories_by_slug()
    presets_by_name = {p["name"]: p for p in _all_presets()}

    problems: list[str] = []
    for vertical, items in _VERTICAL_ITEMS.items():
        preset = presets_by_name.get(vertical)
        if preset is None:
            # Only assert for demo lists that map to a real preset.
            continue
        valid = _preset_category_slugs(preset, cats_by_slug) | _PLACEHOLDER_CATEGORIES
        for item in items:
            cat = item.get("category")
            if cat not in valid:
                problems.append(
                    f"{vertical}: demo {item.get('sku')} category {cat!r} "
                    f"does not bind to a preset schema slug"
                )
    assert not problems, "demo/schema binding failures:\n" + "\n".join(problems)


# ---------------------------------------------------------------------------
# Service inventory type
# ---------------------------------------------------------------------------

def test_service_type():
    """Demo items in a service-default category carry inventory_type=service."""
    cats_by_slug = _categories_by_slug()
    service_slugs = {
        slug
        for slug, cat in cats_by_slug.items()
        if cat.get("default_inventory_type") == "service"
    }
    assert service_slugs, "expected at least one service category"

    problems: list[str] = []
    for vertical, items in _VERTICAL_ITEMS.items():
        for item in items:
            if item.get("category") in service_slugs:
                if item.get("inventory_type") != "service":
                    problems.append(
                        f"{vertical}: demo {item.get('sku')} in service category "
                        f"{item.get('category')!r} lacks inventory_type=service"
                    )
    assert not problems, "service-type failures:\n" + "\n".join(problems)


@pytest.mark.parametrize("vertical", ["consulting", "saas", "property_rental"])
def test_simple_business_onboarding_has_demo_rows(vertical: str):
    """Service / simple verticals seed at least one demo row for first-run UX."""
    assert _VERTICAL_ITEMS.get(vertical), f"{vertical} has no demo rows"
