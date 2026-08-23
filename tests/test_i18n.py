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


# ---------------------------------------------------------------------------
# Cross-locale parity (V1): a shipped locale must render correctly, and its
# completeness is a graded release signal, not merely "it does not crash".
#
# Two tiers:
#   CORRECTNESS - hard for EVERY shipped locale. An extra key is untranslatable
#     dead weight drifting from en; a {placeholder} set that differs from en
#     makes t(key, code, **kwargs).format() raise KeyError at render time.
#     Neither falls back safely, so both are bugs regardless of the language.
#   COMPLETENESS - a locale missing an en key falls back to English via t(),
#     which is safe, so partial coverage is a release-readiness signal, not a
#     crash. It is a HARD gate only for locales declared release-complete in
#     _COMPLETE_LOCALES; for every other shipped locale the missing-key count
#     is reported (a warning), not failed. Membership of _COMPLETE_LOCALES is
#     the release cutover that separates "safe fallback" from "shippable"; a
#     locale is added once it is fully translated.
# ---------------------------------------------------------------------------

# Locales whose keyset completeness is enforced as a hard CI gate. English is
# the source of truth and always complete; a translated locale joins this set
# when it reaches full parity, after which any future missing key fails CI.
_COMPLETE_LOCALES = frozenset({"en"})


def _shipped_locales() -> list[str]:
    """Every central locale shipped on disk (ui/locales/<code>.json)."""
    return sorted(p.stem for p in Path(_LOCALES_DIR).glob("*.json"))


def test_every_shipped_locale_has_no_extra_keys():
    """CORRECTNESS (hard, all locales): no locale may carry a key absent from
    en.json - an extra key is dead weight and a sign of drift from the source."""
    en = set(_load_locale("en"))
    offenders = {
        code: sorted(set(_load_locale(code)) - en)
        for code in _shipped_locales()
        if set(_load_locale(code)) - en
    }
    assert not offenders, f"Locales with keys absent from en.json: {offenders}"


def test_every_shipped_locale_placeholder_parity():
    """CORRECTNESS (hard, all locales): for every key a locale defines, its
    {placeholder} set must equal en's, or t(key, code, **kwargs).format()
    raises KeyError at render time for that language."""
    en = _load_locale("en")
    mismatched = {}
    for code in _shipped_locales():
        if code == "en":
            continue
        loc = _load_locale(code)
        bad = {
            key: (sorted(_placeholders(en[key])), sorted(_placeholders(loc[key])))
            for key in loc
            if key in en and _placeholders(en[key]) != _placeholders(loc[key])
        }
        if bad:
            mismatched[code] = bad
    assert not mismatched, f"Placeholder mismatch vs en per locale: {mismatched}"


def test_release_complete_locales_have_full_keyset():
    """COMPLETENESS gate (hard, _COMPLETE_LOCALES only): every locale declared
    release-complete must define every en.json key. This flips a locale from
    'safe fallback' to 'shippable'; add a locale here only once it is fully
    translated, and this gate keeps it complete thereafter."""
    en = set(_load_locale("en"))
    incomplete = {
        code: len(en - set(_load_locale(code)))
        for code in sorted(_COMPLETE_LOCALES)
        if code != "en" and (en - set(_load_locale(code)))
    }
    assert not incomplete, (
        f"Release-complete locales missing en keys (count per locale): {incomplete}. "
        "Finish the translation, or drop the locale from _COMPLETE_LOCALES."
    )


def test_locale_completeness_report():
    """COMPLETENESS report (warns, never fails on partial coverage): surfaces
    how many en keys each not-yet-complete shipped locale is missing, so the
    release gap is visible in CI output. Missing keys fall back to English, so
    this is a readiness signal; the hard gate is
    test_release_complete_locales_have_full_keyset. A locale that reaches ZERO
    missing keys is flagged as ready to promote onto _COMPLETE_LOCALES."""
    import warnings
    en = set(_load_locale("en"))
    report = {
        code: len(en - set(_load_locale(code)))
        for code in _shipped_locales()
        if code not in _COMPLETE_LOCALES
    }
    if report:
        ready = sorted(c for c, n in report.items() if n == 0)
        warnings.warn(
            f"locale completeness (en keys missing): {report}"
            + (f"; ready to promote to _COMPLETE_LOCALES: {ready}" if ready else ""),
            stacklevel=2,
        )


def test_every_shipped_locale_values_nonempty_valid():
    """Every shipped locale must be valid JSON with every value a non-empty
    string - an empty value renders blank instead of falling back to en."""
    for code in _shipped_locales():
        loc = _load_locale(code)
        assert isinstance(loc, dict), f"{code}.json is not a JSON object"
        bad = [k for k, v in loc.items() if not isinstance(v, str) or not v.strip()]
        assert bad == [], f"{code}.json keys with empty/non-string values: {bad}"


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
