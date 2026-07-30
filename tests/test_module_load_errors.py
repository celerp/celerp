# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Loader error surfacing: a module that fails to load must leave a readable
reason in load_errors() instead of disappearing silently at boot."""
from __future__ import annotations

from pathlib import Path

from celerp.modules.loader import load_all, load_errors, loaded_modules

OK_MODULE = '''PLUGIN_MANIFEST = {
    "name": "ok-module",
    "version": "1.0.0",
    "display_name": "OK Module",
}
'''

BROKEN_MODULE = '''raise RuntimeError("boom at import time")
'''

NO_MANIFEST = '''x = 1
'''

NEEDS_MISSING_DEP = '''PLUGIN_MANIFEST = {
    "name": "needs-dep",
    "version": "1.0.0",
    "depends_on": ["not-installed"],
}
'''


def _mk(dirpath: Path, name: str, init_src: str) -> None:
    pkg = dirpath / name
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text(init_src)


def test_route_collision_is_refused_and_recorded(monkeypatch):
    """Two modules declaring the same route path: the first wins, the second is
    refused (its routes rolled back) and recorded as a load error, never allowed
    to silently shadow the first - Starlette matches the first-registered route."""
    import types

    from starlette.applications import Starlette
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route

    from celerp.modules import loader

    app = Starlette()

    def _handler(request):
        return PlainTextResponse("ok")

    def _setup_a(a):
        a.router.routes.append(Route("/collide", _handler, methods=["GET"]))

    def _setup_b(a):
        a.router.routes.append(Route("/collide", _handler, methods=["GET"]))

    fake = {
        "mod_a.ui": types.SimpleNamespace(setup_ui_routes=_setup_a),
        "mod_b.ui": types.SimpleNamespace(setup_ui_routes=_setup_b),
    }
    monkeypatch.setattr(loader.importlib, "import_module", lambda p: fake[p])
    loader._load_errors.clear()

    loader.register_ui_routes(app, [
        {"name": "mod-a", "ui_routes": "mod_a.ui"},
        {"name": "mod-b", "ui_routes": "mod_b.ui"},
    ])

    paths = [getattr(r, "path", None) for r in app.router.routes]
    assert paths.count("/collide") == 1          # second refused, not duplicated
    errs = load_errors()
    assert "mod-b" in errs and "already registered" in errs["mod-b"]
    assert "mod-a" not in errs


def test_load_errors_are_recorded(tmp_path):
    _mk(tmp_path, "ok-module", OK_MODULE)
    _mk(tmp_path, "broken-module", BROKEN_MODULE)
    _mk(tmp_path, "no-manifest", NO_MANIFEST)
    _mk(tmp_path, "needs-dep", NEEDS_MISSING_DEP)

    loaded = load_all(tmp_path, {"ok-module", "broken-module", "no-manifest", "needs-dep"})

    names = {m["name"] for m in loaded}
    assert "ok-module" in names

    errs = load_errors()
    assert "broken-module" in errs and "boom at import time" in errs["broken-module"]
    assert "no-manifest" in errs and "PLUGIN_MANIFEST" in errs["no-manifest"]
    assert "needs-dep" in errs and "not-installed" in errs["needs-dep"]
    # the good module carries no error
    assert "ok-module" not in errs


def test_errors_cleared_between_runs(tmp_path):
    _mk(tmp_path, "broken-module", BROKEN_MODULE)
    load_all(tmp_path, {"broken-module"})
    assert "broken-module" in load_errors()

    ok_dir = tmp_path / "second"
    ok_dir.mkdir()
    _mk(ok_dir, "ok-module", OK_MODULE)
    load_all(ok_dir, {"ok-module"})
    assert load_errors() == {}
    assert any(m["name"] == "ok-module" for m in loaded_modules())
