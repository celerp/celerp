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

import pytest

from celerp.modules import loader
from celerp.modules.loader import (
    _BUNDLED_MODULES_DIRS,
    ModuleLoadError,
    default_module_names,
    is_core_folded,
    load_all,
    load_errors,
    register_api_routes,
    register_ui_routes,
)


@pytest.fixture(autouse=True)
def _clear_loader_state():
    """This file runs with --noconftest on the 3.10 CI leg, so it cannot rely
    on the repo conftest's loader cleanup; clear both registries itself, before
    each test too so earlier suite tests cannot leak loader state in."""
    loader._loaded.clear()
    loader._load_errors.clear()
    yield
    loader._loaded.clear()
    loader._load_errors.clear()


def _pluggable_default_names() -> set[str]:
    """Default modules the loader actually loads: core-folded components
    (ai/backup/connectors) are wired at app construction, never via load_all."""
    return {n for n in default_module_names() if not is_core_folded(n)}


def test_all_default_modules_load_and_reimport_cleanly():
    loaded = load_all(_BUNDLED_MODULES_DIRS[0], set(default_module_names()))
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


def test_default_module_import_failure_fails_boot():
    """A default module whose route code fails to import must fail boot with
    an error naming the module, never boot without the feature."""
    manifest = {"name": "celerp-docs", "api_routes": "celerp_docs_broken_x",
                "ui_routes": "celerp_docs_broken_x"}
    with pytest.raises(ModuleLoadError, match="celerp-docs"):
        register_api_routes(app=None, loaded=[manifest])
    with pytest.raises(ModuleLoadError, match="celerp-docs"):
        register_ui_routes(app=None, loaded=[manifest])


def test_third_party_route_failure_keeps_booting():
    """Third-party modules keep load-and-continue: a broken route import is
    recorded for the Modules UI badge, and boot proceeds."""
    manifest = {"name": "vendor-widget", "api_routes": "vendor_widget_broken_x",
                "ui_routes": "vendor_widget_broken_x"}
    register_api_routes(app=None, loaded=[manifest])
    register_ui_routes(app=None, loaded=[manifest])
    assert "vendor-widget" in load_errors()
