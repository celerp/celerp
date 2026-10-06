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
import uuid
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest

from celerp.modules import loader, slots
from celerp.modules.importer import PREMIUM_MARKER, install_from_zip


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


def test_marketplace_install_keeps_reserved_prefix(_modules):
    """The reserved prefix is the importer's rule, not a blanket ban: a module the
    Marketplace installed as official keeps its celerp- name."""
    name = f"celerp-{_uid()}"
    install_from_zip(_official_zip(name), official=True, source="marketplace")

    assert [a.name for a in loader.admit_modules(str(_modules), {name}).admitted] == [name]


def test_celerp_module_copied_in_by_hand_loads(_modules):
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


def test_celerp_module_copied_in_by_hand_cannot_take_a_marketplace_modules_package(_modules):
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


def test_runtime_manifest_must_match_the_admitted_one(_modules):
    """Admission reads the literal (and refuses one the source changes); a
    manifest changed after admission cannot widen what was admitted."""
    inner = f"acme_{_uid()}"
    folder = f"acme-{_uid()}"
    pkg = _write_module(
        _modules, folder, {"name": folder, "version": "1.0.0"},
        {f"{inner}/__init__.py": ""})
    admission = loader.admit_modules(str(_modules), {folder})
    assert admission.refused == {}
    init = pkg / "__init__.py"
    init.write_text(init.read_text() + "PLUGIN_MANIFEST['api_routes'] = 'celerp.routers.health'\n")

    loaded = loader.load_all(str(_modules), {folder}, admission=admission)

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


def test_setup_rebound_after_admission_is_refused_at_registration(_modules, tmp_path):
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
    """Refused at load, after its code ran (the manifest changed after
    admission, so it differs at runtime)."""
    folder = f"acme-{_uid()}"
    inner = f"acme_{_uid()}"
    pkg = _write_module(_modules, folder, {"name": folder, "version": "1.0.0"},
                        {f"{inner}/__init__.py": "",
                         f"{inner}/models.py": _MODELS.replace("{inner}", inner)},
                        init_prelude=f"import {inner}.models")
    admission = loader.admit_modules(str(_modules), {folder})
    init = pkg / "__init__.py"
    init.write_text(init.read_text() + "PLUGIN_MANIFEST['api_routes'] = 'celerp.routers.health'\n")

    loader.load_all(str(_modules), {folder}, admission=admission)
    tables = await _created_tables(committed_engine)

    assert "differs" in loader.load_errors()[folder]
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
