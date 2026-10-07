# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Boot smoke test for the bundled default modules.

`register_api_routes` / `register_ui_routes` import route modules lazily and
keep booting for third-party modules, so a default module whose route code
cannot import on the running interpreter would otherwise vanish silently.
This file loads every default module and re-imports its declared route
modules directly, so any construct the interpreter cannot import fails the
test with the real ImportError. It runs on the CI 3.10 matrix leg with
--noconftest: no database, no app fixtures, and its own state teardown.
"""

from __future__ import annotations

import importlib
import types

import pytest

from celerp.modules import loader
from celerp.modules.loader import (
    _BUNDLED_MODULES_DIRS,
    ModuleLoadError,
    is_core_folded,
    load_all,
    load_errors,
    register_api_routes,
    register_ui_routes,
)


def _bundled_names() -> set[str]:
    """Every physically-present bundled module (by folder), independent of the
    content lock - a boot test asserts the shipped tree loads, not what the lock
    happens to trust."""
    d = _BUNDLED_MODULES_DIRS[0]
    return {p.name for p in d.iterdir()
            if p.is_dir() and (p / "__init__.py").exists()}


@pytest.fixture(autouse=True)
def _clear_loader_state():
    """This file runs with --noconftest on the 3.10 CI leg, so it cannot rely
    on the repo conftest's loader cleanup; clear both registries itself, before
    each test too so earlier suite tests cannot leak loader state in."""
    loader._loaded.clear()
    loader._load_errors.clear()
    loader._admitted.clear()
    yield
    loader._loaded.clear()
    loader._load_errors.clear()
    loader._admitted.clear()


def _pluggable_default_names() -> set[str]:
    """Default modules the loader actually loads: core-folded components
    (ai/backup/connectors) are wired at app construction, never via load_all."""
    return {n for n in _bundled_names() if not is_core_folded(n)}


def test_all_default_modules_load_and_reimport_cleanly():
    loaded = load_all(_BUNDLED_MODULES_DIRS[0], set(_bundled_names()))
    loaded_names = {m["name"] for m in loaded}
    for name in sorted(_pluggable_default_names()):
        assert name in loaded_names, (
            f"Default module {name!r} failed to load: "
            f"{load_errors().get(name, 'no error recorded')}"
        )
    # Route modules import lazily at registration; re-import each one directly
    # so a construct this interpreter cannot import surfaces here, not as a
    # silently missing feature. Imports are cached, so this is cheap.
    for m in loaded:
        for key in ("api_routes", "ui_routes"):
            route_mod = m.get(key)
            if route_mod:
                importlib.import_module(route_mod)


class _App:
    def __init__(self):
        self.router = types.SimpleNamespace(routes=[])


def test_default_module_import_failure_fails_boot(monkeypatch):
    """A first-party module whose route code fails to import must fail boot with
    an error naming the module, never boot without the feature. First-party-ness
    travels on the manifest (load_all sets it from the content lock); the route
    registrar trusts that verdict rather than re-deriving it."""
    loaded = load_all(_BUNDLED_MODULES_DIRS[0],
                      {"celerp-docs", "celerp-inventory", "celerp-contacts"})
    docs = [m for m in loaded if m["name"] == "celerp-docs"]
    assert docs and docs[0]["first_party"] is True

    def _broken(dotted):
        raise ImportError(f"cannot import {dotted}")

    monkeypatch.setattr(loader, "resolve_handler", _broken)
    with pytest.raises(ModuleLoadError, match="celerp-docs"):
        register_api_routes(_App(), docs)
    with pytest.raises(ModuleLoadError, match="celerp-docs"):
        register_ui_routes(_App(), docs)


def test_third_party_route_failure_keeps_booting(tmp_path):
    """Third-party modules keep load-and-continue: a broken route import is
    recorded for the Modules UI badge, the module stops running, and boot
    proceeds."""
    inner = tmp_path / "vendor-widget" / "vendor_widget"
    inner.mkdir(parents=True)
    (inner / "__init__.py").write_text("")
    (inner / "routes.py").write_text(
        "import vendor_widget_missing_dependency_x  # noqa: F401\n\n"
        "def setup_api_routes(app):\n    pass\n")
    (tmp_path / "vendor-widget" / "__init__.py").write_text(
        "PLUGIN_MANIFEST = {'name': 'vendor-widget', 'version': '1.0.0', "
        "'api_routes': 'vendor_widget.routes'}\n")
    loaded = load_all(tmp_path, {"vendor-widget"})
    assert [m["name"] for m in loaded] == ["vendor-widget"]

    register_api_routes(_App(), loaded)

    assert "ModuleNotFoundError" in load_errors()["vendor-widget"]
    assert not loader.is_running("vendor-widget")


@pytest.mark.parametrize("name", sorted(loader.first_party_names() - loader.CORE_FOLDED))
def test_enabled_default_module_missing_from_every_module_dir_stops_startup(name, tmp_path):
    """An enabled default that no module directory holds stops startup naming
    it, rather than starting without it."""
    (tmp_path / "other").mkdir()
    module_dir = f"{tmp_path / 'empty'},{tmp_path / 'other'}"

    with pytest.raises(ModuleLoadError, match=f"{name!r} is not installed"):
        loader.admit_modules(module_dir, {name})
    with pytest.raises(ModuleLoadError, match=f"{name!r} is not installed"):
        load_all(module_dir, {name})


def test_enabled_third_party_module_missing_keeps_booting(tmp_path):
    """An enabled module Celerp does not ship that is not installed is skipped,
    as before: nothing loads for it and boot continues."""
    admission = loader.admit_modules(tmp_path, {"vendor-gone"})
    assert admission.admitted == [] and admission.refused == {}

    assert load_all(tmp_path, {"vendor-gone"}) == []
    assert not loader.is_running("vendor-gone")
