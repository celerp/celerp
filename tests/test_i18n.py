# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Unit tests for ui.i18n - translation infrastructure."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Module under test
# ---------------------------------------------------------------------------

# Force non-cached mode so lru_cache doesn't interfere across tests
os.environ.pop("CELERP_DEBUG_I18N", None)
from ui.i18n import t, _LOCALES_DIR


# ---------------------------------------------------------------------------
# Basic lookup
# ---------------------------------------------------------------------------

def test_basic_lookup():
    assert t("btn.save", "en") == "Save"


def test_basic_lookup_nav():
    assert t("nav.dashboard", "en") == "Dashboard"


def test_basic_lookup_page():
    assert t("page.inventory", "en") == "Inventory"


def test_basic_lookup_th():
    assert t("th.name", "en") == "Name"


def test_basic_lookup_label():
    assert t("label.sku", "en") == "SKU"


# ---------------------------------------------------------------------------
# Unknown locale fallback to English
# ---------------------------------------------------------------------------

def test_unknown_locale_falls_back_to_english():
    """A locale code that doesn't exist ('xx') must fall back to English."""
    assert t("btn.save", "xx") == "Save"


def test_unknown_locale_falls_back_all_keys():
    """Every key in en.json should be reachable via an unknown locale."""
    en = json.loads((Path(_LOCALES_DIR) / "en.json").read_text())
    for key in list(en.keys())[:20]:  # spot-check first 20
        assert t(key, "xx") == en[key]


# ---------------------------------------------------------------------------
# Missing key returns the key itself (no crash)
# ---------------------------------------------------------------------------

def test_missing_key_returns_key():
    result = t("nonexistent.key.xyz", "en")
    assert result == "nonexistent.key.xyz"


def test_missing_key_in_unknown_locale():
    result = t("nonexistent.key.xyz", "xx")
    assert result == "nonexistent.key.xyz"


# ---------------------------------------------------------------------------
# Parameterised interpolation
# ---------------------------------------------------------------------------

def test_interpolation_count():
    result = t("flash.items_transferred", "en", count=3)
    assert result == "3 item(s) transferred."


def test_interpolation_import():
    result = t("flash.import_complete", "en", created=10, skipped=2)
    assert result == "Import complete: 10 created, 2 skipped."


def test_interpolation_items_updated():
    result = t("flash.items_updated", "en", count=5, status="active")
    assert result == "5 item(s) updated to 'active'."


def test_interpolation_items_deleted():
    result = t("flash.items_deleted", "en", count=7)
    assert result == "7 item(s) deleted."


# ---------------------------------------------------------------------------
# en.json validation
# ---------------------------------------------------------------------------

def test_en_json_exists():
    path = Path(_LOCALES_DIR) / "en.json"
    assert path.exists(), "ui/locales/en.json must exist"


def test_en_json_is_valid_json():
    path = Path(_LOCALES_DIR) / "en.json"
    data = json.loads(path.read_text())
    assert isinstance(data, dict)


def test_en_json_no_empty_values():
    """All keys in en.json must have non-empty string values."""
    path = Path(_LOCALES_DIR) / "en.json"
    data = json.loads(path.read_text())
    empty_keys = [k for k, v in data.items() if not isinstance(v, str) or not v.strip()]
    assert empty_keys == [], f"Keys with empty values: {empty_keys}"


def test_en_json_has_required_namespaces():
    """en.json must have at least one key per expected namespace."""
    path = Path(_LOCALES_DIR) / "en.json"
    data = json.loads(path.read_text())
    namespaces = {k.split(".")[0] for k in data}
    required = {"nav", "btn", "label", "th", "page", "flash", "error", "msg"}
    missing = required - namespaces
    assert not missing, f"Missing namespaces in en.json: {missing}"


def test_en_json_key_format():
    """All keys must follow namespace.name convention (dot-separated)."""
    path = Path(_LOCALES_DIR) / "en.json"
    data = json.loads(path.read_text())
    bad_keys = [k for k in data if "." not in k]
    assert bad_keys == [], f"Keys without namespace: {bad_keys}"


# ---------------------------------------------------------------------------
# Interpolation edge cases
# ---------------------------------------------------------------------------

def test_no_kwargs_returns_raw_value():
    """Calling t() without kwargs must not attempt .format() on the string."""
    # "Save" has no braces — should work fine
    assert t("btn.save", "en") == "Save"


def test_kwargs_with_missing_placeholder_raises():
    """If the template has {count} but we pass wrong kwarg, KeyError is expected."""
    with pytest.raises(KeyError):
        t("flash.items_transferred", "en", wrong_kwarg=3)


def test_missing_key_with_kwargs_returns_key_formatted():
    """Missing key with kwargs — key itself is returned (no format attempted since key has no braces)."""
    result = t("nonexistent.key.xyz", "en", count=5)
    assert result == "nonexistent.key.xyz"
