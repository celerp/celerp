# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Tests for celerp.services.backup_import - the run_recovery() flow.

Most of the heavy lifting (validate_archive, pg_restore, alembic) is
already tested individually. This file focuses on integration:
  - run_recovery validates the archive before anything else
  - run_recovery carries module warnings, restart and safety archive to the result
  - the restored schema is reconciled through the SHARED migration path
    (regression: used to call AlembicConfig('alembic.ini') naively)
"""

from __future__ import annotations

import io
import json
import os
import shutil
import tarfile
from pathlib import Path

os.environ.setdefault("ALLOW_INSECURE_JWT", "true")

import pytest


@pytest.fixture(autouse=True)
def _isolate_database_side_effects(monkeypatch):
    """run_recovery tests stub the database restore, so the steps that query the
    real database around it are stubbed too; each is exercised directly by its
    own test below, through the original returned here, and end to end in
    test_system_recovery.py."""
    from celerp.services import backup_import

    originals = {
        name: getattr(backup_import, name)
        for name in ("_current_connectors", "_reconcile_connectors", "_clear_restored_connector_state")
    }

    async def _noop(*_args, **_kwargs) -> None:
        return None

    for name in originals:
        monkeypatch.setattr(backup_import, name, _noop)
    monkeypatch.setattr(
        "celerp.services.session_tracker.end_all_sessions", _noop
    )
    return originals


@pytest.mark.asyncio
async def test_current_connector_state_is_revoked_before_restore(
    session, monkeypatch, tmp_path, _isolate_database_side_effects
):
    import uuid
    from contextlib import asynccontextmanager
    from unittest.mock import AsyncMock

    from celerp.config import settings
    from celerp.models.company import Company
    from celerp.models.connector_config import ConnectorConfig
    from celerp.services import backup_import

    company_id = uuid.uuid4()
    session.add(Company(
        id=company_id,
        name="Current Connector Co",
        slug=f"current-connector-{company_id.hex[:8]}",
        settings={},
    ))
    session.add(ConnectorConfig(
        company_id=str(company_id),
        connector="woocommerce",
        webhook_ids_json='["11"]',
    ))
    await session.flush()

    @asynccontextmanager
    async def _shared_session_ctx():
        yield session

    revoke = AsyncMock()
    monkeypatch.setattr(
        "celerp.connectors.remote_state.revoke_connector_remote_state",
        revoke,
    )
    monkeypatch.setattr(
        "celerp.connectors.remote_state.connection_revision",
        AsyncMock(return_value="rev-1"),
    )
    monkeypatch.setattr(
        "celerp.db.get_session_ctx",
        _shared_session_ctx,
    )
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    connectors = await _isolate_database_side_effects["_current_connectors"]()
    backup_import._mark_recovery_started(tmp_path / "safety.celerp-backup", connectors)
    await _isolate_database_side_effects["_reconcile_connectors"]()

    revoke.assert_awaited_once_with(
        str(company_id), "woocommerce", webhook_ids=["11"], revision="rev-1"
    )
    assert json.loads(backup_import._marker_path().read_text())["connectors"] == []


@pytest.mark.asyncio
async def test_connector_maintenance_guard_uses_session_advisory_lock(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    from celerp.connectors.ownership import connector_maintenance_guard

    session = MagicMock()
    session.get_bind.return_value = SimpleNamespace(
        dialect=SimpleNamespace(name="postgresql")
    )
    session.execute = AsyncMock()
    session.rollback = AsyncMock()
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)

    monkeypatch.setattr("celerp.db.LifecycleSessionLocal", lambda: cm)

    async with connector_maintenance_guard():
        assert session.execute.await_count == 1

    sql = [str(call.args[0]) for call in session.execute.await_args_list]
    assert "pg_advisory_lock" in sql[0]
    assert "pg_advisory_unlock" in sql[1]
    session.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_restored_connector_cleanup_fences_stale_context(
    session, _isolate_database_side_effects
):
    import uuid

    from sqlalchemy import select

    from celerp.connectors.sync_runner import CONNECTOR_RESET_ENTITY
    from celerp.models.company import Company
    from celerp.models.connector_config import ConnectorConfig, OutboundQueue
    from celerp.models.sync_run import SyncRun

    company_id = uuid.uuid4()
    session.add(Company(
        id=company_id,
        name="Restored Connector Co",
        slug=f"restored-connector-{company_id.hex[:8]}",
        settings={},
    ))
    session.add(ConnectorConfig(
        company_id=str(company_id),
        connector="woocommerce",
    ))
    session.add(OutboundQueue(
        company_id=str(company_id),
        connector="woocommerce",
        entity_type="item",
        entity_id="item:restore",
    ))
    await session.flush()

    await _isolate_database_side_effects["_clear_restored_connector_state"](session)
    await session.flush()

    assert await session.scalar(select(ConnectorConfig).where(
        ConnectorConfig.company_id == str(company_id)
    )) is None
    assert await session.scalar(select(OutboundQueue).where(
        OutboundQueue.company_id == str(company_id)
    )) is None
    reset = await session.scalar(select(SyncRun).where(
        SyncRun.company_id == str(company_id),
        SyncRun.connector == "woocommerce",
        SyncRun.entity == CONNECTOR_RESET_ENTITY,
    ))
    assert reset is not None
    assert reset.status == "reset"


def _make_archive(
    company_name: str = "Test",
    version: str | None = None,
    extra_meta: dict | None = None,
    include_dump: bool = True,
    include_meta: bool = True,
) -> bytes:
    """Build a minimal .celerp-backup in memory and return bytes.

    `version` defaults to the running version: a backup this copy made itself.
    Pass an explicit string to test a specific version scenario.
    """
    if version is None:
        from celerp import __version__ as version
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


class TestValidateArchivePaths:
    def test_rejects_backslash_member_name(self, monkeypatch):
        from celerp.services import backup_import as bi

        buf = io.BytesIO(_make_archive())
        with tarfile.open(fileobj=buf, mode="r:gz") as src:
            members = [(m, src.extractfile(m).read() if m.isfile() else b"") for m in src.getmembers()]

        out = io.BytesIO()
        with tarfile.open(fileobj=out, mode="w:gz") as tar:
            for member, body in members:
                tar.addfile(member, io.BytesIO(body) if member.isfile() else None)
            evil = tarfile.TarInfo(r"modules\celerp-inventory\stale.py")
            payload = b"stale"
            evil.size = len(payload)
            tar.addfile(evil, io.BytesIO(payload))

        path = _write_archive_to_tmp(out.getvalue())
        monkeypatch.setattr(bi, "_local_pg_restore_major", lambda: None)
        try:
            with pytest.raises(ValueError, match="Unsafe path"):
                bi.validate_archive(path)
        finally:
            path.unlink(missing_ok=True)


class TestRecoveryEnabledModules:
    """validate_archive must surface enabled_modules so the UI can preflight.

    The full run_recovery flow is exercised in test_routers/test_backup.py
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


