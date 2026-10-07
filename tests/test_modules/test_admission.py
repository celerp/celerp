# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Module admission: a module is checked before any of its code runs.

Every enabled module passes one static preflight (loader.admit_modules) before
the migration phase or the loader executes anything it ships. A refused module
runs nothing, in either process, and the refusal is reported. Route entrypoints
are proven to be the module's own code, and a module whose routes fail to
register is taken out of that process whole, together with every module that
depends on it. A module that fails in the UI process stops in the API process
too, through the outcome record both processes share.

Every fixture module is written under tmp_path with a unique inner package, so
nothing depends on an installed module or on another test's imports.
"""
from __future__ import annotations

import ast
import io
import json
import sys
import time
import uuid
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest

from celerp.modules import loader, slots
from celerp.modules.importer import PREMIUM_MARKER, install_from_zip
from celerp.modules.license import UNVERIFIED_MODULE_REFUSAL


@pytest.fixture(autouse=True)
def _clean_loader_state():
    from celerp.models.base import Base

    slots.clear()
    loader._loaded.clear()
    loader._load_errors.clear()
    before_path = list(sys.path)
    before_mods = set(sys.modules)
    before_tables = set(Base.metadata.tables)
    yield
    slots.clear()
    loader._loaded.clear()
    loader._load_errors.clear()
    # A fixture module's models must not reach another test's create_all.
    for key in set(Base.metadata.tables) - before_tables:
        Base.metadata.remove(Base.metadata.tables[key])
    loader._module_tables.clear()
    loader._removed_tables.clear()
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


def _migrating_module(base: Path, folder: str, marker: Path, code: dict[str, str] | None = None,
                      **manifest_extra) -> Path:
    """A module whose single migration file writes `marker` the moment it is
    executed (at import, before upgrade() is even looked up). `code` adds files
    to its inner package; "{inner}" in a manifest string names that package."""
    inner = f"acme_{_uid()}"
    manifest = {"name": folder, "version": "1.0.0",
                "migrations": f"{inner}.migrations", "table_prefix": f"acme{_uid()}_"}
    manifest.update(ast.literal_eval(repr(manifest_extra).replace("{inner}", inner)))
    files = {
        f"{inner}/__init__.py": "",
        f"{inner}/migrations/__init__.py": "",
        f"{inner}/migrations/m_001.py": _marker_line(marker) + "def upgrade():\n    pass\n",
        **{f"{inner}/{rel}": body for rel, body in (code or {}).items()},
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
    return pkg, "celerp.ai"


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


def _relay_identity(monkeypatch, data_dir: Path, detail: dict | None = None,
                    licensed: bool = False, activated: bool = True) -> dict:
    """An instance whose relay answers the Marketplace module-detail request
    with *detail* (unreachable when None). Activated, the licence check answers
    *licensed*; never activated, there is no token to exchange and the real
    licence check runs. Returns the licence checks and detail requests made."""
    from celerp.config import settings
    import celerp.config

    calls: dict = {"licence": [], "detail": []}

    class _Reply(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def _urlopen(url, timeout=None):
        calls["detail"].append(str(url))
        if detail is None:
            raise OSError("unreachable")
        return _Reply(json.dumps(detail).encode())

    def _check_license(**kw):
        calls["licence"].append(kw["slug"])
        return licensed

    def _no_exchange(*a, **k):
        raise AssertionError("a never-activated instance has no token to exchange")

    monkeypatch.setattr(settings, "gateway_token", "test-gateway-token" if activated else "")
    monkeypatch.setattr(settings, "gateway_http_url", "https://relay.invalid")
    monkeypatch.setattr(celerp.config, "ensure_instance_id", lambda: "instance-1")
    monkeypatch.setattr(settings, "data_dir", data_dir)
    monkeypatch.delenv("DATA_DIR", raising=False)
    monkeypatch.setattr("urllib.request.urlopen", _urlopen)
    if activated:
        monkeypatch.setattr(loader, "exchange_api_key_for_jwt", lambda *a, **k: "jwt")
        monkeypatch.setattr(loader, "check_license", _check_license)
    else:
        monkeypatch.setattr(loader, "exchange_api_key_for_jwt", _no_exchange)
    return calls


def _case_celerp_name_without_a_licence(base, marker, monkeypatch):
    pkg = _migrating_module(base, f"celerp-{_uid()}", marker)
    _relay_identity(monkeypatch, base.parent / "data")
    return pkg, "verify this module"


def _case_package_of_a_core_module(root):
    def case(base, marker, monkeypatch):
        assert root in sys.modules
        return _migrating_module(base, f"celerp-{_uid()}", marker,
                                 code={f"../{root}/__init__.py": ""}), repr(root)
    case.__name__ = f"_case_package_of_{root}"
    return case


def _case_package_imported_from_another_module(base, marker, monkeypatch):
    import importlib

    root = f"acme_loaded_{_uid()}"
    other = _write_module(base.parent / "elsewhere", f"acme-{_uid()}",
                          {"name": "other", "version": "1.0.0"}, {f"{root}/__init__.py": ""})
    monkeypatch.syspath_prepend(str(other))
    importlib.import_module(root)
    return _migrating_module(base, f"acme-{_uid()}", marker,
                             code={f"../{root}/__init__.py": ""}), "already used"


def _case_async_api_setup(base, marker, monkeypatch):
    return _migrating_module(base, f"acme-{_uid()}", marker, api_routes="{inner}.api",
                             code={"api.py": "async def setup_api_routes(app):\n    pass\n"}), "async"


def _case_async_ui_setup_imported(base, marker, monkeypatch):
    return _migrating_module(base, f"acme-{_uid()}", marker, ui_routes="{inner}.ui", code={
        "ui.py": "from .pages import setup_ui_routes\n",
        "pages.py": "async def setup_ui_routes(app):\n    pass\n"}), "async"


def _case_hook_not_async(base, marker, monkeypatch):
    return _migrating_module(base, f"acme-{_uid()}", marker,
                             slots={"on_company_created": [{"handler": "{inner}.hooks:created"}]},
                             code={"hooks.py": "def created(session, company_id):\n    pass\n"}), "async"


def _case_render_async(base, marker, monkeypatch):
    return _migrating_module(base, f"acme-{_uid()}", marker,
                             slots={"doc_detail_badges": [{"render": "{inner}.ui:badge"}]},
                             code={"ui.py": "async def badge(doc):\n    return None\n"}), "async"


def _case_callable_missing(base, marker, monkeypatch):
    return _migrating_module(base, f"acme-{_uid()}", marker,
                             slots={"on_modules_ready": [{}]}), "module.path:function"


def _case_search_result_key(base, marker, monkeypatch):
    return _migrating_module(base, f"acme-{_uid()}", marker, slots={"search_provider": {
        "handler": "{inner}.search:find", "result_key": "rows", "permission": "view_inventory"}},
        code={"search.py": "async def find(*a):\n    return {}\n"}), "result_key"


def _case_nav_order_text(base, marker, monkeypatch):
    return _migrating_module(base, f"acme-{_uid()}", marker, slots={
        "nav": [{"key": "acme", "label": "Acme", "href": "/acme", "order": "1"}]}), "order"


def _case_unknown_slot(base, marker, monkeypatch):
    return _migrating_module(base, f"acme-{_uid()}", marker, slots={
        "settings_tab": [{"label": "Acme"}]}), "unknown slot 'settings_tab'"


def _case_category_fields_not_list(base, marker, monkeypatch):
    return _migrating_module(base, f"acme-{_uid()}", marker, slots={
        "category_schema": [{"category": "Rings", "fields": {"key": "size"}}]}), "fields"


def _case_connector_not_text(base, marker, monkeypatch):
    return _migrating_module(base, f"acme-{_uid()}", marker, slots={"bulk_action": [
        {"label": "Go", "form_action": "/acme/go", "requires_connector": ["shop"]}]}), "requires_connector"


def _case_pricing_show_on_entry(base, marker, monkeypatch):
    return _migrating_module(base, f"acme-{_uid()}", marker, slots={"pricing_action": [
        {"label": "Go", "href_template": "/acme/{entity_id}", "show_on": [{}]}]}), "show_on"


def _case_item_action_link_out(base, marker, monkeypatch):
    return _migrating_module(base, f"acme-{_uid()}", marker, slots={
        "item_action": [{"label": "Go", "href_template": "//example.com/{entity_id}"}]}), "href_template"


def _case_lineage_guard_not_async(base, marker, monkeypatch):
    return _migrating_module(base, f"acme-{_uid()}", marker,
                             slots={"item_lineage_guard": [{"handler": "{inner}.lineage:guard"}]},
                             code={"lineage.py": "def guard(*, session, entry, transition):\n    return None\n"}
                             ), "must be async"


def _case_lineage_guard_wrong_arity(base, marker, monkeypatch):
    return _migrating_module(base, f"acme-{_uid()}", marker,
                             slots={"item_lineage_guard": [{"handler": "{inner}.lineage:guard"}]},
                             code={"lineage.py": "async def guard(session, entry):\n    return None\n"}
                             ), "session, entry, transition"


def _case_in_production_wrong_arity(base, marker, monkeypatch):
    monkeypatch.setattr(loader, "is_first_party", lambda pkg_path: True)
    return _migrating_module(base, f"acme-{_uid()}", marker,
                             slots={"inventory_in_production": [{"handler": "{inner}.wip:held"}]},
                             code={"wip.py": "async def held(session, company_id, extra):\n    return 0\n"}
                             ), "session, company_id"


def _case_in_production_not_first_party(base, marker, monkeypatch):
    return _migrating_module(base, f"acme-{_uid()}", marker,
                             slots={"inventory_in_production": [{"handler": "{inner}.wip:held"}]},
                             code={"wip.py": "async def held(*, session, company_id):\n    return 0\n"}
                             ), "is filled by Celerp's own modules only"


_PROJECT = {"proj.py": "def apply(state, event_type, data):\n    return state\n"}


def _projecting_module(base, marker, *prefixes, folder: str | None = None):
    return _migrating_module(
        base, folder or f"acme-{_uid()}", marker, code=_PROJECT,
        slots={"projection_handler": [{"prefix": p, "handler": "{inner}.proj:apply"}
                                      for p in prefixes]})


def _case_projection_prefix_twice(base, marker, monkeypatch):
    return _projecting_module(base, marker, "acme.", "acme."), "overlap"


def _case_projection_prefixes_overlap(base, marker, monkeypatch):
    return _projecting_module(base, marker, "acme.", "acme.order."), "overlap"


def _case_projection_prefix_is_cores(base, marker, monkeypatch):
    return _projecting_module(base, marker, "sys."), "already handles"


def _case_projection_prefix_covers_cores(base, marker, monkeypatch):
    return _projecting_module(base, marker, "s"), "already handles"


def _case_projection_prefix_inside_cores(base, marker, monkeypatch):
    return _projecting_module(base, marker, "mp.order."), "already handles"


_WRAP = "import functools\n\ndef wrap(fn):\n    @functools.wraps(fn)\n    def inner(*a, **k):\n        return fn(*a, **k)\n    return inner\n\n"


def _case_hook_decorated(base, marker, monkeypatch):
    """A decorator can turn an async def into a sync callable: the source cannot
    prove the call style, so admission refuses it rather than let load find out
    after the migration ran."""
    return _migrating_module(base, f"acme-{_uid()}", marker,
                             slots={"on_company_created": [{"handler": "{inner}.hooks:created"}]},
                             code={"hooks.py": _WRAP + "@wrap\nasync def created(session, company_id):\n    pass\n"}
                             ), "top-level def"


def _case_hook_undefined(base, marker, monkeypatch):
    return _migrating_module(base, f"acme-{_uid()}", marker,
                             slots={"on_company_created": [{"handler": "{inner}.hooks:created"}]},
                             code={"hooks.py": "async def other(session, company_id):\n    pass\n"}
                             ), "top-level def"


def _case_hook_call_bound(base, marker, monkeypatch):
    return _migrating_module(base, f"acme-{_uid()}", marker,
                             slots={"on_company_created": [{"handler": "{inner}.hooks:created"}]},
                             code={"hooks.py": "def make():\n    def created(session, company_id):\n"
                                               "        pass\n    return created\n\ncreated = make()\n"}
                             ), "top-level def"


def _case_lineage_guard_decorated(base, marker, monkeypatch):
    return _migrating_module(base, f"acme-{_uid()}", marker,
                             slots={"item_lineage_guard": [{"handler": "{inner}.lineage:guard"}]},
                             code={"lineage.py": _WRAP + "@wrap\nasync def guard(session, entry):\n    return None\n"}
                             ), "top-level def"


def _case_api_setup_decorated(base, marker, monkeypatch):
    return _migrating_module(base, f"acme-{_uid()}", marker, api_routes="{inner}.api",
                             code={"api.py": _WRAP + "@wrap\nasync def setup_api_routes(app):\n    pass\n"}
                             ), "top-level def"


_READY = "async def ready(session=None, **kw):\n    return None\n"
_READY_SLOT = {"on_modules_ready": [{"handler": "{inner}.hooks:ready"}]}


def _rebound_ready(name: str, reason: str, code: dict[str, str]):
    """A module whose hooks.py shows a plain async ``ready``, while other code
    the module runs rebinds that name at import: the source no longer shows
    what core will call."""
    def case(base, marker, monkeypatch):
        return _migrating_module(base, f"acme-{_uid()}", marker, slots=_READY_SLOT,
                                 code=code), reason
    case.__name__ = f"_case_{name}"
    return case


_case_handler_rebound_through_globals = _rebound_ready(
    "handler_rebound_through_globals", "dynamically",
    {"hooks.py": _READY + "globals()['ready'] = print\n"})
_case_handler_rebound_through_globals_alias = _rebound_ready(
    "handler_rebound_through_globals_alias", "dynamically",
    {"hooks.py": _READY + "ns = globals\nns()['ready'] = print\n"})
_case_handler_rebound_through_vars = _rebound_ready(
    "handler_rebound_through_vars", "dynamically",
    {"hooks.py": "import sys\n" + _READY + "vars(sys.modules[__name__])['ready'] = print\n"})
_case_handler_rebound_through_setattr = _rebound_ready(
    "handler_rebound_through_setattr", "dynamically",
    {"hooks.py": "import sys\n" + _READY + "setattr(sys.modules[__name__], 'ready', print)\n"})
_case_handler_rebound_through_computed_setattr = _rebound_ready(
    "handler_rebound_through_computed_setattr", "dynamically",
    {"hooks.py": "import sys\n" + _READY + "me = sys.modules[__name__]\nsetattr(me, 'rea' + 'dy', print)\n"})
_case_handler_rebound_through_exec = _rebound_ready(
    "handler_rebound_through_exec", "dynamically",
    {"hooks.py": _READY + "exec('ready = print')\n"})
_case_handler_rebound_from_package_init = _rebound_ready(
    "handler_rebound_from_package_init", "dynamically",
    {"hooks.py": _READY, "__init__.py": "from . import hooks as _h\n_h.ready = print\n"})
_case_handler_rebound_by_star_import = _rebound_ready(
    "handler_rebound_by_star_import", "top-level def",
    {"hooks.py": _READY + "from .other import *\n",
     "other.py": "def ready(session=None, **kw):\n    return None\n"})
_case_handler_module_replaced = _rebound_ready(
    "handler_module_replaced", "dynamically",
    {"hooks.py": "import sys, types\n" + _READY
                 + "sys.modules[__name__] = types.SimpleNamespace(ready=print)\n"})
_case_handler_module_replaced_by_update = _rebound_ready(
    "handler_module_replaced_by_update", "dynamically",
    {"hooks.py": "import sys, types\n" + _READY
                 + "sys.modules.update({__name__: types.SimpleNamespace(ready=print)})\n"})
_case_handler_deleted = _rebound_ready(
    "handler_deleted", "top-level def",
    {"hooks.py": _READY + "del ready\n"})

# Bindings with no ast.Name node, code-object swaps, and writers reached
# through getattr: each rebinds ``ready`` after the source showed its def.
_SYNC = "def _sync(session=None, **kw):\n    return None\n"
_case_handler_rebound_by_match_capture = _rebound_ready(
    "handler_rebound_by_match_capture", "top-level def",
    {"hooks.py": _READY + _SYNC + "match _sync:\n    case ready:\n        pass\n"})
_case_handler_rebound_by_match_star = _rebound_ready(
    "handler_rebound_by_match_star", "top-level def",
    {"hooks.py": _READY + "match [1]:\n    case [*ready]:\n        pass\n"})
_case_handler_rebound_by_match_mapping_rest = _rebound_ready(
    "handler_rebound_by_match_mapping_rest", "top-level def",
    {"hooks.py": _READY + "match {}:\n    case {**ready}:\n        pass\n"})
_case_handler_deleted_by_except_as = _rebound_ready(
    "handler_deleted_by_except_as", "top-level def",
    {"hooks.py": _READY + "try:\n    raise ValueError\nexcept ValueError as ready:\n    pass\n"})
_case_handler_code_swapped = _rebound_ready(
    "handler_code_swapped", "dynamically",
    {"hooks.py": _READY + _SYNC + "ready.__code__ = _sync.__code__\n"})
_case_handler_code_swapped_through_setattr = _rebound_ready(
    "handler_code_swapped_through_setattr", "dynamically",
    {"hooks.py": _READY + _SYNC + "setattr(ready, '__code__', _sync.__code__)\n"})
_case_handler_defaults_replaced = _rebound_ready(
    "handler_defaults_replaced", "dynamically",
    {"hooks.py": _READY + "ready.__kwdefaults__ = {}\nready.__defaults__ = (1,)\n"})
_case_handler_rebound_through_getattr_setattr = _rebound_ready(
    "handler_rebound_through_getattr_setattr", "dynamically",
    {"hooks.py": "import sys, builtins\n" + _READY + _SYNC
                 + "getattr(builtins, 'set' + 'attr')(sys.modules[__name__], 're' + 'ady', _sync)\n"})
_case_handler_rebound_through_getattr_exec = _rebound_ready(
    "handler_rebound_through_getattr_exec", "dynamically",
    {"hooks.py": "import builtins\n" + _READY + _SYNC
                 + "getattr(builtins, 'ex' + 'ec')('ready = _sync')\n"})
_case_handler_rebound_through_getattr_constant = _rebound_ready(
    "handler_rebound_through_getattr_constant", "dynamically",
    {"hooks.py": "import builtins\n" + _READY + _SYNC
                 + "getattr(builtins, 'exec')('ready = _sync')\n"})
_case_handler_rebound_through_getattr_alias = _rebound_ready(
    "handler_rebound_through_getattr_alias", "dynamically",
    {"hooks.py": "import builtins\n" + _READY + _SYNC
                 + "g = getattr\ng(builtins, 'ex' + 'ec')('ready = _sync')\n"})
_case_handler_rebound_through_attrgetter = _rebound_ready(
    "handler_rebound_through_attrgetter", "dynamically",
    {"hooks.py": "import builtins, operator\n" + _READY + _SYNC
                 + "operator.attrgetter('ex' + 'ec')(builtins)('ready = _sync')\n"})
_case_handler_rebound_through_attrgetter_value = _rebound_ready(
    "handler_rebound_through_attrgetter_value", "dynamically",
    {"hooks.py": "import builtins, operator\n" + _READY + _SYNC
                 + "ag = operator.attrgetter\nag('ex' + 'ec')(builtins)('ready = _sync')\n"})
_case_handler_rebound_through_getattribute = _rebound_ready(
    "handler_rebound_through_getattribute", "dynamically",
    {"hooks.py": "import builtins\n" + _READY + _SYNC
                 + "object.__getattribute__(builtins, 'ex' + 'ec')('ready = _sync')\n"})
_case_handler_rebound_through_imported_exec_alias = _rebound_ready(
    "handler_rebound_through_imported_exec_alias", "dynamically",
    {"hooks.py": "from builtins import exec as run\n" + _READY + _SYNC
                 + "run('ready = _sync')\n"})
_case_handler_rebound_through_imported_setattr_alias = _rebound_ready(
    "handler_rebound_through_imported_setattr_alias", "dynamically",
    {"hooks.py": "import sys\nfrom builtins import setattr as put\n" + _READY + _SYNC
                 + "put(sys.modules[__name__], 'ready', _sync)\n"})
# The module namespace reached as a mapping through a function, a frame, or
# locals() at module scope, and a module class whose property shadows the def.
_case_handler_rebound_through_module_locals = _rebound_ready(
    "handler_rebound_through_module_locals", "dynamically",
    {"hooks.py": _READY + _SYNC + "locals()['ready'] = _sync\n"})
_case_handler_rebound_through_function_globals = _rebound_ready(
    "handler_rebound_through_function_globals", "dynamically",
    {"hooks.py": _READY + _SYNC + "_sync.__globals__['ready'] = _sync\n"})
_case_handler_rebound_through_frame_globals = _rebound_ready(
    "handler_rebound_through_frame_globals", "dynamically",
    {"hooks.py": "import sys\n" + _READY + _SYNC + "sys._getframe().f_globals['ready'] = _sync\n"})
_case_handler_shadowed_by_module_class = _rebound_ready(
    "handler_shadowed_by_module_class", "dynamically",
    {"hooks.py": "import sys, types\n" + _READY + _SYNC
                 + "class _M(types.ModuleType):\n    locals()['re' + 'ady'] = property(lambda self: _sync)\n"
                 + "sys.modules[__name__].__class__ = _M\n"})
_case_handler_rebound_through_gc_referrers = _rebound_ready(
    "handler_rebound_through_gc_referrers", "dynamically",
    {"hooks.py": "import gc\n" + _READY + _SYNC
                 + "[d for d in gc.get_referrers(ready) if isinstance(d, dict)][0]['ready'] = _sync\n"})

# locals() in code Python evaluates at definition time, in the enclosing scope:
# a def's decorators, defaults and annotations, a lambda's defaults, a class's
# decorators, bases and keywords, a comprehension inlined into module scope.
# Only a def's or lambda's body is the function's own frame.
_REBIND = "locals().__setitem__('ready', _sync)"


def _rebound_at_definition(name: str, code: str):
    return _rebound_ready(name, "dynamically", {"hooks.py": _READY + _SYNC + code})


_case_handler_rebound_through_def_default = _rebound_at_definition(
    "handler_rebound_through_def_default", f"def helper(x={_REBIND}):\n    pass\n")
_case_handler_rebound_through_kwonly_default = _rebound_at_definition(
    "handler_rebound_through_kwonly_default", f"def helper(*, x={_REBIND}):\n    pass\n")
_case_handler_rebound_through_def_decorator = _rebound_at_definition(
    "handler_rebound_through_def_decorator",
    f"@({_REBIND} or (lambda f: f))\ndef helper():\n    pass\n")
_case_handler_rebound_through_annotation = _rebound_at_definition(
    "handler_rebound_through_annotation", f"def helper(x: {_REBIND}):\n    pass\n")
_case_handler_rebound_through_return_annotation = _rebound_at_definition(
    "handler_rebound_through_return_annotation", f"def helper() -> {_REBIND}:\n    pass\n")
_case_handler_rebound_through_lambda_default = _rebound_at_definition(
    "handler_rebound_through_lambda_default", f"helper = lambda x={_REBIND}: x\n")
_case_handler_rebound_through_class_decorator = _rebound_at_definition(
    "handler_rebound_through_class_decorator",
    f"@({_REBIND} or (lambda c: c))\nclass Helper:\n    pass\n")
_case_handler_rebound_through_class_base = _rebound_at_definition(
    "handler_rebound_through_class_base", f"class Helper({_REBIND} or object):\n    pass\n")
_case_handler_rebound_through_class_keyword = _rebound_at_definition(
    "handler_rebound_through_class_keyword",
    f"class Helper(metaclass={_REBIND} or type):\n    pass\n")
_case_handler_rebound_through_method_default = _rebound_at_definition(
    "handler_rebound_through_method_default",
    f"class Helper:\n    def m(self, x={_REBIND}):\n        pass\n")
_case_handler_rebound_through_module_comprehension = _rebound_at_definition(
    "handler_rebound_through_module_comprehension", f"[{_REBIND} for _ in [0]]\n")
_case_handler_rebound_through_module_generator = _rebound_at_definition(
    "handler_rebound_through_module_generator",
    f"list(_ for _ in ({_REBIND} or [0]))\n")
_case_handler_rebound_through_bare_vars = _rebound_at_definition(
    "handler_rebound_through_bare_vars", "vars().__setitem__('ready', _sync)\n")
# The same builtin reached by another name, or handed out of a function body as
# a value: called from module code, it reads the module namespace.
_case_handler_rebound_through_builtins_locals = _rebound_at_definition(
    "handler_rebound_through_builtins_locals",
    "import builtins\nbuiltins.locals().__setitem__('ready', _sync)\n")
_case_handler_rebound_through_imported_locals_alias = _rebound_at_definition(
    "handler_rebound_through_imported_locals_alias",
    "from builtins import locals as here\nhere().__setitem__('ready', _sync)\n")
_case_handler_rebound_through_getattr_locals = _rebound_at_definition(
    "handler_rebound_through_getattr_locals",
    "import builtins\ngetattr(builtins, 'locals')().__setitem__('ready', _sync)\n")
_case_handler_rebound_through_locals_returned = _rebound_at_definition(
    "handler_rebound_through_locals_returned",
    "def grab():\n    return locals\ngrab()().__setitem__('ready', _sync)\n")
_DEFINITION_TIME_CASES = [
    _case_handler_rebound_through_def_default,
    _case_handler_rebound_through_kwonly_default,
    _case_handler_rebound_through_def_decorator,
    _case_handler_rebound_through_annotation,
    _case_handler_rebound_through_return_annotation,
    _case_handler_rebound_through_lambda_default,
    _case_handler_rebound_through_class_decorator,
    _case_handler_rebound_through_class_base,
    _case_handler_rebound_through_class_keyword,
    _case_handler_rebound_through_method_default,
    _case_handler_rebound_through_module_comprehension,
    _case_handler_rebound_through_module_generator,
    _case_handler_rebound_through_bare_vars,
    _case_handler_rebound_through_builtins_locals,
    _case_handler_rebound_through_imported_locals_alias,
    _case_handler_rebound_through_getattr_locals,
    _case_handler_rebound_through_locals_returned,
]


def _case_route_setup_rebound_by_match_capture(base, marker, monkeypatch):
    return _migrating_module(base, f"acme-{_uid()}", marker, api_routes="{inner}.api",
                             code={"api.py": "def setup_api_routes(app):\n    pass\n"
                                             "async def _real(app):\n    pass\n"
                                             "match _real:\n    case setup_api_routes:\n"
                                             "        pass\n"}
                             ), "top-level def"


def _manifest_changed_by_submodule(name: str, body: str):
    """A module whose __init__.py holds a valid PLUGIN_MANIFEST literal and
    imports a file of its own that reaches the manifest and changes it."""
    def case(base, marker, monkeypatch):
        pkg = _migrating_module(base, f"acme-{_uid()}", marker, code={"mut.py": body})
        inner = next(p.name for p in pkg.iterdir() if p.is_dir())
        init = pkg / "__init__.py"
        init.write_text(init.read_text() + f"from .{inner} import mut\n")
        return pkg, "dynamically"
    case.__name__ = f"_case_{name}"
    return case


_case_manifest_changed_by_submodule_import = _manifest_changed_by_submodule(
    "manifest_changed_by_submodule_import",
    "from .. import PLUGIN_MANIFEST as m\nm['version'] = '9.9.9'\n")
_case_manifest_changed_by_submodule_attribute = _manifest_changed_by_submodule(
    "manifest_changed_by_submodule_attribute",
    "import sys\nsys.modules[__package__.rpartition('.')[0]].PLUGIN_MANIFEST['version'] = '9.9.9'\n")


def _case_route_setup_rebound_through_globals(base, marker, monkeypatch):
    return _migrating_module(base, f"acme-{_uid()}", marker, api_routes="{inner}.api",
                             code={"api.py": "def setup_api_routes(app):\n    pass\n"
                                             "async def _a(app):\n    pass\n"
                                             "globals()['setup_api_routes'] = _a\n"}
                             ), "dynamically"


def _manifest_changed(name: str, appended: str, reason: str = "bound once"):
    """A module whose __init__.py holds a valid PLUGIN_MANIFEST literal and then
    changes it: Python runs the change, so admission must not read the literal
    alone."""
    def case(base, marker, monkeypatch):
        pkg = _migrating_module(base, f"acme-{_uid()}", marker)
        init = pkg / "__init__.py"
        init.write_text(init.read_text() + appended)
        return pkg, reason
    case.__name__ = f"_case_{name}"
    return case


_case_manifest_bound_twice = _manifest_changed(
    "manifest_bound_twice", "PLUGIN_MANIFEST = {'name': 'other', 'version': '1.0.0'}\n")
_case_manifest_item_set = _manifest_changed(
    "manifest_item_set", "PLUGIN_MANIFEST['depends_on'] = ['missing-module']\n")
_case_manifest_updated = _manifest_changed(
    "manifest_updated", "PLUGIN_MANIFEST.update(version='9.9.9')\n")
_case_manifest_augmented = _manifest_changed(
    "manifest_augmented", "PLUGIN_MANIFEST |= {'version': '9.9.9'}\n")
_case_manifest_annotated_rebind = _manifest_changed(
    "manifest_annotated_rebind", "PLUGIN_MANIFEST: dict = {}\n")
_case_manifest_deleted = _manifest_changed("manifest_deleted", "del PLUGIN_MANIFEST\n")
_case_manifest_set_through_module = _manifest_changed(
    "manifest_set_through_module",
    "import sys\nsetattr(sys.modules[__name__], 'PLUGIN_MANIFEST', {})\n", "dynamically")
_case_manifest_rebound_by_match_capture = _manifest_changed(
    "manifest_rebound_by_match_capture", "match {}:\n    case PLUGIN_MANIFEST:\n        pass\n")
_case_manifest_deleted_by_except_as = _manifest_changed(
    "manifest_deleted_by_except_as",
    "try:\n    raise ValueError\nexcept ValueError as PLUGIN_MANIFEST:\n    pass\n")


async def test_locals_in_a_function_body_is_admitted(_db_engine, _modules, tmp_path):
    """Control: locals() where a def or lambda body runs it, including in a nested def's
    default and a comprehension inside a function, reads only that function's
    own frame."""
    marker = tmp_path / "ran.txt"
    pkg = _migrating_module(_modules, f"acme-{_uid()}", marker, slots=_READY_SLOT, code={
        "hooks.py": _READY
                    + "def context(a, b=1):\n    return dict(locals())\n"
                    + "def outer():\n    def inner(x=locals()):\n        return x\n"
                    + "    return [locals() for _ in [0]], inner()\n"
                    + "pick = lambda key: locals()[key]\n"})

    admission, loaded = await _admit_and_migrate(_db_engine, _modules, {pkg.name})

    assert admission.refused == {}
    assert marker.exists()
    assert [m["name"] for m in loaded] == [pkg.name]


async def test_ordinary_attribute_writes_and_an_early_star_import_are_admitted(
        _db_engine, _modules, tmp_path):
    """Control: setattr on data objects, attribute writes of other names and a
    star import placed before the handler do not change what core calls."""
    marker = tmp_path / "ran.txt"
    pkg = _migrating_module(_modules, f"acme-{_uid()}", marker, slots=_READY_SLOT, code={
        "hooks.py": "from json import *\n" + _READY
                    + "def apply(row, changes):\n    for k, v in changes.items():\n"
                    "        setattr(row, k, v)\n    row.status = 'done'\n"})

    admission, loaded = await _admit_and_migrate(_db_engine, _modules, {pkg.name})

    assert admission.refused == {}
    assert marker.exists()
    assert [m["name"] for m in loaded] == [pkg.name]


@pytest.mark.parametrize("case", [
    _case_async_api_setup,
    _case_handler_rebound_through_globals,
    _case_handler_rebound_through_globals_alias,
    _case_handler_rebound_through_vars,
    _case_handler_rebound_through_setattr,
    _case_handler_rebound_through_computed_setattr,
    _case_handler_rebound_through_exec,
    _case_handler_rebound_from_package_init,
    _case_handler_rebound_by_star_import,
    _case_handler_deleted,
    _case_handler_module_replaced,
    _case_handler_module_replaced_by_update,
    _case_route_setup_rebound_through_globals,
    _case_handler_rebound_by_match_capture,
    _case_handler_rebound_by_match_star,
    _case_handler_rebound_by_match_mapping_rest,
    _case_handler_deleted_by_except_as,
    _case_handler_code_swapped,
    _case_handler_code_swapped_through_setattr,
    _case_handler_defaults_replaced,
    _case_handler_rebound_through_getattr_setattr,
    _case_handler_rebound_through_getattr_exec,
    _case_handler_rebound_through_getattr_constant,
    _case_handler_rebound_through_getattr_alias,
    _case_handler_rebound_through_attrgetter,
    _case_handler_rebound_through_attrgetter_value,
    _case_handler_rebound_through_getattribute,
    _case_handler_rebound_through_imported_exec_alias,
    _case_handler_rebound_through_imported_setattr_alias,
    _case_handler_rebound_through_module_locals,
    _case_handler_rebound_through_function_globals,
    _case_handler_rebound_through_frame_globals,
    _case_handler_shadowed_by_module_class,
    _case_handler_rebound_through_gc_referrers,
    *_DEFINITION_TIME_CASES,
    _case_route_setup_rebound_by_match_capture,
    _case_manifest_changed_by_submodule_import,
    _case_manifest_changed_by_submodule_attribute,
    _case_manifest_rebound_by_match_capture,
    _case_manifest_deleted_by_except_as,
    _case_manifest_bound_twice,
    _case_manifest_item_set,
    _case_manifest_updated,
    _case_manifest_augmented,
    _case_manifest_annotated_rebind,
    _case_manifest_deleted,
    _case_manifest_set_through_module,
    _case_async_ui_setup_imported,
    _case_hook_not_async,
    _case_render_async,
    _case_callable_missing,
    _case_search_result_key,
    _case_nav_order_text,
    _case_category_fields_not_list,
    _case_connector_not_text,
    _case_unknown_slot,
    _case_pricing_show_on_entry,
    _case_item_action_link_out,
    _case_lineage_guard_not_async,
    _case_lineage_guard_wrong_arity,
    _case_in_production_wrong_arity,
    _case_in_production_not_first_party,
    _case_projection_prefix_twice,
    _case_projection_prefixes_overlap,
    _case_projection_prefix_is_cores,
    _case_projection_prefix_covers_cores,
    _case_projection_prefix_inside_cores,
    _case_hook_decorated,
    _case_hook_undefined,
    _case_hook_call_bound,
    _case_lineage_guard_decorated,
    _case_api_setup_decorated,
    _case_name_mismatch,
    _case_min_version,
    _case_missing_table_prefix,
    _case_migrations_absolute_path,
    _case_migrations_symlink_escape,
    _case_route_outside_module,
    _case_route_source_missing,
    _case_protected_import_in_migration,
    _case_protected_import_in_init,
    _case_unlicensed_premium,
    _case_celerp_name_without_a_licence,
    _case_package_of_a_core_module("celerp_ai"),
    _case_package_of_a_core_module("celerp_backup"),
    _case_package_of_a_core_module("celerp_connectors"),
    _case_package_imported_from_another_module,
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


def _sharing_pair(base: Path, tmp_path: Path, shared: str):
    """Two migrating modules, the second sorting after the first, that both ship
    `shared`: the same inner package, or the same top-level source file."""
    uid, inner = _uid(), f"acme_shared_{_uid()}"
    pkgs = []
    for tag in ("a", "b"):
        marker = tmp_path / f"{tag}.txt"
        if shared == "package":
            pkg = _write_module(base, f"acme-{tag}{uid}", {
                "name": f"acme-{tag}{uid}", "version": "1.0.0",
                "migrations": f"{inner}.migrations", "table_prefix": f"acme{tag}{uid}_"}, {
                f"{inner}/__init__.py": "",
                f"{inner}/migrations/__init__.py": "",
                f"{inner}/migrations/m_001.py": _marker_line(marker) + "def upgrade():\n    pass\n"})
        else:
            pkg = _migrating_module(base, f"acme-{tag}{uid}", marker)
            (pkg / f"{inner}.py").write_text("")
        pkgs.append(pkg)
    return pkgs


@pytest.mark.parametrize("shared", ["package", "file"])
async def test_second_module_shipping_the_same_import_name_is_refused(
        shared, _db_engine, _modules, tmp_path):
    """Python holds one module per import name, so two modules answering to the
    same one would run each other's code. The first in name order keeps it; the
    second is refused before any of its migrations run."""
    first, second = _sharing_pair(_modules, tmp_path, shared)

    admission, loaded = await _admit_and_migrate(
        _db_engine, _modules, {first.name, second.name})

    assert [m["name"] for m in loaded] == [first.name]
    assert (tmp_path / "a.txt").exists()
    assert not (tmp_path / "b.txt").exists()
    assert first.name in admission.refused[second.name]
    assert "also ships" in loader.load_errors()[second.name]


async def test_first_party_module_fills_a_first_party_slot(
        _db_engine, _modules, tmp_path, monkeypatch):
    """Control for the refusal above: Celerp's own module fills the slot."""
    monkeypatch.setattr(loader, "is_first_party", lambda pkg_path: True)
    marker = tmp_path / "ran.txt"
    pkg, _ = _case_in_production_not_first_party(_modules, marker, monkeypatch)

    admission, loaded = await _admit_and_migrate(_db_engine, _modules, {pkg.name})

    assert admission.refused == {}
    assert marker.exists()
    assert [m["name"] for m in loaded] == [pkg.name]


