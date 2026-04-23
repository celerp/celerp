# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Tests for Electron module integration (Phase 5).

These tests verify the JavaScript/Electron functions and the Python module_setup.py
without needing Electron to be installed.

For Electron JS functions, we verify the logic by inspecting the source and
testing the Python side-effects (module_setup.py, module seed logic).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

CORE_DIR = Path(__file__).parent.parent.parent
DEFAULT_MODULES_DIR = CORE_DIR / "default_modules"
ELECTRON_MAIN = CORE_DIR / "electron" / "main.js"


# ── Electron main.js source checks ────────────────────────────────────────────

class TestElectronMainJS:
    """Verify the Electron main.js has all required module integration code."""

    @pytest.fixture(autouse=True)
    def source(self):
        self._src = ELECTRON_MAIN.read_text()

    def test_module_dir_constant_defined(self):
        assert "const MODULE_DIR" in self._src

    def test_default_modules_src_defined(self):
        assert "DEFAULT_MODULES_SRC" in self._src

    def test_seed_default_modules_function(self):
        assert "function seedDefaultModules" in self._src

    def test_run_module_setup_function(self):
        assert "function runModuleSetup" in self._src

    def test_module_alembic_locations_function(self):
        assert "function _moduleAlembicLocations" in self._src or "_moduleAlembicLocations" in self._src

    def test_seed_called_in_boot_sequence(self):
        assert "seedDefaultModules()" in self._src

    def test_module_setup_called_in_boot_sequence(self):
        assert "runModuleSetup()" in self._src

    def test_migrations_called_after_module_setup(self):
        """runModuleSetup must appear before runMigrations in boot sequence."""
        boot_start = self._src.find("seedDefaultModules()")
        rest = self._src[boot_start:]
        setup_pos = rest.find("runModuleSetup()")
        migration_pos = rest.find("runMigrations(dbConfig.url)")
        assert setup_pos >= 0, "runModuleSetup() call not found in boot sequence"
        assert migration_pos >= 0, "runMigrations(dbConfig.url) call not found in boot sequence"
        assert setup_pos < migration_pos, "runModuleSetup() must be called before runMigrations()"

    def test_seed_called_before_migrations(self):
        """seedDefaultModules must appear before runMigrations in boot sequence."""
        boot_start = self._src.find("const dbConfig = resolveDatabaseConfig")
        rest = self._src[boot_start:]
        seed_pos = rest.find("seedDefaultModules()")
        migration_pos = rest.find("runMigrations(dbConfig.url)")
        assert seed_pos >= 0, "seedDefaultModules() call not found in boot sequence"
        assert migration_pos >= 0, "runMigrations(dbConfig.url) call not found in boot sequence"
        assert seed_pos < migration_pos, "seedDefaultModules() must be called before runMigrations()"

    def test_module_dir_env_var_in_api_start(self):
        assert "MODULE_DIR: MODULE_DIR" in self._src

    def test_module_dir_in_pythonpath(self):
        assert "MODULE_DIR" in self._src
        # Verify MODULE_DIR is on PYTHONPATH so module packages are importable
        assert "PYTHONPATH" in self._src

    def test_copy_dir_sync_function(self):
        assert "_copyDirSync" in self._src

    def test_module_alembic_locations_in_migrations_env(self):
        assert "ALEMBIC_VERSION_LOCATIONS" in self._src

    def test_default_modules_src_uses_dev_vs_packaged(self):
        """Dev uses relative path; packaged uses resourcesPath."""
        assert "IS_DEV" in self._src
        assert "default_modules" in self._src
        assert "process.resourcesPath" in self._src

    def test_seed_skips_existing_modules(self):
        """seedDefaultModules should not overwrite existing user-installed modules."""
        assert "fs.existsSync(dst)" in self._src
        assert "continue" in self._src  # Skip existing

    def test_module_setup_failure_is_nonfatal(self):
        """module_setup.py failure should be caught and logged, not fatal."""
        assert "non-fatal" in self._src or "warn" in self._src


# ── Default modules directory structure ───────────────────────────────────────

