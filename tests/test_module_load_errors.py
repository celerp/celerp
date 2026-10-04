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

BROKEN_MODULE = '''PLUGIN_MANIFEST = {"name": "broken-module", "version": "1.0.0"}
raise RuntimeError("boom at import time")
'''

NO_MANIFEST = '''x = 1
'''

NEEDS_MISSING_DEP = '''PLUGIN_MANIFEST = {
    "name": "needs-dep",
    "version": "1.0.0",
    "depends_on": ["not-installed"],
}
'''

BAD_PERMISSION = '''PLUGIN_MANIFEST = {
    "name": "bad-perm",
    "version": "1.0.0",
    "slots": {"nav": [{"key": "bp", "label": "Bad Perm", "href": "/bp",
                       "permission": "manage_equipment"}]},
}
'''


def _mk(dirpath: Path, name: str, init_src: str) -> None:
    pkg = dirpath / name
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text(init_src)


def _route_module(base: Path, name: str, path: str) -> None:
    """A module whose UI setup registers ``path``."""
    inner = base / name / name.replace("-", "_")
    inner.mkdir(parents=True)
    (inner / "__init__.py").write_text("")
    (inner / "ui.py").write_text(
        "from starlette.responses import PlainTextResponse\n"
        "from starlette.routing import Route\n\n"
        "def _handler(request):\n    return PlainTextResponse('ok')\n\n"
        "def setup_ui_routes(app):\n"
        f"    app.router.routes.append(Route({path!r}, _handler, methods=['GET']))\n")
    (base / name / "__init__.py").write_text(
        f"PLUGIN_MANIFEST = {{'name': {name!r}, 'version': '1.0.0', "
        f"'ui_routes': '{inner.name}.ui'}}\n")


def test_route_collision_is_refused_and_recorded(tmp_path):
    """Two modules declaring the same route path: the first wins, the second is
    refused (its routes rolled back) and recorded as a load error, never allowed
    to silently shadow the first - Starlette matches the first-registered route."""
    import sys
    import uuid

    from starlette.applications import Starlette

    from celerp.modules import loader

    tag = uuid.uuid4().hex[:6]
    first, second = f"mod-a{tag}", f"mod-b{tag}"
    _route_module(tmp_path, first, "/collide")
    _route_module(tmp_path, second, "/collide")
    app = Starlette()
    try:
        loader.register_ui_routes(app, load_all(tmp_path, {first, second}))
    finally:
        for key in [k for k in sys.modules if tag in k]:
            sys.modules.pop(key)

    paths = [getattr(r, "path", None) for r in app.router.routes]
    assert paths.count("/collide") == 1          # second refused, not duplicated
    errs = load_errors()
    assert second in errs and "already registered" in errs[second]
    assert first not in errs
    assert not loader.is_running(second)


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


def test_unknown_permission_key_is_refused(tmp_path):
    """A nav entry naming a permission key outside the registry would KeyError
    inside the sidebar builder on every page render, taking the whole UI down.
    The loader refuses the module at load with the key named instead."""
    _mk(tmp_path, "bad-perm", BAD_PERMISSION)
    loaded = load_all(tmp_path, {"bad-perm"})
    assert not any(m["name"] == "bad-perm" for m in loaded)
    errs = load_errors()
    assert "bad-perm" in errs and "manage_equipment" in errs["bad-perm"]

    from celerp.modules.slots import get as get_slot
    assert not any(item.get("_module") == "bad-perm" for item in get_slot("nav"))


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
