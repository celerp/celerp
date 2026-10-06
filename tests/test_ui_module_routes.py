# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""A module's UI routes, added through FastHTML's decorators, are recorded for
the per-company page gate (ui/app.py ModuleGateMiddleware -> loader.route_module),
checked for clashes, and rolled back on failure.

FastHTML's add_route rebinds app.router.routes to a new list, so the loader
must re-read the app's routes after a module's setup rather than slicing a list
it captured before."""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest
from fasthtml.common import FastHTML

from celerp.modules import loader
from celerp.modules.loader import load_all, load_errors


def _scope(path: str, method: str = "GET") -> dict:
    return {"type": "http", "path": path, "method": method, "root_path": "",
            "headers": [], "query_string": b""}


def _ui_module(base: Path, name: str, body: str) -> None:
    """A module whose ``setup_ui_routes(app)`` runs ``body``."""
    inner = base / name / name.replace("-", "_")
    inner.mkdir(parents=True)
    (inner / "__init__.py").write_text("")
    (inner / "ui.py").write_text("def setup_ui_routes(app):\n" + body)
    (base / name / "__init__.py").write_text(
        f"PLUGIN_MANIFEST = {{'name': {name!r}, 'version': '1.0.0', "
        f"'ui_routes': '{inner.name}.ui'}}\n")


@pytest.fixture
def mod(tmp_path):
    tag = uuid.uuid4().hex[:6]
    name = f"ui-mod{tag}"

    def register(app, body: str) -> str:
        _ui_module(tmp_path, name, body)
        loader.register_ui_routes(app, load_all(tmp_path, {name}))
        return name

    yield register
    for key in [k for k in sys.modules if tag in k]:
        sys.modules.pop(key)


def _core_app() -> FastHTML:
    app = FastHTML()

    @app.get("/core-page")
    def core_page():
        return "core"

    return app


def _get(app, path: str) -> str:
    from starlette.testclient import TestClient
    return TestClient(app).get(path).text


def test_fasthtml_module_page_is_known_to_the_gate(mod):
    app = _core_app()
    name = mod(app, "    @app.get('/notes-page')\n    def notes_page():\n        return 'notes'\n")
    assert loader.is_running(name)
    assert loader.route_module(_scope("/notes-page")) == name
    assert loader.route_module(_scope("/core-page")) is None


def test_fasthtml_module_page_clashing_with_core_is_refused(mod):
    app = _core_app()
    name = mod(app, "    @app.get('/core-page')\n    def module_page():\n        return 'module'\n")
    assert "already registered" in load_errors().get(name, "")
    assert not loader.is_running(name)
    assert _get(app, "/core-page") == "core"


def test_fasthtml_module_page_replacing_core_route_is_refused_and_core_restored(mod):
    """FastHTML's add_route silently replaces a route with the same path, name
    and methods; the clash is still caught and core's route comes back."""
    app = _core_app()
    core_routes = list(app.router.routes)
    name = mod(app, "    @app.get('/core-page')\n    def core_page():\n        return 'module'\n")
    assert "already registered" in load_errors().get(name, "")
    assert app.router.routes == core_routes
    assert _get(app, "/core-page") == "core"


def test_fasthtml_module_failing_setup_rolls_back_its_pages(mod):
    app = _core_app()
    core_routes = list(app.router.routes)
    name = mod(app, "    @app.get('/half-page')\n    def half_page():\n        return 'half'\n"
                    "    raise RuntimeError('setup broke')\n")
    assert "setup broke" in load_errors().get(name, "")
    assert app.router.routes == core_routes
    assert loader.route_module(_scope("/half-page")) is None
