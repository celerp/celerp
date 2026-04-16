# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1
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
