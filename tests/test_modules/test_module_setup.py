# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1
"""Tests for scripts/module_setup.py"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Detect externally-managed pip environments (e.g. Homebrew Python on CI/dev)
# where pip install always fails with PEP 668. Tests that invoke real pip are
# skipped on such systems; they run fine in Docker/venv-based CI.
def _pip_works() -> bool:
    r = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--dry-run", "pip", "--quiet", "--disable-pip-version-check"],
        capture_output=True, text=True,
    )
    return r.returncode == 0

_PIP_AVAILABLE = _pip_works()
_skip_no_pip = pytest.mark.skipif(not _PIP_AVAILABLE, reason="pip not available (externally-managed environment)")


def _run_setup(data_dir: Path, extra_args: list[str] | None = None) -> subprocess.CompletedProcess:
    cmd = [sys.executable, "scripts/module_setup.py", "--data-dir", str(data_dir)]
    if extra_args:
        cmd.extend(extra_args)
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).parent.parent.parent),  # core/
    )


class TestModuleSetup:
    def test_no_modules_dir_exits_zero(self, tmp_path):
        result = _run_setup(tmp_path)
        assert result.returncode == 0

    def test_no_requirements_files_exits_zero(self, tmp_path):
        modules = tmp_path / "modules"
        modules.mkdir()
        (modules / "my-mod").mkdir()
        (modules / "my-mod" / "__init__.py").write_text("")
        result = _run_setup(tmp_path)
        assert result.returncode == 0

    def test_dry_run_skips_install(self, tmp_path):
        modules = tmp_path / "modules"
        modules.mkdir()
        mod = modules / "my-mod"
        mod.mkdir()
        (mod / "requirements.txt").write_text("requests\n")
        result = _run_setup(tmp_path, ["--dry-run"])
        assert result.returncode == 0
        assert "dry-run" in result.stdout

    def test_json_output_on_dry_run(self, tmp_path):
        modules = tmp_path / "modules"
        modules.mkdir()
        mod = modules / "my-mod"
        mod.mkdir()
        (mod / "requirements.txt").write_text("requests\n")
        result = _run_setup(tmp_path, ["--dry-run", "--json"])
        assert result.returncode == 0
        # Last line should be JSON
        lines = [l for l in result.stdout.strip().splitlines() if l.strip().startswith("{")]
        assert lines
        data = json.loads(lines[-1])
        assert "my-mod" in data
        assert data["my-mod"] is True

    @_skip_no_pip
    def test_failed_install_exits_one(self, tmp_path):
        modules = tmp_path / "modules"
        modules.mkdir()
        mod = modules / "bad-mod"
        mod.mkdir()
        (mod / "requirements.txt").write_text("this-package-does-not-exist-xyz-abc-999\n")
        result = _run_setup(tmp_path)
        assert result.returncode == 1
        assert "FAILED" in result.stdout or "failed" in result.stdout.lower()

    @_skip_no_pip
    def test_already_installed_deps_exits_zero(self, tmp_path):
        """Test with a dep that's definitely already installed (pytest itself)."""
        modules = tmp_path / "modules"
        modules.mkdir()
        mod = modules / "good-mod"
        mod.mkdir()
        (mod / "requirements.txt").write_text("pytest\n")
        result = _run_setup(tmp_path)
        assert result.returncode == 0
