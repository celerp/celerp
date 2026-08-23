# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Unit and component tests for the i18n module seam.

A loadable module contributes UI translation catalogs (a whole new language
and/or extra keys for an existing one) through a `locales` manifest key. The
loader pushes each declared catalog into ui.i18n via register_catalog; ui.i18n
merges the central locale file over the module registry, central winning for an
existing language.

Symbols under test (register_catalog, clear_registry, available_langs) do not
exist at merge-base, so the assertions below have no code to satisfy there.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

# ui.i18n always imports; the seam symbols are referenced as attributes inside
# each test so a merge-base run fails per-test (AttributeError) rather than as a
# single collection error.
from ui import i18n

_FIXTURES_MODULES_DIR = Path(__file__).parent / "fixtures" / "modules"


@pytest.fixture(autouse=True)
def _clean_registry():
    """Reset the i18n catalog registry (and its load cache) around each test so
    module-contributed catalogs never leak between tests. Guarded so the fixture
    is harmless before the seam exists."""
    getattr(i18n, "clear_registry", lambda: None)()
    getattr(getattr(i18n, "_cached_load", None), "cache_clear", lambda: None)()
    yield
    getattr(i18n, "clear_registry", lambda: None)()
    getattr(getattr(i18n, "_cached_load", None), "cache_clear", lambda: None)()


class _StubRequest:
    """Minimal stand-in for get_lang: a cookie jar and a header bag."""

    def __init__(self, cookie: str = "", accept_language: str = ""):
        self.cookies = {"celerp_lang": cookie} if cookie else {}
        self.headers = {"accept-language": accept_language}


def _write_module(dirpath: Path, name: str, manifest: dict, files: dict[str, str]) -> None:
    """Build a loadable module package on disk: __init__.py plus catalog files."""
    pkg = dirpath / name
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text(
        "PLUGIN_MANIFEST = " + repr(manifest) + "\n"
    )
    for rel, content in files.items():
        target = pkg / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)


# ---------------------------------------------------------------------------
# Registry: resolution, precedence, fallback
# ---------------------------------------------------------------------------

def test_module_language_resolves():
    """J1: a module-contributed language renders its own strings via t(key, 'xx')."""
    i18n.register_catalog("xx", {"testlang.greeting": "Hi from Testish"})
    assert i18n.t("testlang.greeting", "xx") == "Hi from Testish"


def test_module_clash_first_wins():
    """On a module-vs-module clash for the same lang+key, the FIRST value wins."""
    i18n.register_catalog("xx", {"testlang.k": "first"})
    i18n.register_catalog("xx", {"testlang.k": "second"})
    assert i18n.t("testlang.k", "xx") == "first"


def test_missing_key_falls_back_to_en():
    """A PRESENT key returns the module's own value; an OMITTED key falls to en."""
    i18n.register_catalog("xx", {"testlang.greeting": "Hi from Testish"})
    assert i18n.t("testlang.greeting", "xx") == "Hi from Testish"
    # btn.save is absent from the xx catalog but present in en.
    assert i18n.t("btn.save", "xx") == "Save"


def test_existing_lang_additive_central_wins():
    """A module adds a NEW key to an existing language but cannot override a
    central key: central copy wins, the new key is additive."""
    i18n.register_catalog("en", {"testlang.newkey": "added", "btn.save": "HIJACKED"})
    assert i18n.t("testlang.newkey", "en") == "added"
    assert i18n.t("btn.save", "en") == "Save"


def test_new_language_in_available_langs():
    """available_langs() surfaces a module-contributed language code."""
    i18n.register_catalog("xx", {"testlang.greeting": "Hi from Testish"})
    assert "xx" in i18n.available_langs()


def test_rtl_from_manifest():
    """A module can declare a language right-to-left via its manifest `rtl` flag;
    is_rtl resolves it. Driven end to end through the loader push path."""
    from celerp.modules.loader import load_all

    load_all(_FIXTURES_MODULES_DIR, {"celerp-testlang"})
    assert i18n.is_rtl("xr") is True
    assert i18n.is_rtl("xx") is False


