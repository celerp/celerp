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


# ---------------------------------------------------------------------------
# Amharic (am) locale invariants
#
# am.json is a full mirror of en.json's keyset with native Amharic values.
# These invariants guard the two hazards of adding a locale: a keyset drift
# that silently drops UI copy, and a {name} placeholder mismatch that makes
# t().format() raise KeyError at render time for am users.
# ---------------------------------------------------------------------------

import string


def _load_locale(code: str) -> dict:
    path = Path(_LOCALES_DIR) / f"{code}.json"
    assert path.exists(), f"ui/locales/{code}.json must exist"
    return json.loads(path.read_text())


def _placeholders(value: str) -> set[str]:
    """Named {placeholder} fields in a t() template, matching str.format()."""
    return {
        field for _, field, _, _ in string.Formatter().parse(value)
        if field is not None
    }


def test_am_keyset_parity():
    """am.json must contain exactly en.json's keyset: no missing, no extra key."""
    en = _load_locale("en")
    am = _load_locale("am")
    missing = sorted(set(en) - set(am))
    extra = sorted(set(am) - set(en))
    assert not missing, f"am.json missing keys: {missing}"
    assert not extra, f"am.json has keys absent from en.json: {extra}"


def test_am_placeholder_parity():
    """For every key, am's {name} placeholder set must equal en's; a mismatch
    makes t(key, 'am', **kwargs).format() raise KeyError at render time."""
    en = _load_locale("en")
    am = _load_locale("am")
    mismatched = {
        key: (sorted(_placeholders(en[key])), sorted(_placeholders(am[key])))
        for key in en
        if key in am and _placeholders(en[key]) != _placeholders(am[key])
    }
    assert not mismatched, f"Placeholder mismatch (en, am) per key: {mismatched}"


def test_am_values_nonempty_valid():
    """am.json must be valid JSON with every value a non-empty string."""
    am = _load_locale("am")
    assert isinstance(am, dict)
    bad = [k for k, v in am.items() if not isinstance(v, str) or not v.strip()]
    assert bad == [], f"am.json keys with empty/non-string values: {bad}"


def test_am_no_em_dash():
    """No em dash (U+2014) in any am value; Amharic uses its own punctuation.

    Deliberately scoped to am, the content this branch introduces. en.json and
    every shipped locale already carry em dashes in a handful of legacy keys
    (e.g. label._none, inv._select_a_preset), so a suite-wide invariant would
    fail on pre-existing English copy unrelated to this change."""
    am = _load_locale("am")
    em_dash = "\u2014"  # U+2014 EM DASH
    offending = [k for k, v in am.items() if isinstance(v, str) and em_dash in v]
    assert offending == [], f"am.json values containing an em dash: {offending}"


# ---------------------------------------------------------------------------
# Topbar language switcher: native labels + searchable combobox
# ---------------------------------------------------------------------------

def _render_topbar() -> str:
    from fasthtml.common import to_xml
    from ui.components.shell import _topbar
    return to_xml(_topbar([], lang="en"))


def test_switcher_renders_native_labels():
    """The switcher shows each locale's native name, not its upper-case code."""
    html = _render_topbar()
    assert "ไทย" in html, "topbar switcher must render the native locale label 'ไทย', not 'TH'"


def test_switcher_is_searchable_combobox():
    """The switcher is the app's searchable_select combobox, not a native <select>."""
    html = _render_topbar()
    assert "combobox-input" in html, "topbar switcher must be the searchable combobox (combobox-input)"


def test_switcher_renders_native_amharic_label():
    """am.json ships with this branch, so the switcher must show its native name
    'አማርኛ' rather than the upper-case code 'AM'."""
    html = _render_topbar()
    assert "አማርኛ" in html, "topbar switcher must render the native Amharic label 'አማርኛ', not 'AM'"


def test_switcher_has_aria_combobox_semantics():
    """The switcher exposes ARIA combobox semantics (rule i): the visible input is
    a role=combobox owning a listbox of role=option items. The dynamic wiring
    (aria-controls / aria-expanded / aria-activedescendant) is added by
    initCombobox at runtime; these static roles are what the server must render."""
    html = _render_topbar()
    assert 'role="combobox"' in html, "combobox input must carry role=combobox"
    assert 'aria-haspopup="listbox"' in html, "combobox input must declare aria-haspopup=listbox"
    assert 'aria-autocomplete="list"' in html, "combobox input must declare aria-autocomplete=list"
    assert 'role="listbox"' in html, "the option list must carry role=listbox"
    assert 'role="option"' in html, "each option must carry role=option"
