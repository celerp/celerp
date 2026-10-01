# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""What a company needs from the installed modules: one planner that says, for every
module a migration or a backup depends on, whether it is ready, needs turning on,
needs a restart, needs an update or is missing, and one preparation step that turns
on what may be turned on without the owner editing configuration by hand.

Also the module side of the company backup contract: a module that owns tables says
how each travels with a company backup, and a malformed or missing declaration is
refused when the module is imported."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _module(root: Path, name: str, *, version: str = "1.0.0", label: str | None = None,
            prefix: str | None = None, backup=None, migrations: bool = False) -> Path:
    pkg = root / name
    pkg.mkdir(parents=True, exist_ok=True)
    lines = ["PLUGIN_MANIFEST = {", f'    "name": "{name}",', f'    "version": "{version}",',
             f'    "display_name": "{label or name}",']
    if prefix is not None:
        lines.append(f'    "table_prefix": "{prefix}",')
    if migrations:
        lines.append(f'    "migrations": "{name.replace("-", "_")}.migrations",')
    if backup is not None:
        lines.append(f'    "company_backup": {backup!r},')
    lines.append("}")
    (pkg / "__init__.py").write_text("\n".join(lines) + "\n")
    return pkg


@pytest.fixture()
def modules(tmp_path, monkeypatch):
    """An empty MODULE_DIR, a config file with nothing enabled and nothing running."""
    from celerp.config import write_config
    from celerp.modules import loader
    root = tmp_path / "modules"
    root.mkdir()
    monkeypatch.setenv("MODULE_DIR", str(root))
    monkeypatch.delenv("ENABLED_MODULES", raising=False)
    cfg = tmp_path / "celerp" / "config.toml"
    monkeypatch.setenv("CELERP_CONFIG", str(cfg))
    write_config({"modules": {"enabled": []}})
    monkeypatch.setattr(loader, "_loaded", [])
    monkeypatch.setattr(loader, "_load_errors", {})
    return root


def _first_party(monkeypatch, *names: str) -> None:
    from celerp.modules import loader
    monkeypatch.setattr(loader, "is_first_party", lambda path: Path(path).name in names)
    monkeypatch.setattr(loader, "first_party_names", lambda: frozenset(names))


def _running(monkeypatch, name: str, version: str) -> None:
    from celerp.modules import loader
    monkeypatch.setattr(loader, "_loaded", [*loader._loaded, {"name": name, "version": version}])


def _enabled() -> list[str]:
    from celerp.config import read_config
    return list(read_config().get("modules", {}).get("enabled", []))


# ── Requirement classification ───────────────────────────────────────────────

def test_requirement_status_matrix(modules, monkeypatch):
    """Every state a needed module can be in maps to exactly one status."""
    from celerp.config import set_enabled_modules
    from celerp.modules import loader
    from celerp.modules.requirements import Status, plan_requirements
    _module(modules, "acme-ready", version="2.0.0")
    _module(modules, "acme-off")
    _module(modules, "acme-pending")
    _module(modules, "acme-old-disk", version="1.0.0")
    _module(modules, "acme-old-running", version="3.0.0")
    _module(modules, "acme-broken")
    _running(monkeypatch, "acme-ready", "2.0.0")
    _running(monkeypatch, "acme-old-running", "1.0.0")
    set_enabled_modules(["acme-pending", "acme-broken"])
    monkeypatch.setitem(loader._load_errors, "acme-broken", "boom")
    plan = plan_requirements({
        "acme-ready": "2.0.0", "acme-off": None, "acme-pending": None, "acme-old-disk": "2.0.0",
        "acme-old-running": "3.0.0", "acme-broken": None, "acme-gone": None, "celerp-ai": None,
    })
    status = {r.name: r.status for r in plan.requirements}
    assert status == {
        "acme-ready": Status.READY,
        "acme-off": Status.ENABLE_REQUIRED,
        "acme-pending": Status.RESTART_REQUIRED,
        "acme-old-disk": Status.INCOMPATIBLE,
        "acme-old-running": Status.UPGRADE_RESTART_REQUIRED,
        "acme-broken": Status.INCOMPATIBLE,
        "acme-gone": Status.MISSING,
        "celerp-ai": Status.READY,
    }
    assert not plan.ready
    assert {r.name for r in plan.blocked} == {"acme-old-disk", "acme-broken", "acme-gone"}


def test_version_comparison_is_semantic(modules, monkeypatch):
    """1.10.0 is newer than 1.9.0; an unparseable installed version is not new enough."""
    from celerp.modules.requirements import Status, plan_requirements
    _module(modules, "acme-a", version="1.10.0")
    _module(modules, "acme-b", version="banana")
    _running(monkeypatch, "acme-a", "1.10.0")
    _running(monkeypatch, "acme-b", "banana")
    plan = plan_requirements({"acme-a": "1.9.0", "acme-b": "1.0.0"})
    assert {r.name: r.status for r in plan.requirements} == {
        "acme-a": Status.READY, "acme-b": Status.INCOMPATIBLE}


def test_plan_labels_never_show_package_names_for_first_party(modules, monkeypatch):
    """The public form of a plan carries labels and statuses, not paths or internals."""
    from celerp.modules.requirements import plan_requirements
    _module(modules, "celerp-accounting", label="Accounting")
    _first_party(monkeypatch, "celerp-accounting")
    public = plan_requirements({"celerp-accounting": None}).public()
    assert public == [{"name": "celerp-accounting", "label": "Accounting", "status": "enable_required",
                       "first_party": True}]


