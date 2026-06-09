# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Tests for celerp.modules.audit — the read-only enabled-vs-installed diff.

This is the foundation of the restore-flow fix: a fresh server restoring
a .celerp-backup can have its database back online, but the dashboard
will 404 if the modules that own the dashboard route (celerp-dashboard,
celerp-inventory, ...) aren't installed. audit_missing_modules gives
the UI a list to warn about.
"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path

os.environ.setdefault("ALLOW_INSECURE_JWT", "true")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import pytest

from celerp.modules.audit import audit_missing_modules, _module_dirs


def _make_pkg(parent: Path, name: str) -> Path:
    """Create a fake module package (dir + __init__.py) under parent."""
    p = parent / name
    p.mkdir(parents=True, exist_ok=True)
    (p / "__init__.py").write_text("# fake module\n")
    return p


def _set_module_dir(monkeypatch, *dirs: Path) -> None:
    """Point MODULE_DIR env var at the given dirs (comma-separated)."""
    monkeypatch.setenv("MODULE_DIR", ",".join(str(d) for d in dirs))


class TestAuditMissingModules:
    """audit_missing_modules returns enabled names not present on disk."""

    def test_all_installed_returns_empty(self, monkeypatch, tmp_path):
        _set_module_dir(monkeypatch, tmp_path)
        _make_pkg(tmp_path, "celerp-inventory")
        _make_pkg(tmp_path, "celerp-dashboard")
        assert audit_missing_modules({"celerp-inventory", "celerp-dashboard"}) == []

    def test_one_missing_returns_sorted(self, monkeypatch, tmp_path):
        _set_module_dir(monkeypatch, tmp_path)
        _make_pkg(tmp_path, "celerp-inventory")
        # celerp-fake is enabled but not on disk
        missing = audit_missing_modules({"celerp-inventory", "celerp-fake"})
        assert missing == ["celerp-fake"]

    def test_multiple_missing_returns_sorted(self, monkeypatch, tmp_path):
        _set_module_dir(monkeypatch, tmp_path)
        _make_pkg(tmp_path, "celerp-inventory")
        missing = audit_missing_modules(
            {"celerp-zzz", "celerp-aaa", "celerp-inventory"}
        )
        assert missing == ["celerp-aaa", "celerp-zzz"]

    def test_accepts_list_input(self, monkeypatch, tmp_path):
        _set_module_dir(monkeypatch, tmp_path)
        _make_pkg(tmp_path, "celerp-inventory")
        # List input (same as set); celerp-fake is not on disk
        missing = audit_missing_modules(["celerp-inventory", "celerp-fake"])
        assert missing == ["celerp-fake"]

    def test_empty_enabled_returns_empty(self, monkeypatch, tmp_path):
        _set_module_dir(monkeypatch, tmp_path)
        assert audit_missing_modules(set()) == []
        assert audit_missing_modules([]) == []

    def test_no_module_dir_env_still_finds_bundled(self, monkeypatch):
        """Without MODULE_DIR, bundled default_modules/ is still scanned."""
        monkeypatch.delenv("MODULE_DIR", raising=False)
        monkeypatch.delenv("CELERP_TRUSTED_MODULE_DIRS", raising=False)
        # These exist in default_modules/ — should NOT be missing
        missing = audit_missing_modules({"celerp-inventory", "celerp-dashboard"})
        assert missing == []

    def test_nonexistent_module_dir_still_finds_bundled(self, monkeypatch, tmp_path):
        """If MODULE_DIR is nonexistent, bundled default_modules/ is still scanned."""
        fake_path = tmp_path / "does_not_exist"
        _set_module_dir(monkeypatch, fake_path)
        # celerp-inventory exists in default_modules/ — not missing
        missing = audit_missing_modules({"celerp-inventory"})
        assert missing == []
        # But a fake module is missing
        missing = audit_missing_modules({"celerp-fake"})
        assert missing == ["celerp-fake"]

    def test_dir_without_init_py_not_counted(self, monkeypatch, tmp_path):
        """A dir without __init__.py is not a package, so the module is missing."""
        _set_module_dir(monkeypatch, tmp_path)
        # Create a directory but no __init__.py
        (tmp_path / "celerp-broken").mkdir()
        missing = audit_missing_modules({"celerp-broken"})
        assert missing == ["celerp-broken"]

    def test_file_at_top_level_not_counted(self, monkeypatch, tmp_path):
        """A loose .py file at the module-dir top level is not a module."""
        _set_module_dir(monkeypatch, tmp_path)
        (tmp_path / "celerp-loose.py").write_text("# loose file\n")
        missing = audit_missing_modules({"celerp-loose"})
        assert missing == ["celerp-loose"]

    def test_multiple_module_dirs_searched(self, monkeypatch, tmp_path):
        """MODULE_DIR can be a comma-separated list; audit walks all of them."""
        d1 = tmp_path / "default_modules"
        d1.mkdir()
        d2 = tmp_path / "premium_modules"
        d2.mkdir()
        _make_pkg(d1, "celerp-inventory")
        _make_pkg(d2, "celerp-sales-funnel")
        _set_module_dir(monkeypatch, d1, d2)

        missing = audit_missing_modules(
            {"celerp-inventory", "celerp-sales-funnel", "celerp-not-installed"}
        )
        assert missing == ["celerp-not-installed"]


