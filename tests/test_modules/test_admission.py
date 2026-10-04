# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Module admission: a module is checked before any of its code runs.

Every enabled module passes one static preflight (loader.admit_modules) before
the migration phase or the loader executes anything it ships. A refused module
runs nothing, in either process, and the refusal is reported. Route entrypoints
are proven to be the module's own code, and a module whose routes fail to
register is taken out of that process whole, together with every module that
depends on it.

Every fixture module is written under tmp_path with a unique inner package, so
nothing depends on an installed module or on another test's imports.
"""
from __future__ import annotations

import json
import sys
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest

from celerp.modules import loader, slots
from celerp.modules.importer import PREMIUM_MARKER
from celerp.modules.meta import write_meta


@pytest.fixture(autouse=True)
def _clean_loader_state():
    slots.clear()
    loader._loaded.clear()
    loader._load_errors.clear()
    before_path = list(sys.path)
    before_mods = set(sys.modules)
    yield
    slots.clear()
    loader._loaded.clear()
    loader._load_errors.clear()
    sys.path[:] = before_path
    for key in set(sys.modules) - before_mods:
        if key.startswith(("acme", "_celerp_module_migration_")):
            sys.modules.pop(key, None)


def _uid() -> str:
    return uuid.uuid4().hex[:8]


def _write_module(base: Path, folder: str, manifest: dict, files: dict[str, str] | None = None,
                  init_prelude: str = "") -> Path:
    """base/folder/__init__.py holding `manifest` as a literal, plus `files`
    (paths relative to the module folder)."""
    pkg = base / folder
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text(f"{init_prelude}\nPLUGIN_MANIFEST = {manifest!r}\n")
    for rel, body in (files or {}).items():
        target = pkg / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)
    return pkg


def _marker_line(marker: Path) -> str:
    return f"open({str(marker)!r}, 'a').write('ran\\n')\n"


def _migrating_module(base: Path, folder: str, marker: Path, **manifest_extra) -> Path:
    """A module whose single migration file writes `marker` the moment it is
    executed (at import, before upgrade() is even looked up)."""
    inner = f"acme_{_uid()}"
    manifest = {"name": folder, "version": "1.0.0",
                "migrations": f"{inner}.migrations", "table_prefix": f"acme{_uid()}_"}
    manifest.update(manifest_extra)
    files = {
        f"{inner}/__init__.py": "",
        f"{inner}/migrations/__init__.py": "",
        f"{inner}/migrations/m_001.py": _marker_line(marker) + "def upgrade():\n    pass\n",
    }
    return _write_module(base, folder, manifest, files)


# ── A1: static admission before migrations ───────────────────────────────────


@pytest.fixture
def _modules(tmp_path, monkeypatch):
    base = tmp_path / "modules"
    base.mkdir()
    monkeypatch.setenv("MODULE_DIR", str(base))
    return base


async def _admit_and_migrate(engine, base: Path, enabled: set[str]):
    from celerp.modules.migrations_runner import run_migration_phase

    admission = loader.admit_modules(str(base), enabled)
    admission = await run_migration_phase(engine, admission)
    loaded = loader.load_all(str(base), enabled, admission=admission)
    return admission, loaded


async def test_admitted_module_migration_runs(_db_engine, _modules, tmp_path):
    """Control: a module that passes every rule has its migration executed."""
    marker = tmp_path / "ran.txt"
    pkg = _migrating_module(_modules, f"acme-{_uid()}", marker)

    admission, loaded = await _admit_and_migrate(_db_engine, _modules, {pkg.name})

    assert marker.exists()
    assert [m["name"] for m in loaded] == [pkg.name]
    assert admission.refused == {}


def _case_name_mismatch(base, marker, monkeypatch):
    return _migrating_module(base, f"acme-{_uid()}", marker, name=f"acme-other-{_uid()}"), "folder"


def _case_reserved_prefix(base, marker, monkeypatch):
    return _migrating_module(base, f"celerp-{_uid()}", marker), "reserved"


def _case_min_version(base, marker, monkeypatch):
    import celerp
    monkeypatch.setattr(celerp, "__version__", "1.0.0")
    return _migrating_module(base, f"acme-{_uid()}", marker,
                             min_celerp_version="999.0.0"), "requires Celerp 999.0.0"


def _case_missing_table_prefix(base, marker, monkeypatch):
    pkg = _migrating_module(base, f"acme-{_uid()}", marker)
    manifest = loader.read_manifest(pkg)
    del manifest["table_prefix"]
    (pkg / "__init__.py").write_text(f"PLUGIN_MANIFEST = {manifest!r}\n")
    return pkg, "table_prefix"


def _case_migrations_absolute_path(base, marker, monkeypatch):
    outside = base.parent / f"outside{_uid()}"
    outside.mkdir()
    (outside / "m_001.py").write_text(_marker_line(marker) + "def upgrade():\n    pass\n")
    return _migrating_module(base, f"acme-{_uid()}", marker,
                             migrations=str(outside)), "migrations"


def _case_migrations_symlink_escape(base, marker, monkeypatch):
    outside = base.parent / f"outside{_uid()}"
    outside.mkdir()
    (outside / "m_001.py").write_text(_marker_line(marker) + "def upgrade():\n    pass\n")
    pkg = _migrating_module(base, f"acme-{_uid()}", tmp_marker := marker.with_suffix(".unused"))
    (pkg / "linked").symlink_to(outside, target_is_directory=True)
    manifest = loader.read_manifest(pkg)
    manifest["migrations"] = "linked"
    (pkg / "__init__.py").write_text(f"PLUGIN_MANIFEST = {manifest!r}\n")
    assert not tmp_marker.exists()
    return pkg, "outside"


def _case_route_outside_module(base, marker, monkeypatch):
    return _migrating_module(base, f"acme-{_uid()}", marker,
                             api_routes="celerp.routers.health"), "api_routes"


def _case_route_source_missing(base, marker, monkeypatch):
    return _migrating_module(base, f"acme-{_uid()}", marker,
                             ui_routes=f"acme_{_uid()}.ui_routes"), "ui_routes"


def _case_protected_import_in_migration(base, marker, monkeypatch):
    pkg = _migrating_module(base, f"acme-{_uid()}", marker)
    mig = next(pkg.rglob("m_001.py"))
    mig.write_text("import celerp.session_gate\n" + mig.read_text())
    return pkg, "celerp.session_gate"


def _case_protected_import_in_init(base, marker, monkeypatch):
    pkg = _migrating_module(base, f"acme-{_uid()}", marker)
    init = pkg / "__init__.py"
    init.write_text("from celerp.ai import quota\n" + init.read_text())
    return pkg, "celerp.ai.quota"


def _case_unlicensed_premium(base, marker, monkeypatch):
    from celerp.config import settings
    import celerp.config
    import celerp.gateway.state

    pkg = _migrating_module(base, f"acme-{_uid()}", marker)
    (pkg / PREMIUM_MARKER).write_text("")
    monkeypatch.setattr(settings, "gateway_token", "test-gateway-token")
    monkeypatch.setattr(celerp.gateway.state, "relay_http_url", lambda: "https://relay.invalid")
    monkeypatch.setattr(celerp.config, "ensure_instance_id", lambda: "instance-1")
    monkeypatch.setattr(loader, "exchange_api_key_for_jwt", lambda *a, **k: "jwt")
    monkeypatch.setattr(loader, "check_license", lambda **k: False)
    return pkg, "license"


@pytest.mark.parametrize("case", [
    _case_name_mismatch,
    _case_reserved_prefix,
    _case_min_version,
    _case_missing_table_prefix,
    _case_migrations_absolute_path,
    _case_migrations_symlink_escape,
    _case_route_outside_module,
    _case_route_source_missing,
    _case_protected_import_in_migration,
    _case_protected_import_in_init,
    _case_unlicensed_premium,
], ids=lambda c: c.__name__.removeprefix("_case_"))
async def test_refused_module_runs_no_migration_and_is_reported(
        case, _db_engine, _modules, tmp_path, monkeypatch):
    marker = tmp_path / "ran.txt"
    pkg, reason = case(_modules, marker, monkeypatch)

    admission, loaded = await _admit_and_migrate(_db_engine, _modules, {pkg.name})

    assert not marker.exists(), "a refused module's migration code was executed"
    assert loaded == []
    assert not loader.is_running(pkg.name)
    assert reason in loader.load_errors()[pkg.name]


async def test_dependent_of_a_refused_module_runs_no_migration(
        _db_engine, _modules, tmp_path):
    bad_marker = tmp_path / "bad.txt"
    dep_marker = tmp_path / "dependent.txt"
    bad = _migrating_module(_modules, f"acme-{_uid()}", bad_marker, name=f"acme-x{_uid()}")
    dependent = _migrating_module(_modules, f"acme-{_uid()}", dep_marker, depends_on=[bad.name])

    await _admit_and_migrate(_db_engine, _modules, {bad.name, dependent.name})

    assert not bad_marker.exists()
    assert not dep_marker.exists()
    assert loader.load_errors()[dependent.name] == f"Requires {bad.name!r}, which failed to load."


async def test_dependent_of_a_failed_migration_runs_no_migration(
        _db_engine, _modules, tmp_path):
    failing = _migrating_module(_modules, f"acme-{_uid()}", tmp_path / "f.txt")
    mig = next(failing.rglob("m_001.py"))
    mig.write_text("def upgrade():\n    raise RuntimeError('boom')\n")
    dep_marker = tmp_path / "dependent.txt"
    dependent = _migrating_module(_modules, f"acme-{_uid()}", dep_marker,
                                  depends_on=[failing.name])

    admission, loaded = await _admit_and_migrate(
        _db_engine, _modules, {failing.name, dependent.name})

    assert not dep_marker.exists()
    assert loaded == []
    assert "boom" in loader.load_errors()[failing.name]
    assert loader.load_errors()[dependent.name] == (
        f"Requires {failing.name!r}, which failed to load.")


def test_official_marketplace_module_keeps_reserved_prefix(_modules):
    """The reserved prefix is the importer's rule, not a blanket ban: a module the
    marketplace installed as official keeps its celerp- name."""
    pkg = _write_module(_modules, f"celerp-{_uid()}", {"name": "", "version": "1.0.0"})
    manifest = {"name": pkg.name, "version": "1.0.0"}
    (pkg / "__init__.py").write_text(f"PLUGIN_MANIFEST = {manifest!r}\n")
    write_meta(pkg, source="marketplace")

    admission = loader.admit_modules(str(_modules), {pkg.name})

    assert [a.name for a in admission.admitted] == [pkg.name]


# ── A1 at load: a refused module's own code never runs ───────────────────────


def _init_marker_module(base: Path, folder: str, marker: Path, manifest: dict,
                        files: dict[str, str] | None = None) -> Path:
    return _write_module(base, folder, manifest, files, init_prelude=_marker_line(marker))


@pytest.mark.parametrize("variant", ["name_mismatch", "reserved_prefix", "route_outside",
                                     "protected_import_in_slot_module"])
def test_load_all_refuses_before_import(variant, _modules, tmp_path):
    marker = tmp_path / "imported.txt"
    folder = f"celerp-{_uid()}" if variant == "reserved_prefix" else f"acme-{_uid()}"
    inner = f"acme_{_uid()}"
    manifest = {"name": folder, "version": "1.0.0"}
    files: dict[str, str] = {f"{inner}/__init__.py": ""}
    if variant == "name_mismatch":
        manifest["name"] = f"acme-{_uid()}"
    elif variant == "route_outside":
        manifest["api_routes"] = "celerp.routers.health"
    elif variant == "protected_import_in_slot_module":
        files[f"{inner}/hooks.py"] = (
            "async def ready(session=None, **kw):\n"
            "    import celerp.ai.quota\n")
        manifest["slots"] = {"on_modules_ready": {"handler": f"{inner}.hooks:ready"}}
    pkg = _init_marker_module(_modules, folder, marker, manifest, files)

    loaded = loader.load_all(str(_modules), {pkg.name})

    assert loaded == []
    assert not marker.exists(), "a refused module's __init__ was executed"
    assert pkg.name in loader.load_errors()


def test_runtime_manifest_must_match_the_admitted_one(_modules):
    """Admission reads the literal; code that rewrites PLUGIN_MANIFEST at import
    cannot widen what was admitted."""
    inner = f"acme_{_uid()}"
    folder = f"acme-{_uid()}"
    pkg = _write_module(
        _modules, folder, {"name": folder, "version": "1.0.0"},
        {f"{inner}/__init__.py": ""})
    init = pkg / "__init__.py"
    init.write_text(init.read_text() + "PLUGIN_MANIFEST['api_routes'] = 'celerp.routers.health'\n")

    loaded = loader.load_all(str(_modules), {folder})

    assert loaded == []
    assert "differs" in loader.load_errors()[folder]


# ── A2: route entrypoint provenance ──────────────────────────────────────────


class _App:
    """Just enough of a Starlette app for route registration."""

    def __init__(self):
        from starlette.routing import Router
        self.router = Router()

    def add_api_route(self, path, endpoint, **kw):
        self.router.add_route(path, endpoint, **kw)


def _route_module(base: Path, folder: str, *, kind: str = "api", body: str | None = None,
                  depends_on=None, extra_files=None, slots_manifest=None,
                  route_path: str | None = None) -> tuple[Path, str]:
    inner = f"acme_{_uid()}"
    path = route_path or f"/{inner}/ping"
    manifest = {"name": folder, "version": "1.0.0", f"{kind}_routes": f"{inner}.routes"}
    if depends_on:
        manifest["depends_on"] = depends_on
    if slots_manifest:
        manifest["slots"] = {k: _fill(v, inner) for k, v in slots_manifest.items()}
    files = {
        f"{inner}/__init__.py": "",
        f"{inner}/routes.py": body if body is not None else (
            "from starlette.responses import PlainTextResponse\n\n"
            f"def setup_{kind}_routes(app):\n"
            f"    app.router.add_route({path!r}, lambda r: PlainTextResponse('ok'))\n"),
    }
    files.update({k.replace("{inner}", inner): v.replace("{inner}", inner)
                  for k, v in (extra_files or {}).items()})
    return _write_module(base, folder, manifest, files), inner


def _fill(value, inner):
    if isinstance(value, dict):
        return {k: _fill(v, inner) for k, v in value.items()}
    if isinstance(value, list):
        return [_fill(v, inner) for v in value]
    return value.replace("{inner}", inner) if isinstance(value, str) else value


def _paths(app) -> set[str]:
    return {getattr(r, "path", None) for r in app.router.routes}


def test_route_module_of_another_module_is_refused_before_import(_modules, tmp_path):
    marker = tmp_path / "other_setup_ran.txt"
    other, other_inner = _route_module(_modules, f"acme-{_uid()}", body=(
        _marker_line(marker) + "def setup_api_routes(app):\n    pass\n"))
    folder = f"acme-{_uid()}"
    _write_module(_modules, folder, {"name": folder, "version": "1.0.0",
                                     "api_routes": f"{other_inner}.routes"})

    loaded = loader.load_all(str(_modules), {folder})
    loader.register_api_routes(_App(), loaded)

    assert not marker.exists()
    assert not loader.is_running(folder)
    assert "api_routes" in loader.load_errors()[folder]


def test_missing_route_source_is_refused(_modules):
    folder = f"acme-{_uid()}"
    _write_module(_modules, folder, {"name": folder, "version": "1.0.0",
                                     "ui_routes": f"acme_{_uid()}.routes"})

    loaded = loader.load_all(str(_modules), {folder})

    assert loaded == []
    assert "ui_routes" in loader.load_errors()[folder]


def test_route_module_without_its_setup_function_is_refused(_modules):
    folder = f"acme-{_uid()}"
    _route_module(_modules, folder, body="def something_else(app):\n    pass\n")

    loaded = loader.load_all(str(_modules), {folder})

    assert loaded == []
    assert "setup_api_routes" in loader.load_errors()[folder]


def test_setup_imported_from_another_module_is_refused_before_import(_modules, tmp_path):
    marker = tmp_path / "borrowed_setup_ran.txt"
    other, other_inner = _route_module(_modules, f"acme-{_uid()}", body=(
        _marker_line(marker) + "def setup_api_routes(app):\n    pass\n"))
    folder = f"acme-{_uid()}"
    _route_module(_modules, folder,
                  body=f"from {other_inner}.routes import setup_api_routes  # noqa: F401\n")

    loaded = loader.load_all(str(_modules), {folder})
    loader.register_api_routes(_App(), loaded)

    assert not marker.exists()
    assert not loader.is_running(folder)
    assert "outside the module" in loader.load_errors()[folder]


def test_setup_rebound_to_another_module_is_refused(_modules, tmp_path):
    """The route file defines its setup, then rebinds the name to another module's
    function: provenance is proven on the callable import actually returns,
    before core calls it."""
    marker = tmp_path / "borrowed_setup_ran.txt"
    other, other_inner = _route_module(_modules, f"acme-{_uid()}", kind="ui", body=(
        "def setup_ui_routes(app):\n    pass\n\n"
        "def setup_api_routes(app):\n    " + _marker_line(marker)))
    folder = f"acme-{_uid()}"
    _route_module(_modules, folder, depends_on=[other.name], body=(
        "def setup_api_routes(app):\n    pass\n\n"
        f"from {other_inner}.routes import setup_api_routes  # noqa: E402,F811\n"))

    loaded = loader.load_all(str(_modules), {folder, other.name})
    assert loader.is_running(folder)
    loader.register_api_routes(_App(), loaded)

    assert not marker.exists()
    assert not loader.is_running(folder)
    assert loader.is_running(other.name)
    assert "api_routes setup" in loader.load_errors()[folder]
    assert "outside" in loader.load_errors()[folder]


def test_owned_route_module_registers(_modules):
    folder = f"acme-{_uid()}"
    _pkg, inner = _route_module(_modules, folder)
    app = _App()

    loaded = loader.load_all(str(_modules), {folder})
    loader.register_api_routes(app, loaded)

    assert loader.is_running(folder)
    assert f"/{inner}/ping" in _paths(app)


def test_protected_internal_imported_as_a_submodule_name_is_refused(_modules, tmp_path):
    """`from celerp.ai import quota` imports the protected celerp.ai.quota as
    surely as `import celerp.ai.quota` does."""
    marker = tmp_path / "setup_ran.txt"
    folder = f"acme-{_uid()}"
    _route_module(_modules, folder, body=(
        "from celerp.ai import quota  # noqa: F401\n\n"
        "def setup_api_routes(app):\n    " + _marker_line(marker)))

    loader.register_api_routes(_App(), loader.load_all(str(_modules), {folder}))

    assert not marker.exists()
    assert not loader.is_running(folder)
    assert "celerp.ai.quota" in loader.load_errors()[folder]


def test_locale_file_outside_the_module_is_not_registered(_modules):
    from ui.i18n import t

    folder = f"acme-{_uid()}"
    (_modules / "outside.json").write_text(json.dumps({"acme.outside.label": "Leaked"}))
    _write_module(_modules, folder, {"name": folder, "version": "1.0.0",
                                     "locales": {"zx": {"file": "../outside.json"}}})

    loader.load_all(str(_modules), {folder})

    assert loader.is_running(folder)
    assert t("acme.outside.label", "zx") == "acme.outside.label"


# ── A3: a route failure takes the module, and its dependents, out whole ─────


_FAILING_SETUP = (
    "from starlette.responses import PlainTextResponse\n\n"
    "def setup_{kind}_routes(app):\n"
    "    app.router.add_route('/{inner}/half', lambda r: PlainTextResponse('half'))\n"
    "    raise RuntimeError('route setup exploded')\n")

_HOOK_FILE = (
    "async def ready(session=None, **kw):\n"
    "    open({marker!r}, 'a').write('hook ran\\n')\n")


def _failing_pair(base: Path, kind: str, hook_marker: Path):
    failing_folder = f"acme-{_uid()}"
    failing, failing_inner = _route_module(
        base, failing_folder, kind=kind,
        body=_FAILING_SETUP.replace("{kind}", kind).replace("{inner}", "PLACEHOLDER"),
        slots_manifest={
            "nav": {"label": "Failing", "href": "/{inner}/home"},
            "on_modules_ready": {"handler": "{inner}.hooks:ready"},
        },
        extra_files={"{inner}/hooks.py": _HOOK_FILE.format(marker=str(hook_marker))})
    routes = failing / failing_inner / "routes.py"
    routes.write_text(routes.read_text().replace("PLACEHOLDER", failing_inner))
    dependent_folder = f"acme-{_uid()}"
    dependent, dependent_inner = _route_module(
        base, dependent_folder, kind=kind, depends_on=[failing_folder],
        slots_manifest={
            "nav": {"label": "Dependent", "href": "/{inner}/home"},
            "on_modules_ready": {"handler": "{inner}.hooks:ready"},
        },
        extra_files={"{inner}/hooks.py": _HOOK_FILE.format(marker=str(hook_marker))})
    return failing_folder, failing_inner, dependent_folder, dependent_inner


@pytest.mark.parametrize("kind", ["api", "ui"])
async def test_route_failure_deactivates_module_and_dependents(kind, _modules, tmp_path):
    hook_marker = tmp_path / "hook.txt"
    failing, failing_inner, dependent, dependent_inner = _failing_pair(
        _modules, kind, hook_marker)
    app = _App()

    loaded = loader.load_all(str(_modules), {failing, dependent})
    assert {m["name"] for m in loaded} == {failing, dependent}
    register = loader.register_api_routes if kind == "api" else loader.register_ui_routes
    register(app, loaded)

    for name in (failing, dependent):
        assert not loader.is_running(name)
        assert name not in {m["name"] for m in loader.loaded_modules()}
        assert all(e["_module"] != name for entries in slots.all_slots().values()
                   for e in entries)
    assert "route setup exploded" in loader.load_errors()[failing]
    assert loader.load_errors()[dependent] == f"Requires {failing!r}, which failed to load."
    assert f"/{failing_inner}/half" not in _paths(app)
    assert f"/{dependent_inner}/ping" not in _paths(app)
    await slots.fire_lifecycle("on_modules_ready", session=None)
    assert not hook_marker.exists()


def test_route_failure_drops_the_module_locale_catalog(_modules):
    from ui.i18n import t

    folder = f"acme-{_uid()}"
    pkg, inner = _route_module(_modules, folder, body=_FAILING_SETUP.replace(
        "{kind}", "ui").replace("{inner}", "x"))
    (pkg / "zz.json").write_text(json.dumps({"acme.failing.label": "Echec"}))
    manifest = loader.read_manifest(pkg)
    manifest["ui_routes"] = manifest.pop("api_routes")
    manifest["locales"] = {"zz": {"file": "zz.json"}}
    (pkg / "__init__.py").write_text(f"PLUGIN_MANIFEST = {manifest!r}\n")

    loaded = loader.load_all(str(_modules), {folder})
    assert t("acme.failing.label", "zz") == "Echec"
    loader.register_ui_routes(_App(), loaded)

    assert t("acme.failing.label", "zz") == "acme.failing.label"


# ── A3b: the Modules page shows a failure from the process that renders it ──


@pytest.fixture
def _mock_get_modules_default():
    """The real get_modules: overrides the suite-wide mock of the same name."""
    yield


async def test_modules_listing_reports_this_process_load_failures(monkeypatch):
    from ui import api_client

    api_rows = [
        {"name": "acme-ok", "enabled": True, "running": True, "load_error": None},
        {"name": "acme-ui-broke", "enabled": True, "running": True, "load_error": None},
    ]

    @asynccontextmanager
    async def _fake_client(token, timeout=10.0):
        class _C:
            async def get(self, url):
                return httpx.Response(200, json=api_rows,
                                      request=httpx.Request("GET", "http://api" + url))
        yield _C()

    monkeypatch.setattr(api_client, "_api_client", _fake_client)
    loader._load_errors["acme-ui-broke"] = "ui_routes failed (RuntimeError: nope)"

    rows = {r["name"]: r for r in await api_client.get_modules("tok")}

    assert rows["acme-ok"]["running"] is True
    assert rows["acme-ui-broke"]["running"] is False
    assert rows["acme-ui-broke"]["load_error"] == "ui_routes failed (RuntimeError: nope)"
