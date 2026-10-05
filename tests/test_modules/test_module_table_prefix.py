# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Every table a module's code defines carries the module's table_prefix.

A module whose import or route setup defines a table outside its prefix is
taken out before any table is created: it stops running, its routes go, and
none of its tables are created.
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

from celerp.models.base import Base
from celerp.modules import loader, slots

_TABLE = ("import sqlalchemy as sa\nfrom celerp.models.base import Base\n"
          "sa.Table({table!r}, Base.metadata, sa.Column('id', sa.Integer, primary_key=True))\n")
_ROUTES = ("from starlette.responses import PlainTextResponse\n\n"
           "def setup_api_routes(app):\n"
           "{extra}"
           "    app.router.add_route('/{inner}/ping', lambda r: PlainTextResponse('ok'))\n")


class _App:
    def __init__(self):
        from starlette.routing import Router
        self.router = Router()


@pytest.fixture
def module_dir(tmp_path, monkeypatch):
    base = tmp_path / "modules"
    base.mkdir()
    monkeypatch.setenv("MODULE_DIR", str(base))
    before_path, before_mods = list(sys.path), dict(sys.modules)
    before_tables = set(Base.metadata.tables)
    removed = set(loader._removed_tables)
    slots.clear()
    yield base
    slots.clear()
    loader._loaded.clear()
    loader._load_errors.clear()
    for key in set(Base.metadata.tables) - before_tables:
        Base.metadata.remove(Base.metadata.tables[key])
    loader._removed_tables.intersection_update(removed)
    sys.path[:] = before_path
    for key in set(sys.modules) - set(before_mods):
        sys.modules.pop(key, None)


def _module(base: Path, *, prefix: str | None, init_table: str | None = None,
            route_table: str | None = None) -> tuple[str, str]:
    name, inner = f"acme-{uuid.uuid4().hex[:8]}", f"acme_{uuid.uuid4().hex[:8]}"
    manifest = {"name": name, "version": "1.0.0", "api_routes": f"{inner}.routes"}
    if prefix is not None:
        manifest["table_prefix"] = prefix
    pkg = base / name
    (pkg / inner).mkdir(parents=True)
    (pkg / "__init__.py").write_text(
        (f"from {inner} import models\n" if init_table else "")
        + f"PLUGIN_MANIFEST = {manifest!r}\n")
    (pkg / inner / "__init__.py").write_text("")
    if init_table:
        (pkg / inner / "models.py").write_text(_TABLE.format(table=init_table))
    extra = ""
    if route_table:
        (pkg / inner / "route_models.py").write_text(_TABLE.format(table=route_table))
        extra = f"    import {inner}.route_models\n"
    (pkg / inner / "routes.py").write_text(_ROUTES.format(extra=extra, inner=inner))
    return name, inner


def _start(base: Path, name: str) -> _App:
    app = _App()
    loaded = loader.load_all(str(base), {name})
    loader.register_api_routes(app, loaded)
    return app


def _paths(app) -> set:
    return {getattr(r, "path", None) for r in app.router.routes}


def _tag() -> str:
    return uuid.uuid4().hex[:8]


@pytest.mark.parametrize("where", ["import", "route_setup"])
def test_module_defining_a_table_outside_its_prefix_is_taken_out(module_dir, where):
    tag = _tag()
    stray = f"other{tag}_records"
    kw = {"init_table": stray} if where == "import" else {"route_table": stray}
    name, inner = _module(module_dir, prefix=f"acme{tag}_", **kw)

    app = _start(module_dir, name)

    assert not loader.is_running(name)
    assert stray in loader.load_errors()[name]
    assert stray not in Base.metadata.tables
    assert f"/{inner}/ping" not in _paths(app)


def test_module_without_a_prefix_defining_a_table_is_taken_out(module_dir):
    stray = f"acme{_tag()}_records"
    name, _inner = _module(module_dir, prefix=None, init_table=stray)

    _start(module_dir, name)

    assert not loader.is_running(name)
    assert "table_prefix" in loader.load_errors()[name]
    assert stray not in Base.metadata.tables


def test_module_tables_inside_its_prefix_are_kept(module_dir):
    tag = _tag()
    name, inner = _module(module_dir, prefix=f"acme{tag}_",
                          init_table=f"acme{tag}_items", route_table=f"acme{tag}_logs")

    app = _start(module_dir, name)

    assert loader.is_running(name), loader.load_errors()
    assert {f"acme{tag}_items", f"acme{tag}_logs"} <= set(Base.metadata.tables)
    assert f"/{inner}/ping" in _paths(app)


def test_default_module_tables_keep_their_names(module_dir, monkeypatch):
    """A first-party module's tables are part of Celerp's own schema."""
    table = f"other{_tag()}_records"
    name, _inner = _module(module_dir, prefix=None, init_table=table)
    monkeypatch.setattr(loader, "is_first_party", lambda path: True)

    _start(module_dir, name)

    assert loader.is_running(name), loader.load_errors()
    assert table in Base.metadata.tables


_RESHAPES = {
    "added_column": "sa.Table('users', Base.metadata, sa.Column('acme_extra', sa.Text), "
                    "extend_existing=True)\n",
    "replaced_column": "sa.Table('users', Base.metadata, sa.Column('email', sa.Integer), "
                       "extend_existing=True)\n",
    "added_index": "sa.Index('ix_acme_users', Base.metadata.tables['users'].c.email)\n",
    "removed_table": "Base.metadata.remove(Base.metadata.tables['users'])\n",
}


@pytest.mark.parametrize("where", ["import", "route_setup"])
@pytest.mark.parametrize("reshape", sorted(_RESHAPES))
def test_module_reshaping_a_core_table_is_taken_out_and_the_table_restored(
        module_dir, where, reshape):
    """extend_existing (or any other change) on a table the module does not own
    would change what every query against it reads and writes."""
    import celerp.models.company  # noqa: F401  (core tables on the metadata)

    users = Base.metadata.tables["users"]
    shape = (list(users.columns), set(users.constraints), set(users.indexes))
    name, inner = _module(module_dir, prefix=f"acme{_tag()}_")
    pkg = module_dir / name
    code = "import sqlalchemy as sa\nfrom celerp.models.base import Base\n" + _RESHAPES[reshape]
    if where == "import":
        (pkg / inner / "models.py").write_text(code)
        init = pkg / "__init__.py"
        init.write_text(f"from {inner} import models\n" + init.read_text())
    else:
        (pkg / inner / "route_models.py").write_text(code)
        routes = pkg / inner / "routes.py"
        routes.write_text(routes.read_text().replace(
            "def setup_api_routes(app):\n",
            f"def setup_api_routes(app):\n    import {inner}.route_models\n"))

    app = _start(module_dir, name)

    assert not loader.is_running(name)
    assert "does not own: users" in loader.load_errors()[name]
    assert f"/{inner}/ping" not in _paths(app)
    assert Base.metadata.tables["users"] is users
    assert (list(users.columns), set(users.constraints), set(users.indexes)) == shape
