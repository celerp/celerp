# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Tests for celerp.alembic_config — the shared alembic.ini lookup.

Regression: backup_import.py called AlembicConfig("alembic.ini") naively
(CWD-relative) and failed with 'No script_location key found in configuration'
when the working dir was not the repo root. cli.py had its own copy of the
3-path fallback. We consolidated to a single helper.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("ALLOW_INSECURE_JWT", "true")

import pytest


class TestFindAlembicIni:
    """find_alembic_ini must locate alembic.ini in known layouts."""

    def test_finds_alembic_ini_next_to_package(self, monkeypatch, tmp_path):
        """Layout A: celerp/alembic.ini (dev repo, source tree)."""
        from celerp import alembic_config
        fake_ini = tmp_path / "celerp" / "alembic.ini"
        fake_ini.parent.mkdir(parents=True)
        fake_ini.write_text("[alembic]\nscript_location = migrations\n")

        # Monkeypatch Path(__file__).parent so the function looks in tmp_path/celerp
        monkeypatch.setattr(alembic_config, "__file__", str(tmp_path / "celerp" / "alembic_config.py"))

        result = alembic_config.find_alembic_ini()
        assert result == fake_ini

    def test_finds_alembic_ini_in_parent(self, monkeypatch, tmp_path):
        """Layout B: <parent>/alembic.ini (installed site-packages)."""
        from celerp import alembic_config
        fake_ini = tmp_path / "alembic.ini"
        fake_ini.write_text("[alembic]\nscript_location = migrations\n")

        # Create celerp/ inside tmp_path with no alembic.ini in it
        pkg = tmp_path / "celerp"
        pkg.mkdir()

        monkeypatch.setattr(alembic_config, "__file__", str(pkg / "alembic_config.py"))

        result = alembic_config.find_alembic_ini()
        assert result == fake_ini

    def test_finds_alembic_ini_relative_to_cwd(self, monkeypatch, tmp_path):
        """Layout C: cwd/alembic.ini (last-resort fallback)."""
        from celerp import alembic_config
        fake_ini = tmp_path / "alembic.ini"
        fake_ini.write_text("[alembic]\nscript_location = migrations\n")

        # No ini next to package, no ini in parent — only cwd
        empty_pkg = tmp_path / "empty" / "celerp"
        empty_pkg.mkdir(parents=True)
        monkeypatch.setattr(alembic_config, "__file__", str(empty_pkg / "alembic_config.py"))
        monkeypatch.chdir(tmp_path)

        result = alembic_config.find_alembic_ini()
        assert result == fake_ini

    def test_raises_when_not_found(self, monkeypatch, tmp_path):
        """Raises FileNotFoundError with the searched paths listed."""
        from celerp import alembic_config
        empty_pkg = tmp_path / "nowhere" / "celerp"
        empty_pkg.mkdir(parents=True)
        monkeypatch.setattr(alembic_config, "__file__", str(empty_pkg / "alembic_config.py"))
        monkeypatch.chdir(tmp_path)  # cwd also has no alembic.ini

        with pytest.raises(FileNotFoundError, match="alembic.ini not found"):
            alembic_config.find_alembic_ini()

    def test_includes_searched_paths_in_error(self, monkeypatch, tmp_path):
        """The error message must list the candidate paths so debugging is easy."""
        from celerp import alembic_config
        empty_pkg = tmp_path / "nowhere" / "celerp"
        empty_pkg.mkdir(parents=True)
        monkeypatch.setattr(alembic_config, "__file__", str(empty_pkg / "alembic_config.py"))
        monkeypatch.chdir(tmp_path)

        with pytest.raises(FileNotFoundError) as exc_info:
            alembic_config.find_alembic_ini()
        msg = str(exc_info.value)
        assert "Searched" in msg


class TestBuildAlembicConfig:
    """build_alembic_config returns a usable alembic Config with script_location set."""

    def test_returns_config_with_script_location(self, monkeypatch, tmp_path):
        """Real workflow: build the Config and verify script_location is resolved."""
        from celerp import alembic_config

        # Use the real celerp/alembic.ini from the repo
        real_ini = Path(alembic_config.__file__).parent / "alembic.ini"
        if not real_ini.is_file():
            pytest.skip("alembic.ini not present in this layout")

        cfg = alembic_config.build_alembic_config()
        # The Config object's get_main_option returns script_location as a path
        script_loc = cfg.get_main_option("script_location")
        assert script_loc, "script_location must be set after build_alembic_config()"

    def test_raises_when_no_ini_exists(self, monkeypatch, tmp_path):
        """If the real alembic.ini is missing in the package, raise FileNotFoundError."""
        from celerp import alembic_config

        empty_pkg = tmp_path / "empty2" / "celerp"
        empty_pkg.mkdir(parents=True)
        monkeypatch.setattr(alembic_config, "__file__", str(empty_pkg / "alembic_config.py"))
        monkeypatch.chdir(tmp_path)

        with pytest.raises(FileNotFoundError):
            alembic_config.build_alembic_config()