def test_register_catalog_invalidates_cache():
    """Registering a catalog busts the load cache so a primed language re-reads."""
    # Prime the cache for 'xx' (no catalog yet -> central-only, en fallback).
    assert i18n.t("testlang.greeting", "xx") == "testlang.greeting"
    i18n.register_catalog("xx", {"testlang.greeting": "Hi from Testish"})
    assert i18n.t("testlang.greeting", "xx") == "Hi from Testish"


def test_get_lang_accepts_module_language():
    """get_lang ACCEPTS a module-registered code from Accept-Language AND still
    falls back to en for an unknown code (guards the membership check from
    over-accepting)."""
    i18n.register_catalog("xx", {"testlang.greeting": "Hi from Testish"})
    assert i18n.get_lang(_StubRequest(accept_language="xx")) == "xx"
    assert i18n.get_lang(_StubRequest(accept_language="zz")) == "en"


def test_malformed_catalog_skipped(tmp_path):
    """A module with one valid and one malformed catalog loads: the valid
    language registers, the malformed entry is logged and skipped."""
    from celerp.modules.loader import load_all

    _write_module(
        tmp_path,
        "celerp-badlang",
        {
            "name": "celerp-badlang",
            "version": "1.0.0",
            "locales": {
                "xg": {"file": "locales/xg.json", "rtl": False},
                "xb": {"file": "locales/xb.json", "rtl": False},
            },
        },
        {
            "locales/xg.json": json.dumps({"testlang.greeting": "Good"}),
            "locales/xb.json": "{ this is not valid json ",
        },
    )
    loaded = load_all(tmp_path, {"celerp-badlang"})

    assert any(m["name"] == "celerp-badlang" for m in loaded)
    assert "xg" in i18n.available_langs()
    assert "xb" not in i18n.available_langs()
    assert i18n.t("testlang.greeting", "xg") == "Good"


def test_register_catalog_rejects_non_dict():
    """A non-dict mapping is rejected without poisoning the registry."""
    i18n.register_catalog("xx", ["not", "a", "dict"])
    assert "xx" not in i18n.available_langs()
    # The registry is not poisoned: a later valid registration still works.
    i18n.register_catalog("xx", {"testlang.greeting": "Hi from Testish"})
    assert i18n.t("testlang.greeting", "xx") == "Hi from Testish"


def test_register_catalog_drops_non_string_value():
    """A non-str catalog value is dropped and logged; str values still register."""
    i18n.register_catalog("xx", {"testlang.a": "ok", "testlang.b": 123})
    assert i18n.t("testlang.a", "xx") == "ok"
    # 123 was dropped, so the key falls back to itself (absent from en).
    assert i18n.t("testlang.b", "xx") == "testlang.b"


# ---------------------------------------------------------------------------
# Rendering surfaces
# ---------------------------------------------------------------------------

def test_switcher_lists_module_language():
    """The topbar language switcher lists a module-contributed language."""
    from fasthtml.common import to_xml

    from ui.components.shell import _topbar

    i18n.register_catalog("xx", {"testlang.greeting": "Hi from Testish"})
    xml = to_xml(_topbar([], lang="en"))
    assert 'value="xx"' in xml
    assert "XX" in xml


def test_module_catalog_value_escaped_in_attribute_sink():
    """A module catalog value carrying an attribute-breakout payload is
    neutralized when rendered by the REAL production component (the files
    section's delete-button hx_confirm sink at ui/components/files.py), not a
    hand-mirrored stand-in. The active language is set to 'xx' so files_section's
    own t('action.delete_file') call picks up the module payload."""
    from fasthtml.common import to_xml

    from ui.components.files import files_section

    payload = '"><img src=x onerror=alert(1)>'
    i18n.register_catalog("xx", {"action.delete_file": payload})

    token = i18n._current_lang.set("xx")
    try:
        section = files_section(
            "doc",
            "doc-1",
            [{"id": "f1", "filename": "file.txt", "size": 12, "uploaded_at": "2026-08-23"}],
        )
        xml = to_xml(section)
    finally:
        i18n._current_lang.reset(token)

    assert i18n.t("action.delete_file", "xx") == payload  # stored raw
    assert payload not in xml                              # not present verbatim
    assert "<img src=x" not in xml                         # no raw breakout tag
    assert "&lt;img src=x" in xml                          # escaped instead