def _stub_recovery(monkeypatch, tmp_path, *, restart: bool = False) -> dict:
    """Stub the database and safety steps of run_recovery; returns what they received."""
    from contextlib import asynccontextmanager

    from celerp.config import settings
    from celerp.services import backup_import

    captured: dict = {}
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    monkeypatch.setattr(settings, "data_dir", data)

    async def _none(*_args, **_kwargs):
        return None

    async def _safety():
        path = tmp_path / "safety.celerp-backup"
        path.write_bytes(_make_archive())
        return backup_import.SafetyResult(ok=True, path=path)

    async def _every_company(session):
        return []

    def _apply(modules):
        captured["modules"] = list(modules)
        return restart

    @asynccontextmanager
    async def _guard():
        yield

    for name in ("_run_restore_script", "_dispose_engine", "_reconcile_schema", "_cloud_safety_snapshot"):
        monkeypatch.setattr(backup_import, name, _none)
    monkeypatch.setattr(backup_import, "make_safety_archive", _safety)
    monkeypatch.setattr("celerp.services.backup.check_backup_dump", lambda path, url: None)
    monkeypatch.setattr("celerp.services.backup.write_restore_script", shutil.copyfile)
    monkeypatch.setattr(backup_import, "_apply_modules", _apply)
    monkeypatch.setattr("celerp.modules.registry.load_set", _every_company)
    monkeypatch.setattr("celerp.connectors.ownership.connector_maintenance_guard", _guard)
    return captured