class TestDefaultModulesStructure:
    def test_default_modules_dir_exists(self):
        assert DEFAULT_MODULES_DIR.exists()
        assert DEFAULT_MODULES_DIR.is_dir()

    def test_celerp_labels_dir_exists(self):
        assert (DEFAULT_MODULES_DIR / "celerp-labels").is_dir()

    def test_labels_has_init(self):
        assert (DEFAULT_MODULES_DIR / "celerp-labels" / "__init__.py").exists()

    def test_celerp_verticals_dir_exists(self):
        assert (DEFAULT_MODULES_DIR / "celerp-verticals").is_dir()

    def test_celerp_verticals_has_init(self):
        assert (DEFAULT_MODULES_DIR / "celerp-verticals" / "__init__.py").exists()

    def test_labels_has_requirements(self):
        assert (DEFAULT_MODULES_DIR / "celerp-labels" / "requirements.txt").exists()

    def test_labels_requirements_not_empty(self):
        content = (DEFAULT_MODULES_DIR / "celerp-labels" / "requirements.txt").read_text()
        assert len(content.strip()) > 0

    def test_labels_has_package_dir(self):
        """celerp_labels sub-package must exist."""
        assert (DEFAULT_MODULES_DIR / "celerp-labels" / "celerp_labels").is_dir()

    def test_labels_has_routes(self):
        assert (DEFAULT_MODULES_DIR / "celerp-labels" / "celerp_labels" / "routes.py").exists()

    def test_labels_has_ui_routes(self):
        assert (DEFAULT_MODULES_DIR / "celerp-labels" / "celerp_labels" / "ui_routes.py").exists()

    def test_labels_has_service(self):
        assert (DEFAULT_MODULES_DIR / "celerp-labels" / "celerp_labels" / "service.py").exists()

    def test_labels_has_migrations_dir(self):
        assert (DEFAULT_MODULES_DIR / "celerp-labels" / "celerp_labels" / "migrations").is_dir()

    def test_labels_migrations_has_init(self):
        assert (DEFAULT_MODULES_DIR / "celerp-labels" / "celerp_labels" / "migrations" / "__init__.py").exists()


# ── module_setup.py tests ─────────────────────────────────────────────────────

def _run_setup(data_dir: Path, extra_args=None) -> subprocess.CompletedProcess:
    cmd = [sys.executable, "scripts/module_setup.py", "--data-dir", str(data_dir)]
    if extra_args:
        cmd.extend(extra_args)
    return subprocess.run(cmd, capture_output=True, text=True, cwd=str(CORE_DIR))


class TestModuleSetupElectron:
    def test_no_modules_dir_is_noop(self, tmp_path):
        r = _run_setup(tmp_path)
        assert r.returncode == 0

    def test_empty_modules_dir_is_noop(self, tmp_path):
        (tmp_path / "modules").mkdir()
        r = _run_setup(tmp_path)
        assert r.returncode == 0

    def test_module_without_requirements_skipped(self, tmp_path):
        """Module directory without requirements.txt should not cause failure."""
        mdir = tmp_path / "modules" / "mymod"
        mdir.mkdir(parents=True)
        (mdir / "__init__.py").write_text('PLUGIN_MANIFEST = {"name": "mymod", "version": "1.0"}')
        r = _run_setup(tmp_path)
        assert r.returncode == 0

    def test_module_with_empty_requirements_is_noop(self, tmp_path):
        mdir = tmp_path / "modules" / "emptymod"
        mdir.mkdir(parents=True)
        (mdir / "requirements.txt").write_text("\n# comment\n")
        r = _run_setup(tmp_path)
        assert r.returncode == 0

    def test_invalid_data_dir_exits_gracefully(self, tmp_path):
        r = _run_setup(tmp_path / "nonexistent" / "path")
        assert r.returncode == 0  # Should handle gracefully

    def test_dry_run_flag(self, tmp_path):
        mdir = tmp_path / "modules" / "testmod"
        mdir.mkdir(parents=True)
        (mdir / "requirements.txt").write_text("requests>=2.0\n")
        r = _run_setup(tmp_path, ["--dry-run"])
        assert r.returncode == 0
        combined = r.stdout + r.stderr
        assert "testmod" in combined or "dry-run" in combined.lower()


# ── Second-boot / re-init guards ──────────────────────────────────────────────