@pytest.mark.parametrize("first_prefix, second_prefix", [
    ("acme.", "acme."), ("acme.", "acme.order."), ("acme.order.", "acme."),
])
async def test_second_module_with_an_overlapping_projection_prefix_is_refused(
        first_prefix, second_prefix, _db_engine, _modules, tmp_path):
    """Each event type has one projection handler. Of two modules whose
    prefixes overlap, the first in name order keeps its prefix; the second is
    refused before any of its migrations run."""
    uid = _uid()
    first = _projecting_module(_modules, tmp_path / "a.txt", first_prefix, folder=f"acme-a{uid}")
    second = _projecting_module(_modules, tmp_path / "b.txt", second_prefix, folder=f"acme-b{uid}")

    admission, loaded = await _admit_and_migrate(
        _db_engine, _modules, {first.name, second.name})

    assert [m["name"] for m in loaded] == [first.name]
    assert (tmp_path / "a.txt").exists()
    assert not (tmp_path / "b.txt").exists()
    assert first.name in admission.refused[second.name]
    assert "overlaps" in loader.load_errors()[second.name]


async def test_first_party_module_claims_its_projection_prefix_first(
        _db_engine, _modules, tmp_path, monkeypatch):
    """A first-party module keeps its prefix even when another module sorts
    before it."""
    uid = _uid()
    other = _projecting_module(_modules, tmp_path / "a.txt", "acme.", folder=f"acme-a{uid}")
    own = _projecting_module(_modules, tmp_path / "b.txt", "acme.", folder=f"acme-b{uid}")
    monkeypatch.setattr(loader, "is_first_party", lambda pkg_path: pkg_path.name == own.name)

    admission, loaded = await _admit_and_migrate(_db_engine, _modules, {other.name, own.name})

    assert [m["name"] for m in loaded] == [own.name]
    assert own.name in admission.refused[other.name]