# run_recovery reads the restored companies, so the schema must exist.
@pytest.mark.usefixtures("_db_engine")
class TestRecoveryMissingModuleWarnings:
    """run_recovery must populate BackupResult.warnings with missing module names.

    The destination may not have all the modules the source had enabled.
    A 15/15 success that silently fails to load 1 module is a bug from the
    user's perspective. The warning lets the UI say 'X module is not
    installed on this server' before the user clicks the broken link.
    """

    @pytest.mark.asyncio
    async def test_warns_when_enabled_module_missing(self, monkeypatch, tmp_path):
        """When meta.enabled_modules includes a name with no on-disk package,
        run_recovery must record it in warnings."""
        from celerp.services import backup_import

        modules_dir = tmp_path / "modules"
        modules_dir.mkdir()
        (modules_dir / "celerp-inventory").mkdir()
        (modules_dir / "celerp-inventory" / "__init__.py").write_text("# x")
        monkeypatch.setenv("MODULE_DIR", str(modules_dir))
        _stub_recovery(monkeypatch, tmp_path)

        archive = _make_archive(extra_meta={
            "enabled_modules": ["celerp-inventory", "celerp-fictional"],
        })
        path = _write_archive_to_tmp(archive)
        try:
            result = await backup_import.run_recovery(path)
            assert result.ok, result.error
            assert any("celerp-fictional" in w for w in result.warnings)
            assert not any("celerp-inventory" in w for w in result.warnings)
        finally:
            path.unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_no_warnings_when_all_modules_installed(self, monkeypatch, tmp_path):
        """When all enabled modules have on-disk packages, warnings is empty."""
        from celerp.services import backup_import

        modules_dir = tmp_path / "modules"
        modules_dir.mkdir()
        for name in ("celerp-inventory", "celerp-dashboard"):
            (modules_dir / name).mkdir()
            (modules_dir / name / "__init__.py").write_text("# x")
        monkeypatch.setenv("MODULE_DIR", str(modules_dir))
        _stub_recovery(monkeypatch, tmp_path)

        archive = _make_archive(extra_meta={
            "enabled_modules": ["celerp-inventory", "celerp-dashboard"],
        })
        path = _write_archive_to_tmp(archive)
        try:
            result = await backup_import.run_recovery(path)
            assert result.ok, result.error
            assert result.warnings == []
        finally:
            path.unlink(missing_ok=True)


