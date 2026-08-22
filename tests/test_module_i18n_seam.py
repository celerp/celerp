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
    neutralized when rendered into an f-string attribute sink (the files delete
    button's hx_confirm), exactly as the central catalog would be."""
    from fasthtml.common import Button, to_xml

    from ui.components.attrs import esc_attr

    payload = '"><img src=x onerror=alert(1)>'
    i18n.register_catalog("xx", {"action.delete_file": payload})

    # Mirror the ui/components/files.py delete-button sink verbatim: the t()
    # value is interpolated into hx_confirm without a manual esc_attr, relying
    # on FastHTML's to_xml attribute escaping.
    btn = Button(
        "×",
        hx_confirm=f"{i18n.t('action.delete_file', 'xx')}: {esc_attr('file.txt')}?",
        cls="btn btn--ghost btn--xs",
    )
    xml = to_xml(btn)

    assert i18n.t("action.delete_file", "xx") == payload  # stored raw
    assert "<img src=x" not in xml                        # no raw breakout tag
    assert "&lt;img src=x" in xml                          # escaped instead