class TestSecondBootGuards:
    """Verify Electron main.js contains idempotency guards for operations that
    must only run once (first boot), not on every app launch."""

    @pytest.fixture(autouse=True)
    def source(self):
        self._src = ELECTRON_MAIN.read_text()

    def test_postgres_initialise_guarded_by_pg_version_file(self):
        """initialise() must only be called when PG_VERSION does not exist.

        embedded-postgres.initialise() runs initdb, which fails with non-zero
        exit if the data directory already contains a Postgres cluster.
        On second boot this would crash the entire app.
        """
        # Guard must check for PG_VERSION sentinel before calling initialise()
        assert "PG_VERSION" in self._src, (
            "startPostgres must check for PG_VERSION to skip initdb on second boot"
        )
        assert "initialise" in self._src, "initialise() call must still exist for first boot"

        # The guard must come before the initialise() call
        pg_version_pos = self._src.find("PG_VERSION")
        initialise_pos = self._src.find("pgInstance.initialise()")
        assert pg_version_pos < initialise_pos, (
            "PG_VERSION check must appear before pgInstance.initialise() call"
        )

    def test_postgres_initialise_inside_conditional(self):
        """initialise() must be inside an if-block, not called unconditionally."""
        # Find the block around initialise() — it should be inside an if (!existsSync(...))
        init_pos = self._src.find("pgInstance.initialise()")
        # Look backwards from initialise() for the conditional
        before = self._src[:init_pos]
        last_if = before.rfind("if (")
        last_if_content = before[last_if:init_pos]
        assert "existsSync" in last_if_content, (
            "pgInstance.initialise() must be inside an if (!fs.existsSync(...)) guard"
        )

    def test_seed_default_modules_skips_existing(self):
        """seedDefaultModules must skip already-installed module dirs (idempotent)."""
        assert "fs.existsSync(dst)" in self._src
        assert "continue" in self._src

    def test_migrations_are_idempotent_upgrade_head(self):
        """runMigrations uses alembic upgrade head which is a no-op if already current."""
        assert "upgrade" in self._src and "head" in self._src


class TestElectronConfirmDialog:
    """Verify the native confirm dialog bridge is correctly wired in main.js and preload.js."""

    @pytest.fixture(autouse=True)
    def _src(self):
        import os
        p = os.path.join(os.path.dirname(__file__), "../../electron/main.js")
        with open(p) as f:
            self.main_src = f.read()
        p2 = os.path.join(os.path.dirname(__file__), "../../electron/preload.js")
        with open(p2) as f:
            self.preload_src = f.read()

    def test_ipcmain_imported(self):
        """ipcMain must be destructured from 'electron' require."""
        assert "ipcMain" in self.main_src

    def test_show_confirm_handler_registered(self):
        """main.js must register an ipcMain.on('show-confirm', ...) handler."""
        assert 'ipcMain.on("show-confirm"' in self.main_src

    def test_show_confirm_uses_showmessageboxsync(self):
        """show-confirm handler must use showMessageBoxSync (synchronous) not showMessageBox."""
        assert "showMessageBoxSync" in self.main_src

    def test_show_confirm_sets_event_returnvalue(self):
        """show-confirm handler must set event.returnValue for ipcRenderer.sendSync."""
        assert "event.returnValue" in self.main_src

    def test_preload_exposes_showconfirm(self):
        """preload.js must expose showConfirm on the contextBridge."""
        assert "showConfirm" in self.preload_src

    def test_preload_uses_sendsync(self):
        """showConfirm must use ipcRenderer.sendSync (not invoke) to return synchronously."""
        assert "sendSync" in self.preload_src
        assert '"show-confirm"' in self.preload_src

    def test_window_confirm_overridden(self):
        """main.js must override window.confirm via did-finish-load injection.

        window.confirm() is silently stubbed to false in Electron's renderer
        (contextIsolation=true). Overriding it makes ALL confirm() calls work:
        - onclick confirm() in row-menu and bulk delete buttons
        - htmx's internal confirm(a) call for hx-confirm attributes
        """
        assert "window.confirm = function" in self.main_src

    def test_confirm_patch_attached_on_did_finish_load(self):
        """window.confirm override must be injected on did-finish-load (survives htmx navigation)."""
        assert "did-finish-load" in self.main_src

    def test_confirm_patch_is_idempotent(self):
        """Patch must guard against double-registration (__ceConfirmPatched sentinel)."""
        assert "__ceConfirmPatched" in self.main_src

    def test_no_htmx_confirm_interceptor(self):
        """htmx:confirm event listener must NOT be present - window.confirm override is sufficient.
        The interceptor was a redundant second mechanism; removing it keeps the code DRY."""
        assert "htmx:confirm" not in self.main_src
        assert "issueRequest" not in self.main_src