# run_recovery reads the restored companies, so the schema must exist.
@pytest.mark.usefixtures("_db_engine")
class TestRecoveryAppliesModules:
    """run_recovery makes the enabled modules the ones listed in meta.json."""

    @pytest.mark.asyncio
    async def test_apply_modules_called_with_meta_list(self, monkeypatch, tmp_path):
        from celerp.services import backup_import

        captured = _stub_recovery(monkeypatch, tmp_path)
        archive = _make_archive(extra_meta={"enabled_modules": ["celerp-inventory"]})
        path = _write_archive_to_tmp(archive)
        try:
            result = await backup_import.run_recovery(path)
            assert result.ok, result.error
            assert captured["modules"] == ["celerp-inventory"]
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

    def test_backup_import_uses_cli_migration_path(self):
        """Positive check: backup_import delegates to the CLI migration sequence
        (stamp-repair walker + grants + reconcile), which itself uses the shared
        alembic config lookup. Raw `alembic upgrade head` dies on restored dumps
        whose stamp is behind their actual DDL."""
        from pathlib import Path
        src = (Path(__file__).parent.parent / "celerp" / "services" / "backup_import.py").read_text()
        assert "_apply_migrations" in src, (
            "backup_import.py must reconcile the restored schema via "
            "celerp.cli._apply_migrations (the stamp-repair walker), not raw alembic."
        )

    def test_cli_uses_shared_helper(self):
        """cli.py must also use the shared helper (no duplicate lookup logic)."""
        from pathlib import Path
        src = (Path(__file__).parent.parent / "celerp" / "cli.py").read_text()
        assert "build_alembic_config" in src, (
            "cli.py should use celerp.alembic_config.build_alembic_config() to share the lookup."
        )


class TestApplyModulesRestarts:
    """_apply_modules must trigger a process restart so the loader picks
    up new enabled_modules.

    Without a restart, the API keeps running with the old module set.
    The sentinel is only checked when the subprocess exits. The /system/restart
    endpoint uses _send_sigterm(); _apply_modules must do the same.
    """

    @pytest.mark.asyncio
    async def test_apply_modules_triggers_restart(self, monkeypatch, tmp_path):
        """When the module set changes, _apply_modules must schedule a restart."""
        import asyncio
        import signal
        from celerp.services import backup_import

        # _apply_modules imports replace_enabled_modules from celerp.config at call
        # time. True means the enabled set actually changed, which gates the restart.
        monkeypatch.setattr("celerp.config.replace_enabled_modules", lambda m: True)

        sentinel = tmp_path / ".restart_requested"
        monkeypatch.setattr(
            "celerp.routers.system._restart_sentinel_path",
            lambda: sentinel,
        )

        kill_calls: list[tuple[int, int]] = []
        monkeypatch.setattr("os.kill", lambda pid, sig: kill_calls.append((pid, sig)))
        monkeypatch.setattr("time.sleep", lambda _: None)

        assert backup_import._apply_modules(["celerp-inventory"]) is True
        assert sentinel.exists()

        # _send_sigterm is scheduled via call_later(0.5, ...). Yield to the
        # event loop so it fires before we assert.
        await asyncio.sleep(1)

        assert len(kill_calls) == 1, (
            "Expected exactly one os.kill(SIGTERM) call, "
            f"got {len(kill_calls)}: {kill_calls}"
        )
        assert kill_calls[0][1] == signal.SIGTERM

    @pytest.mark.asyncio
    async def test_apply_modules_skips_restart_when_unchanged(self, monkeypatch):
        """When the enabled set didn't change, no restart is scheduled."""
        import asyncio
        from celerp.services import backup_import

        monkeypatch.setattr("celerp.config.replace_enabled_modules", lambda m: False)

        kill_calls: list[tuple[int, int]] = []
        monkeypatch.setattr("os.kill", lambda pid, sig: kill_calls.append((pid, sig)))

        assert backup_import._apply_modules(["celerp-inventory"]) is False
        await asyncio.sleep(0.1)

        assert kill_calls == [], "Unchanged module set must not trigger a restart"

    @pytest.mark.asyncio
    async def test_apply_modules_writes_an_empty_set(self, monkeypatch):
        """A backup with no modules enabled is restored with no modules enabled."""
        from celerp.services import backup_import

        config_written: list[list[str]] = []
        monkeypatch.setattr("celerp.config.replace_enabled_modules",
                            lambda m: config_written.append(list(m)) or True)

        kill_calls: list[tuple[int, int]] = []
        monkeypatch.setattr("os.kill", lambda pid, sig: kill_calls.append((pid, sig)))

        assert backup_import._apply_modules([]) is True

        assert config_written == [[]]