# ---------------------------------------------------------------------------
# Malformed-input robustness (the module-supplied trust boundary must degrade,
# never crash the loader / boot)
# ---------------------------------------------------------------------------

def test_malformed_locales_container_skipped(tmp_path):
    """A `locales` value that is not a dict (here a list) is logged and skipped;
    the module still loads with no exception escaping the loader."""
    from celerp.modules.loader import load_all

    _write_module(
        tmp_path,
        "celerp-badcontainer",
        {"name": "celerp-badcontainer", "version": "1.0.0", "locales": ["xx"]},
        {},
    )
    loaded = load_all(tmp_path, {"celerp-badcontainer"})
    assert any(m["name"] == "celerp-badcontainer" for m in loaded)
    assert "xx" not in i18n.available_langs()


def test_non_string_file_value_skipped(tmp_path):
    """An entry whose `file` is not a string is skipped (no Path / non-string),
    the module still loads, and a valid sibling locale registers."""
    from celerp.modules.loader import load_all

    _write_module(
        tmp_path,
        "celerp-badfile",
        {
            "name": "celerp-badfile",
            "version": "1.0.0",
            "locales": {
                "xg": {"file": "locales/xg.json"},
                "xb": {"file": 123},
            },
        },
        {"locales/xg.json": json.dumps({"testlang.greeting": "Good"})},
    )
    loaded = load_all(tmp_path, {"celerp-badfile"})
    assert any(m["name"] == "celerp-badfile" for m in loaded)
    assert "xg" in i18n.available_langs()
    assert "xb" not in i18n.available_langs()


def test_non_string_language_code_skipped(tmp_path):
    """A non-string language code in the manifest is skipped: the module loads,
    a valid sibling registers, and available_langs() (which sorts codes) does
    not crash on a poisoned key."""
    from celerp.modules.loader import load_all

    _write_module(
        tmp_path,
        "celerp-badcode",
        {
            "name": "celerp-badcode",
            "version": "1.0.0",
            "locales": {
                123: {"file": "locales/n.json"},
                "xg": {"file": "locales/xg.json"},
            },
        },
        {
            "locales/n.json": json.dumps({"testlang.greeting": "Nope"}),
            "locales/xg.json": json.dumps({"testlang.greeting": "Good"}),
        },
    )
    loaded = load_all(tmp_path, {"celerp-badcode"})
    assert any(m["name"] == "celerp-badcode" for m in loaded)
    langs = i18n.available_langs()  # must not raise on a mixed-type sort
    assert "xg" in langs
    assert 123 not in i18n._registry


def test_register_catalog_rejects_non_string_lang():
    """The public API self-defends: a non-string language code is ignored."""
    i18n.register_catalog(123, {"testlang.k": "v"})
    assert 123 not in i18n._registry
    assert i18n.available_langs() == sorted(i18n._DISK_LANGS)


def test_non_bool_rtl_treated_false(tmp_path):
    """A non-bool `rtl` value in the manifest is not treated as True: the
    language registers but is left LTR."""
    from celerp.modules.loader import load_all

    _write_module(
        tmp_path,
        "celerp-truthyrtl",
        {
            "name": "celerp-truthyrtl",
            "version": "1.0.0",
            "locales": {"xy": {"file": "locales/xy.json", "rtl": "yes"}},
        },
        {"locales/xy.json": json.dumps({"testlang.greeting": "Hi"})},
    )
    load_all(tmp_path, {"celerp-truthyrtl"})
    assert "xy" in i18n.available_langs()
    assert i18n.is_rtl("xy") is False


def test_module_cannot_flip_central_language_rtl():
    """A module declaring rtl=True for an EXISTING central language cannot flip
    its direction: central owns a disk language's direction."""
    i18n.register_catalog("en", {"testlang.k": "v"}, rtl=True)
    assert i18n.is_rtl("en") is False