def test_kernel_projection_prefixes_cover_the_core_folded_modules():
    """Core handles the system events and every prefix a core-folded module
    declares; admission claims all of them before any module."""
    default_modules = Path(loader.__file__).parents[2] / "default_modules"
    folded = [default_modules / name for name in loader.CORE_FOLDED]
    declared = {item["prefix"] for path in folded if path.is_dir()
                for item in (loader.read_manifest(path).get("slots") or {}).get(
                    "projection_handler", [])}
    assert "mp." in declared
    assert slots.KERNEL_PROJECTION_PREFIXES == {"sys."} | declared


def _official_zip(name: str, files: dict[str, str] | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(f"{name}/__init__.py",
                    f"PLUGIN_MANIFEST = {{'name': {name!r}, 'version': '1.0.0'}}\n")
        for rel, body in (files or {}).items():
            zf.writestr(f"{name}/{rel}", body)
    return buf.getvalue()


def test_marketplace_install_keeps_reserved_prefix(_modules, tmp_path, monkeypatch):
    """The reserved prefix is the importer's rule, not a blanket ban: a module the
    Marketplace installed as official keeps its celerp- name."""
    _relay_identity(monkeypatch, tmp_path / "data", _FREE, activated=False)
    name = f"celerp-{_uid()}"
    install_from_zip(_official_zip(name), official=True, source="marketplace")

    assert [a.name for a in loader.admit_modules(str(_modules), {name}).admitted] == [name]


def test_celerp_module_copied_in_by_hand_loads(_modules, tmp_path, monkeypatch):
    _relay_identity(monkeypatch, tmp_path / "data", _FREE, activated=False)
    name = f"celerp-{_uid()}"
    inner = name.replace("-", "_")
    _write_module(_modules, name, {"name": name, "version": "1.0.0", "api_routes": f"{inner}.api"},
                  {f"{inner}/__init__.py": "", f"{inner}/api.py": "def setup_api_routes(app):\n    pass\n"})

    admission = loader.admit_modules(str(_modules), {name})

    assert admission.refused == {}
    assert [(a.name, a.first_party) for a in admission.admitted] == [(name, False)]
    loader.load_all(str(_modules), {name}, admission=admission)
    assert loader.is_running(name), loader.load_errors()


@pytest.mark.parametrize("case", [_case_protected_import_in_init, _case_in_production_not_first_party],
                         ids=lambda c: c.__name__.removeprefix("_case_"))
def test_celerp_name_carries_no_first_party_allowance(case, _modules, tmp_path, monkeypatch):
    pkg, reason = case(_modules, tmp_path / "ran.txt", monkeypatch)
    name = f"celerp-{_uid()}"
    init = pkg / "__init__.py"
    init.write_text(init.read_text().replace(repr(pkg.name), repr(name)))
    pkg = pkg.rename(_modules / name)

    admission = loader.admit_modules(str(_modules), {name})

    assert admission.admitted == []
    assert reason in admission.refused[name]


def test_celerp_module_copied_in_by_hand_cannot_take_a_default_modules_package(_modules):
    name = f"celerp-{_uid()}"
    _write_module(_modules, name, {"name": name, "version": "1.0.0"},
                  {"celerp_inventory/__init__.py": ""})
    module_dir = f"{_modules},{loader.BUNDLED_SOURCE_DIR}"

    admission = loader.admit_modules(module_dir, {name, "celerp-inventory"})

    assert [a.name for a in admission.admitted] == ["celerp-inventory"]
    assert _NOT_ITS_PACKAGE.format("celerp_inventory") in admission.refused[name]


_NOT_ITS_PACKAGE = "The package name '{}' belongs to the celerp- module of that name"


def test_celerp_module_copied_in_by_hand_cannot_take_a_marketplace_modules_package(_modules, tmp_path, monkeypatch):
    _relay_identity(monkeypatch, tmp_path / "data", _FREE, activated=False)
    u = _uid()
    real, copy, package = f"celerp-zzz{u}", f"celerp-aaa{u}", f"celerp_zzz{u}"
    install_from_zip(_official_zip(real, {f"{package}/__init__.py": ""}),
                     official=True, source="marketplace")
    _write_module(_modules, copy, {"name": copy, "version": "1.0.0"},
                  {f"{package}/__init__.py": ""})

    admission = loader.admit_modules(str(_modules), {real, copy})

    assert [a.name for a in admission.admitted] == [real]
    assert _NOT_ITS_PACKAGE.format(package) in admission.refused[copy]


def test_celerp_module_cannot_take_the_package_of_a_module_not_installed(_modules):
    name, package = f"celerp-aaa{_uid()}", f"celerp_other{_uid()}"
    _write_module(_modules, name, {"name": name, "version": "1.0.0"},
                  {f"{package}/__init__.py": ""})

    admission = loader.admit_modules(str(_modules), {name})

    assert admission.admitted == []
    assert _NOT_ITS_PACKAGE.format(package) in admission.refused[name]


def test_celerp_module_cannot_take_the_package_of_another_spelling(_modules):
    # Marketplace names use '-' only, so celerp_zz_q is the package of celerp-zz-q
    # and never of a copy named celerp-zz_q.
    u = _uid()
    copy, package = f"celerp-zz_q{u}", f"celerp_zz_q{u}"
    _write_module(_modules, copy, {"name": copy, "version": "1.0.0"},
                  {f"{package}/__init__.py": ""})

    admission = loader.admit_modules(str(_modules), {copy})

    assert admission.admitted == []
    assert _NOT_ITS_PACKAGE.format(package) in admission.refused[copy]


def test_marketplace_module_keeps_its_package_beside_another_spelling(_modules, tmp_path, monkeypatch):
    _relay_identity(monkeypatch, tmp_path / "data", _FREE, activated=False)
    u = _uid()
    real, copy, package = f"celerp-zz-q{u}", f"celerp-zz_q{u}", f"celerp_zz_q{u}"
    install_from_zip(_official_zip(real, {f"{package}/__init__.py": ""}),
                     official=True, source="marketplace")
    _write_module(_modules, copy, {"name": copy, "version": "1.0.0"},
                  {f"{package}/__init__.py": ""})

    admission = loader.admit_modules(str(_modules), {real, copy})

    assert [a.name for a in admission.admitted] == [real]


@pytest.mark.parametrize("shipped", ["{}.pyc", "{}.abi3.so", "{}/__init__.pyc"],
                         ids=["compiled_file", "extension", "compiled_package"])
def test_celerp_package_without_source_is_checked_like_a_source_one(shipped, _modules):
    name, package = f"celerp-aaa{_uid()}", f"celerp_zzz{_uid()}"
    _write_module(_modules, name, {"name": name, "version": "1.0.0"}, {})
    target = _modules / name / shipped.format(package)
    target.parent.mkdir(exist_ok=True)
    target.write_bytes(b"")

    assert package in loader._import_roots(name, _modules / name)
    admission = loader.admit_modules(str(_modules), {name})
    assert admission.admitted == []
    assert _NOT_ITS_PACKAGE.format(package) in admission.refused[name]


# ── A1 at load: a refused module's own code never runs ───────────────────────


def _init_marker_module(base: Path, folder: str, marker: Path, manifest: dict,
                        files: dict[str, str] | None = None) -> Path:
    return _write_module(base, folder, manifest, files, init_prelude=_marker_line(marker))


@pytest.mark.parametrize("variant", ["name_mismatch", "route_outside",
                                     "protected_import_in_slot_module"])
def test_load_all_refuses_before_import(variant, _modules, tmp_path):
    marker = tmp_path / "imported.txt"
    folder = f"acme-{_uid()}"
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


_ALWAYS_EQUAL_STR = (
    "class _Same(str):\n"
    "    def __eq__(self, other):\n"
    "        return True\n"
    "    __hash__ = str.__hash__\n")


@pytest.mark.parametrize("rewrite", [
    "PLUGIN_MANIFEST['api_routes'] = 'celerp.routers.health'\n"
    "PLUGIN_MANIFEST['slots'] = {'nav': {'key': 'x', 'label': 'X', 'href': '/x'}}\n",
    _ALWAYS_EQUAL_STR
    + "PLUGIN_MANIFEST['api_routes'] = _Same('celerp.routers.health')\n"
    + "PLUGIN_MANIFEST['version'] = _Same('9.9.9')\n",
], ids=["extra-route-and-slot", "always-equal-str"])
def test_module_rewriting_its_manifest_loads_with_the_admitted_one(_modules, rewrite, files_unchecked):
    """Admission reads the literal; a module that rewrites PLUGIN_MANIFEST while
    it imports still loads, with only what was admitted."""
    inner = f"acme_{_uid()}"
    folder = f"acme-{_uid()}"
    pkg = _write_module(
        _modules, folder, {"name": folder, "version": "1.0.0"},
        {f"{inner}/__init__.py": ""})
    admission = loader.admit_modules(str(_modules), {folder})
    assert admission.refused == {}
    init = pkg / "__init__.py"
    init.write_text(init.read_text() + rewrite)

    loaded = loader.load_all(str(_modules), {folder}, admission=admission)

    assert folder not in loader.load_errors()
    assert [m["name"] for m in loaded] == [folder]
    manifest = loaded[0]
    assert manifest.get("api_routes") is None
    assert type(manifest["version"]) is str and manifest["version"] == "1.0.0"
    assert manifest["slots"] == {}
    assert not any(e.get("_module") == folder for e in slots.get("nav"))


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
    function: the source no longer shows one plain def, so admission refuses it."""
    marker = tmp_path / "borrowed_setup_ran.txt"
    other, other_inner = _route_module(_modules, f"acme-{_uid()}", kind="ui", body=(
        "def setup_ui_routes(app):\n    pass\n\n"
        "def setup_api_routes(app):\n    " + _marker_line(marker)))
    folder = f"acme-{_uid()}"
    _route_module(_modules, folder, depends_on=[other.name], body=(
        "def setup_api_routes(app):\n    pass\n\n"
        f"from {other_inner}.routes import setup_api_routes  # noqa: E402,F811\n"))

    loaded = loader.load_all(str(_modules), {folder, other.name})
    loader.register_api_routes(_App(), loaded)

    assert not marker.exists()
    assert not loader.is_running(folder)
    assert loader.is_running(other.name)
    assert "api_routes setup" in loader.load_errors()[folder]
    assert "top-level def" in loader.load_errors()[folder]


def test_setup_rebound_after_admission_is_refused_at_registration(_modules, tmp_path, files_unchecked):
    """Registration proves where the setup it calls comes from, not what admission
    read: a route file changed after admission to rebind its setup to another
    module's function never runs that function as this module's."""
    marker = tmp_path / "borrowed_setup_ran.txt"
    other, other_inner = _route_module(_modules, f"acme-{_uid()}", kind="ui", body=(
        "def setup_ui_routes(app):\n    pass\n\n"
        "def setup_api_routes(app):\n    " + _marker_line(marker)))
    folder = f"acme-{_uid()}"
    pkg, inner = _route_module(_modules, folder, depends_on=[other.name],
                               body="def setup_api_routes(app):\n    pass\n")
    admission = loader.admit_modules(str(_modules), {folder, other.name})
    assert admission.refused == {}
    routes = pkg / inner / "routes.py"
    routes.write_text(routes.read_text()
                      + f"\nfrom {other_inner}.routes import setup_api_routes  # noqa: E402,F811\n")

    loaded = loader.load_all(str(_modules), {folder, other.name}, admission=admission)
    loader.register_api_routes(_App(), loaded)

    assert not marker.exists(), "another module's setup ran as this module's"
    assert not loader.is_running(folder)
    assert loader.is_running(other.name)
    assert "outside module" in loader.load_errors()[folder]


def test_owned_route_module_registers(_modules):
    folder = f"acme-{_uid()}"
    _pkg, inner = _route_module(_modules, folder)
    app = _App()

    loaded = loader.load_all(str(_modules), {folder})
    loader.register_api_routes(app, loaded)

    assert loader.is_running(folder)
    assert f"/{inner}/ping" in _paths(app)


def test_protected_internal_imported_as_a_submodule_name_is_refused(_modules, tmp_path):
    """`from celerp.ai import quota` reaches the protected celerp.ai namespace as
    surely as `import celerp.ai.quota` does."""
    marker = tmp_path / "setup_ran.txt"
    folder = f"acme-{_uid()}"
    _route_module(_modules, folder, body=(
        "from celerp.ai import quota  # noqa: F401\n\n"
        "def setup_api_routes(app):\n    " + _marker_line(marker)))

    loader.register_api_routes(_App(), loader.load_all(str(_modules), {folder}))

    assert not marker.exists()
    assert not loader.is_running(folder)
    assert "celerp.ai" in loader.load_errors()[folder]


_GETATTR_RAISES = "class G:\n    def __getattr__(self, n):\n        raise RuntimeError('no context')\n"
_CORE_SERVICES = ("celerp.services.company_files", "celerp.services.company_backup",
                  "celerp.services.company_backup_files", "celerp.services.company_reset",
                  "celerp.services.migrations", "celerp.routers.company_backup")

# What a third-party module's own code does as it activates, and whether it loads
# (True) or the text its refusal names.
_AI = "protected BSL internals (celerp.ai)"
_CREDENTIALS = "protected BSL internals (celerp.credentials)"
_ISSUERS = ("issue_token_pair", "create_access_token", "create_refresh_token")
_ACTIVATIONS = {
    "direct-import": ("import celerp.ai.llm  # noqa: F401\n", {}, _AI),
    "computed-import": ("import importlib\nimportlib.import_module('celerp.' + 'ai.llm')\n", {}, _AI),
    "computed-builtin-import": ("__import__('celerp.' + 'ai.llm')\n", {}, _AI),
    "computed-importlib-import": ("import importlib\nimportlib.__import__('celerp.' + 'ai.llm')\n", {}, _AI),
    "computed-import-in-own-submodule": (
        "from .reach import VALUE  # noqa: F401\n",
        {"reach.py": "import importlib\nllm = importlib.import_module('celerp.' + 'ai.llm')\nVALUE = 1\n"}, _AI),
    "computed-import-in-a-class-body": (
        "import importlib\nclass C:\n    llm = importlib.import_module('celerp.' + 'ai.llm')\n", {}, _AI),
    "computed-import-in-a-default": (
        "import importlib\ndef f(llm=importlib.import_module('celerp.' + 'ai.llm')):\n    return llm\n", {}, _AI),
    "computed-import-on-a-thread": (
        "import importlib, threading\nt = threading.Thread(target=lambda: importlib.import_module('celerp.' + 'ai.llm'))\n"
        "t.start()\nt.join()\n", {}, _AI),
    "computed-import-in-a-thread-pool": (
        "import importlib\nfrom concurrent.futures import ThreadPoolExecutor\nwith ThreadPoolExecutor(1) as pool:\n"
        "    try:\n        pool.submit(importlib.import_module, 'celerp.' + 'ai.llm').result()\n"
        "    except ImportError:\n        pass\n", {}, _AI),
    "computed-import-through-asyncio-to-thread": (
        "import asyncio, importlib\ntry:\n    asyncio.run(asyncio.to_thread(importlib.import_module, 'celerp.' + 'ai.llm'))\n"
        "except ImportError:\n    pass\n", {}, _AI),
    **{f"credential-issuer-{name}": (f"from celerp.credentials import {name}  # noqa: F401\n", {}, _CREDENTIALS)
       for name in _ISSUERS},
    "own-submodule": ("from .helper import VALUE  # noqa: F401\n", {"helper.py": "VALUE = 1\n"}, True),
    "unusual-import-arguments": (
        "__import__('os', 5)\nclass F:\n    def __iter__(self):\n        raise RuntimeError('no names')\n"
        "__import__('json.decoder', fromlist=F())\n", {}, True),
    "core-services": ("".join(f"import {s}  # noqa: F401\n" for s in _CORE_SERVICES), {}, True),
    "core-service-called": (
        "import asyncio\nfrom celerp.modules.api import ai_query\n"
        "try:\n    asyncio.run(ai_query('q', 'c'))\nexcept Exception:\n    pass\n", {}, True),
    "sys-modules-values": ("import sys\nHOLD = list(sys.modules.values())\n", {}, True),
    "sys-modules-copy": ("import sys\nHOLD = dict(sys.modules)\n", {}, True),
    "sys-modules-filtered": (
        "import sys\nHOLD = [m for n, m in sys.modules.items() if n.startswith('celerp.' + 'ai')]\n", {}, True),
    "sys-modules-lookup": ("import sys\nX = sys.modules.get('celerp.' + 'ai.llm')\n", {}, True),
    "raising-object": (_GETATTR_RAISES + "X = G()\n", {}, True),
    "raising-object-in-a-list": (_GETATTR_RAISES + "HOLD = [G()]\n", {}, True),
}

# The process states modules load in: the API and UI processes as they start, a
# process that imported a protected internal first, and one that imported nothing.
_VERDICT = """
import faulthandler, json, sys, types
faulthandler.dump_traceback_later(60, exit=True)
process = sys.argv[2]
if process == "preloaded":
    import celerp.ai.llm  # noqa: F401
elif process == "api":
    import celerp.main  # noqa: F401
elif process == "ui":
    src = open("ui/app.py").read().split("# Register UI routes from the loaded modules.")[0]
    app = types.ModuleType("ui.app")
    app.__file__ = "ui/app.py"
    sys.modules["ui.app"] = app
    import ui  # noqa: F401
    exec(compile(src, "ui/app.py", "exec"), app.__dict__)
from pathlib import Path
from celerp.modules import loader
preloaded = bool([n for n in sys.modules if n.startswith("celerp.ai")])
ui_routes = "celerp_ai.ui_routes" in sys.modules
folders = set(json.loads(sys.argv[3]))
loaded = {m["name"] for m in loader.load_all(sys.argv[1], folders)}
errors = loader.load_errors()
defaults = {p.name for p in Path("default_modules").iterdir() if (p / "__init__.py").exists()}
loader.load_all("default_modules", defaults)
print(json.dumps({"preloaded": preloaded, "ui_routes": ui_routes, "loads": {f: f in loaded for f in folders}, "errors": errors,
                  "default_errors": loader.load_errors()}))
"""
_PROCESSES = ("preloaded", "fresh", "api", "ui")


@pytest.mark.process
@pytest.mark.timeout(120)  # four interpreters each load every default module; slower than the suite guard allows on a shared runner
def test_module_gets_the_same_verdict_in_every_process(_modules, tmp_path):
    """A protected import the module's own code attempts as it activates refuses it;
    what core imports on its own behalf, and what is already loaded, do not count.
    The API process has imported protected internals before modules load, the UI
    process others, so each state must give every module the same verdict."""
    import os
    import subprocess

    folders = {}
    for case, (prelude, files, _) in _ACTIVATIONS.items():
        folder = f"acme-{case}-{_uid()}"
        _write_module(_modules, folder, {"name": folder, "version": "1.0.0", "slots": {}, "depends_on": []},
                      files=files, init_prelude=prelude)
        folders[folder] = case
    repo = Path(__file__).resolve().parents[2]
    # The licensed defaults ask the Marketplace; a relay address that refuses
    # at once keeps every verdict independent of the network. With no module
    # enabled, the UI process sets up its own routes without waiting for an API.
    config = tmp_path / "config.toml"
    config.write_text("[modules]\nenabled = []\n")
    env = {**os.environ, "MODULE_DIR": str(_modules), "GATEWAY_HTTP_URL": "http://127.0.0.1:9", "GATEWAY_TOKEN": "",
           "CELERP_CONFIG": str(config), "ENABLED_MODULES": ""}
    logs = {p: (tmp_path / f"{p}.out", tmp_path / f"{p}.err") for p in _PROCESSES}
    runs = {}
    for p, (out, err) in logs.items():
        with open(out, "w") as out_file, open(err, "w") as err_file:
            runs[p] = subprocess.Popen([sys.executable, "-c", _VERDICT, str(_modules), p, json.dumps(list(folders))],
                                       cwd=repo, env=env, stdin=subprocess.DEVNULL, stdout=out_file, stderr=err_file)
    results = {}
    for process, run in runs.items():
        run.wait()
        out, err = (path.read_text() for path in logs[process])
        assert run.returncode == 0, (process, err[-4000:])
        results[process] = json.loads(out.strip().splitlines()[-1])

    assert results["preloaded"]["preloaded"] and results["api"]["preloaded"]
    assert not results["fresh"]["preloaded"]
    assert results["ui"]["ui_routes"]
    expected = {case: verdict is True for case, (_, _, verdict) in _ACTIVATIONS.items()}
    for process, result in results.items():
        assert {folders[f]: v for f, v in result["loads"].items()} == expected, (process, result["errors"])
        assert result["default_errors"] == {}, process
    for folder, case in folders.items():
        if not expected[case]:
            assert _ACTIVATIONS[case][2] in results["fresh"]["errors"][folder], case


def test_core_import_on_another_thread_is_not_charged_to_an_activating_module(_modules):
    """While a module activates, core code on another thread imports a protected
    internal; that import is not the module's, so the module still loads."""
    import importlib
    import threading

    started, release = threading.Event(), threading.Event()
    folder = f"acme-{_uid()}"
    _write_module(_modules, folder, {"name": folder, "version": "1.0.0", "slots": {}, "depends_on": []},
                  init_prelude="import builtins\nbuiltins._acme_started.set()\nbuiltins._acme_release.wait(10)\n")
    import builtins
    builtins._acme_started, builtins._acme_release = started, release
    result = {}
    worker = threading.Thread(target=lambda: result.update(loaded=loader.load_all(str(_modules), {folder})))
    try:
        worker.start()
        assert started.wait(10)
        importlib.import_module("celerp.ai.llm")
        release.set()
        worker.join(10)
    finally:
        release.set()
        del builtins._acme_started, builtins._acme_release

    assert folder in [m["name"] for m in result["loaded"]], loader.load_errors()


def test_core_executor_job_during_an_activation_is_not_charged_to_the_module(_modules):
    """While a module's activation is held, a core thread-pool job imports a
    protected internal; the module did not start that work, so it still loads."""
    import importlib
    import threading
    from concurrent.futures import ThreadPoolExecutor

    started, release = threading.Event(), threading.Event()
    folder = f"acme-{_uid()}"
    _write_module(_modules, folder, {"name": folder, "version": "1.0.0", "slots": {}, "depends_on": []},
                  init_prelude="import builtins\nbuiltins._acme_started.set()\nbuiltins._acme_release.wait(10)\n")
    import builtins
    builtins._acme_started, builtins._acme_release = started, release
    result = {}
    worker = threading.Thread(target=lambda: result.update(loaded=loader.load_all(str(_modules), {folder})))
    try:
        worker.start()
        assert started.wait(10)
        with ThreadPoolExecutor(1) as pool:
            pool.submit(importlib.import_module, "celerp.ai.llm").result(10)
        release.set()
        worker.join(10)
    finally:
        release.set()
        del builtins._acme_started, builtins._acme_release

    assert folder in [m["name"] for m in result["loaded"]], loader.load_errors()


@pytest.mark.parametrize("other_state", ["disabled", "refused"])
def test_protected_import_in_another_installed_modules_file_refuses_the_module(_modules, other_state):
    """A file in any installed third-party module's folder is module code, whether
    that module is enabled or not: a protected import it makes while a module
    activates and runs it refuses the activating module."""
    other = f"acme-other-{_uid()}"
    _write_module(_modules, other, {"name": other, "version": "1.0.0", "slots": {}, "depends_on": []},
                  files={"reach.py": "import importlib\nimportlib.import_module('celerp.' + 'ai.llm')\n"},
                  init_prelude="import celerp.ai.llm  # noqa: F401\n" if other_state == "refused" else "")
    folder = f"acme-{_uid()}"
    _write_module(_modules, folder, {"name": folder, "version": "1.0.0", "slots": {}, "depends_on": []},
                  init_prelude=f"import os, runpy\nrunpy.run_path(os.path.join(os.path.dirname(os.path.dirname(__file__)), "
                               f"{other!r}, 'reach.py'))\n")

    loader.load_all(str(_modules), {folder, other} if other_state == "refused" else {folder})

    assert not loader.is_running(folder)
    assert not loader.is_running(other)
    assert _AI in loader.load_errors()[folder]


def test_module_with_a_linked_file_is_refused(_modules, tmp_path):
    """A file in the module's folder that links to a file kept elsewhere cannot be
    checked as the module's own, so the module is refused before it runs."""
    folder = f"acme-{_uid()}"
    pkg = _write_module(_modules, folder, {"name": folder, "version": "1.0.0", "slots": {}, "depends_on": []},
                        init_prelude="from . import linked  # noqa: F401\n")
    (tmp_path / "linked.py").write_text("import importlib\nimportlib.import_module('celerp.' + 'ai.llm')\n")
    (pkg / "linked.py").symlink_to(tmp_path / "linked.py")

    loader.load_all(str(_modules), {folder})

    assert not loader.is_running(folder)
    assert loader.load_errors()[folder] == "Cannot check the module's files."


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


# ── A4: the UI process offers only what the API process is running ──────────


def _offered_module(base: Path, folder: str, *, fail_migration: bool = False,
                    depends_on=None) -> tuple[Path, str]:
    """A module with a migration, UI routes, a nav entry and a bulk action."""
    inner = f"acme_{_uid()}"
    manifest = {
        "name": folder, "version": "1.0.0", "table_prefix": f"acme{_uid()}_",
        "migrations": f"{inner}.migrations", "ui_routes": f"{inner}.routes",
        "slots": {"nav": {"label": folder, "href": f"/{inner}/home"},
                  "bulk_action": {"label": "Act", "form_action": f"/{inner}/act"}},
    }
    if depends_on:
        manifest["depends_on"] = depends_on
    upgrade = "    raise RuntimeError('migration boom')\n" if fail_migration else "    pass\n"
    files = {
        f"{inner}/__init__.py": "",
        f"{inner}/migrations/__init__.py": "",
        f"{inner}/migrations/m_001.py": "def upgrade():\n" + upgrade,
        f"{inner}/routes.py": (
            "from starlette.responses import PlainTextResponse\n\n"
            "def setup_ui_routes(app):\n"
            f"    app.router.add_route('/{inner}/home', lambda r: PlainTextResponse('ok'))\n"),
    }
    return _write_module(base, folder, manifest, files), inner


def _engine_url(engine) -> str:
    return engine.url.render_as_string(hide_password=False)


async def _api_boot(engine, base: Path, enabled: set[str]) -> None:
    """What the API process does at startup: admit, migrate, load, record."""
    from celerp.modules import outcome

    await _admit_and_migrate(engine, base, enabled)
    async with engine.begin() as conn:
        await conn.run_sync(outcome.publish)


def _ui_boot(base: Path, enabled: set[str], database_url: str, monkeypatch,
             api_token: str | None = None):
    """What the UI process does at import, in a fresh loader state: read the API's
    record and load only what it reports as running."""
    from celerp.modules import outcome

    slots.clear()
    loader._loaded.clear()
    loader._load_errors.clear()
    monkeypatch.setattr(outcome, "_health_token",
                        lambda url: api_token or outcome.BOOT_TOKEN)
    admission = outcome.admission_as_reported(
        str(base), enabled, outcome.reported_by_api("http://api.invalid", database_url))
    app = _App()
    loader.register_ui_routes(app, loader.load_all(str(base), enabled, admission=admission))
    return app


def _contributes_nothing(name: str, inner: str, app) -> bool:
    return (not loader.is_running(name)
            and f"/{inner}/home" not in _paths(app)
            and all(e["_module"] != name for entries in slots.all_slots().values()
                    for e in entries))


async def test_api_migration_failure_keeps_the_module_out_of_the_ui(
        committed_engine, _modules, monkeypatch):
    failing, failing_inner = _offered_module(_modules, f"acme-{_uid()}", fail_migration=True)
    dependent, dependent_inner = _offered_module(
        _modules, f"acme-{_uid()}", depends_on=[failing.name])
    healthy, healthy_inner = _offered_module(_modules, f"acme-{_uid()}")
    enabled = {failing.name, dependent.name, healthy.name}
    await _api_boot(committed_engine, _modules, enabled)

    app = _ui_boot(_modules, enabled, _engine_url(committed_engine), monkeypatch)

    assert _contributes_nothing(failing.name, failing_inner, app)
    assert _contributes_nothing(dependent.name, dependent_inner, app)
    assert "migration boom" in loader.load_errors()[failing.name]
    assert loader.load_errors()[dependent.name] == (
        f"Requires {failing.name!r}, which failed to load.")
    # Control: the module the API is running is offered.
    assert loader.is_running(healthy.name)
    assert f"/{healthy_inner}/home" in _paths(app)
    assert any(e["_module"] == healthy.name for e in slots.get("bulk_action"))


async def test_modules_page_shows_the_api_reason(
        committed_engine, _modules, monkeypatch):
    from ui import api_client

    failing, _ = _offered_module(_modules, f"acme-{_uid()}", fail_migration=True)
    await _api_boot(committed_engine, _modules, {failing.name})
    _ui_boot(_modules, {failing.name}, _engine_url(committed_engine), monkeypatch)
    # A listing that has the module as running: the reason this process holds wins.
    api_rows = [{"name": failing.name, "enabled": True, "running": True, "load_error": None}]

    @asynccontextmanager
    async def _fake_client(token, timeout=10.0):
        class _C:
            async def get(self, url):
                return httpx.Response(200, json=api_rows,
                                      request=httpx.Request("GET", "http://api" + url))
        yield _C()

    monkeypatch.setattr(api_client, "_api_client", _fake_client)
    row = (await api_client.get_modules("tok"))[0]

    assert row["running"] is False
    assert "migration boom" in row["load_error"]


@pytest.mark.parametrize("record", ["missing", "other_process"])
async def test_ui_offers_nothing_without_this_apis_record(
        record, committed_engine, _modules, monkeypatch):
    """Fail closed: a record from another API process (or none) offers nothing."""
    from celerp.modules import outcome

    healthy, inner = _offered_module(_modules, f"acme-{_uid()}")
    if record == "other_process":
        await _api_boot(committed_engine, _modules, {healthy.name})

    app = _ui_boot(_modules, {healthy.name}, _engine_url(committed_engine), monkeypatch,
                   api_token="a-different-api-process")

    assert _contributes_nothing(healthy.name, inner, app)
    assert loader.load_errors()[healthy.name] == outcome.NOT_REPORTED


def test_ui_waits_for_the_api_to_finish_starting(monkeypatch):
    from celerp.modules import outcome

    answers = iter([None, None, "token-1"])
    monkeypatch.setattr(outcome, "_health_token", lambda url: next(answers))
    monkeypatch.setattr(outcome, "_POLL_SECONDS", 0)

    assert outcome.wait_for_api_token("http://api.invalid") == "token-1"


def test_api_health_serves_its_boot_token():
    from fastapi.testclient import TestClient

    from celerp.main import app
    from celerp.modules import outcome

    assert TestClient(app).get("/health").json()["boot"] == outcome.BOOT_TOKEN


# ── A5: a module that is not running creates no tables ──────────────────────


_MODELS = (
    "from sqlalchemy import Column, ForeignKey, Integer\n"
    "from celerp.models.base import Base\n\n"
    "class Thing(Base):\n"
    "    __tablename__ = '{inner}_things'\n"
    "    id = Column(Integer, primary_key=True)\n")


async def _created_tables(engine) -> set[str]:
    import sqlalchemy as sa

    from celerp.models.base import Base

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        return set(await conn.run_sync(lambda c: sa.inspect(c).get_table_names()))


async def test_route_failure_creates_none_of_the_module_tables(committed_engine, _modules):
    failing_folder, dependent_folder = f"acme-{_uid()}", f"acme-{_uid()}"
    failing, failing_inner = _route_module(
        _modules, failing_folder,
        body="from {inner} import models\n" + _FAILING_SETUP.replace("{kind}", "api"),
        extra_files={"{inner}/models.py": _MODELS})
    routes = failing / failing_inner / "routes.py"
    routes.write_text(routes.read_text().replace("{inner}", failing_inner))
    dependent, dependent_inner = _route_module(
        _modules, dependent_folder, depends_on=[failing_folder],
        extra_files={"{inner}/models.py": _MODELS})
    (dependent / dependent_inner / "__init__.py").write_text("from . import models\n")

    loaded = loader.load_all(str(_modules), {failing_folder, dependent_folder})
    loader.register_api_routes(_App(), loaded)
    tables = await _created_tables(committed_engine)

    assert not loader.is_running(failing_folder)
    assert f"{failing_inner}_things" not in tables
    assert f"{dependent_inner}_things" not in tables
    assert "companies" in tables  # core tables are still created


async def test_refused_module_creates_none_of_its_tables(committed_engine, _modules):
    """Refused at load, after its code ran (a slot names a handler it lacks)."""
    folder = f"acme-{_uid()}"
    inner = f"acme_{_uid()}"
    _write_module(_modules, folder,
                  {"name": folder, "version": "1.0.0",
                   "slots": {"on_modules_ready": {"handler": f"{inner}.hooks:missing"}}},
                  {f"{inner}/__init__.py": "",
                   f"{inner}/hooks.py": "x = 1\n",
                   f"{inner}/models.py": _MODELS.replace("{inner}", inner)},
                  init_prelude=f"import {inner}.models")

    loader.load_all(str(_modules), {folder})
    tables = await _created_tables(committed_engine)

    assert folder in loader.load_errors()
    assert f"{inner}_things" not in tables


async def test_admission_refused_module_creates_none_of_its_tables(committed_engine, _modules):
    """Control: refused before any of its code runs, so its models never load."""
    folder = f"acme-{_uid()}"
    inner = f"acme_{_uid()}"
    _write_module(_modules, folder, {"name": f"acme-other-{_uid()}", "version": "1.0.0"},
                  {f"{inner}/__init__.py": "",
                   f"{inner}/models.py": _MODELS.replace("{inner}", inner)},
                  init_prelude=f"import {inner}.models")

    loader.load_all(str(_modules), {folder})
    tables = await _created_tables(committed_engine)

    assert folder in loader.load_errors()
    assert f"{inner}_things" not in tables


async def test_table_referencing_a_stopped_module_is_not_created(committed_engine, _modules):
    """A running module's table with a foreign key into a stopped module's table
    cannot be created, and must not stop the others being created."""
    failing_folder, other_folder = f"acme-{_uid()}", f"acme-{_uid()}"
    failing, failing_inner = _route_module(
        _modules, failing_folder,
        body="from {inner} import models\n" + _FAILING_SETUP.replace("{kind}", "api"),
        extra_files={"{inner}/models.py": _MODELS})
    routes = failing / failing_inner / "routes.py"
    routes.write_text(routes.read_text().replace("{inner}", failing_inner))
    other, other_inner = _route_module(
        _modules, other_folder,
        extra_files={"{inner}/models.py": _MODELS + (
            "\nclass Link(Base):\n"
            "    __tablename__ = '{inner}_links'\n"
            "    id = Column(Integer, primary_key=True)\n"
            f"    thing_id = Column(Integer, ForeignKey('{failing_inner}_things.id'))\n")})
    (other / other_inner / "__init__.py").write_text("from . import models\n")
    manifest = loader.read_manifest(other)
    manifest["table_prefix"] = f"{other_inner}_"
    (other / "__init__.py").write_text(f"PLUGIN_MANIFEST = {manifest!r}\n")

    loaded = loader.load_all(str(_modules), {failing_folder, other_folder})
    loader.register_api_routes(_App(), loaded)
    tables = await _created_tables(committed_engine)

    assert loader.is_running(other_folder)
    assert f"{failing_inner}_things" not in tables
    assert f"{other_inner}_links" not in tables
    assert f"{other_inner}_things" in tables


def test_stopped_module_keeps_its_table_prefix_reserved(_modules):
    """Table creation skips a module that is not running; prefix reservation does not."""
    from celerp.modules.importer import installed_table_prefixes

    folder = f"acme-{_uid()}"
    prefix = f"acme{_uid()}_"
    _route_module(_modules, folder, body=_FAILING_SETUP.replace("{kind}", "api").replace(
        "{inner}", "x"))
    pkg = _modules / folder
    manifest = loader.read_manifest(pkg)
    manifest["table_prefix"] = prefix
    (pkg / "__init__.py").write_text(f"PLUGIN_MANIFEST = {manifest!r}\n")

    loader.register_api_routes(_App(), loader.load_all(str(_modules), {folder}))

    assert not loader.is_running(folder)
    assert installed_table_prefixes(exclude="")[folder] == prefix


def _ui_process(token: str, database_url: str, module_dir: Path, enabled: str,
                data_dir: Path, *, start: bool = True) -> dict:
    """Import ui.app in a separate process and, with *start*, run its startup, as
    the UI process does, against an API whose /health serves ``token``. Returns
    the routes it offers and its load errors."""
    import os
    import subprocess
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class _Health(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps({"status": "ok", "boot": token}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    server = HTTPServer(("127.0.0.1", 0), _Health)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    repo = Path(__file__).resolve().parents[2]
    env = {**os.environ, "MODULE_DIR": str(module_dir), "ENABLED_MODULES": enabled,
           "API_URL": f"http://127.0.0.1:{server.server_port}",
           "DATABASE_URL": database_url, "CELERP_DATA_DIR": str(data_dir)}
    env.pop("CELERP_API_URL", None)
    try:
        out = subprocess.run(
            [sys.executable, "-c",
             "import json, ui.app as a\n"
             "from celerp.modules.loader import load_errors\n"
             + ("from starlette.testclient import TestClient\n"
                "with TestClient(a.app):\n    pass\n" if start else "")
             +
             "print(json.dumps({'paths': [getattr(r, 'path', '') for r in a.app.routes],"
             " 'errors': load_errors()}))"],
            cwd=repo, env=env, capture_output=True, text=True, timeout=120)
    finally:
        server.shutdown()
    assert out.returncode == 0, out.stderr[-2000:]
    return json.loads(out.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("dashboard_running", [True, False])
async def test_ui_process_offers_core_pages_only_for_running_modules(
        dashboard_running, committed_engine, tmp_path):
    """The UI process at import: a core page gated on a bundled module is offered
    only when the API process reports that module running."""
    from celerp.migrations._data_reconcile import set_meta

    token = uuid.uuid4().hex
    record = {"boot": token, "running": ["celerp-dashboard"] if dashboard_running else [],
              "failed": {} if dashboard_running else {"celerp-dashboard": "dashboard boom"}}
    async with committed_engine.begin() as conn:
        await conn.run_sync(lambda c: set_meta(c, "module_outcome", json.dumps(record)))

    repo = Path(__file__).resolve().parents[2]
    result = _ui_process(token, _engine_url(committed_engine), repo / "default_modules",
                         "celerp-dashboard", tmp_path)

    assert ("/dashboard" in result["paths"]) is dashboard_running
    if not dashboard_running:
        assert result["errors"]["celerp-dashboard"] == "dashboard boom"


# ── A6: a module that fails in the UI process stops in the API process ──────


def _two_sided_module(base: Path, folder: str, *, fail_ui: bool = False,
                      depends_on=None) -> tuple[Path, str]:
    """A module with API routes, UI routes and a nav entry; ``fail_ui`` makes its
    UI route setup raise."""
    inner = f"acme_{_uid()}"
    manifest = {"name": folder, "version": "1.0.0",
                "api_routes": f"{inner}.api", "ui_routes": f"{inner}.ui",
                "slots": {"nav": {"label": folder, "href": f"/{inner}/home"}}}
    if depends_on:
        manifest["depends_on"] = depends_on
    ui_body = ("    raise RuntimeError('ui setup exploded')\n" if fail_ui else
               f"    app.router.add_route('/{inner}/home', lambda r: PlainTextResponse('ok'))\n")
    files = {
        f"{inner}/__init__.py": "",
        f"{inner}/api.py": (
            "from starlette.responses import PlainTextResponse\n\n"
            "def setup_api_routes(app):\n"
            f"    app.router.add_route('/{inner}/api', lambda r: PlainTextResponse('ok'))\n"),
        f"{inner}/ui.py": (
            "from starlette.responses import PlainTextResponse\n\n"
            "def setup_ui_routes(app):\n" + ui_body),
    }
    return _write_module(base, folder, manifest, files), inner


async def test_ui_route_failure_stops_the_module_in_the_api_process(
        committed_engine, _modules, tmp_path, monkeypatch):
    """Two processes: this one is the API, the UI is a real ui.app import. No module
    route is served until the UI has reported; a module whose UI routes failed
    there is stopped here, with its dependents, before any module route answers."""
    from celerp.modules import outcome

    monkeypatch.setattr(outcome, "_awaiting_ui", False)
    failing, failing_inner = _two_sided_module(_modules, f"acme-{_uid()}", fail_ui=True)
    dependent, dependent_inner = _two_sided_module(
        _modules, f"acme-{_uid()}", depends_on=[failing.name])
    healthy, healthy_inner = _two_sided_module(_modules, f"acme-{_uid()}")
    enabled = {failing.name, dependent.name, healthy.name}
    api = _App()
    loader.register_api_routes(api, loader.load_all(str(_modules), enabled))
    async with committed_engine.begin() as conn:
        await conn.run_sync(outcome.publish)
    outcome.await_ui_report()
    assert all(loader.is_running(n) for n in enabled)
    assert await outcome.confirm_ui_report(api, committed_engine) is False

    ui = _ui_process(outcome.BOOT_TOKEN, _engine_url(committed_engine), _modules,
                     ",".join(sorted(enabled)), tmp_path)
    assert "ui setup exploded" in ui["errors"][failing.name]

    assert await outcome.confirm_ui_report(api, committed_engine) is True
    assert outcome.ui_report_applied()

    for name, inner in ((failing.name, failing_inner), (dependent.name, dependent_inner)):
        assert not loader.is_running(name)
        assert f"/{inner}/api" not in _paths(api)
        assert all(e["_module"] != name for entries in slots.all_slots().values()
                   for e in entries)
    assert "ui setup exploded" in loader.load_errors()[failing.name]
    assert loader.load_errors()[dependent.name] == (
        f"Requires {failing.name!r}, which failed to load.")
    async with committed_engine.connect() as conn:
        record = await conn.run_sync(outcome.read)
    assert record["boot"] == outcome.BOOT_TOKEN
    assert record["running"] == [healthy.name]
    assert "ui setup exploded" in record["failed"][failing.name]
    # Control: the module that works in both processes keeps running in both.
    assert loader.is_running(healthy.name)
    assert f"/{healthy_inner}/api" in _paths(api)
    assert f"/{healthy_inner}/home" in ui["paths"]


async def test_importing_the_ui_writes_no_module_outcome(committed_engine, _modules, tmp_path):
    """The UI records a module it could not start only once it runs, never while
    its code is being imported."""
    from celerp.modules import outcome

    failing, _inner = _two_sided_module(_modules, f"acme-{_uid()}", fail_ui=True)
    api = _App()
    loader.register_api_routes(api, loader.load_all(str(_modules), {failing.name}))
    async with committed_engine.begin() as conn:
        await conn.run_sync(outcome.publish)

    ui = _ui_process(outcome.BOOT_TOKEN, _engine_url(committed_engine), _modules,
                     failing.name, tmp_path, start=False)
    assert "ui setup exploded" in ui["errors"][failing.name]
    async with committed_engine.connect() as conn:
        record = await conn.run_sync(outcome.read)
    assert record["running"] == [failing.name]
    assert failing.name not in record["failed"]


async def test_ui_outcome_is_not_written_once_a_newer_version_opened_the_database(
        committed_engine, _modules, monkeypatch):
    """The UI lost its hold on the database and a newer Celerp opened it since:
    the UI's report is not written."""
    import sqlalchemy as sa

    from celerp.db_url import sync_url
    from celerp.migrations import compatibility
    from celerp.migrations._data_reconcile import set_meta
    from celerp.modules import outcome

    healthy, _inner = _two_sided_module(_modules, f"acme-{_uid()}")
    api = _App()
    loader.register_api_routes(api, loader.load_all(str(_modules), {healthy.name}))
    async with committed_engine.begin() as conn:
        await conn.run_sync(outcome.publish)
        record = await conn.run_sync(outcome.read)
    loader._loaded.clear()  # this "UI" could not start it

    url = sync_url(_engine_url(committed_engine))
    fence = compatibility.Fence.join(url)
    other = sa.create_engine(url, poolclass=sa.pool.NullPool)
    try:
        with other.begin() as conn:
            conn.execute(sa.text("SELECT pg_terminate_backend(:p)"), {"p": fence._session[0]})
            set_meta(conn, compatibility.NEWEST_CELERP_KEY, "9999.0.0")

        def _ended(exc):
            raise SystemExit(str(exc))
        monkeypatch.setattr(compatibility, "_end_process", _ended)
        with pytest.raises(SystemExit):
            outcome.report_stopped(fence, record)
    finally:
        fence.release()
        other.dispose()
    async with committed_engine.connect() as conn:
        after = await conn.run_sync(outcome.read)
    assert after["running"] == [healthy.name]
    assert healthy.name not in after["failed"]


async def test_report_from_an_earlier_api_process_stops_nothing(committed_engine, _modules):
    """A UI that read another API process's record cannot stop this one's modules."""
    from celerp.modules import outcome

    healthy, inner = _two_sided_module(_modules, f"acme-{_uid()}")
    api = _App()
    loader.register_api_routes(api, loader.load_all(str(_modules), {healthy.name}))
    async with committed_engine.begin() as conn:
        await conn.run_sync(outcome.publish)
    stale = {"boot": "an-earlier-api-process", "running": [healthy.name], "failed": {}}
    loader._loaded.clear()  # this "UI" is not running it

    from celerp.db_url import sync_url
    from celerp.migrations.compatibility import Fence
    fence = Fence.join(sync_url(_engine_url(committed_engine)))
    try:
        assert outcome.report_stopped(fence, stale) == {}
    finally:
        fence.release()
    async with committed_engine.connect() as conn:
        assert (await conn.run_sync(outcome.read))["running"] == [healthy.name]


async def test_api_serves_no_module_route_until_the_ui_has_reported(
        committed_engine, _modules, monkeypatch):
    """Between the API's start and the UI's report a module route answers 503 with
    a plain message; the rest of the API is served throughout."""
    import celerp.db
    from starlette.applications import Starlette
    from starlette.responses import PlainTextResponse

    from celerp.db_url import sync_url
    from celerp.middleware import ModuleStartupMiddleware
    from celerp.migrations.compatibility import Fence
    from celerp.modules import outcome

    monkeypatch.setattr(outcome, "_awaiting_ui", False)
    monkeypatch.setattr(celerp.db, "lifecycle_engine", committed_engine)
    healthy, inner = _two_sided_module(_modules, f"acme-{_uid()}")
    app = Starlette(routes=[])
    app.router.add_route("/kernel", lambda r: PlainTextResponse("kernel"))
    app.add_api_route = app.router.add_route
    loader.register_api_routes(app, loader.load_all(str(_modules), {healthy.name}))
    app.add_middleware(ModuleStartupMiddleware)
    async with committed_engine.begin() as conn:
        await conn.run_sync(outcome.publish)
        record = await conn.run_sync(outcome.read)
    outcome.await_ui_report()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://api") as client:
        early = await client.get(f"/{inner}/api")
        assert early.status_code == 503
        assert early.json() == {"detail": outcome.STARTING}
        assert (await client.get("/kernel")).status_code == 200

        fence = Fence.join(sync_url(_engine_url(committed_engine)))
        try:
            assert outcome.report_stopped(fence, record) == {}
        finally:
            fence.release()
        assert (await client.get(f"/{inner}/api")).status_code == 200
    assert outcome.ui_report_applied()


# ── A7: a celerp- module that is not a default is licence-checked by name ────


def _premium_zip(name: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(f"{name}/__init__.py",
                    f"PLUGIN_MANIFEST = {{'name': {name!r}, 'version': '1.0.0'}}\n")
    return buf.getvalue()


_PAID = {"is_official": True, "price_monthly": 15}
_FREE = {"is_official": True, "price_monthly": None, "price_once": None}


def test_paid_module_without_its_marker_needs_a_licence(_modules, tmp_path, monkeypatch):
    calls = _relay_identity(monkeypatch, tmp_path / "data", _PAID)
    name = f"celerp-{_uid()}"
    install_from_zip(_premium_zip(name), official=True, premium=True, source="marketplace")
    (_modules / name / PREMIUM_MARKER).unlink()

    admission = loader.admit_modules(_modules, {name})

    assert admission.admitted == []
    assert "no valid license" in admission.refused[name]
    assert calls["licence"] == [name]


def test_premium_tree_module_copied_to_the_module_dir_needs_a_licence(
        _modules, tmp_path, monkeypatch):
    import shutil

    _relay_identity(monkeypatch, tmp_path / "data", _PAID)
    name = f"celerp-{_uid()}"
    premium = tmp_path / "premium_modules"
    _write_module(premium, name, {"name": name, "version": "1.0.0"})
    shutil.copytree(premium / name, _modules / name)

    admission = loader.admit_modules(_modules, {name})

    assert admission.admitted == []
    assert "no valid license" in admission.refused[name]


def test_celerp_module_with_no_verdict_is_refused_while_the_marketplace_is_unreachable(
        _modules, tmp_path, monkeypatch):
    calls = _relay_identity(monkeypatch, tmp_path / "data")
    name = f"celerp-{_uid()}"
    _write_module(_modules, name, {"name": name, "version": "1.0.0"})

    admission = loader.admit_modules(_modules, {name})

    assert admission.admitted == []
    assert admission.refused[name] == UNVERIFIED_MODULE_REFUSAL
    assert calls["detail"] == [f"https://relay.invalid/marketplace/modules/{name}"]


def test_unofficial_free_listing_still_needs_a_licence(_modules, tmp_path, monkeypatch):
    _relay_identity(monkeypatch, tmp_path / "data", {"is_official": False, "price_monthly": 0})
    name = f"celerp-{_uid()}"
    _write_module(_modules, name, {"name": name, "version": "1.0.0"})

    admission = loader.admit_modules(_modules, {name})

    assert "no valid license" in admission.refused[name]


def test_free_official_module_loads_from_its_cached_verdict(_modules, tmp_path, monkeypatch):
    data = tmp_path / "data"
    calls = _relay_identity(monkeypatch, data, _FREE)
    name = f"celerp-{_uid()}"
    _write_module(_modules, name, {"name": name, "version": "1.0.0"})

    assert [m.name for m in loader.admit_modules(_modules, {name}).admitted] == [name]
    assert calls["licence"] == []
    assert (data / "license_cache" / f"{name}.free.json").is_file()

    # Going offline later changes nothing: the verdict is kept.
    calls = _relay_identity(monkeypatch, data)
    assert [m.name for m in loader.admit_modules(_modules, {name}).admitted] == [name]
    assert calls == {"licence": [], "detail": []}


def test_premium_module_requires_the_normal_license_path(_modules, tmp_path, monkeypatch):
    """A premium module requires the normal license path, whatever its listing."""
    calls = _relay_identity(monkeypatch, tmp_path / "data", _FREE)
    name = f"celerp-{_uid()}"
    premium = tmp_path / "premium_modules"
    _write_module(premium, name, {"name": name, "version": "1.0.0"})

    admission = loader.admit_modules(premium, {name})

    assert "no valid license" in admission.refused[name]
    assert calls["licence"] == [name]


@pytest.mark.parametrize("copy", ["premium_tree", "paid_listing"])
def test_never_activated_install_refuses_a_copied_paid_module(
        copy, _modules, tmp_path, monkeypatch):
    import shutil

    _relay_identity(monkeypatch, tmp_path / "data", _PAID, activated=False)
    if copy == "premium_tree":
        name = f"acme-{_uid()}"
        premium = tmp_path / "premium_modules"
        _write_module(premium, name, {"name": name, "version": "1.0.0"})
        shutil.copytree(premium / name, _modules / name)
        (_modules / name / PREMIUM_MARKER).write_text("")
    else:
        name = f"celerp-{_uid()}"
        _write_module(_modules, name, {"name": name, "version": "1.0.0"})

    admission = loader.admit_modules(_modules, {name})

    assert admission.admitted == []
    assert "no valid license" in admission.refused[name]


def test_never_activated_install_loads_a_free_official_module(
        _modules, tmp_path, monkeypatch):
    data = tmp_path / "data"
    calls = _relay_identity(monkeypatch, data, _FREE, activated=False)
    name = f"celerp-{_uid()}"
    _write_module(_modules, name, {"name": name, "version": "1.0.0"})

    assert [m.name for m in loader.admit_modules(_modules, {name}).admitted] == [name]
    assert calls["detail"] == [f"https://relay.invalid/marketplace/modules/{name}"]
    assert (data / "license_cache" / f"{name}.free.json").is_file()


def test_never_activated_install_refuses_with_no_verdict_while_the_marketplace_is_unreachable(
        _modules, tmp_path, monkeypatch):
    calls = _relay_identity(monkeypatch, tmp_path / "data", activated=False)
    name = f"celerp-{_uid()}"
    _write_module(_modules, name, {"name": name, "version": "1.0.0"})

    admission = loader.admit_modules(_modules, {name})

    assert admission.admitted == []
    assert admission.refused[name] == UNVERIFIED_MODULE_REFUSAL
    assert calls["detail"] == [f"https://relay.invalid/marketplace/modules/{name}"]


def test_licence_cache_lives_in_the_celerp_data_dir(_modules, tmp_path, monkeypatch):
    """The desktop app sets only CELERP_DATA_DIR; every licence cache must live
    there, so a free official module still loads after a restart offline."""
    data = tmp_path / "celerp-data"
    calls = _relay_identity(monkeypatch, data, _FREE)
    monkeypatch.setenv("CELERP_DATA_DIR", str(data))
    seen = {}
    monkeypatch.setattr(loader, "check_license",
                        lambda **kw: seen.setdefault("cache_dir", kw["cache_dir"]) and False)
    free, paid = f"celerp-{_uid()}", f"celerp-{_uid()}"
    _write_module(_modules, free, {"name": free, "version": "1.0.0"})
    _write_module(_modules, paid, {"name": paid, "version": "1.0.0"})
    (_modules / paid / PREMIUM_MARKER).write_text("")

    admission = loader.admit_modules(_modules, {free, paid})
    assert [m.name for m in admission.admitted] == [free]
    assert (data / "license_cache" / f"{free}.free.json").is_file()
    assert Path(seen["cache_dir"]) == data

    # A restart offline.
    calls = _relay_identity(monkeypatch, data)
    monkeypatch.setenv("CELERP_DATA_DIR", str(data))
    assert [m.name for m in loader.admit_modules(_modules, {free}).admitted] == [free]
    assert calls["detail"] == []


def _marketplace_install(base: Path, name: str) -> Path:
    """A free official module as the Marketplace installed it before verdicts
    were recorded: the folder and its install metadata, no verdict anywhere."""
    pkg = _write_module(base, name, {"name": name, "version": "1.0.0"})
    (pkg / ".celerp-meta.json").write_text(json.dumps(
        {"source": "marketplace", "installed_at": "2026-09-01T00:00:00+00:00"}))
    return pkg


# Module metadata a module folder may carry.
_METADATA = {
    "marketplace": {"source": "marketplace", "installed_at": "2026-09-01T00:00:00+00:00"},
    "marketplace_extra_field": {"source": "marketplace", "installed_at": "2026-09-01T00:00:00+00:00",
                                "paid": False},
    "marketplace_listing_fields": {"source": "marketplace", "paid": False, "free": True,
                                   "is_official": True, "is_paid": False},
    "community": {"source": "community"},
    "sideloaded": {"source": "sideloaded"},
    "none": None,
}


@pytest.mark.parametrize("metadata", list(_METADATA))
@pytest.mark.parametrize("activated", [True, False], ids=["activated", "never_activated"])
def test_module_metadata_does_not_affect_admission(
        activated, metadata, _modules, tmp_path, monkeypatch):
    """Module metadata does not affect admission: with no free verdict kept, no
    licence and the Marketplace unreachable, a celerp- module is refused, and no
    verdict is written."""
    data = tmp_path / "data"
    name = f"celerp-{_uid()}"
    pkg = _write_module(_modules, name, {"name": name, "version": "1.0.0"})
    if _METADATA[metadata] is not None:
        (pkg / ".celerp-meta.json").write_text(json.dumps(_METADATA[metadata]))
    calls = _relay_identity(monkeypatch, data, activated=activated)

    admission = loader.admit_modules(_modules, {name})

    assert admission.admitted == []
    assert admission.refused[name] == UNVERIFIED_MODULE_REFUSAL
    assert calls["licence"] == ([name] if activated else [])
    assert not (data / "license_cache" / f"{name}.free.json").exists()


def test_marketplace_install_from_before_verdicts_needs_the_marketplace_once(
        _modules, tmp_path, monkeypatch):
    """A free module the Marketplace installed before verdicts were kept loads on
    the first start that reaches the Marketplace, and offline from then on."""
    data = tmp_path / "data"
    name = f"celerp-{_uid()}"
    _marketplace_install(_modules, name)

    _relay_identity(monkeypatch, data, _FREE, activated=False)
    assert [m.name for m in loader.admit_modules(_modules, {name}).admitted] == [name]

    calls = _relay_identity(monkeypatch, data, activated=False)
    assert [m.name for m in loader.admit_modules(_modules, {name}).admitted] == [name]
    assert calls["detail"] == []


@pytest.mark.parametrize("detail", [_PAID, {"is_official": False, "price_monthly": None}],
                         ids=["paid", "unofficial"])
def test_marketplace_install_takes_the_licence_check_once_the_marketplace_says_not_free(
        detail, _modules, tmp_path, monkeypatch):
    calls = _relay_identity(monkeypatch, tmp_path / "data", detail)
    name = f"celerp-{_uid()}"
    _marketplace_install(_modules, name)

    admission = loader.admit_modules(_modules, {name})

    assert admission.admitted == []
    assert "no valid license" in admission.refused[name]
    assert calls["licence"] == [name]


def test_marketplace_install_stays_on_the_licence_check_offline_once_the_marketplace_said_not_free(
        _modules, tmp_path, monkeypatch):
    data = tmp_path / "data"
    name = f"celerp-{_uid()}"
    _marketplace_install(_modules, name)
    _relay_identity(monkeypatch, data, _PAID, activated=False)
    assert "no valid license" in loader.admit_modules(_modules, {name}).refused[name]

    _relay_identity(monkeypatch, data, activated=False)
    assert "no valid license" in loader.admit_modules(_modules, {name}).refused[name]
    assert not (data / "license_cache" / f"{name}.free.json").exists()


@pytest.mark.parametrize("copy", ["marker_deleted", "copied_without_marker"])
def test_paid_marketplace_install_without_its_marker_is_refused_offline(
        copy, _modules, tmp_path, monkeypatch):
    import shutil

    _relay_identity(monkeypatch, tmp_path / "data", activated=False)
    name = f"celerp-{_uid()}"
    install_from_zip(_premium_zip(name), official=True, premium=True, source="marketplace")
    modules = _modules
    if copy == "marker_deleted":
        (_modules / name / PREMIUM_MARKER).unlink()
    else:
        modules = tmp_path / "other-install"
        shutil.copytree(_modules / name, modules / name,
                        ignore=shutil.ignore_patterns(PREMIUM_MARKER))

    admission = loader.admit_modules(modules, {name})

    assert admission.admitted == []
    assert admission.refused[name] == UNVERIFIED_MODULE_REFUSAL


def test_marketplace_install_unknown_to_the_marketplace_takes_the_licence_check(
        _modules, tmp_path, monkeypatch):
    import urllib.error

    calls = _relay_identity(monkeypatch, tmp_path / "data")

    def _not_found(url, timeout=None):
        raise urllib.error.HTTPError(str(url), 404, "Not Found", {}, None)

    monkeypatch.setattr("urllib.request.urlopen", _not_found)
    name = f"celerp-{_uid()}"
    _marketplace_install(_modules, name)

    assert "no valid license" in loader.admit_modules(_modules, {name}).refused[name]
    assert calls["licence"] == [name]


def test_unconfirmed_module_refusal_is_not_shown_as_a_licence_on_another_computer(
        _modules, tmp_path, monkeypatch):
    """The modules page keeps its move-your-licence prompt for paid modules; a
    module the Marketplace could not yet confirm as free gets a plain reason."""
    from fasthtml.common import to_xml

    from ui.routes.modules_page import _local_panel

    _relay_identity(monkeypatch, tmp_path / "data", activated=False)
    name = f"celerp-{_uid()}"
    _write_module(_modules, name, {"name": name, "version": "1.0.0"})
    refusal = loader.admit_modules(_modules, {name}).refused[name]

    row = {"name": name, "label": name, "version": "1.0.0", "author": "",
           "enabled": True, "running": False, "is_default": False, "load_error": refusal}
    html = to_xml(_local_panel([row], "en", owner=True))

    assert "module-license-upsell" not in html
    assert "bought it on" not in html


@pytest.mark.parametrize("activated", [True, False], ids=["activated", "never_activated"])
def test_unverified_module_is_shown_as_not_loaded_with_its_reason(
        activated, _modules, tmp_path, monkeypatch):
    """A celerp- module installed before verdicts were kept, started offline:
    Celerp keeps running, the module is not running, and the modules page shows
    the reason with the failed badge."""
    from fasthtml.common import to_xml

    from ui.routes.modules_page import _local_panel

    _relay_identity(monkeypatch, tmp_path / "data", activated=activated)
    name = f"celerp-{_uid()}"
    _marketplace_install(_modules, name)

    assert loader.load_all(_modules, {name}) == []

    assert not loader.is_running(name)
    assert loader.load_errors()[name] == UNVERIFIED_MODULE_REFUSAL
    row = {"name": name, "label": name, "version": "1.0.0", "author": "",
           "enabled": True, "running": loader.is_running(name), "is_default": False,
           "load_error": loader.load_errors().get(name)}
    html = to_xml(_local_panel([row], "en", owner=True))
    assert "Connect once to verify this module, then restart. It will work offline afterward." in html
    assert "badge--danger" in html
    assert "badge--active" not in html


def test_refusal_log_says_why_the_module_did_not_load(_modules, tmp_path, monkeypatch, caplog):
    _relay_identity(monkeypatch, tmp_path / "data", activated=False)
    unconfirmed, paid = f"celerp-{_uid()}", f"celerp-{_uid()}"
    _write_module(_modules, unconfirmed, {"name": unconfirmed, "version": "1.0.0"})
    pkg = _write_module(_modules, paid, {"name": paid, "version": "1.0.0"})
    (pkg / PREMIUM_MARKER).write_text("")

    with caplog.at_level("WARNING", logger="celerp.modules.loader"):
        loader.admit_modules(_modules, {unconfirmed, paid})

    lines = {r.args[0]: r.getMessage() for r in caplog.records
             if r.name == "celerp.modules.loader" and r.args}
    assert lines[paid].startswith("Premium module")
    assert not lines[unconfirmed].startswith("Premium module")
    assert "could not confirm" in lines[unconfirmed]


def _legacy_licence(legacy: Path, slug: str, entry: dict) -> None:
    (legacy / "license_cache").mkdir(parents=True, exist_ok=True)
    (legacy / "license_cache" / f"{slug}.json").write_text(json.dumps(entry))


@pytest.mark.parametrize("kind", ["lifetime", "grace"])
def test_paid_licence_kept_in_the_old_cache_dir_still_loads_offline(
        kind, _modules, tmp_path, monkeypatch):
    """Licences kept in the old default data dir are carried into the data dir,
    so a paid module still loads on the first offline start after the update."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from jose import jwt as jose_jwt

    from celerp.modules import license as lic

    key = ec.generate_private_key(ec.SECP256R1())
    priv = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption()).decode()
    monkeypatch.setattr(lic, "_LICENSE_PUBLIC_KEY", key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode())
    data, legacy = tmp_path / "data", tmp_path / "legacy"
    slug = f"celerp-{_uid()}"
    pkg = _write_module(_modules, slug, {"name": slug, "version": "1.0.0"})
    (pkg / PREMIUM_MARKER).write_text("")
    if kind == "lifetime":
        token = jose_jwt.encode({"kind": "lifetime", "mod": slug, "sub": "instance-1",
                                 "iss": "celerp-relay"}, priv, algorithm="ES256")
        entry = {"licensed": True, "status": "active", "cached_at": 0,
                 "license_kind": "lifetime", "license_jwt": token}
    else:
        entry = {"licensed": True, "status": "active", "cached_at": time.time(),
                 "license_kind": "subscription", "license_jwt": ""}
    _legacy_licence(legacy, slug, entry)
    _relay_identity(monkeypatch, data, activated=False)
    monkeypatch.setenv("DATA_DIR", str(legacy))

    admission = loader.admit_modules(_modules, {slug})

    assert [m.name for m in admission.admitted] == [slug], admission.refused
    assert json.loads((data / "license_cache" / f"{slug}.json").read_text()) == entry


def test_old_cache_dir_never_replaces_a_licence_already_in_the_data_dir(
        _modules, tmp_path, monkeypatch):
    data, legacy = tmp_path / "data", tmp_path / "legacy"
    slug = f"celerp-{_uid()}"
    pkg = _write_module(_modules, slug, {"name": slug, "version": "1.0.0"})
    (pkg / PREMIUM_MARKER).write_text("")
    _legacy_licence(legacy, slug, {"licensed": True, "status": "active",
                                   "cached_at": time.time()})
    current = {"licensed": False, "status": "cancelled", "cached_at": time.time()}
    _legacy_licence(data, slug, current)
    _relay_identity(monkeypatch, data, activated=False)
    monkeypatch.setenv("DATA_DIR", str(legacy))

    assert "no valid license" in loader.admit_modules(_modules, {slug}).refused[slug]
    assert json.loads((data / "license_cache" / f"{slug}.json").read_text()) == current


def test_old_cache_dir_carries_no_free_verdict(_modules, tmp_path, monkeypatch):
    data, legacy = tmp_path / "data", tmp_path / "legacy"
    slug = f"celerp-{_uid()}"
    _write_module(_modules, slug, {"name": slug, "version": "1.0.0"})
    _legacy_licence(legacy, f"{slug}.free", {"free": True})
    _relay_identity(monkeypatch, data, activated=False)
    monkeypatch.setenv("DATA_DIR", str(legacy))

    assert loader.admit_modules(_modules, {slug}).refused[slug] == UNVERIFIED_MODULE_REFUSAL
    assert not (data / "license_cache" / f"{slug}.free.json").exists()


def test_startup_fetches_missing_verdicts_for_installed_celerp_modules(
        _modules, tmp_path, monkeypatch):
    """Every installed celerp- module that is not a default and has no verdict
    gets one while online, enabled or not, so it loads later offline."""
    data = tmp_path / "data"
    calls = _relay_identity(monkeypatch, data, _FREE, activated=False)
    fresh, known, other = f"celerp-{_uid()}", f"celerp-{_uid()}", f"acme-{_uid()}"
    for name in (fresh, known, other):
        _write_module(_modules, name, {"name": name, "version": "1.0.0"})
    (data / "license_cache").mkdir(parents=True)
    (data / "license_cache" / f"{known}.free.json").write_text('{"free": true}')

    loader.fetch_missing_free_verdicts(str(_modules))

    assert calls["detail"] == [f"https://relay.invalid/marketplace/modules/{fresh}"]
    assert (data / "license_cache" / f"{fresh}.free.json").is_file()


def test_startup_fetch_leaves_defaults_and_the_premium_tree_alone(tmp_path, monkeypatch):
    calls = _relay_identity(monkeypatch, tmp_path / "data", _FREE, activated=False)
    premium = tmp_path / "premium_modules"
    name = f"celerp-{_uid()}"
    _write_module(premium, name, {"name": name, "version": "1.0.0"})
    default = sorted(loader.first_party_names())[0]
    _write_module(premium, default, {"name": default, "version": "1.0.0"})

    loader.fetch_missing_free_verdicts(str(premium))

    assert calls["detail"] == []


def test_default_names_are_not_licence_checked(tmp_path):
    """Control: the defaults Celerp ships load without a Marketplace round trip."""
    def _no_relay():
        raise AssertionError("a default module asked for relay credentials")

    for name in sorted(loader.first_party_names()):
        module = loader.AdmittedModule(name, tmp_path / name, {}, False, "")
        assert loader._license_refusal(module, _no_relay) is None


async def test_celerp_module_restored_from_a_backup_needs_a_licence(tmp_path, monkeypatch):
    import asyncio
    import tarfile

    from celerp import __version__
    from celerp.services import backup_import

    modules = tmp_path / "modules"
    monkeypatch.setenv("MODULE_DIR", str(modules))
    _relay_identity(monkeypatch, tmp_path, _PAID)
    name = f"celerp-{_uid()}"
    meta = json.dumps({"celerp_version": __version__, "pg_version": "16",
                       "created_at": "2026-10-06T00:00:00Z", "company_name": "T"}).encode()
    init = f"PLUGIN_MANIFEST = {{'name': {name!r}, 'version': '1.0.0'}}\n".encode()
    archive = tmp_path / "backup.celerp-backup"
    with tarfile.open(archive, mode="w:gz") as tar:
        for member, body in [("database.dump", b"PGDMP"), ("meta.json", meta),
                             (f"modules/{name}/__init__.py", init)]:
            info = tarfile.TarInfo(member)
            info.size = len(body)
            tar.addfile(info, io.BytesIO(body))
    prepared = await backup_import.prepare_recovery(archive)
    try:
        await asyncio.to_thread(backup_import._swap_roots, prepared)
    finally:
        backup_import._remove_staging(prepared.root)
    assert (modules / name / "__init__.py").is_file()

    admission = loader.admit_modules(modules, {name})

    assert admission.admitted == []
    assert "no valid license" in admission.refused[name]


_HELD_PROTECTED_NAMES = """
import enum, importlib, inspect, pkgutil, sys, types
from pathlib import Path
from celerp.modules.loader import _PROTECTED_BSL_INTERNALS

def protected(name):
    return any(name == p or name.startswith(p + ".") for p in _PROTECTED_BSL_INTERNALS)

def fail(name):
    raise ImportError(name)

def sources(value, seen):
    # Where a module or function comes from, for the value itself or anything a
    # dict, list, tuple, set or frozenset holds, at any depth. Nothing else is
    # looked into.
    if isinstance(value, (dict, list, tuple, set, frozenset)):
        if id(value) in seen:
            return
        seen.add(id(value))
        items = [x for pair in dict.items(value) for x in pair] if isinstance(value, dict) else value
        for item in items:
            yield from sources(item, seen)
    elif isinstance(value, types.ModuleType):
        yield value.__name__
    elif inspect.isclass(value) and issubclass(value, enum.Enum):
        return
    elif callable(value):
        yield getattr(value, "__module__", None) or ""

def packages():
    if len(sys.argv) > 1:
        sys.path.insert(0, sys.argv[1])
        return [importlib.import_module(p.parent.name) for p in sorted(Path(sys.argv[1]).glob("*/__init__.py"))]
    import celerp, ui
    found = [celerp, ui]
    for folder in sorted(Path("default_modules").iterdir()):
        for inner in sorted(folder.glob("*/__init__.py")):
            if inner.parent.name == "tests":
                continue
            sys.path.insert(0, str(folder.resolve()))
            found.append(importlib.import_module(inner.parent.name))
    return found

held = []
for pkg in packages():
    for info in pkgutil.walk_packages(pkg.__path__, pkg.__name__ + ".", onerror=fail):
        if protected(info.name) or ".migrations.versions." in info.name:
            continue
        for name, value in vars(importlib.import_module(info.name)).items():
            if any(protected(source) for source in sources(value, set())):
                held.append(f"{info.name}.{name}")
print("\\n".join(held))
"""


def _held_protected_names(*folder: Path) -> list[str]:
    """The names holding a protected module or function, in the repo's packages or
    in the packages in *folder*."""
    import os
    import subprocess
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(p for p in sys.path if p)}
    out = subprocess.run([sys.executable, "-c", _HELD_PROTECTED_NAMES, *map(str, folder)], env=env,
                         cwd=Path(__file__).resolve().parents[2],
                         capture_output=True, text=True, timeout=300)
    assert out.returncode == 0, out.stderr[-3000:]
    return out.stdout.split()


def test_core_modules_import_protected_functions_where_they_are_used():
    """Protected functions are imported inside the code that uses them, so no core
    module and no bundled first-party module holds one, or a protected module,
    among its names, directly or in a container. Enum value classes are data, not
    functions, and are not counted."""
    assert _held_protected_names() == []


def test_protected_names_held_in_containers_are_found(tmp_path):
    pkg = tmp_path / f"held_{_uid()}"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "holder.py").write_text(
        "import celerp.ai.llm\n"
        "from celerp.ai.llm import __name__ as _text\n"
        "NESTED = {'a': [({1: celerp.ai.llm},)]}\n"
        "KEYED = {frozenset({celerp.ai.llm}): 1}\n"
        "LOOP = []\n"
        "LOOP.append(LOOP)\n"
        "LOOP.append({'inner': (LOOP, {celerp.ai.llm})})\n"
        "PLAIN = [_text, {'k': ('celerp.ai.llm',)}]\n"
        "del celerp\n")

    held = _held_protected_names(tmp_path)

    assert held == [f"{pkg.name}.holder.{n}" for n in ("NESTED", "KEYED", "LOOP")]



# ── A8: Python runs exactly the files admission checked ─────────────────────


@pytest.fixture
def _first_party(tmp_path, monkeypatch):
    """Lock the given module folders as first-party, at their current content."""
    lock_file = tmp_path / "fp.lock.json"
    monkeypatch.setattr(loader, "_lock_path", lambda: lock_file)

    def lock(*pkgs: Path) -> None:
        lock_file.write_text(json.dumps({p.name: loader.module_content_digest(p) for p in pkgs}))
        loader._first_party_lock.cache_clear()

    yield lock
    loader._first_party_lock.cache_clear()


def _bytecode_left(pkg: Path) -> list[str]:
    return sorted(str(p.relative_to(pkg)) for p in pkg.rglob("*")
                  if p.name == "__pycache__" or p.suffix == ".pyc")


def test_compiled_files_are_removed_before_the_module_runs(_modules, tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    marker = tmp_path / "ran.txt"
    folder = f"acme-{_uid()}"
    inner = f"acme_{_uid()}"
    pkg = _init_marker_module(_modules, folder, marker, {"name": folder, "version": "1.0.0"},
                              {f"{inner}/__init__.py": "", f"{inner}/sub/__init__.py": ""})
    (pkg / "__pycache__").mkdir()
    (pkg / "__pycache__" / "__init__.cpython-312.pyc").write_bytes(b"\x00")
    (pkg / inner / "sub" / "__pycache__").mkdir()
    (pkg / inner / "sub" / "__pycache__" / "x.cpython-312.pyc").write_bytes(b"\x00")
    (pkg / inner / "stale.pyc").write_bytes(b"\x00")
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "kept.pyc").write_bytes(b"\x00")
    (pkg / inner / "__pycache__").symlink_to(outside, target_is_directory=True)

    loaded = loader.load_all(str(_modules), {folder})

    assert [m["name"] for m in loaded] == [folder]
    assert marker.exists()
    assert _bytecode_left(pkg) == []
    assert not (pkg / inner / "__pycache__").is_symlink()
    assert (outside / "kept.pyc").is_file()


@pytest.fixture
def _stuck_bytecode():
    """Make a module's bytecode impossible to remove, and undo that afterwards."""
    stuck: list[Path] = []

    def stick(pkg: Path) -> None:
        cache = pkg / "__pycache__"
        cache.mkdir()
        (cache / "__init__.cpython-312.pyc").write_bytes(b"\x00")
        cache.chmod(0o500)
        stuck.append(cache)

    yield stick
    for cache in stuck:
        cache.chmod(0o700)


def test_loading_a_module_writes_no_compiled_files_beside_its_source(
        _modules, tmp_path, monkeypatch):
    """Two processes loading the same module folder never see each other's bytecode."""
    from celerp.config import settings
    monkeypatch.setattr(sys, "dont_write_bytecode", False)
    monkeypatch.setattr(sys, "pycache_prefix", None)
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    marker = tmp_path / "ran.txt"
    folder = f"acme-{_uid()}"
    inner = f"acme_{_uid()}"
    pkg = _init_marker_module(_modules, folder, marker, {"name": folder, "version": "1.0.0"},
                              {f"{inner}/__init__.py": "from . import sub\n",
                               f"{inner}/sub/__init__.py": ""})
    (pkg / "__init__.py").write_text(
        (pkg / "__init__.py").read_text() + f"import {inner}\n")

    loaded = loader.load_all(str(_modules), {folder})

    assert [m["name"] for m in loaded] == [folder]
    assert marker.exists()
    assert _bytecode_left(pkg) == []


def test_module_whose_compiled_files_cannot_be_removed_is_refused(
        _modules, tmp_path, _stuck_bytecode):
    marker = tmp_path / "ran.txt"
    stuck, other = f"acme-{_uid()}", f"acme-{_uid()}"
    pkg = _init_marker_module(_modules, stuck, marker, {"name": stuck, "version": "1.0.0"})
    _write_module(_modules, other, {"name": other, "version": "1.0.0"})
    _stuck_bytecode(pkg)

    loaded = loader.load_all(str(_modules), {stuck, other})

    assert [m["name"] for m in loaded] == [other]
    assert not marker.exists()
    assert "Cannot remove compiled Python files" in loader.load_errors()[stuck]


def test_default_module_whose_compiled_files_cannot_be_removed_stops_startup(
        _modules, tmp_path, _first_party, _stuck_bytecode):
    marker = tmp_path / "ran.txt"
    name = f"acme-{_uid()}"
    pkg = _init_marker_module(_modules, name, marker, {"name": name, "version": "1.0.0"})
    _first_party(pkg)
    _stuck_bytecode(pkg)

    with pytest.raises(loader.ModuleLoadError, match="Cannot remove compiled Python files"):
        loader.load_all(str(_modules), {name})
    assert not marker.exists()


def test_module_changed_after_admission_is_refused_before_it_runs(_modules, tmp_path):
    marker = tmp_path / "ran.txt"
    name = f"acme-{_uid()}"
    pkg = _write_module(_modules, name, {"name": name, "version": "1.0.0"})
    admission = loader.admit_modules(str(_modules), {name})
    assert [m.name for m in admission.admitted] == [name]
    (pkg / "__init__.py").write_text(_marker_line(marker) + (pkg / "__init__.py").read_text())

    loaded = loader.load_all(str(_modules), {name}, admission=admission)

    assert loaded == []
    assert not marker.exists()
    assert loader.load_errors()[name] == loader.MODULE_CHANGED


def test_default_module_changed_after_admission_stops_startup(_modules, tmp_path, _first_party):
    marker = tmp_path / "ran.txt"
    name = f"acme-{_uid()}"
    pkg = _write_module(_modules, name, {"name": name, "version": "1.0.0"})
    _first_party(pkg)
    admission = loader.admit_modules(str(_modules), {name})
    assert [m.first_party for m in admission.admitted] == [True]
    (pkg / "__init__.py").write_text(_marker_line(marker) + (pkg / "__init__.py").read_text())

    with pytest.raises(loader.ModuleLoadError, match="changed after it was checked"):
        loader.load_all(str(_modules), {name}, admission=admission)
    assert not marker.exists()