# ---------------------------------------------------------------------------
# _reconcile_schema - restored dumps must migrate through the stamp-repair
# walker, and a failure must fail the recovery, not just the log
# ---------------------------------------------------------------------------

class TestReconcileSchema:
    @pytest.mark.asyncio
    async def test_runs_cli_migration_sequence(self, monkeypatch):
        """Restore reconciles via the same walker path as `celerp migrate`:
        stamp repair + upgrade, grants, then the develop-to-release reconcile."""
        import celerp.cli as cli
        from celerp.services import backup_import

        calls: list[str] = []
        monkeypatch.setattr(cli, "_apply_migrations", lambda url: calls.append("migrate"))
        monkeypatch.setattr(cli, "_post_migration_grants", lambda url: calls.append("grants"))
        monkeypatch.setattr(cli, "_reconcile_after_migrate", lambda url: calls.append("reconcile"))

        await backup_import._reconcile_schema()
        assert calls == ["migrate", "grants", "reconcile"]

    @pytest.mark.asyncio
    async def test_failure_raises_user_facing_error(self, monkeypatch):
        """A failed reconcile leaves the schema stale (every read breaks), so it
        must fail the recovery with a plain message instead of claiming success."""
        import celerp.cli as cli
        from celerp.services import backup_import

        def boom(url):
            raise RuntimeError("DuplicateColumn: column already exists")

        monkeypatch.setattr(cli, "_apply_migrations", boom)
        with pytest.raises(RuntimeError) as exc_info:
            await backup_import._reconcile_schema()
        message = str(exc_info.value)
        assert "schema could not be brought up to date" in message
        assert "DuplicateColumn" in message


# ---------------------------------------------------------------------------
# Restore journey - the outcome must survive the post-restore restart and the
# user must always have a way to continue (GDR: no dead ends)
# ---------------------------------------------------------------------------

class TestRestoreNotice:
    def test_notice_roundtrip_and_one_shot(self, monkeypatch, tmp_path):
        """The importer persists the outcome; the login page consumes it exactly once,
        so the banner survives the automatic restart but never becomes permanent."""
        from celerp.config import settings
        from celerp.services.backup_import import RESTORE_NOTICE_FILE, _write_restore_notice
        from ui.routes.auth import _consume_restore_notice

        monkeypatch.setattr(settings, "data_dir", tmp_path)
        _write_restore_notice("Acme", ["celerp-labels"], "/data/recovery-safety/pre.celerp-backup", True)
        assert (tmp_path / RESTORE_NOTICE_FILE).is_file()

        notice = _consume_restore_notice()
        assert notice is not None
        assert notice["company_name"] == "Acme"
        assert notice["warnings"] == ["celerp-labels"]
        assert notice["safety_archive"] == "/data/recovery-safety/pre.celerp-backup"
        assert notice["restart_scheduled"] is True
        assert not (tmp_path / RESTORE_NOTICE_FILE).exists()  # one-shot
        assert _consume_restore_notice() is None

    def test_notice_message_composes_all_parts(self):
        from ui.routes.auth import _restore_notice_message

        msg = _restore_notice_message({
            "company_name": "Acme",
            "warnings": ["celerp-labels"],
            "safety_archive": "/data/recovery-safety/pre.celerp-backup",
        })
        assert "Acme" in msg
        assert "celerp-labels" in msg
        assert "/data/recovery-safety/pre.celerp-backup" in msg

    def test_no_notice_file_means_no_banner(self, monkeypatch, tmp_path):
        from celerp.config import settings
        from ui.routes.auth import _consume_restore_notice

        monkeypatch.setattr(settings, "data_dir", tmp_path)
        assert _consume_restore_notice() is None