# ---------------------------------------------------------------------------
# Repeated-loader lifecycle (the registry is rebuilt each pass)
# ---------------------------------------------------------------------------

def _write_langmod(dirpath: Path, name: str, code: str, value: str) -> None:
    _write_module(
        dirpath,
        name,
        {"name": name, "version": "1.0.0", "locales": {code: {"file": f"locales/{code}.json"}}},
        {f"locales/{code}.json": json.dumps({"testlang.greeting": value})},
    )


def test_reload_reflects_changed_catalog(tmp_path):
    """Two consecutive load_all() passes over the SAME module whose catalog
    changed between them: the second pass reflects the new value, not the stale
    first-registered one (the registry is rebuilt, not accreted)."""
    from celerp.modules.loader import load_all

    _write_langmod(tmp_path, "celerp-reload", "xx", "first")
    load_all(tmp_path, {"celerp-reload"})
    assert i18n.t("testlang.greeting", "xx") == "first"

    (tmp_path / "celerp-reload" / "locales" / "xx.json").write_text(
        json.dumps({"testlang.greeting": "second"})
    )
    load_all(tmp_path, {"celerp-reload"})
    assert i18n.t("testlang.greeting", "xx") == "second"


def test_disabled_module_language_removed(tmp_path):
    """A language contributed on the first pass is gone after a second pass that
    no longer enables the contributing module."""
    from celerp.modules.loader import load_all

    _write_langmod(tmp_path, "celerp-gone", "xx", "here")
    load_all(tmp_path, {"celerp-gone"})
    assert "xx" in i18n.available_langs()

    load_all(tmp_path, set())  # nothing enabled this pass
    assert "xx" not in i18n.available_langs()


def test_two_module_collision_deterministic(tmp_path):
    """Two modules contributing the SAME new language resolve deterministically:
    name-sorted discovery loads the alphabetically-first module first, and
    first-registered wins - stable across runs regardless of filesystem order."""
    from celerp.modules.loader import load_all

    _write_langmod(tmp_path, "celerp-la", "xx", "from-la")
    _write_langmod(tmp_path, "celerp-lb", "xx", "from-lb")

    load_all(tmp_path, {"celerp-la", "celerp-lb"})
    first = i18n.t("testlang.greeting", "xx")
    load_all(tmp_path, {"celerp-la", "celerp-lb"})
    second = i18n.t("testlang.greeting", "xx")

    assert first == second == "from-la"


# ---------------------------------------------------------------------------
# English fallback for module-contributed keys, and cookie availability
# ---------------------------------------------------------------------------

def test_module_english_fallback_for_new_keys():
    """A module that ships a NEW key for English makes it the fallback for every
    language: an English user sees it, and a module language missing that key
    falls back to the module-contributed English value."""
    i18n.register_catalog("en", {"testlang.only_en": "English only"})
    i18n.register_catalog("xx", {"testlang.greeting": "Hi"})
    assert i18n.t("testlang.only_en", "en") == "English only"
    # xx has no such key -> falls back through en to the module-contributed value.
    assert i18n.t("testlang.only_en", "xx") == "English only"


def test_cookie_unavailable_language_falls_back():
    """get_lang honours a cookie only when the language is actually available;
    a stale cookie for an unknown/removed language falls back to en."""
    assert i18n.get_lang(_StubRequest(cookie="zz")) == "en"
    i18n.register_catalog("xx", {"testlang.greeting": "Hi"})
    assert i18n.get_lang(_StubRequest(cookie="xx")) == "xx"


def test_switcher_marks_module_language_selected():
    """The topbar switcher renders a module-contributed language as the SELECTED
    option when it is the active language (render path)."""
    from fasthtml.common import to_xml

    from ui.components.shell import _topbar

    i18n.register_catalog("xx", {"testlang.greeting": "Hi"})
    xml = to_xml(_topbar([], lang="xx"))
    assert 'value="xx"' in xml
    assert "selected" in xml
