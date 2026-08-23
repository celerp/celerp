# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Module-contributed UI translation catalogs.

Pluggable first-party modules ship their English UI source in a module-local
``locales/en.json`` and register it through the manifest ``locales`` seam, under
their own namespace (celerp-labels -> ``labels.*``). Core-folded modules
(celerp-ai, celerp-backup) never run that seam, so their strings live in the
central catalog instead (celerp-ai -> ``ai.*``). These tests load the bundled
modules and assert both routes resolve, and that every shipped module catalog is
internally consistent. They are red against a tree with no module catalogs and
no central ``ai.*`` UI keys.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

import pytest

from celerp.modules import loader
from celerp.modules.loader import _BUNDLED_MODULES_DIRS, is_core_folded, load_all

_ROOT = Path(__file__).resolve().parent.parent


def _bundled_names() -> set[str]:
    d = _BUNDLED_MODULES_DIRS[0]
    return {p.name for p in d.iterdir()
            if p.is_dir() and (p / "__init__.py").exists()}


@pytest.fixture
def _loaded_modules():
    from ui import i18n
    loader._loaded.clear()
    loader._load_errors.clear()
    i18n.clear_registry()
    i18n._cached_load.cache_clear()
    load_all(_BUNDLED_MODULES_DIRS[0], set(_bundled_names()))
    yield
    loader._loaded.clear()
    loader._load_errors.clear()
    i18n.clear_registry()
    i18n._cached_load.cache_clear()


def test_module_locale_catalogs_registered(_loaded_modules):
    """Loading the bundled modules registers celerp-labels' labels.* catalog
    through the manifest locales seam."""
    from ui.i18n import _registry
    en_catalog = _registry.get("en", {}).get("catalog", {})
    labels_keys = [k for k in en_catalog if k.startswith("labels.")]
    assert labels_keys, "celerp-labels did not register any labels.* keys"
    # celerp-labels is pluggable, so its seam fires; celerp-ai is core-folded, so
    # it never registers a module catalog and owns no keys here.
    assert not is_core_folded("celerp-labels")
    assert is_core_folded("celerp-ai")


def test_module_ui_renders_translated(_loaded_modules):
    """A labels.* key resolves from the module catalog (absent from central); an
    ai.* key resolves from the central catalog (core-folded module)."""
    from ui.i18n import t, _LOCALES_DIR
    central = json.loads((Path(_LOCALES_DIR) / "en.json").read_text(encoding="utf-8"))

    assert "labels.print_labels" not in central
    assert t("labels.print_labels", "en") == "Print Labels"

    assert "ai.top_up_credits" in central
    assert t("ai.top_up_credits", "en") == "Top-up credits"


def _placeholders(value: str) -> set[str]:
    import string
    return {f for _, f, _, _ in string.Formatter().parse(value) if f is not None}


def test_module_catalog_parity():
    """Every shipped module locale is valid, dotted, non-empty, a keyset subset
    of its module's own en.json, and placeholder-compatible with en."""
    locale_files = sorted((_ROOT / "default_modules").glob("*/*/locales/*.json"))
    assert locale_files, "no module locale catalogs present"

    by_module: dict[Path, dict[str, dict]] = defaultdict(dict)
    for f in locale_files:
        data = json.loads(f.read_text(encoding="utf-8"))
        assert isinstance(data, dict) and data, f"{f} is empty or not an object"
        bad = [k for k, v in data.items()
               if not isinstance(v, str) or not v.strip()]
        assert not bad, f"{f}: empty/non-string values: {bad}"
        undotted = [k for k in data if "." not in k]
        assert not undotted, f"{f}: keys must be dotted namespace.name: {undotted}"
        by_module[f.parent.parent][f.stem] = data

    for mod_pkg, locales in by_module.items():
        en = locales.get("en")
        assert en is not None, f"{mod_pkg} ships a locale without an en.json reference"
        en_keys = set(en)
        for code, loc in locales.items():
            extra = set(loc) - en_keys
            assert not extra, f"{mod_pkg}/{code}: keys absent from en.json: {sorted(extra)}"
            for key in loc:
                if key in en:
                    assert _placeholders(en[key]) == _placeholders(loc[key]), (
                        f"{mod_pkg}/{code}:{key} placeholder mismatch vs en"
                    )