class TestRestoreFlashContinuation:
    """A restore flash must always let the user continue: every session has ended,
    so either the restart is already happening (status + auto-reload to sign-in)
    or there is a Sign in link."""

    def _result(self, **kw):
        from celerp.services.backup import BackupResult
        defaults = dict(ok=True, size_bytes=1)
        defaults.update(kw)
        return BackupResult(**defaults)

    def test_no_restart_offers_sign_in(self):
        from celerp.services.backup_import import SESSION_ENDED_HEADER
        from celerp_backup.routes import _restore_flash

        resp = _restore_flash(self._result(), "Restored.")
        body = resp.body.decode()
        assert 'href="/login"' in body
        assert "Sign in" in body and "signed out" in body
        assert "restart" not in body.lower()
        assert resp.headers.get(SESSION_ENDED_HEADER) == "1"

    def test_scheduled_restart_shows_status_and_reload(self):
        from celerp_backup.routes import _restore_flash

        resp = _restore_flash(self._result(restart_scheduled=True), "Restored.")
        body = resp.body.decode()
        assert "Restarting automatically" in body
        assert resp.headers.get("X-Session-Ended") == "1"
        assert "setInterval" in body                 # page recovers on its own

    def test_warnings_render_as_warning_flash(self):
        from celerp_backup.routes import _restore_flash

        body = _restore_flash(
            self._result(warnings=["celerp-labels"], safety_archive="/data/pre.celerp-backup"), "Restored."
        ).body.decode()
        assert "flash--warning" in body
        assert "celerp-labels" in body and "/data/pre.celerp-backup" in body


# run_recovery reads the restored companies, so the schema must exist.
@pytest.mark.usefixtures("_db_engine")
class TestRecoveryPropagation:
    """run_recovery must carry the restart decision and safety archive through to the
    result AND persist the one-shot notice, or the journey guarantees fall apart."""

    @pytest.mark.asyncio
    async def test_result_and_notice_carry_safety_archive_and_restart(self, monkeypatch, tmp_path):
        from celerp.services import backup_import

        modules_dir = tmp_path / "modules"
        modules_dir.mkdir()
        monkeypatch.setenv("MODULE_DIR", str(modules_dir))
        _stub_recovery(monkeypatch, tmp_path, restart=True)

        archive = _make_archive(
            company_name="Acme",
            extra_meta={"enabled_modules": ["celerp-fictional"]},
        )
        path = _write_archive_to_tmp(archive)
        try:
            result = await backup_import.run_recovery(path)
        finally:
            path.unlink(missing_ok=True)

        assert result.ok, result.error
        assert result.safety_archive == str(tmp_path / "safety.celerp-backup")
        assert result.restart_scheduled is True
        assert any("celerp-fictional" in w for w in result.warnings)

        notice = json.loads((tmp_path / "data" / backup_import.RESTORE_NOTICE_FILE).read_text())
        assert notice["company_name"] == "Acme"
        assert notice["restart_scheduled"] is True
        assert notice["safety_archive"] == result.safety_archive
        assert notice["warnings"] == result.warnings


class TestApplyModulesRestartDecision:
    def test_no_restart_when_module_set_unchanged(self, monkeypatch):
        import celerp.config as config
        from celerp.services import backup_import

        monkeypatch.setattr(config, "replace_enabled_modules", lambda modules: False)
        assert backup_import._apply_modules(["celerp-inventory"]) is False
        assert backup_import._apply_modules([]) is False

    @pytest.mark.asyncio
    async def test_restart_scheduled_when_modules_changed(self, monkeypatch, tmp_path):
        import celerp.config as config
        import celerp.routers.system as system
        from celerp.services import backup_import

        monkeypatch.setattr(config, "replace_enabled_modules", lambda modules: True)
        monkeypatch.setattr(system, "_restart_sentinel_path", lambda: tmp_path / ".restart_requested")
        monkeypatch.setattr(system, "_send_sigterm", lambda: None)  # never SIGTERM the test runner

        assert backup_import._apply_modules(["celerp-labels"]) is True
        assert (tmp_path / ".restart_requested").exists()