class TestBackupExportWritesEnabledModules:
    """Backup export must include enabled_modules in meta.json (Layer 1)."""

    @pytest.mark.asyncio
    async def test_meta_json_contains_enabled_modules(self, monkeypatch, tmp_path):
        """When exporting, meta.json must list the company's enabled modules."""
        from celerp.services import backup_export as be

        captured: dict = {}

        def fake_build(dump, attachment_dirs, meta):
            captured["meta"] = meta
            return tmp_path / "test.celerp-backup"

        async def fake_read_company_enabled_modules():
            return ["celerp-inventory", "celerp-dashboard", "celerp-contacts"]

        # Lazy imports happen inside export_full, so patch the source modules
        import celerp.services.backup as backup_mod
        import celerp.config as cfg_mod
        monkeypatch.setattr(be, "_build_archive", fake_build)
        monkeypatch.setattr(backup_mod, "dump_database", lambda url: b"FAKE_DUMP")
        monkeypatch.setattr(be, "_version", lambda: "1.0.0")
        monkeypatch.setattr(be, "_read_company_enabled_modules", fake_read_company_enabled_modules)
        monkeypatch.setattr(cfg_mod, "read_config", lambda: {"company": {"name": "Test"}})

        await be.export_full()
        assert captured["meta"]["enabled_modules"] == [
            "celerp-inventory", "celerp-dashboard", "celerp-contacts"
        ]

    @pytest.mark.asyncio
    async def test_round_trip_export_then_validate_reads_enabled_modules(self, tmp_path):
        """End-to-end: build a real archive on disk with enabled_modules, then
        validate_archive must read it back."""
        import tarfile
        from celerp.services.backup_import import validate_archive, _safe_test_version

        archive_path = tmp_path / "test.celerp-backup"
        meta = {
            "celerp_version": _safe_test_version(),
            "pg_version": "16",
            "created_at": "2026-06-04T00:00:00Z",
            "company_name": "Round-trip Co",
            "enabled_modules": ["celerp-inventory", "celerp-fulfillment"],
        }
        with tarfile.open(archive_path, "w:gz") as tar:
            meta_bytes = json.dumps(meta).encode()
            info = tarfile.TarInfo("meta.json")
            info.size = len(meta_bytes)
            tar.addfile(info, io.BytesIO(meta_bytes))
            dump_bytes = b"FAKE_PG_DUMP"
            info2 = tarfile.TarInfo("database.dump")
            info2.size = len(dump_bytes)
            tar.addfile(info2, io.BytesIO(dump_bytes))

        parsed = validate_archive(archive_path)
        assert parsed.company_name == "Round-trip Co"
        assert parsed.enabled_modules == ["celerp-inventory", "celerp-fulfillment"]
