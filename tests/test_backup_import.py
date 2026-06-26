# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Tests for celerp.services.backup_import — the run_import() flow.

Most of the heavy lifting (validate_archive, pg_restore, alembic) is
already tested individually. This file focuses on integration:
  - run_import calls validate_archive first
  - run_import disposes the engine before pg_restore
  - run_import runs alembic upgrade head from the SHARED config helper
    (regression: used to call AlembicConfig('alembic.ini') naively)
"""

from __future__ import annotations

import io
import json
import os
import tarfile
from pathlib import Path

os.environ.setdefault("ALLOW_INSECURE_JWT", "true")

import pytest


def _make_archive(
    company_name: str = "Test",
    version: str | None = None,
    extra_meta: dict | None = None,
    include_dump: bool = True,
    include_meta: bool = True,
) -> bytes:
    """Build a minimal .celerp-backup in memory and return bytes.

    `version` defaults to a value guaranteed to be <= the current
    installed version, so tests never trip the version policy in any
    environment (CI's 0.1.dev1 vs local 1.1.11.dev20). Pass an explicit
    string to test a specific version scenario.
    """
    if version is None:
        from celerp.services.backup_import import _safe_test_version
        version = _safe_test_version()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        if include_meta:
            meta = {
                "celerp_version": version,
                "pg_version": "16",
                "created_at": "2026-01-01T00:00:00Z",
                "company_name": company_name,
            }
            if extra_meta:
                meta.update(extra_meta)
            meta_bytes = json.dumps(meta).encode()
            info = tarfile.TarInfo("meta.json")
            info.size = len(meta_bytes)
            tar.addfile(info, io.BytesIO(meta_bytes))
        if include_dump:
            dump = b"PGDMP dummy dump"
            info2 = tarfile.TarInfo("database.dump")
            info2.size = len(dump)
            tar.addfile(info2, io.BytesIO(dump))
    buf.seek(0)
    return buf.read()


def _write_archive_to_tmp(archive_bytes: bytes) -> Path:
    """Write archive bytes to a temp .celerp-backup file and return its path."""
    import tempfile
    tmp = tempfile.NamedTemporaryFile(suffix=".celerp-backup", delete=False)
    tmp.write(archive_bytes)
    tmp.close()
    return Path(tmp.name)


class TestValidateArchiveEnabledModules:
    """Validate archive reads enabled_modules from meta.json (Layer 1)."""

    def test_reads_enabled_modules_from_meta(self):
        from celerp.services.backup_import import validate_archive
        archive = _make_archive(extra_meta={"enabled_modules": ["celerp-inventory", "celerp-contacts"]})
        path = _write_archive_to_tmp(archive)
        try:
            meta = validate_archive(path)
            assert hasattr(meta, "enabled_modules")
            assert meta.enabled_modules == ["celerp-inventory", "celerp-contacts"]
        finally:
            path.unlink(missing_ok=True)

    def test_enabled_modules_defaults_to_empty_list(self):
        """Backwards compat: old archives without the key still parse."""
        from celerp.services.backup_import import validate_archive
        archive = _make_archive(extra_meta={})  # no enabled_modules
        path = _write_archive_to_tmp(archive)
        try:
            meta = validate_archive(path)
            assert meta.enabled_modules == []
        finally:
            path.unlink(missing_ok=True)

    def test_enabled_modules_can_be_none_in_json(self):
        """enabled_modules: null in JSON should become empty list, not None."""
        from celerp.services.backup_import import validate_archive
        archive = _make_archive(extra_meta={"enabled_modules": None})
        path = _write_archive_to_tmp(archive)
        try:
            meta = validate_archive(path)
            assert meta.enabled_modules == []
        finally:
            path.unlink(missing_ok=True)


class TestPgVersionPreCheck:
    """validate_archive must fail early (actionable message) when the backup was made by a
    newer PostgreSQL than the local pg_restore can read — instead of the cryptic
    'unsupported version (1.16) in file header' pg_restore emits mid-restore."""

    _PG17 = "pg_dump (PostgreSQL) 17.2 (Ubuntu 17.2-1.pgdg24.04+1)"

    def test_pg_major_parsing(self):
        from celerp.services.backup_import import _pg_major
        assert _pg_major(self._PG17) == 17
        assert _pg_major("pg_restore (PostgreSQL) 16.14 (Ubuntu ...)") == 16
        assert _pg_major("16") is None      # bare/unrecognized -> unknown (skip check)
        assert _pg_major("unknown") is None
        assert _pg_major(None) is None

    def test_blocks_newer_pg_backup_with_actionable_message(self, monkeypatch):
        from celerp.services import backup_import as bi
        monkeypatch.setattr(bi, "_local_pg_restore_major", lambda: 16)
        path = _write_archive_to_tmp(_make_archive(extra_meta={"pg_version": self._PG17}))
        try:
            with pytest.raises(ValueError) as ei:
                bi.validate_archive(path)
            msg = str(ei.value)
            assert "PostgreSQL 17" in msg and "PostgreSQL 16" in msg
        finally:
            path.unlink(missing_ok=True)

    def test_allows_same_or_older_pg(self, monkeypatch):
        from celerp.services import backup_import as bi
        monkeypatch.setattr(bi, "_local_pg_restore_major", lambda: 17)  # local newer than backup
        path = _write_archive_to_tmp(_make_archive(extra_meta={"pg_version": self._PG17}))
        try:
            bi.validate_archive(path)  # must not raise
        finally:
            path.unlink(missing_ok=True)

    def test_skips_when_version_unknown(self, monkeypatch):
        from celerp.services import backup_import as bi
        monkeypatch.setattr(bi, "_local_pg_restore_major", lambda: 16)
        # pg_version not in the recognized format -> can't compare -> don't block
        path = _write_archive_to_tmp(_make_archive(extra_meta={"pg_version": "unknown"}))
        try:
            bi.validate_archive(path)  # must not raise
        finally:
            path.unlink(missing_ok=True)


class TestRunImportEnabledModules:
    """validate_archive must surface enabled_modules so the UI can preflight.

    The full run_import flow is exercised in test_routers/test_backup.py
    (auth + bootstrap endpoints). This file focuses on validate_archive
    because it is the single point that reads meta.json.
    """

    def test_validate_archive_passes_enabled_modules_to_meta(self):
        """The ImportMeta dataclass must carry the enabled_modules list."""
        from celerp.services.backup_import import ImportMeta
        assert "enabled_modules" in ImportMeta.__dataclass_fields__


class TestBackupResultWarningsField:
    """BackupResult must carry a warnings list (Layer 2 — the must-have)."""

    def test_warnings_field_exists(self):
        from celerp.services.backup import BackupResult
        assert "warnings" in BackupResult.__dataclass_fields__

    def test_warnings_defaults_to_empty_list(self):
        from celerp.services.backup import BackupResult
        r = BackupResult(ok=True, size_bytes=100)
        assert r.warnings == []

    def test_warnings_is_a_list_field(self):
        """Warnings must be a list, not a string. The UI iterates over it."""
        from celerp.services.backup import BackupResult
        from dataclasses import fields
        field_obj = next(f for f in fields(BackupResult) if f.name == "warnings")
        assert field_obj.default_factory is not None


class TestRunImportMissingModuleWarnings:
    """run_import must populate BackupResult.warnings with missing module names.

    The destination may not have all the modules the source had enabled.
    A 15/15 success that silently fails to load 1 module is a bug from the
    user's perspective. The warning lets the UI say 'X module is not
    installed on this server' before the user clicks the broken link.
    """

    @pytest.mark.asyncio
    async def test_warns_when_enabled_module_missing(self, monkeypatch, tmp_path):
        """When meta.enabled_modules includes a name with no on-disk package,
        run_import must record it in warnings."""
        from celerp.services import backup_import
        from celerp.services.backup import BackupResult

        # Set up MODULE_DIR to a tmp dir that has SOME but not all modules
        modules_dir = tmp_path / "modules"
        modules_dir.mkdir()
        (modules_dir / "celerp-inventory").mkdir()
        (modules_dir / "celerp-inventory" / "__init__.py").write_text("# x")
        monkeypatch.setenv("MODULE_DIR", str(modules_dir))

        # Build an archive that needs celerp-inventory (installed) and
        # celerp-fictional (not installed)
        archive = _make_archive(extra_meta={
            "enabled_modules": ["celerp-inventory", "celerp-fictional"],
        })
        path = _write_archive_to_tmp(archive)
        try:
            # Stub out the heavy work so we only exercise the audit path
            async def fake_restore_db(dump_bytes, db_url):
                return None
            monkeypatch.setattr(backup_import, "_run_pg_restore", fake_restore_db)

            async def fake_dispose():
                pass
            monkeypatch.setattr(backup_import, "_dispose_engine", fake_dispose, raising=False)

            async def fake_alembic():
                pass
            monkeypatch.setattr(backup_import, "_run_alembic", fake_alembic, raising=False)

            async def fake_extract(path):
                pass
            monkeypatch.setattr(backup_import, "_extract_files", fake_extract, raising=False)

            async def fake_activate(modules):
                return None
            monkeypatch.setattr(backup_import, "_activate_modules", fake_activate, raising=False)

            async def fake_safety(label):
                return BackupResult(ok=True, size_bytes=0)
            monkeypatch.setattr(backup_import, "_safety_backup", fake_safety, raising=False)

            # Run the real run_import
            result = await backup_import.run_import(path)
            assert result.ok
            assert "celerp-fictional" in result.warnings
            assert "celerp-inventory" not in result.warnings
        finally:
            path.unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_no_warnings_when_all_modules_installed(self, monkeypatch, tmp_path):
        """When all enabled modules have on-disk packages, warnings is empty."""
        from celerp.services import backup_import
        from celerp.services.backup import BackupResult

        modules_dir = tmp_path / "modules"
        modules_dir.mkdir()
        for name in ("celerp-inventory", "celerp-dashboard"):
            (modules_dir / name).mkdir()
            (modules_dir / name / "__init__.py").write_text("# x")
        monkeypatch.setenv("MODULE_DIR", str(modules_dir))

        archive = _make_archive(extra_meta={
            "enabled_modules": ["celerp-inventory", "celerp-dashboard"],
        })
        path = _write_archive_to_tmp(archive)
        try:
            async def fake_restore(dump, url): return None
            async def fake_dispose(): pass
            async def fake_alembic(): pass
            async def fake_extract(path): pass
            async def fake_activate(m): return None
            async def fake_safety(label): return BackupResult(ok=True, size_bytes=0)
            monkeypatch.setattr(backup_import, "_run_pg_restore", fake_restore)
            monkeypatch.setattr(backup_import, "_dispose_engine", fake_dispose, raising=False)
            monkeypatch.setattr(backup_import, "_run_alembic", fake_alembic, raising=False)
            monkeypatch.setattr(backup_import, "_extract_files", fake_extract, raising=False)
            monkeypatch.setattr(backup_import, "_activate_modules", fake_activate, raising=False)
            monkeypatch.setattr(backup_import, "_safety_backup", fake_safety, raising=False)

            result = await backup_import.run_import(path)
            assert result.ok
            assert result.warnings == []
        finally:
            path.unlink(missing_ok=True)


class TestRunImportActivateModules:
    """run_import must call set_enabled_modules to update config.toml.

    After pg_restore, the destination's config.toml may not list the modules
    the source had enabled. We write the .restart_requested sentinel so the
    celerp start process manager picks them up on respawn.
    """

    @pytest.mark.asyncio
    async def test_activate_modules_called_with_meta_list(self, monkeypatch, tmp_path):
        """The activate helper receives the same list that was in meta.json."""
        from celerp.services import backup_import
        from celerp.services.backup import BackupResult

        captured: dict = {}
        modules_dir = tmp_path / "modules"
        modules_dir.mkdir()
        (modules_dir / "celerp-inventory").mkdir()
        (modules_dir / "celerp-inventory" / "__init__.py").write_text("# x")
        monkeypatch.setenv("MODULE_DIR", str(modules_dir))

        async def fake_activate(modules):
            captured["modules"] = list(modules)
        async def fake_restore(dump, url): return None
        async def fake_dispose(): pass
        async def fake_alembic(): pass
        async def fake_extract(path): pass
        async def fake_safety(label): return BackupResult(ok=True, size_bytes=0)
        monkeypatch.setattr(backup_import, "_activate_modules", fake_activate, raising=False)
        monkeypatch.setattr(backup_import, "_run_pg_restore", fake_restore)
        monkeypatch.setattr(backup_import, "_dispose_engine", fake_dispose, raising=False)
        monkeypatch.setattr(backup_import, "_run_alembic", fake_alembic, raising=False)
        monkeypatch.setattr(backup_import, "_extract_files", fake_extract, raising=False)
        monkeypatch.setattr(backup_import, "_safety_backup", fake_safety, raising=False)

        archive = _make_archive(extra_meta={"enabled_modules": ["celerp-inventory"]})
        path = _write_archive_to_tmp(archive)
        try:
            await backup_import.run_import(path)
            assert captured["modules"] == ["celerp-inventory"]
        finally:
            path.unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_activate_skipped_when_no_modules(self, monkeypatch, tmp_path):
        """No enabled_modules in meta → activate is not called."""
        from celerp.services import backup_import
        from celerp.services.backup import BackupResult

        activate_called = {"v": False}
        async def fake_activate(modules):
            activate_called["v"] = True
        async def fake_restore(dump, url): return None
        async def fake_dispose(): pass
        async def fake_alembic(): pass
        async def fake_extract(path): pass
        async def fake_safety(label): return BackupResult(ok=True, size_bytes=0)
        monkeypatch.setattr(backup_import, "_activate_modules", fake_activate, raising=False)
        monkeypatch.setattr(backup_import, "_run_pg_restore", fake_restore)
        monkeypatch.setattr(backup_import, "_dispose_engine", fake_dispose, raising=False)
        monkeypatch.setattr(backup_import, "_run_alembic", fake_alembic, raising=False)
        monkeypatch.setattr(backup_import, "_extract_files", fake_extract, raising=False)
        monkeypatch.setattr(backup_import, "_safety_backup", fake_safety, raising=False)

        archive = _make_archive(extra_meta={})  # no enabled_modules
        path = _write_archive_to_tmp(archive)
        try:
            await backup_import.run_import(path)
            assert activate_called["v"] is False
        finally:
            path.unlink(missing_ok=True)


class TestAlembicConfigHelperUsed:
    """Regression: backup_import must use celerp.alembic_config, not raw AlembicConfig.

    Before this refactor, backup_import.py called AlembicConfig('alembic.ini')
    directly, which is CWD-relative and failed when running from a non-repo
    directory (e.g. an installed package, a frozen Electron .app, or a test
    that changed cwd). The fix is to call build_alembic_config() which uses
    the shared Path-based lookup.
    """

    def test_backup_import_does_not_call_AlembicConfig_with_bare_string(self):
        """Static check: no `AlembicConfig('alembic.ini')` in backup_import.py."""
        from pathlib import Path
        src = (Path(__file__).parent.parent / "celerp" / "services" / "backup_import.py").read_text()
        # Reject the buggy pattern: bare "alembic.ini" string passed to Config
        assert 'AlembicConfig("alembic.ini")' not in src, (
            "backup_import.py still uses bare AlembicConfig('alembic.ini'). "
            "Use celerp.alembic_config.build_alembic_config() instead."
        )

    def test_backup_import_uses_shared_helper(self):
        """Positive check: backup_import.py imports and uses build_alembic_config."""
        from pathlib import Path
        src = (Path(__file__).parent.parent / "celerp" / "services" / "backup_import.py").read_text()
        assert "build_alembic_config" in src, (
            "backup_import.py must use celerp.alembic_config.build_alembic_config()."
        )

    def test_cli_uses_shared_helper(self):
        """cli.py must also use the shared helper (no duplicate lookup logic)."""
        from pathlib import Path
        src = (Path(__file__).parent.parent / "celerp" / "cli.py").read_text()
        assert "build_alembic_config" in src, (
            "cli.py should use celerp.alembic_config.build_alembic_config() to share the lookup."
        )


class TestActivateModulesRestarts:
    """_activate_modules must trigger a process restart so the loader picks
    up new enabled_modules.

    Without a restart, the API keeps running with the old module set.
    The sentinel is only checked when the subprocess exits. The /system/restart
    endpoint uses _send_sigterm() — _activate_modules must do the same.
    """

    @pytest.mark.asyncio
    async def test_activate_modules_triggers_restart(self, monkeypatch, tmp_path):
        """When modules are provided, _activate_modules must schedule a restart."""
        import asyncio
        import signal
        from celerp.services import backup_import

        # Stub set_enabled_modules at its source (_activate_modules imports it
        # function-locally from celerp.config). Return True to signal the
        # enabled set actually changed, which is what gates the restart.
        monkeypatch.setattr("celerp.config.set_enabled_modules", lambda m: True)

        # Stub _restart_sentinel_path to a temp file
        sentinel = tmp_path / ".restart_requested"
        monkeypatch.setattr(
            "celerp.routers.system._restart_sentinel_path",
            lambda: sentinel,
        )

        # Track os.kill calls
        kill_calls: list[tuple[int, int]] = []
        monkeypatch.setattr("os.kill", lambda pid, sig: kill_calls.append((pid, sig)))

        # Stub time.sleep to avoid actual delay
        monkeypatch.setattr("time.sleep", lambda _: None)

        await backup_import._activate_modules(["celerp-inventory"])

        # _send_sigterm is scheduled via call_later(0.5, ...). Yield to the
        # event loop so it fires before we assert.
        await asyncio.sleep(1)

        assert len(kill_calls) == 1, (
            "Expected exactly one os.kill(SIGTERM) call — "
            f"got {len(kill_calls)}: {kill_calls}"
        )
        assert kill_calls[0][1] == signal.SIGTERM

    @pytest.mark.asyncio
    async def test_activate_modules_skips_restart_when_unchanged(self, monkeypatch):
        """When the enabled set didn't change, no restart is scheduled."""
        import asyncio
        from celerp.services import backup_import

        # set_enabled_modules returns False => destination already had them all.
        monkeypatch.setattr("celerp.config.set_enabled_modules", lambda m: False)

        kill_calls: list[tuple[int, int]] = []
        monkeypatch.setattr("os.kill", lambda pid, sig: kill_calls.append((pid, sig)))

        await backup_import._activate_modules(["celerp-inventory"])
        await asyncio.sleep(0.1)

        assert kill_calls == [], "Unchanged module set must not trigger a restart"

    @pytest.mark.asyncio
    async def test_activate_modules_skips_restart_when_empty(self, monkeypatch):
        """Empty module list → no restart, no config write."""
        from celerp.services import backup_import

        config_written: list[str] = []
        monkeypatch.setattr(backup_import, "set_enabled_modules",
                            lambda m: config_written.extend(m), raising=False)

        kill_calls: list[tuple[int, int]] = []
        monkeypatch.setattr("os.kill", lambda pid, sig: kill_calls.append((pid, sig)))

        await backup_import._activate_modules([])

        assert config_written == [], "Empty list should not write config"
        assert kill_calls == [], "Empty list should not trigger restart"