# ── Preparation policy ───────────────────────────────────────────────────────

def test_prepare_turns_on_first_party_and_needs_restart(modules, monkeypatch):
    """A bundled module that is installed but off is turned on additively; a restart follows."""
    from celerp.config import set_enabled_modules
    from celerp.modules.requirements import plan_requirements, prepare
    _module(modules, "celerp-accounting")
    _module(modules, "celerp-sales-funnel")
    _first_party(monkeypatch, "celerp-accounting", "celerp-sales-funnel")
    set_enabled_modules(["celerp-sales-funnel"])
    plan = plan_requirements({"celerp-accounting": None})
    assert [r.name for r in plan.preparable] == ["celerp-accounting"] and not plan.needs_consent
    assert prepare(plan) is True
    assert _enabled() == ["celerp-sales-funnel", "celerp-accounting"]
    # Idempotent: preparing again changes nothing and still reports the pending restart.
    again = plan_requirements({"celerp-accounting": None})
    assert prepare(again) is True
    assert _enabled() == ["celerp-sales-funnel", "celerp-accounting"]


def test_prepare_never_turns_on_third_party_without_consent(modules, monkeypatch):
    """A third-party module is turned on only when the owner named it."""
    from celerp.modules.requirements import ConsentRequired, plan_requirements, prepare
    _module(modules, "acme-maintenance", label="Maintenance")
    _first_party(monkeypatch)
    plan = plan_requirements({"acme-maintenance": None})
    assert [r.name for r in plan.needs_consent] == ["acme-maintenance"]
    with pytest.raises(ConsentRequired):
        prepare(plan)
    assert _enabled() == []
    assert prepare(plan, consent=frozenset({"acme-maintenance"})) is True
    assert _enabled() == ["acme-maintenance"]


def test_prepare_refuses_missing_and_incompatible(modules, monkeypatch):
    """Nothing is turned on when any requirement cannot be met here."""
    from celerp.modules.requirements import RequirementsBlocked, plan_requirements, prepare
    _module(modules, "celerp-accounting")
    _first_party(monkeypatch, "celerp-accounting")
    plan = plan_requirements({"celerp-accounting": None, "acme-gone": None})
    with pytest.raises(RequirementsBlocked):
        prepare(plan)
    assert _enabled() == []


def test_prepare_with_everything_running_needs_no_restart(modules, monkeypatch):
    from celerp.modules.requirements import plan_requirements, prepare
    _module(modules, "celerp-accounting")
    _first_party(monkeypatch, "celerp-accounting")
    _running(monkeypatch, "celerp-accounting", "1.0.0")
    plan = plan_requirements({"celerp-accounting": None})
    assert plan.ready and prepare(plan) is False


# ── Module import: the company backup declaration ────────────────────────────

@pytest.fixture()
def module_dir(tmp_path, monkeypatch):
    d = tmp_path / "installed"
    d.mkdir()
    monkeypatch.setenv("MODULE_DIR", str(d))
    return d


@pytest.mark.parametrize("backup, message", [
    (None, "company_backup"),
    ("include", "company_backup"),
    ({"acme_things": "maybe"}, "include"),
    ({"other_things": "include"}, "acme_"),
    ({"acme_Things!": "include"}, "acme_Things!"),
])
def test_import_refuses_module_without_valid_backup_declaration(module_dir, tmp_path, backup, message):
    """A module owning tables must say how each travels with a company backup, in the
    documented shape, before it can be installed."""
    from celerp.modules.importer import ModuleImportError, install_from_folder
    src = _module(tmp_path / "src", "acme-things", prefix="acme_", backup=backup, migrations=True)
    with pytest.raises(ModuleImportError, match=message.replace("!", r"\!")):
        install_from_folder(src)
    assert not (module_dir / "acme-things").exists()


def test_import_accepts_declared_module(module_dir, tmp_path):
    from celerp.modules.importer import install_from_folder
    src = _module(tmp_path / "src", "acme-things", prefix="acme_", migrations=True,
                  backup={"acme_things": "include", "acme_tokens": "exclude"})
    assert install_from_folder(src)["name"] == "acme-things"


def test_template_module_conforms(module_dir, tmp_path):
    """The published module template's manifest passes the declaration check."""
    from celerp.modules.importer import _validate_company_backup
    _validate_company_backup("acme-maintenance", {
        "name": "acme-maintenance", "table_prefix": "acme_", "migrations": "acme_maintenance.migrations",
        "company_backup": {"acme_equipment": "include", "acme_service_log": "include",
                           "acme_equipment_file": "include"},
    })


def test_bundled_modules_need_no_declaration():
    """No bundled module owns prefixed tables, so none needs a declaration."""
    from celerp.modules.loader import read_manifest
    for pkg in (REPO_ROOT / "default_modules").iterdir():
        if (pkg / "__init__.py").is_file():
            assert not read_manifest(pkg).get("table_prefix"), pkg.name


def test_env_untouched_by_planner(modules):
    """Planning has no side effects on the environment or the config file."""
    from celerp.modules.requirements import plan_requirements
    before = (dict(os.environ), _enabled())
    plan_requirements({"acme-gone": None})
    assert (dict(os.environ), _enabled()) == before
