# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Strings waiting for their translation: i18n_pending_translation.json lists each one with
its English text and the {parameters} a translation must keep, for the translators.

Until a string is translated, every other locale carries its English text, reviewed into the
source-identical allowlist; a bundled module's pending strings (under "modules") carry it in
the module's own catalogs. A translated string leaves the list, so the list is always exactly
the strings still in English."""

from __future__ import annotations

import json
import re
from pathlib import Path

TESTS = Path(__file__).resolve().parent
LOCALES = TESTS.parent / "ui" / "locales"
PENDING = json.loads((TESTS / "i18n_pending_translation.json").read_text(encoding="utf-8"))
ALLOWLIST = json.loads((TESTS / "i18n_source_identical_allowlist.json").read_text(encoding="utf-8"))
MODULES = TESTS.parent / "default_modules"


def _catalog(code: str, locales: Path = LOCALES) -> dict[str, str]:
    return json.loads((locales / f"{code}.json").read_text(encoding="utf-8"))


def _module_locales(module: str) -> Path:
    return next((MODULES / module).glob("*/locales"))


def test_each_pending_string_states_its_english_and_parameters():
    en = _catalog("en")
    for key, entry in PENDING["keys"].items():
        assert entry["en"] == en[key], key
        assert entry["params"] == sorted(set(re.findall(r"\{(\w+)\}", en[key]))), key


def test_each_pending_string_is_still_english_in_every_other_locale_and_reviewed():
    en = _catalog("en")
    assert PENDING["locales"] == sorted(p.stem for p in LOCALES.glob("*.json") if p.stem != "en")
    for code in PENDING["locales"]:
        catalog = _catalog(code)
        assert [k for k in PENDING["keys"] if catalog.get(k) != en[k]] == [], code
        assert [k for k in PENDING["keys"] if k not in ALLOWLIST[code]] == [], code


def test_each_pending_module_string_states_its_english_and_is_english_in_every_locale():
    for module, keys in PENDING.get("modules", {}).items():
        locales = _module_locales(module)
        en = _catalog("en", locales)
        for key, entry in keys.items():
            assert entry["en"] == en[key], (module, key)
            assert entry["params"] == sorted(set(re.findall(r"\{(\w+)\}", en[key]))), (module, key)
        for code in PENDING["locales"]:
            catalog = _catalog(code, locales)
            assert [k for k in keys if catalog.get(k) != en[k]] == [], (module, code)
