# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""System Recovery replaces the installation exactly: the module set and file roots end up
as the backup had them, a local safety archive is made before anything is overwritten, and a
failed safety archive stops the restore until the owner explicitly continues without one."""

from __future__ import annotations

import contextlib
import hashlib
import html
import io
import json
import re
import shutil
import subprocess
import tarfile
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from company_backup_support import company, owner, token
from migration_support import auth, code_config, maker, real_client, real_engine  # noqa: F401

from celerp.services.backup_import import _clear_restored_connector_state as _real_clear
from celerp.services.backup_import import _reconcile_connectors as _real_revoke
from celerp.services.session_tracker import end_all_sessions as _real_end_sessions


SAFETY_WARNING = "A safety backup could not be made before restoring."
SOURCE_DUMP = b"SOURCE-DUMP"
SAFETY_DUMP = b"SAFETY-DUMP"
ROOTS = {"attachments": ("static", "attachments"), "ai_uploads": ("ai_uploads",), "modules": ("modules",)}

# The destination before a recovery: its own files in every restore-owned root, plus a
# directory named after a bundled module, which the application owns.
DEST_FILES = {
    "attachments/old.pdf": b"DEST-ATTACHMENT",
    "attachments/shared.pdf": b"DEST-SHARED",
    "ai_uploads/old.txt": b"DEST-AI",
    "modules/celerp-example-old/__init__.py": b"PLUGIN_MANIFEST = {}\n",
    "modules/celerp-inventory/keep.py": b"# bundled\n",
}
SOURCE_FILES = {
    "attachments/new.pdf": bytes(range(256)) * 4,
    "attachments/shared.pdf": b"SOURCE-SHARED",
    "ai_uploads/new.txt": b"SOURCE-AI",
    "modules/celerp-example-new/__init__.py": b"PLUGIN_MANIFEST = {'name': 'celerp-example-new'}\n",
    "modules/celerp-inventory/evil.py": b"raise SystemExit\n",
}


# ── Helpers ──────────────────────────────────────────────────────────────────

def _add(tar: tarfile.TarFile, name: str, body: bytes) -> None:
    info = tarfile.TarInfo(name=name)
    info.size = len(body)
    tar.addfile(info, io.BytesIO(body))


def _archive(path: Path, files: dict[str, bytes] | None = None, *, dump: bytes = SOURCE_DUMP,
             modules=("celerp-inventory",), extra: tuple[tarfile.TarInfo, ...] = (),
             version: str | None = None) -> Path:
    """A whole-installation archive in the .celerp-backup layout, made by Celerp *version*
    (None: an archive from before backups recorded their version)."""
    meta = {"pg_version": "unknown", "company_name": "Harbor Goods Ltd", "enabled_modules": modules}
    if version is not None:
        meta["celerp_version"] = version
    with tarfile.open(path, "w:gz") as tar:
        _add(tar, "database.dump", dump)
        _add(tar, "meta.json", json.dumps(meta).encode())
        for name, body in (files or {}).items():
            _add(tar, name, body)
        for info in extra:
            tar.addfile(info)
    return path


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _members(path: Path) -> dict[str, bytes]:
    with tarfile.open(path, "r:gz") as tar:
        return {m.name: tar.extractfile(m).read() for m in tar.getmembers() if m.isfile()}


def _enabled() -> list[str]:
    from celerp.config import read_config
    return list(read_config().get("modules", {}).get("enabled", []))


def _set_enabled(names: list[str]) -> None:
    from celerp.config import read_config, write_config
    cfg = read_config()
    cfg.setdefault("modules", {})["enabled"] = list(names)
    write_config(cfg)


def _closure(names: list[str]) -> list[str]:
    from celerp.config import resolve_install_order
    return resolve_install_order(list(names), Path(__file__).resolve().parent.parent / "default_modules")


class _Recovery:
    """Stubs the database side of a recovery (dump, dump check, pg_restore, schema reconcile, connector
    and session steps) and records each step with whether writes were paused and the
    connector maintenance guard held at that moment."""

    def __init__(self, tmp_path: Path, monkeypatch, *, real_database: bool = False):
        import celerp.connectors.ownership as ownership
        import celerp.routers.system as system
        from celerp.config import settings
        from celerp.services import backup, backup_import, backup_repo, backup_state, session_tracker

        self.mp = monkeypatch
        self.data = tmp_path / "data"
        self.data.mkdir()
        self.calls: list[tuple[str, bool, bool]] = []
        self.restored: list[bytes] = []
        self.safety_at_restore: list[Path] = []
        self.write_status_during_restore: int | None = None
        self.pg_error: Exception | None = None
        self.guard_held = False
        self.reassembled = 0
        self._state = backup_state
        monkeypatch.setattr(settings, "data_dir", self.data)
        monkeypatch.setattr(settings, "backup_encryption_key", None)
        monkeypatch.setattr(system, "_send_sigterm", lambda: self.record("sigterm"))
        self.cloud_snapshot = AsyncMock(return_value=backup.BackupResult(ok=True, size_bytes=1))
        monkeypatch.setattr(backup_repo, "run_snapshot", self.cloud_snapshot)

        real_guard = ownership.connector_maintenance_guard

        @asynccontextmanager
        async def _guard():
            async with real_guard():
                self.guard_held = True
                self.record("guard")
                try:
                    yield
                finally:
                    self.guard_held = False

        monkeypatch.setattr(ownership, "connector_maintenance_guard", _guard)

        async def _reconcile():
            self.record("reconcile")

        self.real_reconcile = backup_import._reconcile_schema
        monkeypatch.setattr(backup_import, "_reconcile_schema", _reconcile)
        if real_database:
            return

        def _dump(url):
            self.record("safety_dump")
            return SAFETY_DUMP

        async def _restore(dump, url):
            self.record("pg_restore")
            self.restored.append(Path(dump).read_bytes() if isinstance(dump, (str, Path)) else dump)
            self.safety_at_restore = sorted((self.data / "recovery-safety").glob("*.celerp-backup"))
            from fastapi import HTTPException

            from celerp.events.engine import emit_event
            try:
                await emit_event(None, event_type="item.created", data={})
            except HTTPException as exc:
                self.write_status_during_restore = exc.status_code
            if self.pg_error is not None:
                raise self.pg_error

        async def _none(*a, **kw):
            return None

        monkeypatch.setattr(backup, "dump_database", _dump)
        monkeypatch.setattr(backup, "check_backup_dump", lambda path, url: None)
        monkeypatch.setattr(backup_import, "_run_pg_restore", _restore)
        monkeypatch.setattr(backup_import, "_dispose_engine", _none)

        def _recorder(name):
            async def _run(*a, **kw):
                self.record(name)
            return _run

        monkeypatch.setattr(backup_import, "_reconcile_connectors", _recorder("revoke"))
        monkeypatch.setattr(backup_import, "_clear_restored_connector_state", _recorder("clear_connectors"))
        monkeypatch.setattr(session_tracker, "end_all_sessions", _recorder("end_sessions"))

    def record(self, name: str) -> None:
        self.calls.append((name, self._state.is_active(), self.guard_held))

    def names(self) -> list[str]:
        return [c[0] for c in self.calls]

    def root(self, key: str) -> Path:
        return self.data.joinpath(*ROOTS[key])

    def seed(self, files: dict[str, bytes] = DEST_FILES) -> None:
        for name, body in files.items():
            key, rel = name.split("/", 1)
            path = self.root(key) / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)

    def tree(self, key: str) -> dict[str, bytes]:
        root = self.root(key)
        if not root.exists():
            return {}
        return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}

    def trees(self) -> dict[str, dict[str, bytes]]:
        return {key: self.tree(key) for key in ROOTS}

    def fail_safety(self, message: str = "No space left on device") -> None:
        from celerp.services import backup_export

        async def _fail():
            raise RuntimeError(message)

        self.mp.setattr(backup_export, "export_full", _fail)

    def staging(self) -> list[Path]:
        root = self.data / "recovery-staging"
        return sorted(root.iterdir()) if root.exists() else []

    def safety_archives(self) -> list[Path]:
        return sorted((self.data / "recovery-safety").glob("*.celerp-backup"))

    def cloud(self, archive: Path, tmp_path: Path) -> None:
        """Serve ``archive`` as cloud recovery point snap-1, counting downloads."""
        from celerp.config import settings
        from celerp.services import backup_repo
        import base64
        import secrets
        self.mp.setattr(settings, "backup_encryption_key", base64.b64encode(secrets.token_bytes(32)).decode())

        async def _reassemble(snapshot_id):
            assert snapshot_id == "snap-1"
            self.reassembled += 1
            copy = tmp_path / f"snap-{self.reassembled}.celerp-backup"
            shutil.copyfile(archive, copy)
            return copy

        self.mp.setattr(backup_repo, "reassemble_snapshot", _reassemble)

    async def start(self, kind: str, archive: Path, tmp_path: Path):
        from celerp.services import backup_import, backup_repo
        if kind == "local":
            return await backup_import.run_recovery(archive)
        self.cloud(archive, tmp_path)
        return await backup_repo.restore_snapshot("snap-1")


@pytest.fixture
def rec(tmp_path, monkeypatch, code_config, real_engine):
    _set_enabled(["celerp-inventory"])
    return _Recovery(tmp_path, monkeypatch)


async def _install_owner(engine):
    user = await owner(engine)
    cid = await company(engine, user, "Alpha Trading", "alpha-marker")
    return await token(engine, user, cid)


def _continue_vals(body: str) -> dict:
    """The fields the continue-without-safety button posts."""
    button = re.search(r'<button[^>]*hx-post="/backup/import/continue"[^>]*>', body)
    assert button, body
    match = re.search(r"""hx-vals=(["'])(.*?)\1""", button.group(0))
    assert match, body
    return json.loads(html.unescape(match.group(2)))


# ── Installation-wide module metadata ────────────────────────────────────────

async def test_load_set_unions_all_companies(code_config, real_engine):
    """Every company's modules count, not only the first company's; a company that
    has never chosen counts with whatever the installation loads."""
    from celerp.db import get_session_ctx
    from celerp.modules.registry import load_set
    _set_enabled(["celerp-inventory"])
    user = await owner(real_engine)
    await company(real_engine, user, "Alpha Trading", "alpha", settings={"enabled_modules": ["celerp-labels"]})
    await company(real_engine, user, "Beta Trading", "beta", settings={"enabled_modules": ["celerp-contacts"]})
    await company(real_engine, user, "Gamma Trading", "gamma")
    async with get_session_ctx() as session:
        assert set(await load_set(session)) == set(
            _closure(["celerp-contacts", "celerp-inventory", "celerp-labels"]))


async def test_local_backup_meta_lists_every_company_module(rec, real_engine):
    """A whole-installation backup lists the modules of every company, with the
    modules they need, in its metadata."""
    from celerp.services.backup_export import export_full
    user = await owner(real_engine)
    await company(real_engine, user, "Alpha Trading", "alpha", settings={"enabled_modules": ["celerp-labels"]})
    await company(real_engine, user, "Beta Trading", "beta", settings={"enabled_modules": ["celerp-contacts"]})
    path = await export_full()
    try:
        meta = json.loads(_members(path)["meta.json"])
    finally:
        path.unlink(missing_ok=True)
    assert sorted(meta["enabled_modules"]) == sorted(_closure(["celerp-contacts", "celerp-labels"]))


async def test_cloud_snapshot_meta_lists_every_company_module(rec, real_engine):
    """A cloud recovery point lists the modules of every company, with the modules
    they need, in its metadata."""
    from celerp.services import backup_export
    user = await owner(real_engine)
    await company(real_engine, user, "Alpha Trading", "alpha", settings={"enabled_modules": ["celerp-labels"]})
    await company(real_engine, user, "Beta Trading", "beta", settings={"enabled_modules": ["celerp-contacts"]})
    meta = await backup_export.archive_meta()
    assert sorted(meta["enabled_modules"]) == sorted(_closure(["celerp-contacts", "celerp-labels"]))


async def test_old_backup_fallback_uses_every_company_module(rec, real_engine, tmp_path):
    """A backup without module metadata takes the module set from every restored company."""
    from celerp.services import backup_import
    user = await owner(real_engine)
    await company(real_engine, user, "Alpha Trading", "alpha", settings={"enabled_modules": ["celerp-labels"]})
    await company(real_engine, user, "Beta Trading", "beta", settings={"enabled_modules": ["celerp-contacts"]})
    result = await backup_import.run_recovery(_archive(tmp_path / "old.celerp-backup", modules=None))
    assert result.ok is True, result.error
    assert set(_enabled()) == set(_closure(["celerp-contacts", "celerp-labels"]))


# ── Exact module configuration ───────────────────────────────────────────────

async def test_replace_enabled_modules_includes_dependencies(code_config):
    """The exact set written includes every dependency of the requested modules."""
    from celerp.config import replace_enabled_modules
    _set_enabled(["celerp-inventory", "celerp-contacts", "celerp-ai"])
    assert replace_enabled_modules(["celerp-labels"]) is True
    assert _enabled() == ["celerp-inventory", "celerp-labels"]


async def test_replace_enabled_modules_unchanged_schedules_no_restart(rec, tmp_path):
    """Restoring the module set the installation already has changes nothing and needs no restart."""
    from celerp.config import replace_enabled_modules
    from celerp.routers.system import _restart_sentinel_path
    from celerp.services import backup_import
    assert replace_enabled_modules(["celerp-inventory"]) is False
    result = await backup_import.run_recovery(_archive(tmp_path / "same.celerp-backup"))
    assert result.ok is True, result.error
    assert result.restart_scheduled is False
    assert not _restart_sentinel_path().exists()
    assert _enabled() == ["celerp-inventory"]


async def test_replace_enabled_modules_restart_on_removal(code_config):
    """Dropping a module is a change, exactly like adding one."""
    from celerp.config import replace_enabled_modules
    _set_enabled(["celerp-inventory", "celerp-labels"])
    assert replace_enabled_modules(["celerp-inventory"]) is True
    assert _enabled() == ["celerp-inventory"]


async def test_recovery_removes_destination_only_module(rec, tmp_path):
    """A module enabled only on this installation is disabled after the recovery, with a restart."""
    from celerp.routers.system import _restart_sentinel_path
    from celerp.services import backup_import
    _set_enabled(["celerp-inventory", "celerp-contacts", "celerp-labels"])
    result = await backup_import.run_recovery(_archive(tmp_path / "src.celerp-backup", modules=["celerp-inventory"]))
    assert result.ok is True, result.error
    assert _enabled() == ["celerp-inventory"]
    assert result.restart_scheduled is True
    assert _restart_sentinel_path().exists()


async def test_recovery_config_reflects_source_and_warns_missing_package(rec, tmp_path):
    """A source module that is not installed here is still enabled as the source had it, and named in a warning."""
    from celerp.services import backup_import
    result = await backup_import.run_recovery(_archive(
        tmp_path / "src.celerp-backup", modules=["celerp-inventory", "celerp-example-absent"]))
    assert result.ok is True, result.error
    assert set(_enabled()) == {"celerp-inventory", "celerp-example-absent"}
    assert any("celerp-example-absent" in w for w in result.warnings), result.warnings


# ── Exact file roots ─────────────────────────────────────────────────────────

async def _files_recovery(rec, tmp_path, files=SOURCE_FILES):
    from celerp.services import backup_import
    rec.seed()
    result = await backup_import.run_recovery(_archive(tmp_path / "src.celerp-backup", files))
    assert result.ok is True, result.error
    return result


async def test_recovery_removes_destination_only_attachment(rec, tmp_path):
    """An attachment only this installation had is gone after the recovery."""
    await _files_recovery(rec, tmp_path)
    assert set(rec.tree("attachments")) == {"new.pdf", "shared.pdf"}


async def test_recovery_removes_destination_only_ai_upload(rec, tmp_path):
    """An AI upload only this installation had is gone after the recovery."""
    await _files_recovery(rec, tmp_path)
    assert set(rec.tree("ai_uploads")) == {"new.txt"}


async def test_recovery_removes_destination_only_custom_module(rec, tmp_path):
    """A custom module only this installation had is gone after the recovery."""
    await _files_recovery(rec, tmp_path)
    assert not (rec.root("modules") / "celerp-example-old").exists()
    assert "celerp-example-new/__init__.py" in rec.tree("modules")


async def test_recovery_restores_source_files_byte_for_byte(rec, tmp_path):
    """Every restore-owned file is exactly the backup's file."""
    await _files_recovery(rec, tmp_path)
    assert rec.tree("attachments") == {"new.pdf": SOURCE_FILES["attachments/new.pdf"],
                                       "shared.pdf": b"SOURCE-SHARED"}
    assert rec.tree("ai_uploads") == {"new.txt": b"SOURCE-AI"}
    assert rec.tree("modules")["celerp-example-new/__init__.py"] == SOURCE_FILES["modules/celerp-example-new/__init__.py"]


async def test_recovery_leaves_bundled_module_files_untouched(rec, tmp_path):
    """A bundled module directory keeps its files and takes nothing from the backup."""
    keep = rec.root("modules") / "celerp-inventory" / "keep.py"
    rec.seed()
    inode = keep.stat().st_ino
    from celerp.services import backup_import
    result = await backup_import.run_recovery(_archive(tmp_path / "src.celerp-backup", SOURCE_FILES))
    assert result.ok is True, result.error
    assert keep.read_bytes() == b"# bundled\n" and keep.stat().st_ino == inode
    assert not (rec.root("modules") / "celerp-inventory" / "evil.py").exists()
    bundled = Path(__file__).resolve().parent.parent / "default_modules" / "celerp-inventory"
    assert not (bundled / "evil.py").exists()


async def test_recovery_empty_root_in_archive_empties_destination(rec, tmp_path):
    """A root the backup has no files for is empty after the recovery."""
    files = {k: v for k, v in SOURCE_FILES.items() if not k.startswith("ai_uploads/")}
    await _files_recovery(rec, tmp_path, files)
    assert rec.root("ai_uploads").is_dir()
    assert rec.tree("ai_uploads") == {}


async def test_recovery_file_swap_failure_is_not_success(rec, tmp_path, monkeypatch):
    """A file swap that fails after the database was restored is a failure naming the safety
    archive, with the roots already swapped put back."""
    from celerp.services import backup_import
    rec.seed()
    before = rec.trees()
    real_rename = backup_import._rename
    failed: list[Path] = []

    def _rename(src, dst):
        if Path(dst) == rec.root("ai_uploads") and not failed:
            failed.append(Path(dst))
            raise OSError("Device or resource busy")
        return real_rename(src, dst)

    monkeypatch.setattr(backup_import, "_rename", _rename)
    result = await backup_import.run_recovery(_archive(tmp_path / "src.celerp-backup", SOURCE_FILES))
    assert failed
    assert result.ok is False
    assert result.safety_archive and Path(result.safety_archive).is_file()
    assert result.safety_archive in result.error
    assert rec.trees() == before
    assert not (rec.data / "restore-notice.json").exists()


# ── Safety archive ───────────────────────────────────────────────────────────

async def test_recovery_writes_local_safety_archive_before_pg_restore(rec, tmp_path, monkeypatch):
    """A validated local safety archive of the current installation exists before pg_restore runs."""
    from celerp.services import backup_export, backup_import
    rec.seed()
    result = await backup_import.run_recovery(_archive(tmp_path / "src.celerp-backup", SOURCE_FILES))
    assert result.ok is True, result.error
    assert rec.names().index("safety_dump") < rec.names().index("pg_restore")
    assert len(rec.safety_at_restore) == 1
    safety = rec.safety_at_restore[0]
    assert result.safety_archive == str(safety)
    backup_import.validate_archive(safety)
    saved = _members(safety)
    assert saved["database.dump"] == SAFETY_DUMP
    assert saved["attachments/old.pdf"] == b"DEST-ATTACHMENT"

    # An export that does not validate is no safety archive: nothing is restored.
    async def _corrupt():
        bad = tmp_path / "corrupt.celerp-backup"
        bad.write_bytes(b"not an archive")
        return bad

    monkeypatch.setattr(backup_export, "export_full", _corrupt)
    rec.calls.clear()
    result = await backup_import.run_recovery(_archive(tmp_path / "src2.celerp-backup"))
    assert result.ok is False and result.needs_confirmation is True
    assert "pg_restore" not in rec.names()


@pytest.mark.parametrize("kind", ["local", "cloud"])
async def test_recovery_safety_failure_stops_before_pg_restore(rec, tmp_path, kind):
    """When no safety archive can be made nothing is changed and the owner is asked to confirm."""
    from celerp.services import backup_state
    rec.seed()
    before, modules = rec.trees(), _enabled()
    rec.fail_safety()
    archive = _archive(tmp_path / "src.celerp-backup", SOURCE_FILES)
    result = await rec.start(kind, archive, tmp_path)
    assert result.ok is False
    assert result.needs_confirmation is True
    assert result.confirmation_id
    assert result.archive_digest == _sha(archive)
    assert SAFETY_WARNING in result.error and "No space left on device" in result.error
    assert not {"revoke", "pg_restore", "reconcile", "end_sessions"} & set(rec.names())
    assert rec.trees() == before and _enabled() == modules
    assert backup_state.is_active() is False
    assert not (rec.data / "restore-notice.json").exists()


async def test_safety_archive_taken_with_writes_paused(rec, tmp_path):
    """The safety archive is made while writes are paused and connector work is excluded."""
    from celerp.services import backup_import, backup_state
    result = await backup_import.run_recovery(_archive(tmp_path / "src.celerp-backup"))
    assert result.ok is True, result.error
    dump = next(c for c in rec.calls if c[0] == "safety_dump")
    assert dump == ("safety_dump", True, True)
    assert backup_state.is_active() is False


async def test_recovery_blocks_writes_during_commit(rec, tmp_path):
    """Guard, then paused writes, then the safety archive, then connector revoke, then pg_restore;
    a write attempted during pg_restore is refused."""
    from celerp.services import backup_import
    result = await backup_import.run_recovery(_archive(tmp_path / "src.celerp-backup"))
    assert result.ok is True, result.error
    steps = [c for c in rec.calls if c[0] in {"guard", "safety_dump", "revoke", "pg_restore"}]
    assert [c[0] for c in steps] == ["guard", "safety_dump", "revoke", "pg_restore"]
    assert all(active and held for _, active, held in steps[1:])
    assert rec.write_status_during_restore == 503


async def test_recovery_cloud_safety_snapshot_optional(rec, tmp_path):
    """A cloud safety snapshot that fails does not stop a recovery that has its local safety archive."""
    from celerp.services.backup import BackupResult
    rec.cloud(tmp_path / "unused.celerp-backup", tmp_path)
    rec.cloud_snapshot.return_value = BackupResult(ok=False, size_bytes=0, error="relay unavailable")
    from celerp.services import backup_import
    result = await backup_import.run_recovery(_archive(tmp_path / "src.celerp-backup"))
    assert result.ok is True, result.error
    rec.cloud_snapshot.assert_awaited_once()
    assert len(rec.safety_archives()) == 1
    assert rec.restored == [SOURCE_DUMP]


async def test_recovery_safety_archive_without_cloud_key(rec, tmp_path):
    """With no cloud encryption key the local safety archive is still made and the restore runs."""
    from celerp.config import settings
    from celerp.services import backup_import
    assert not settings.backup_encryption_key
    result = await backup_import.run_recovery(_archive(tmp_path / "src.celerp-backup"))
    assert result.ok is True, result.error
    assert len(rec.safety_archives()) == 1
    rec.cloud_snapshot.assert_not_awaited()
    assert not any(SAFETY_WARNING in w for w in result.warnings)
    assert rec.restored == [SOURCE_DUMP]


async def test_recovery_safety_archive_retention_is_bounded(rec, tmp_path):
    """Only the newest safety archives are kept."""
    from celerp.services import backup_import
    safety_dir = rec.data / "recovery-safety"
    safety_dir.mkdir()
    old = [safety_dir / f"pre-recovery-2026010{d}T000000000000Z.celerp-backup" for d in range(1, 5)]
    for path in old:
        _archive(path)
    result = await backup_import.run_recovery(_archive(tmp_path / "src.celerp-backup"))
    assert result.ok is True, result.error
    assert rec.safety_archives() == sorted([old[2], old[3], Path(result.safety_archive)])


async def test_recovery_safety_archive_outside_restore_roots_and_survives_swap(rec, tmp_path):
    """The safety archive sits outside every replaced root and still holds the replaced files."""
    from celerp.services import backup_import
    rec.seed()
    result = await backup_import.run_recovery(_archive(tmp_path / "src.celerp-backup", SOURCE_FILES))
    assert result.ok is True, result.error
    safety = Path(result.safety_archive)
    assert safety.is_file() and safety.parent == rec.data / "recovery-safety"
    for key in ROOTS:
        assert not safety.is_relative_to(rec.root(key))
    saved = _members(safety)
    assert saved["attachments/old.pdf"] == b"DEST-ATTACHMENT"
    assert saved["modules/celerp-example-old/__init__.py"] == DEST_FILES["modules/celerp-example-old/__init__.py"]
    assert "old.pdf" not in rec.tree("attachments")


async def test_recovery_notice_names_safety_archive(rec, tmp_path):
    """The result, the post-restore sign-in notice and the page all name the safety archive."""
    from celerp.services import backup_import
    from celerp_backup.routes import _restore_flash
    from ui.routes.auth import _restore_notice_message
    result = await backup_import.run_recovery(_archive(tmp_path / "src.celerp-backup"))
    assert result.ok is True, result.error
    notice = json.loads((rec.data / "restore-notice.json").read_text())
    assert notice["safety_archive"] == result.safety_archive
    assert result.safety_archive in _restore_notice_message(notice)
    assert result.safety_archive in _restore_flash(result, "Restored.").body.decode()


# ── Preparing the recovery ───────────────────────────────────────────────────

def _special(name: str, kind: bytes, link: str = "") -> tarfile.TarInfo:
    info = tarfile.TarInfo(name=name)
    info.type = kind
    info.linkname = link
    return info


async def test_recovery_invalid_archive_makes_no_safety_and_no_change(rec, tmp_path):
    """An archive that is not a valid recovery point is refused before any safety archive or change."""
    from celerp.services import backup_import
    rec.seed()
    before, modules = rec.trees(), _enabled()
    not_tar = tmp_path / "not-tar.celerp-backup"
    not_tar.write_bytes(b"not a tar archive")
    no_dump = tmp_path / "no-dump.celerp-backup"
    with tarfile.open(no_dump, "w:gz") as tar:
        _add(tar, "meta.json", b"{}")
    bad = [
        not_tar,
        no_dump,
        _archive(tmp_path / "traversal.celerp-backup", {"attachments/../../escape.pdf": b"X"}),
        _archive(tmp_path / "symlink.celerp-backup",
                 extra=(_special("attachments/link.pdf", tarfile.SYMTYPE, "/etc/passwd"),)),
        _archive(tmp_path / "hardlink.celerp-backup", {"attachments/a.pdf": b"A"},
                 extra=(_special("modules/celerp-example-new/b.py", tarfile.LNKTYPE, "attachments/a.pdf"),)),
        _archive(tmp_path / "device.celerp-backup", extra=(_special("ai_uploads/dev", tarfile.CHRTYPE),)),
        _archive(tmp_path / "modules-string.celerp-backup", modules="celerp-inventory"),
        _archive(tmp_path / "modules-number.celerp-backup", modules=[7]),
        _archive(tmp_path / "modules-path.celerp-backup", modules=["../celerp-inventory"]),
    ]
    for path in bad:
        result = await backup_import.run_recovery(path)
        assert result.ok is False and result.error, path.name
        assert result.needs_confirmation is False, path.name
    assert rec.calls == [] or set(rec.names()) <= {"guard"}
    assert rec.safety_archives() == []
    assert rec.staging() == []
    assert rec.trees() == before and _enabled() == modules


async def test_recovery_stages_and_validates_files_before_destruction(rec, tmp_path, monkeypatch):
    """Every incoming file is staged on the installation's filesystem before pg_restore, and the
    destination roots are untouched until the database is restored."""
    from celerp.services import backup_import
    rec.seed()
    before = rec.trees()
    seen: dict = {}
    restore = backup_import._run_pg_restore

    async def _restore(dump, url):
        staged = {p.name: p for p in (rec.data / "recovery-staging").rglob("*") if p.is_file()}
        seen["staged"] = {name: staged[name].read_bytes() for name in ("new.pdf", "new.txt") if name in staged}
        seen["same_fs"] = all(p.stat().st_dev == rec.data.stat().st_dev for p in staged.values())
        seen["dest"] = rec.trees()
        await restore(dump, url)

    monkeypatch.setattr(backup_import, "_run_pg_restore", _restore)
    result = await backup_import.run_recovery(_archive(tmp_path / "src.celerp-backup", SOURCE_FILES))
    assert result.ok is True, result.error
    assert seen["staged"] == {"new.pdf": SOURCE_FILES["attachments/new.pdf"], "new.txt": b"SOURCE-AI"}
    assert seen["same_fs"] is True
    assert seen["dest"] == before
    assert rec.staging() == []


# ── Continuing without a safety archive ──────────────────────────────────────

@pytest.mark.parametrize("kind", ["local", "cloud"])
async def test_recovery_no_safety_confirmation_bound_to_digest(rec, tmp_path, kind):
    """Continuing without a safety archive needs the exact staged archive's digest, and a staged
    archive that changed since it was confirmed is refused."""
    from celerp.services import backup_import
    rec.fail_safety()
    archive = _archive(tmp_path / "src.celerp-backup")
    first = await rec.start(kind, archive, tmp_path)
    assert first.needs_confirmation is True

    refused = await backup_import.continue_recovery(first.confirmation_id, "0" * 64)
    assert refused.ok is False and refused.needs_confirmation is False
    assert rec.restored == []

    done = await backup_import.continue_recovery(first.confirmation_id, first.archive_digest)
    assert done.ok is True, done.error
    assert rec.restored == [SOURCE_DUMP]
    assert done.safety_archive is None

    second = await rec.start(kind, _archive(tmp_path / "src2.celerp-backup", dump=b"SECOND"), tmp_path)
    assert second.needs_confirmation is True
    staged = next((rec.data / "recovery-staging" / second.confirmation_id).glob("*.celerp-backup"))
    _archive(staged, dump=b"SWAPPED")
    refused = await backup_import.continue_recovery(second.confirmation_id, second.archive_digest)
    assert refused.ok is False
    assert rec.restored == [SOURCE_DUMP]


@pytest.mark.parametrize("kind", ["local", "cloud"])
async def test_recovery_no_safety_confirmation_expires(rec, tmp_path, monkeypatch, kind):
    """A confirmation to continue without a safety archive expires."""
    from celerp.services import backup_import
    rec.fail_safety()
    result = await rec.start(kind, _archive(tmp_path / "src.celerp-backup"), tmp_path)
    assert result.needs_confirmation is True
    later = datetime.now(timezone.utc) + timedelta(minutes=16)
    monkeypatch.setattr(backup_import, "_now", lambda: later)
    refused = await backup_import.continue_recovery(result.confirmation_id, result.archive_digest)
    assert refused.ok is False and "expired" in refused.error.lower()
    assert rec.restored == []
    assert rec.staging() == []


@pytest.mark.parametrize("kind", ["local", "cloud"])
async def test_recovery_no_safety_confirmation_needs_no_reupload(rec, real_client, real_engine, tmp_path, kind):
    """The owner continues from the staged archive: no second upload or download."""
    tok = await _install_owner(real_engine)
    rec.fail_safety()
    archive = _archive(tmp_path / "src.celerp-backup", {"attachments/new.pdf": b"NEW"})
    if kind == "local":
        r = await real_client.post("/backup/import", files={"file": ("src.celerp-backup", archive.read_bytes())},
                                   headers=auth(tok))
    else:
        rec.cloud(archive, tmp_path)
        r = await real_client.post("/backup/restore/snap-1", headers=auth(tok))
    assert r.status_code == 200, r.text
    assert SAFETY_WARNING in r.text
    assert "Import failed" not in r.text and "Restore failed" not in r.text
    vals = _continue_vals(r.text)
    assert rec.restored == []

    r = await real_client.post("/backup/import/continue", data=vals, headers=auth(tok))
    assert r.status_code == 200, r.text
    assert "Database restored from the recovery point." in r.text
    assert r.headers.get("X-Session-Ended") == "1"
    assert rec.restored == [SOURCE_DUMP]
    assert rec.tree("attachments") == {"new.pdf": b"NEW"}
    assert rec.reassembled == (1 if kind == "cloud" else 0)


# ── One commit engine ────────────────────────────────────────────────────────

async def test_cloud_and_local_recovery_share_commit_engine(rec, tmp_path, monkeypatch):
    """Local upload, cloud recovery point and bootstrap restore all run the one commit engine."""
    from celerp.services import backup_import, backup_repo
    commit = backup_import.commit_recovery
    seen: list[tuple[str, bool]] = []

    async def _commit(prepared, safety):
        seen.append((prepared.digest, safety is not None))
        return await commit(prepared, safety)

    monkeypatch.setattr(backup_import, "commit_recovery", _commit)
    local = _archive(tmp_path / "local.celerp-backup", dump=b"LOCAL")
    cloud = _archive(tmp_path / "cloud.celerp-backup", dump=b"CLOUD")
    boot = _archive(tmp_path / "boot.celerp-backup", dump=b"BOOT")
    digests = [_sha(local), _sha(cloud), _sha(boot)]
    assert (await backup_import.run_recovery(local)).ok
    rec.cloud(cloud, tmp_path)
    assert (await backup_repo.restore_snapshot("snap-1")).ok
    assert (await backup_import.bootstrap_recovery(boot)).ok
    assert seen == [(digests[0], True), (digests[1], True), (digests[2], False)]
    assert rec.restored == [b"LOCAL", b"CLOUD", b"BOOT"]


async def test_bootstrap_restore_needs_no_safety_archive(rec, real_client, code_config, tmp_path):
    """Restoring into a fresh installation makes no safety archive."""
    archive = _archive(tmp_path / "boot.celerp-backup", {"attachments/new.pdf": b"NEW"})
    r = await real_client.post("/backup/import-bootstrap", files={"file": ("boot.celerp-backup", archive.read_bytes())},
                               data={"setup_code": code_config})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and "schema_warning" not in body
    assert "safety_dump" not in rec.names()
    assert rec.safety_archives() == []
    assert rec.restored == [SOURCE_DUMP]
    assert rec.tree("attachments") == {"new.pdf": b"NEW"}


# ── Failures after the safety archive ────────────────────────────────────────

async def test_recovery_pg_restore_failure_reports_failure(rec, tmp_path):
    """A failed pg_restore is a failure naming the safety archive; no file or module is replaced."""
    from celerp.services import backup_import
    rec.seed()
    before, modules = rec.trees(), _enabled()
    rec.pg_error = RuntimeError("pg_restore exited with status 1")
    result = await backup_import.run_recovery(_archive(tmp_path / "src.celerp-backup", SOURCE_FILES,
                                                       modules=["celerp-labels"]))
    assert result.ok is False
    assert "pg_restore exited with status 1" in result.error
    assert result.safety_archive and result.safety_archive in result.error
    assert Path(result.safety_archive).is_file()
    assert rec.trees() == before and _enabled() == modules
    assert "end_sessions" not in rec.names()
    assert rec.staging() == []


async def test_recovery_schema_reconcile_failure_reported(rec, tmp_path, monkeypatch):
    """A schema that cannot be brought up to date after the restore is a failure naming the safety archive."""
    from celerp import cli
    from celerp.services import backup_import

    def _migrate(url):
        raise RuntimeError("DuplicateColumn: column already exists")

    monkeypatch.setattr(backup_import, "_reconcile_schema", rec.real_reconcile)
    monkeypatch.setattr(cli, "_migration_lock", lambda url: contextlib.nullcontext())
    monkeypatch.setattr(cli, "_apply_migrations", _migrate)
    rec.seed()
    before = rec.trees()
    result = await backup_import.run_recovery(_archive(tmp_path / "src.celerp-backup", SOURCE_FILES))
    assert result.ok is False
    assert "DuplicateColumn" in result.error
    assert result.safety_archive and result.safety_archive in result.error
    assert Path(result.safety_archive).is_file()
    assert rec.trees() == before
    assert not (rec.data / "restore-notice.json").exists()


async def test_recovery_real_database_replaces_installation(tmp_path, monkeypatch, code_config, real_engine,
                                                            real_client):
    """A real dump restored through System Recovery replaces the whole database, and a
    session from before the recovery no longer signs in."""
    from sqlalchemy import text

    from celerp.services import backup_export, backup_import
    _set_enabled(["celerp-inventory"])
    rec = _Recovery(tmp_path, monkeypatch, real_database=True)
    user = await owner(real_engine)
    alpha = await company(real_engine, user, "Alpha Trading", "alpha")
    tok = await token(real_engine, user, alpha)
    source = await backup_export.export_full()
    try:
        await company(real_engine, user, "Beta Trading", "beta")
        assert (await real_client.get("/companies/me", headers=auth(tok))).status_code == 200
        result = await backup_import.run_recovery(source)
    finally:
        source.unlink(missing_ok=True)
    assert result.ok is True, result.error
    async with real_engine.connect() as conn:
        names = {r[0] for r in await conn.execute(text("SELECT name FROM companies"))}
    assert names == {"Alpha Trading"}
    assert len(rec.safety_archives()) == 1
    assert (await real_client.get("/companies/me", headers=auth(tok))).status_code == 401


# ── A failed replacement is undone, or the installation stays closed ─────────

def _fail_once(monkeypatch, owner, name: str, label: str, *, after: bool = False) -> list[str]:
    """Make ``owner.name`` raise *label* the first time it is called (after running, when *after*)."""
    import inspect
    real = getattr(owner, name)
    failed: list[str] = []

    def _raise():
        failed.append(label)
        raise RuntimeError(f"{label} broke")

    if inspect.iscoroutinefunction(real):
        async def _wrapped(*a, **kw):
            if failed:
                return await real(*a, **kw)
            if after:
                await real(*a, **kw)
            _raise()
    else:
        def _wrapped(*a, **kw):
            if failed:
                return real(*a, **kw)
            if after:
                real(*a, **kw)
            _raise()
    monkeypatch.setattr(owner, name, _wrapped)
    return failed


def _inject(monkeypatch, boundary: str) -> list[str]:
    from celerp import config
    from celerp.services import backup_import, session_tracker
    target = {
        "pg_restore": (backup_import, "_run_pg_restore", False),
        "schema": (backup_import, "_reconcile_schema", False),
        "connector_cleanup": (backup_import, "_clear_restored_connector_state", True),
        "session_rotation": (session_tracker, "end_all_sessions", True),
        "root_swap": (backup_import, "_swap_roots", False),
        "module_application": (config, "replace_enabled_modules", True),
    }[boundary]
    return _fail_once(monkeypatch, target[0], target[1], boundary, after=target[2])


BOUNDARIES = ["pg_restore", "schema", "connector_cleanup", "session_rotation", "root_swap",
              "module_application"]


@pytest.mark.parametrize("boundary", BOUNDARIES)
async def test_failed_recovery_puts_installation_back(rec, tmp_path, monkeypatch, real_engine, boundary):
    """A recovery failing at any step after the remote connectors were revoked puts the
    database, files and modules back from the safety archive, clears the revoked
    connectors, ends every session, and leaves the installation open."""
    from celerp.services import backup_import
    user = await owner(real_engine)
    await company(real_engine, user, "Alpha Trading", "alpha", settings={"enabled_modules": ["celerp-inventory"]})
    rec.seed()
    before, modules = rec.trees(), _enabled()
    failed = _inject(monkeypatch, boundary)
    result = await backup_import.run_recovery(_archive(tmp_path / "src.celerp-backup", SOURCE_FILES,
                                                       modules=["celerp-labels"]))
    assert failed == [boundary]
    assert result.ok is False
    assert f"{boundary} broke" in result.error and "put back" in result.error
    assert result.safety_archive and result.safety_archive in result.error
    assert rec.restored[-1] == SAFETY_DUMP
    names = rec.names()
    last_restore = len(names) - 1 - names[::-1].index("pg_restore")
    assert {"clear_connectors", "end_sessions"} <= set(names[last_restore:])
    assert rec.trees() == before and _enabled() == modules
    assert backup_import.recovery_incomplete() is False
    assert rec.staging() == []
    assert not (rec.data / "restore-notice.json").exists()


RELAY = "https://relay.test"


class _Relay:
    """The Connect relay's connections for this installation and the store's webhooks.

    ``plan[connector]`` scripts each DELETE in turn: ``"fail"`` answers 500 without
    revoking, ``"lost"`` revokes and then loses the response, ``"timeout"`` loses the
    request before it is applied. Unscripted DELETEs behave as the relay does."""

    def __init__(self, monkeypatch, connectors):
        import httpx
        import respx

        from celerp.connectors import remote_state
        from celerp.gateway import state
        self.live = {c: f"{c}-rev-1" for c in connectors}
        self.webhooks: set[str] = set()
        self.plan: dict[str, list[str]] = {}
        self.deletes: list[tuple[str, str]] = []
        monkeypatch.setattr(state, "relay_http_url", lambda: RELAY)
        monkeypatch.setattr(state, "relay_session_headers", lambda: {"X-Session-Token": "s"})

        async def _remove_webhooks(company_id, webhook_ids, *, force):
            self.webhooks.difference_update(webhook_ids)

        monkeypatch.setattr(remote_state, "_remove_woocommerce_webhooks", _remove_webhooks)
        self.router = respx.mock(assert_all_called=False)
        self.router.get(url__regex=rf"{RELAY}/tokens/(?P<name>[\w-]+)/revision").mock(
            side_effect=lambda request, name: (
                httpx.Response(200, json={"revision": self.live[name]}) if name in self.live
                else httpx.Response(404)))

        def _delete(request, name):
            revision = request.headers["X-Celerp-Connector-Revision"]
            self.deletes.append((name, revision))
            step = (self.plan.get(name) or [None]).pop(0) if self.plan.get(name) else None
            if step == "fail":
                return httpx.Response(500)
            if step == "timeout":
                raise httpx.ReadTimeout("lost", request=request)
            if name not in self.live:
                return httpx.Response(404)
            if revision != self.live[name]:
                return httpx.Response(409)
            del self.live[name]
            if step == "lost":
                raise httpx.ReadTimeout("lost", request=request)
            return httpx.Response(200, json={"revoked": True})

        self.router.delete(url__regex=rf"{RELAY}/tokens/(?P<name>[\w-]+)$").mock(side_effect=_delete)
        self.router.start()

    def stop(self):
        self.router.stop()


@pytest.fixture
def relay_connectors(rec, monkeypatch, real_engine):
    """Three connected connectors, each with a live relay connection; WooCommerce also
    has two store webhooks. The real connector steps run against the fake relay."""
    from celerp.services import backup_import, session_tracker
    relay = _Relay(monkeypatch, ["shopify", "woocommerce", "quickbooks"])
    relay.webhooks = {"w1", "w2"}
    monkeypatch.setattr(backup_import, "_reconcile_connectors", _real_revoke)
    monkeypatch.setattr(backup_import, "_clear_restored_connector_state", _real_clear)
    monkeypatch.setattr(session_tracker, "end_all_sessions", _real_end_sessions)
    yield relay
    relay.stop()


async def _connect_three(engine) -> str:
    from sqlalchemy import text
    user = await owner(engine)
    cid = await company(engine, user, "Alpha Trading", "alpha", settings={"enabled_modules": ["celerp-inventory"]})
    async with engine.begin() as conn:
        for connector in ("shopify", "woocommerce", "quickbooks"):
            await conn.execute(text(
                "INSERT INTO connector_configs (company_id, connector, direction, sync_frequency, "
                "daily_sync_hour, webhook_ids_json) VALUES (:c, :n, 'both', 'realtime', 2, :w)"),
                {"c": str(cid), "n": connector, "w": '["w1", "w2"]' if connector == "woocommerce" else "[]"})
    return str(cid)


async def _local_connectors(engine) -> list[str]:
    from sqlalchemy import text
    async with engine.connect() as conn:
        return sorted((await conn.execute(text("SELECT connector FROM connector_configs"))).scalars().all())


def _marked_connectors() -> list[dict] | None:
    from celerp.services import backup_import
    if not backup_import.recovery_incomplete():
        return None
    return json.loads(backup_import._marker_path().read_text())["connectors"]


async def test_connector_revoke_failure_does_not_abandon_the_connectors_after_it(
        rec, tmp_path, relay_connectors, real_engine):
    """The second of three connectors cannot be revoked: the third is still revoked,
    the second stays recorded for the next attempt with the connection it was sent
    for, and nothing local is replaced while it is outstanding."""
    from celerp.services import backup_import
    await _connect_three(real_engine)
    rec.seed()
    before = rec.trees()
    relay_connectors.plan["woocommerce"] = ["fail"] * 4

    result = await backup_import.run_recovery(_archive(tmp_path / "src.celerp-backup", SOURCE_FILES))

    assert result.ok is False and "restart Celerp" in result.error
    assert set(relay_connectors.live) == {"woocommerce"}
    assert relay_connectors.webhooks == set()
    marked = _marked_connectors()
    assert [(e["connector"], e["revision"]) for e in marked] == [("woocommerce", "woocommerce-rev-1")]
    assert rec.restored == [] and rec.trees() == before
    assert await _local_connectors(real_engine) == ["quickbooks", "shopify", "woocommerce"]


async def test_lost_disconnect_response_is_retried_as_the_same_disconnect(
        rec, tmp_path, relay_connectors, real_engine):
    """A disconnect whose response is lost is retried for the connection it was sent
    for: once applied it is confirmed gone, and one lost before it was applied is sent
    again with the same revision. The recovery then completes."""
    from celerp.services import backup_import
    await _connect_three(real_engine)
    rec.seed()
    relay_connectors.plan = {"shopify": ["lost"], "quickbooks": ["timeout"]}

    result = await backup_import.run_recovery(_archive(tmp_path / "src.celerp-backup", SOURCE_FILES))

    assert result.ok is True, result.error
    assert relay_connectors.live == {} and relay_connectors.webhooks == set()
    assert [d for d in relay_connectors.deletes if d[0] != "woocommerce"] == [
        ("shopify", "shopify-rev-1"), ("quickbooks", "quickbooks-rev-1"), ("quickbooks", "quickbooks-rev-1")]
    assert backup_import.recovery_incomplete() is False
    assert await _local_connectors(real_engine) == []


async def test_rollback_after_partial_revoke_leaves_no_remote_connection_behind(
        rec, tmp_path, relay_connectors, real_engine):
    """Revocation fails part way and the recovery is put back from the safety archive:
    the put-back revokes what was left first, so once the restored local connector
    configs are cleared no relay connection or store webhook outlives them."""
    from celerp.services import backup_import
    await _connect_three(real_engine)
    rec.seed()
    relay_connectors.plan["woocommerce"] = ["fail", "fail"]

    result = await backup_import.run_recovery(_archive(tmp_path / "src.celerp-backup", SOURCE_FILES))

    assert result.ok is False and "put back" in result.error, result.error
    assert rec.restored == [SAFETY_DUMP]
    assert relay_connectors.live == {} and relay_connectors.webhooks == set()
    assert await _local_connectors(real_engine) == []
    assert backup_import.recovery_incomplete() is False


async def test_marker_stays_while_a_connector_outcome_is_unconfirmed(
        rec, tmp_path, relay_connectors, real_engine):
    """While any connector's disconnect stays unconfirmed the recovery marker stays,
    across restarts; the start that confirms it puts the installation back and only
    then clears the marker."""
    from celerp.services import backup_import
    await _connect_three(real_engine)
    rec.seed()
    relay_connectors.plan["quickbooks"] = ["timeout"] * 6

    result = await backup_import.run_recovery(_archive(tmp_path / "src.celerp-backup", SOURCE_FILES))
    assert result.ok is False and "restart Celerp" in result.error
    await backup_import.finish_incomplete_recovery()
    assert [e["connector"] for e in _marked_connectors()] == ["quickbooks"]
    assert rec.restored == []

    await backup_import.finish_incomplete_recovery()
    assert backup_import.recovery_incomplete() is False
    assert relay_connectors.live == {}
    assert {d[1] for d in relay_connectors.deletes if d[0] == "quickbooks"} == {"quickbooks-rev-1"}
    assert rec.restored == [SAFETY_DUMP]
    assert await _local_connectors(real_engine) == []


async def test_recovery_that_cannot_be_undone_keeps_installation_closed(rec, tmp_path, monkeypatch,
                                                                         real_client, real_engine):
    """When the safety archive cannot be put back either, nothing but the health probes are
    served, not even a session from before; the next start puts the installation back."""
    from celerp.services import backup_import
    rec.seed()
    before, modules = rec.trees(), _enabled()
    tok = await _install_owner(real_engine)
    real_restore = backup_import._run_pg_restore
    broken = [True]

    async def _restore(dump, url):
        await real_restore(dump, url)
        if broken:
            raise RuntimeError("disk full")

    monkeypatch.setattr(backup_import, "_run_pg_restore", _restore)
    result = await backup_import.run_recovery(_archive(tmp_path / "src.celerp-backup", SOURCE_FILES))
    assert result.ok is False and "restart Celerp" in result.error
    assert backup_import.recovery_incomplete() is True
    assert (await real_client.get("/companies/me", headers=auth(tok))).status_code == 503
    assert (await real_client.get("/health")).status_code == 200

    broken.clear()
    await backup_import.finish_incomplete_recovery()
    assert backup_import.recovery_incomplete() is False
    assert rec.restored[-1] == SAFETY_DUMP
    assert rec.trees() == before and _enabled() == modules


async def test_recovery_without_safety_archive_is_finished_at_next_start(rec, tmp_path, monkeypatch):
    """With no safety archive to go back to, a failed recovery keeps the installation closed
    and the next start finishes restoring the archive the owner chose."""
    from celerp.services import backup_import
    rec.seed()
    _inject(monkeypatch, "root_swap")
    rec.fail_safety()
    first = await backup_import.run_recovery(_archive(tmp_path / "src.celerp-backup", SOURCE_FILES))
    result = await backup_import.continue_recovery(first.confirmation_id, first.archive_digest)
    assert result.ok is False and "restart Celerp" in result.error
    assert backup_import.recovery_incomplete() is True

    await backup_import.finish_incomplete_recovery()
    assert backup_import.recovery_incomplete() is False
    assert rec.restored[-1] == SOURCE_DUMP
    assert rec.tree("ai_uploads") == {"new.txt": b"SOURCE-AI"}
    assert list((rec.data / "recovery-safety").glob("unfinished-*")) == []


async def test_failed_recovery_does_not_revive_sessions(tmp_path, monkeypatch, code_config, real_engine,
                                                        real_client):
    """A session valid in the restored database does not sign in after the recovery
    failed and the installation was put back."""
    from celerp.services import backup_export, backup_import
    _set_enabled(["celerp-inventory"])
    rec = _Recovery(tmp_path, monkeypatch, real_database=True)
    user = await owner(real_engine)
    alpha = await company(real_engine, user, "Alpha Trading", "alpha")
    tok = await token(real_engine, user, alpha)
    source = await backup_export.export_full()
    try:
        failed = _inject(monkeypatch, "schema")
        result = await backup_import.run_recovery(source)
    finally:
        source.unlink(missing_ok=True)
    assert failed == ["schema"] and result.ok is False
    assert len(rec.safety_archives()) == 1
    assert (await real_client.get("/companies/me", headers=auth(tok))).status_code == 401
    assert backup_import.recovery_incomplete() is False


# ---------------------------------------------------------------------------
# An unfinished recovery is resolved before normal startup touches the database
# ---------------------------------------------------------------------------


async def _break_schema_init(engine) -> str:
    """Leave *engine*'s database where the current metadata cannot be created on it,
    as an interrupted pg_restore can: a table is gone and a relation holds the
    name of that table's index. Returns the blocking relation's name."""
    from sqlalchemy import text

    from celerp.models.base import Base
    table = next(t for t in Base.metadata.sorted_tables if t.indexes)
    index = sorted(i.name for i in table.indexes)[0]
    async with engine.begin() as conn:
        await conn.execute(text(f'DROP TABLE "{table.name}" CASCADE'))
        await conn.execute(text(f'CREATE TABLE "{index}" (x int)'))
    with pytest.raises(Exception):
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    return index


def _boot_to_schema(monkeypatch, engine):
    """Run the real lifespan on *engine* only as far as schema initialization."""
    import celerp.main as main_mod
    from celerp import runtime

    monkeypatch.setattr(main_mod, "lifecycle_engine", engine)
    monkeypatch.setattr(main_mod, "_MODULE_DIR", None)
    monkeypatch.setenv(runtime.UPDATE_VERIFY_ENV, "1")
    verified = AsyncMock()
    monkeypatch.setattr(main_mod, "_verify_runtime_dependencies", verified)
    return main_mod, verified


async def test_boot_finishes_unfinished_recovery_before_schema_init(rec, tmp_path, monkeypatch, committed_engine):
    """A recovery marker over a database the current schema cannot be created on: the
    recovery puts the installation back first, then startup initializes the schema on
    the whole restored database and comes up."""
    from sqlalchemy import text

    from celerp.models.base import Base
    from celerp.services import backup_import
    rec.seed()
    blocker = await _break_schema_init(committed_engine)
    safety = _archive(tmp_path / "safety.celerp-backup", SOURCE_FILES)
    backup_import._mark_recovery_started(safety, [])
    stub_restore = backup_import._run_pg_restore

    async def _restore(dump, url):
        await stub_restore(dump, url)
        async with committed_engine.begin() as conn:
            await conn.execute(text(f'DROP TABLE "{blocker}"'))

    monkeypatch.setattr(backup_import, "_run_pg_restore", _restore)
    main_mod, verified = _boot_to_schema(monkeypatch, committed_engine)

    async with main_mod.lifespan(None):
        pass

    assert rec.names()[:4] == ["guard", "revoke", "pg_restore", "reconcile"]
    assert rec.restored == [SOURCE_DUMP]
    assert backup_import.recovery_incomplete() is False
    assert rec.tree("ai_uploads") == {"new.txt": b"SOURCE-AI"}
    verified.assert_awaited_once()
    async with committed_engine.connect() as conn:
        tables = set(await conn.run_sync(lambda c: __import__("sqlalchemy").inspect(c).get_table_names()))
    assert set(Base.metadata.tables) <= tables


async def test_boot_with_recovery_still_unfinished_starts_nothing_else(rec, tmp_path, monkeypatch, committed_engine):
    """When the recovery cannot be finished at startup the marker stays, the database
    is left exactly as it was for the next start, and nothing else starts: only the
    liveness and readiness probes answer."""
    from sqlalchemy import text

    from celerp.services import backup_import
    rec.seed()
    blocker = await _break_schema_init(committed_engine)
    backup_import._mark_recovery_started(_archive(tmp_path / "safety.celerp-backup", SOURCE_FILES), [])
    rec.pg_error = RuntimeError("disk full")
    main_mod, verified = _boot_to_schema(monkeypatch, committed_engine)

    async with main_mod.lifespan(None):
        pass

    assert backup_import.recovery_incomplete() is True
    verified.assert_not_awaited()
    async with committed_engine.connect() as conn:
        still = (await conn.execute(text("SELECT to_regclass(:n) IS NOT NULL"), {"n": f'"{blocker}"'})).scalar_one()
    assert still


async def test_migrate_leaves_a_database_under_unfinished_recovery_to_the_recovery(tmp_path, monkeypatch):
    """`celerp migrate` and `celerp start` run no migration on a database whose
    recovery is unfinished; the server's startup recovery brings it to a whole state."""
    from celerp import cli
    from celerp.config import settings
    from celerp.services import backup_import

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    backup_import._mark_recovery_started(tmp_path / "safety.celerp-backup", [])
    ran = []
    monkeypatch.setattr(cli, "_run_migrations", lambda url: ran.append(url))
    monkeypatch.setattr(cli, "_migration_lock", lambda url: contextlib.nullcontext())
    monkeypatch.setattr(cli, "_stamped_revision", lambda url: None)
    monkeypatch.setattr(cli, "_post_migration_grants", lambda url: ran.append(url))
    monkeypatch.setattr(cli, "_reconcile_after_migrate", lambda url: ran.append(url))

    cli._migrate_to_head("postgresql://x/y")
    assert ran == []

    backup_import._mark_recovery_finished()
    cli._migrate_to_head("postgresql://x/y")
    assert ran == ["postgresql://x/y"] * 3


def test_migrate_does_not_report_done_while_the_recovery_is_unfinished(tmp_path, monkeypatch):
    """`celerp migrate` under an unfinished recovery says the database waits for it and
    stops there, with no "Done" line. It still exits 0: the desktop launcher runs it
    before the server, and the server is what finishes the recovery."""
    from click.testing import CliRunner

    from celerp import cli
    from celerp.config import settings
    from celerp.services import backup_import

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    backup_import._mark_recovery_started(tmp_path / "safety.celerp-backup", [])
    monkeypatch.setattr(cli, "_run_migrations", lambda url: pytest.fail("migrated mid recovery"))

    result = CliRunner().invoke(cli.main, ["migrate", "--db-url", "postgresql://x/y"])
    assert result.exit_code == 0, result.output
    assert "System Recovery unfinished" in result.output
    assert "Done" not in result.output


# ── Backups and recoveries from a newer Celerp ───────────────────────────────

@pytest.fixture
def running_254(monkeypatch):
    import celerp
    monkeypatch.setattr(celerp, "__version__", "2.5.4")


@pytest.mark.parametrize("entry", ["recovery", "confirmed recovery", "bootstrap recovery"])
async def test_every_recovery_checks_its_backup_again_once_locked(rec, tmp_path, monkeypatch, entry):
    """What the database lets Celerp install can change while a backup waits to be restored,
    so its dump is checked again under the locks, before the marker and the first revoke."""
    from celerp.services import backup, backup_import
    archive = _archive(tmp_path / "src.celerp-backup")
    monkeypatch.setattr(backup, "check_backup_dump", lambda path, url: rec.record(f"check {Path(path).parent.name}"))
    real_mark = backup_import._mark_recovery_started
    monkeypatch.setattr(backup_import, "_mark_recovery_started",
                        lambda *a: (rec.record("marker"), real_mark(*a))[1])
    if entry == "bootstrap recovery":
        result = await backup_import.bootstrap_recovery(archive)
    else:
        if entry == "confirmed recovery":
            rec.fail_safety()
        result = await backup_import.run_recovery(archive)
        if entry == "confirmed recovery":
            result = await backup_import.continue_recovery(result.confirmation_id, result.archive_digest)
    assert result.ok is True, result.error
    names = rec.names()
    staged = next(name for name in names if name.startswith("check "))
    locked = [i for i, (name, _, guard) in enumerate(rec.calls) if name == staged and guard]
    assert locked and locked[-1] < names.index("marker") < names.index("revoke"), rec.calls


@pytest.mark.parametrize("kind", ["local", "cloud"])
async def test_newer_backup_refused_before_any_recovery_step(rec, tmp_path, running_254, kind):
    """A backup made by a newer Celerp is refused before the safety archive, the recovery
    marker, the connector revoke, pg_restore or any file swap."""
    from celerp.services import backup_import
    rec.seed()
    before, modules = rec.trees(), _enabled()
    archive = _archive(tmp_path / "newer.celerp-backup", SOURCE_FILES, version="2.6.0")
    result = await rec.start(kind, archive, tmp_path)
    assert result.ok is False and result.needs_confirmation is False
    assert "made with Celerp 2.6.0, which is newer than this copy (2.5.4)" in result.error
    assert "Update Celerp, then restore it." in result.error
    assert set(rec.names()) <= {"guard"}
    rec.cloud_snapshot.assert_not_awaited()
    assert rec.safety_archives() == [] and rec.staging() == []
    assert backup_import.recovery_incomplete() is False
    assert rec.trees() == before and _enabled() == modules


async def test_newer_backup_refused_on_a_fresh_installation(rec, real_client, code_config, tmp_path, running_254):
    """Restoring into a fresh installation refuses a newer backup the same way."""
    from celerp.services import backup_import
    archive = _archive(tmp_path / "boot.celerp-backup", {"attachments/new.pdf": b"NEW"}, version="3.0.0")
    r = await real_client.post("/backup/import-bootstrap", files={"file": ("boot.celerp-backup", archive.read_bytes())},
                               data={"setup_code": code_config})
    assert r.status_code != 200 or r.json()["ok"] is False, r.text
    assert "Update Celerp, then restore it." in r.text
    assert set(rec.names()) <= {"guard"} and rec.restored == []
    assert backup_import.recovery_incomplete() is False
    assert rec.tree("attachments") == {}


@pytest.mark.parametrize("version", ["2.5.4", "2.4.0", None])
async def test_older_or_same_backup_restored_and_migrated(rec, tmp_path, running_254, version):
    """A backup from this or an older Celerp (or one that never recorded its version) is
    restored and its schema brought up to date."""
    from celerp.services import backup_import
    rec.seed()
    result = await backup_import.run_recovery(_archive(tmp_path / "older.celerp-backup", SOURCE_FILES,
                                                       version=version))
    assert result.ok, result.error
    assert rec.restored[-1] == SOURCE_DUMP
    assert rec.names().index("pg_restore") < rec.names().index("reconcile")
    assert rec.tree("ai_uploads") == {"new.txt": b"SOURCE-AI"}


def _leave_unfinished(target: Path, version: str | None) -> None:
    """A recovery marker as a copy of *version* leaves it when a recovery does not finish
    (None: a marker written before markers recorded their version)."""
    from celerp.services import backup_import
    state = {"target": str(target), "connectors": []}
    if version is not None:
        state["celerp_version"] = version
    backup_import._write_marker(state)


async def test_recovery_marker_records_the_version(rec, tmp_path, monkeypatch, running_254):
    from celerp.services import backup_import
    _inject(monkeypatch, "root_swap")
    rec.fail_safety()
    first = await backup_import.run_recovery(_archive(tmp_path / "src.celerp-backup", SOURCE_FILES))
    result = await backup_import.continue_recovery(first.confirmation_id, first.archive_digest)
    assert result.ok is False and backup_import.recovery_incomplete() is True
    assert json.loads(backup_import._marker_path().read_text())["celerp_version"] == "2.5.4"


@pytest.mark.parametrize("marker_version,archive_version", [("2.6.0", None), ("2.5.4", "2.6.0"),
                                                            ("garbage", None)])
async def test_older_copy_never_resumes_a_newer_copys_recovery(rec, tmp_path, running_254,
                                                               marker_version, archive_version):
    """A recovery a newer Celerp started, or one whose archive a newer Celerp made, is left
    for that version to finish: nothing is revoked, restored or swapped, and the
    installation stays closed."""
    from celerp.services import backup_import
    rec.seed()
    before = rec.trees()
    _leave_unfinished(_archive(tmp_path / "target.celerp-backup", SOURCE_FILES, version=archive_version),
                      marker_version)
    await backup_import.finish_incomplete_recovery()
    assert backup_import.recovery_incomplete() is True
    assert not {"revoke", "pg_restore", "reconcile", "clear_connectors"} & set(rec.names())
    assert rec.trees() == before and rec.staging() == []


@pytest.mark.parametrize("marker_version", ["2.5.4", "2.4.0", None])
async def test_own_or_older_copys_recovery_is_resumed(rec, tmp_path, running_254, marker_version):
    from celerp.services import backup_import
    rec.seed()
    _leave_unfinished(_archive(tmp_path / "target.celerp-backup", SOURCE_FILES, version="2.5.4"),
                      marker_version)
    await backup_import.finish_incomplete_recovery()
    assert backup_import.recovery_incomplete() is False
    assert rec.names().index("revoke") < rec.names().index("pg_restore")
    assert rec.restored[-1] == SOURCE_DUMP
    assert rec.tree("ai_uploads") == {"new.txt": b"SOURCE-AI"}


# ── A recovery replaces the whole database ───────────────────────────────────

async def _execute(engine, sql: str) -> None:
    from sqlalchemy import text
    async with engine.begin() as conn:
        await conn.execute(text(sql))


async def _tables(engine) -> set[str]:
    from sqlalchemy import text
    async with engine.connect() as conn:
        return {r[0] for r in await conn.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))}


async def _company_names(engine) -> set[str]:
    from sqlalchemy import text
    async with engine.connect() as conn:
        return {r[0] for r in await conn.execute(text("SELECT name FROM companies"))}


async def test_recovery_into_a_database_with_tables_the_backup_lacks(tmp_path, monkeypatch, code_config,
                                                                     real_engine):
    """An older backup has no table a newer release added; the newer table references
    one the backup restores, and the recovery still replaces the database."""
    from celerp.services import backup_export, backup_import
    _set_enabled(["celerp-inventory"])
    _Recovery(tmp_path, monkeypatch, real_database=True)
    user = await owner(real_engine)
    await company(real_engine, user, "Alpha Trading", "alpha")
    source = await backup_export.export_full()
    await _execute(real_engine, "CREATE TABLE zz_newer (company_id uuid REFERENCES companies(id))")
    await _execute(real_engine, "INSERT INTO zz_newer SELECT id FROM companies")
    result = await backup_import.run_recovery(source)
    assert result.ok is True, result.error
    assert "zz_newer" not in await _tables(real_engine)
    assert await _company_names(real_engine) == {"Alpha Trading"}


async def test_failed_recovery_of_a_backup_with_extra_tables_is_put_back(tmp_path, monkeypatch, code_config,
                                                                          real_engine):
    """A backup holding a module table this installation lacks fails after its restore;
    putting the installation back from the safety archive removes that table again."""
    from celerp.services import backup_export, backup_import
    _set_enabled(["celerp-inventory"])
    _Recovery(tmp_path, monkeypatch, real_database=True)
    user = await owner(real_engine)
    await company(real_engine, user, "Alpha Trading", "alpha")
    await _execute(real_engine, "CREATE TABLE zz_module (company_id uuid REFERENCES companies(id))")
    source = await backup_export.export_full()
    await _execute(real_engine, "DROP TABLE zz_module")
    await company(real_engine, user, "Beta Trading", "beta")
    failed = _inject(monkeypatch, "schema")
    result = await backup_import.run_recovery(source)
    assert failed == ["schema"]
    assert result.ok is False and "put back" in result.error, result.error
    assert backup_import.recovery_incomplete() is False
    assert "zz_module" not in await _tables(real_engine)
    assert await _company_names(real_engine) == {"Alpha Trading", "Beta Trading"}


async def test_recovery_stopped_after_the_database_was_emptied_is_finished_at_next_start(
        tmp_path, monkeypatch, code_config, committed_engine):
    """A recovery that stopped between emptying the database and restoring it is finished
    from its marked archive at the next start, not opened as a fresh installation.
    The test empties a database of its own, so a failure leaves the shared one intact."""
    import celerp.db
    from celerp.config import settings
    from celerp.services import backup_export, backup_import
    monkeypatch.setattr(celerp.db, "engine", committed_engine)
    monkeypatch.setattr(celerp.db, "SessionLocal", maker(committed_engine))
    monkeypatch.setattr(settings, "database_url", committed_engine.url.render_as_string(hide_password=False))
    _set_enabled(["celerp-inventory"])
    _Recovery(tmp_path, monkeypatch, real_database=True)
    user = await owner(committed_engine)
    await company(committed_engine, user, "Alpha Trading", "alpha")
    safety = await backup_export.export_full()
    backup_import._mark_recovery_started(safety, [])
    await _execute(committed_engine, "DROP TABLE companies CASCADE")
    await backup_import.finish_incomplete_recovery()
    assert backup_import.recovery_incomplete() is False
    assert await _company_names(committed_engine) == {"Alpha Trading"}


async def test_backup_from_2_5_3_keeps_the_restored_companies_modules(rec, real_engine, tmp_path, running_254):
    """2.5.3 recorded an empty module list when it did not record the set."""
    from celerp.services import backup_import
    _set_enabled(["celerp-inventory", "celerp-contacts"])
    user = await owner(real_engine)
    await company(real_engine, user, "Alpha Trading", "alpha")
    result = await backup_import.run_recovery(_archive(tmp_path / "v253.celerp-backup", modules=[], version="2.5.3"))
    assert result.ok is True, result.error
    assert set(_enabled()) == set(_closure(["celerp-inventory", "celerp-contacts"]))


async def test_backup_with_no_modules_enables_no_modules(tmp_path, monkeypatch, code_config, real_engine):
    from celerp.services import backup_export, backup_import
    _set_enabled(["celerp-labels"])
    _Recovery(tmp_path, monkeypatch, real_database=True)
    user = await owner(real_engine)
    await company(real_engine, user, "Alpha Trading", "alpha", settings={"enabled_modules": []})
    source = await backup_export.export_full()
    await _execute(real_engine, """UPDATE companies SET settings = '{"enabled_modules": ["celerp-labels"]}'""")
    result = await backup_import.run_recovery(source)
    assert result.ok is True, result.error
    assert _enabled() == []


# Objects a restore would not replace exactly: (create, the name the refusal gives, a lookup
# that holds while they exist, drop).
UNSUPPORTED = {
    "enum": (["CREATE TYPE zz_status AS ENUM ('open', 'done')"], "type zz_status",
             "to_regtype('zz_status') IS NOT NULL", ["DROP TYPE IF EXISTS zz_status"]),
    "domain": (["CREATE DOMAIN zz_qty AS int CHECK (VALUE >= 0)"], "type zz_qty",
               "to_regtype('zz_qty') IS NOT NULL", ["DROP DOMAIN IF EXISTS zz_qty"]),
    "composite type": (["CREATE TYPE zz_pair AS (a int, b int)"], "type zz_pair",
                       "to_regtype('zz_pair') IS NOT NULL", ["DROP TYPE IF EXISTS zz_pair"]),
    "function": (["CREATE FUNCTION zz_one() RETURNS int LANGUAGE sql AS 'SELECT 1'"], "function zz_one()",
                 "to_regprocedure('zz_one()') IS NOT NULL", ["DROP FUNCTION IF EXISTS zz_one()"]),
    "procedure": (["CREATE PROCEDURE zz_noop() LANGUAGE sql AS 'SELECT 1'"], "zz_noop()",
                  "to_regprocedure('zz_noop()') IS NOT NULL", ["DROP PROCEDURE IF EXISTS zz_noop()"]),
    "view": (["CREATE VIEW zz_names AS SELECT name FROM companies"], "view zz_names",
             "to_regclass('zz_names') IS NOT NULL", ["DROP VIEW IF EXISTS zz_names"]),
    "materialized view": (["CREATE MATERIALIZED VIEW zz_one_row AS SELECT 1 AS one"], "materialized view zz_one_row",
                          "to_regclass('zz_one_row') IS NOT NULL", ["DROP MATERIALIZED VIEW IF EXISTS zz_one_row"]),
    "trigger": (["CREATE FUNCTION zz_touch() RETURNS trigger LANGUAGE plpgsql AS 'BEGIN RETURN NEW; END'",
                 "CREATE TRIGGER zz_touch BEFORE UPDATE ON companies FOR EACH ROW EXECUTE FUNCTION zz_touch()"],
                "trigger zz_touch on table companies",
                "EXISTS (SELECT FROM pg_trigger WHERE tgname = 'zz_touch')",
                ["DROP TRIGGER IF EXISTS zz_touch ON companies", "DROP FUNCTION IF EXISTS zz_touch()"]),
    "rule": (["CREATE RULE zz_keep AS ON DELETE TO companies DO INSTEAD NOTHING"], "rule zz_keep on table companies",
             "EXISTS (SELECT FROM pg_rewrite WHERE rulename = 'zz_keep')", ["DROP RULE IF EXISTS zz_keep ON companies"]),
    "schema": (["CREATE SCHEMA zz", "CREATE TABLE zz.jobs (id int)"], "schema zz",
               "to_regclass('zz.jobs') IS NOT NULL", ["DROP SCHEMA IF EXISTS zz CASCADE"]),
    "foreign key from another schema": (
        ["CREATE SCHEMA zz", "CREATE TABLE zz.links (company_id uuid REFERENCES public.companies(id))"],
        "on table zz.links", "to_regclass('zz.links') IS NOT NULL", ["DROP SCHEMA IF EXISTS zz CASCADE"]),
    "view in another schema": (
        ["CREATE SCHEMA zz", "CREATE VIEW zz.names AS SELECT name FROM public.companies"],
        "on view zz.names", "to_regclass('zz.names') IS NOT NULL", ["DROP SCHEMA IF EXISTS zz CASCADE"]),
}


async def _create(engine, kind: str) -> None:
    for sql in UNSUPPORTED[kind][0]:
        await _execute(engine, sql)


async def _drop(engine, kind: str) -> None:
    for sql in UNSUPPORTED[kind][3]:
        await _execute(engine, sql)


async def _exists(engine, kind: str) -> bool:
    from sqlalchemy import text
    async with engine.connect() as conn:
        return (await conn.execute(text(f"SELECT {UNSUPPORTED[kind][2]}"))).scalar()


def _assert_nothing_started(rec: "_Recovery", connector_calls: list[str], staged: list[Path]) -> None:
    from celerp.services import backup_import
    assert backup_import.recovery_incomplete() is False
    assert connector_calls == []
    assert rec.safety_archives() == []
    rec.cloud_snapshot.assert_not_awaited()
    assert rec.staging() == staged


@pytest.mark.parametrize("kind", list(UNSUPPORTED))
async def test_a_recovery_into_a_database_holding_other_objects_changes_nothing(
        tmp_path, monkeypatch, code_config, real_engine, kind):
    """Refused, naming the object, before the safety archive, the marker and any connector
    revoke; once the owner removes it, the same recovery runs."""
    from celerp.services import backup_export, backup_import
    _set_enabled(["celerp-inventory"])
    rec = _Recovery(tmp_path, monkeypatch, real_database=True)
    user = await owner(real_engine)
    await company(real_engine, user, "Alpha Trading", "alpha")
    source = await backup_export.export_full()
    await company(real_engine, user, "Beta Trading", "beta")
    connector_calls = _record_connector_calls(monkeypatch)
    await _create(real_engine, kind)
    try:
        result = await backup_import.run_recovery(source)
        assert result.ok is False and result.error.startswith("System Recovery did not start: "), result.error
        assert UNSUPPORTED[kind][1] in result.error, result.error
        _assert_nothing_started(rec, connector_calls, [])
        assert await _company_names(real_engine) == {"Alpha Trading", "Beta Trading"}
        assert await _exists(real_engine, kind)
    finally:
        await _drop(real_engine, kind)
    result = await backup_import.run_recovery(source)
    assert result.ok is True, result.error
    assert await _company_names(real_engine) == {"Alpha Trading"}


@pytest.mark.parametrize("entry", ["bootstrap recovery", "confirmed recovery"])
async def test_every_recovery_checks_the_database_before_anything_changes(
        tmp_path, monkeypatch, code_config, real_engine, entry):
    from celerp.services import backup_export, backup_import
    _set_enabled(["celerp-inventory"])
    rec = _Recovery(tmp_path, monkeypatch, real_database=True)
    user = await owner(real_engine)
    await company(real_engine, user, "Alpha Trading", "alpha")
    source = await backup_export.export_full()
    await company(real_engine, user, "Beta Trading", "beta")
    connector_calls = _record_connector_calls(monkeypatch)
    if entry == "confirmed recovery":
        rec.fail_safety()
        pending = await backup_import.run_recovery(source)
        assert pending.needs_confirmation is True, pending.error
    await _create(real_engine, "view")
    try:
        if entry == "bootstrap recovery":
            result = await backup_import.bootstrap_recovery(source)
        else:
            result = await backup_import.continue_recovery(pending.confirmation_id, pending.archive_digest)
        assert result.ok is False and "view zz_names" in result.error, result.error
        _assert_nothing_started(rec, connector_calls, [])
        assert await _company_names(real_engine) == {"Alpha Trading", "Beta Trading"}
    finally:
        await _drop(real_engine, "view")


@pytest.mark.parametrize("kind", list(UNSUPPORTED))
async def test_a_backup_holding_other_objects_is_refused_before_anything_changes(
        tmp_path, monkeypatch, code_config, real_engine, kind):
    from celerp.services import backup_export, backup_import
    _set_enabled(["celerp-inventory"])
    rec = _Recovery(tmp_path, monkeypatch, real_database=True)
    user = await owner(real_engine)
    await company(real_engine, user, "Alpha Trading", "alpha")
    await _create(real_engine, kind)
    try:
        source = await backup_export.export_full()
    finally:
        await _drop(real_engine, kind)
    await company(real_engine, user, "Beta Trading", "beta")
    connector_calls = _record_connector_calls(monkeypatch)
    result = await backup_import.run_recovery(source)
    assert result.ok is False, result.error
    assert result.error.startswith("This backup holds database objects Celerp does not restore. Remove them from "
                                   "the database the backup was taken from, then try again: "), result.error
    _assert_nothing_started(rec, connector_calls, [])
    assert await _company_names(real_engine) == {"Alpha Trading", "Beta Trading"}


async def _is_superuser(engine) -> bool:
    from sqlalchemy import text
    async with engine.connect() as conn:
        return (await conn.execute(text("SELECT current_setting('is_superuser') = 'on'"))).scalar()


async def _exists_sql(engine, predicate: str) -> bool:
    from sqlalchemy import text
    async with engine.connect() as conn:
        return (await conn.execute(text(f"SELECT {predicate}"))).scalar()


def _update_steps():
    from celerp.services import update
    from test_helpers import DATABASE_URL
    return update.SupervisorSteps(
        {"server": {"api_port": 1, "ui_port": 2}, "database": {"url": DATABASE_URL}, "backup": {}},
        lambda root: {}, spawn_api=None, spawn_ui=None, wait_ready=None)


@pytest.mark.parametrize("extension", ["pg_trgm", "uuid-ossp"])
@pytest.mark.parametrize("entry", ["recovery", "update rollback"])
async def test_a_backup_using_an_extension_this_database_can_install_is_restored(
        tmp_path, monkeypatch, code_config, real_engine, entry, extension):
    import asyncio

    from celerp.services import backup_export, backup_import
    _set_enabled(["celerp-inventory"])
    _Recovery(tmp_path, monkeypatch, real_database=True)
    user = await owner(real_engine)
    await company(real_engine, user, "Alpha Trading", "alpha")
    await _execute(real_engine, f'CREATE EXTENSION "{extension}"')
    try:
        if entry == "recovery":
            source = await backup_export.export_full()
            await company(real_engine, user, "Beta Trading", "beta")
            result = await backup_import.run_recovery(source)
            assert result.ok is True, result.error
        else:
            steps, dump = _update_steps(), tmp_path / "database.dump"
            await asyncio.to_thread(steps.preflight)
            await asyncio.to_thread(steps.dump, dump)
            await company(real_engine, user, "Beta Trading", "beta")
            await asyncio.to_thread(steps.restore, dump, "1.1.0")
        assert await _company_names(real_engine) == {"Alpha Trading"}
        assert await _exists_sql(real_engine, f"EXISTS (SELECT FROM pg_extension WHERE extname = '{extension}')")
    finally:
        await _execute(real_engine, f'DROP EXTENSION IF EXISTS "{extension}"')


@pytest.mark.parametrize("entry", ["recovery", "update"])
async def test_a_backup_using_an_extension_this_database_cannot_install_is_refused_before_anything_changes(
        tmp_path, monkeypatch, code_config, real_engine, entry):
    """Here the role may not create extensions, so a restore could not put pg_trgm back."""
    from celerp import runtime
    from celerp.services import backup_export, backup_import, update
    if await _is_superuser(real_engine):
        pytest.skip("a superuser can install every available extension")
    _set_enabled(["celerp-inventory"])
    rec = _Recovery(tmp_path, monkeypatch, real_database=True)
    user = await owner(real_engine)
    await company(real_engine, user, "Alpha Trading", "alpha")
    await _execute(real_engine, "CREATE EXTENSION pg_trgm")
    grant = "EXECUTE format('{} CREATE ON DATABASE %I {} CURRENT_USER', current_database())"
    try:
        source = await backup_export.export_full() if entry == "recovery" else None
        await company(real_engine, user, "Beta Trading", "beta")
        await _execute(real_engine, "DO $$ BEGIN " + grant.format("REVOKE", "FROM") + "; END $$")
        refusal = ("This backup uses database extensions that Celerp's database user cannot install here. "
                   "Remove them from {}, or have a database administrator allow that user to install them, "
                   "then try again: pg_trgm")
        if entry == "recovery":
            connector_calls = _record_connector_calls(monkeypatch)
            result = await backup_import.run_recovery(source)
            assert result.ok is False and result.error == refusal.format(
                "the database the backup was taken from"), result.error
            _assert_nothing_started(rec, connector_calls, [])
        else:
            monkeypatch.setenv("CELERP_CONFIG", str(tmp_path / "config.toml"))
            monkeypatch.setattr(update, "installed_version", lambda: "1.0.0")
            steps = _update_steps()
            with pytest.raises(ValueError, match="pg_trgm"):
                steps.preflight()
            result, children = update.run_update("1.1.0", steps)
            assert (result["outcome"], result["reason"], children) == (update.FAILED, "backup_failed", ())
            assert _refusal_shown(update) == refusal.format("this database")
            assert not runtime.release_dir("1.1.0").exists()
            assert "in_progress" not in update.read_state()
        assert await _company_names(real_engine) == {"Alpha Trading", "Beta Trading"}
        assert await _exists_sql(real_engine, "EXISTS (SELECT FROM pg_extension WHERE extname = 'pg_trgm')")
    finally:
        await _execute(real_engine, "DO $$ BEGIN " + grant.format("GRANT", "TO") + "; END $$")
        await _execute(real_engine, "DROP EXTENSION IF EXISTS pg_trgm")


async def test_a_recovery_without_room_for_the_restore_changes_nothing(tmp_path, monkeypatch, code_config, real_engine):
    from celerp.services import backup_export, backup_import
    _set_enabled(["celerp-inventory"])
    rec = _Recovery(tmp_path, monkeypatch, real_database=True)
    user = await owner(real_engine)
    await company(real_engine, user, "Alpha Trading", "alpha")
    source = await backup_export.export_full()
    await company(real_engine, user, "Beta Trading", "beta")
    connector_calls = _record_connector_calls(monkeypatch)
    _free_space(monkeypatch, 2**30)
    result = await backup_import.run_recovery(source)
    assert result.ok is False, result.error
    assert result.error.startswith("Not enough free disk space to restore this backup"), result.error
    _assert_nothing_started(rec, connector_calls, [])
    assert await _company_names(real_engine) == {"Alpha Trading", "Beta Trading"}


async def test_a_safety_archive_that_could_not_be_restored_stops_the_recovery(
        tmp_path, monkeypatch, code_config, real_engine):
    """A publication is no object in the public schema to refuse, but a safety archive
    holding it is not one Celerp could put back."""
    from celerp.services import backup_export, backup_import
    _set_enabled(["celerp-inventory"])
    rec = _Recovery(tmp_path, monkeypatch, real_database=True)
    user = await owner(real_engine)
    await company(real_engine, user, "Alpha Trading", "alpha")
    source = await backup_export.export_full()
    await company(real_engine, user, "Beta Trading", "beta")
    connector_calls = _record_connector_calls(monkeypatch)
    await _execute(real_engine, "CREATE PUBLICATION zz_feed")
    try:
        result = await backup_import.run_recovery(source)
    finally:
        await _execute(real_engine, "DROP PUBLICATION IF EXISTS zz_feed")
    assert result.ok is False, result.error
    assert "The safety backup of this installation could not be restored: " in result.error, result.error
    assert "PUBLICATION - zz_feed" in result.error, result.error
    assert backup_import.recovery_incomplete() is False
    assert connector_calls == []
    rec.cloud_snapshot.assert_not_awaited()
    assert rec.staging() == []
    assert await _company_names(real_engine) == {"Alpha Trading", "Beta Trading"}


async def test_a_recovery_puts_back_every_table_part_exactly(tmp_path, monkeypatch, code_config, real_engine):
    """Keys, unique and check constraints, foreign keys, defaults, identity and owned sequences,
    a standalone sequence, indexes and a partitioned table are a Celerp database's own parts."""
    from sqlalchemy import text
    from celerp.services import backup_export, backup_import
    _set_enabled(["celerp-inventory"])
    _Recovery(tmp_path, monkeypatch, real_database=True)
    user = await owner(real_engine)
    await company(real_engine, user, "Alpha Trading", "alpha")
    parts = """SELECT conrelid::regclass::text || ' ' || pg_get_constraintdef(oid) FROM pg_constraint
                WHERE connamespace = 'public'::regnamespace
               UNION ALL SELECT indexdef FROM pg_indexes WHERE schemaname = 'public'
               UNION ALL SELECT format('%s.%s %s %s', table_name, column_name, column_default, is_identity)
                 FROM information_schema.columns WHERE table_schema = 'public'
               UNION ALL SELECT format('%s %s', sequencename, last_value) FROM pg_sequences WHERE schemaname = 'public'"""
    for sql in ["CREATE SEQUENCE zz_numbers START 40",
                "CREATE TABLE zz_parts (id int GENERATED ALWAYS AS IDENTITY PRIMARY KEY, n serial,"
                " code text UNIQUE, qty int CHECK (qty >= 0) DEFAULT nextval('zz_numbers'),"
                " company_id uuid REFERENCES companies(id) ON DELETE CASCADE)",
                "CREATE INDEX zz_parts_qty ON zz_parts (qty)",
                "CREATE TABLE zz_log (at int, note text) PARTITION BY RANGE (at)",
                "CREATE TABLE zz_log_1 PARTITION OF zz_log FOR VALUES FROM (0) TO (100)",
                "INSERT INTO zz_parts (code, company_id) SELECT 'a', id FROM companies",
                "INSERT INTO zz_log VALUES (1, 'x')"]:
        await _execute(real_engine, sql)
    try:
        async with real_engine.connect() as conn:
            before = sorted((await conn.execute(text(parts))).scalars())
        source = await backup_export.export_full()
        await _execute(real_engine, "DROP TABLE zz_parts, zz_log")
        await _execute(real_engine, "DROP SEQUENCE zz_numbers")
        await company(real_engine, user, "Beta Trading", "beta")
        result = await backup_import.run_recovery(source)
        assert result.ok is True, result.error
        async with real_engine.connect() as conn:
            assert sorted((await conn.execute(text(parts))).scalars()) == before
            assert (await conn.execute(text("SELECT count(*) FROM zz_log"))).scalar() == 1
        assert await _company_names(real_engine) == {"Alpha Trading"}
    finally:
        await _execute(real_engine, "DROP TABLE IF EXISTS zz_parts, zz_log")
        await _execute(real_engine, "DROP SEQUENCE IF EXISTS zz_numbers")


# Parts of Celerp's own tables a restore puts back: (create, a lookup that holds while they
# exist, drop).
TABLE_PARTS = {
    "check constraint not yet validated": (
        ["ALTER TABLE companies ADD CONSTRAINT zz_named CHECK (name <> '') NOT VALID"],
        "EXISTS (SELECT FROM pg_constraint WHERE conname = 'zz_named' AND NOT convalidated)",
        ["ALTER TABLE companies DROP CONSTRAINT IF EXISTS zz_named"]),
    "index comment": (
        ["CREATE INDEX zz_by_name ON companies (name)", "COMMENT ON INDEX zz_by_name IS 'by name'"],
        "obj_description(to_regclass('zz_by_name'), 'pg_class') = 'by name'", ["DROP INDEX IF EXISTS zz_by_name"]),
    "constraint comment": (
        ["ALTER TABLE companies ADD CONSTRAINT zz_named CHECK (name <> '')",
         "COMMENT ON CONSTRAINT zz_named ON companies IS 'named'"],
        "(SELECT obj_description(oid, 'pg_constraint') FROM pg_constraint WHERE conname = 'zz_named') = 'named'",
        ["ALTER TABLE companies DROP CONSTRAINT IF EXISTS zz_named"]),
    "extended statistics": (
        ["CREATE STATISTICS zz_stats ON id, name FROM companies", "COMMENT ON STATISTICS zz_stats IS 'stats'"],
        "(SELECT obj_description(oid, 'pg_statistic_ext') FROM pg_statistic_ext WHERE stxname = 'zz_stats') = 'stats'",
        ["DROP STATISTICS IF EXISTS zz_stats"]),
    "row security": (
        ["ALTER TABLE companies ENABLE ROW LEVEL SECURITY"],
        "(SELECT relrowsecurity FROM pg_class WHERE oid = 'companies'::regclass)",
        ["ALTER TABLE companies DISABLE ROW LEVEL SECURITY"]),
    "row security policy": (
        ["ALTER TABLE companies ENABLE ROW LEVEL SECURITY", "CREATE POLICY zz_all ON companies USING (true)",
         "COMMENT ON POLICY zz_all ON companies IS 'all'"],
        "(SELECT obj_description(oid, 'pg_policy') FROM pg_policy WHERE polname = 'zz_all') = 'all'",
        ["DROP POLICY IF EXISTS zz_all ON companies", "ALTER TABLE companies DISABLE ROW LEVEL SECURITY"]),
}


@pytest.mark.parametrize("part", list(TABLE_PARTS))
async def test_a_recovery_puts_back_a_table_part(tmp_path, monkeypatch, code_config, real_engine, part):
    from celerp.services import backup_export, backup_import
    create, lookup, drop = TABLE_PARTS[part]
    _set_enabled(["celerp-inventory"])
    _Recovery(tmp_path, monkeypatch, real_database=True)
    user = await owner(real_engine)
    await company(real_engine, user, "Alpha Trading", "alpha")
    for sql in create:
        await _execute(real_engine, sql)
    try:
        source = await backup_export.export_full()
        await company(real_engine, user, "Beta Trading", "beta")
        result = await backup_import.run_recovery(source)
        assert result.ok is True, result.error
        assert await _company_names(real_engine) == {"Alpha Trading"}
        assert await _exists_sql(real_engine, lookup)
    finally:
        for sql in drop:
            await _execute(real_engine, sql)


@pytest.mark.parametrize("entry", ["recovery", "update rollback"])
async def test_a_database_set_up_by_celerp_is_restored(tmp_path, monkeypatch, code_config, real_engine, entry):
    """The default privileges Celerp grants its own database user at setup belong to the
    installation; a restore leaves them as they are."""
    import asyncio

    from celerp import cli
    from celerp.services import backup, backup_export, backup_import
    from test_helpers import DATABASE_URL
    pg_url = DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")
    # Setup runs these statements as the database administrator; here the test's own role runs them.
    monkeypatch.setattr(cli, "_psql", lambda sql, db, *flags: subprocess.run(
        [backup._find_pg_tool("psql"), "-X", "-v", "ON_ERROR_STOP=1", *flags, "-c", sql, "-d", pg_url],
        capture_output=True, text=True))
    _set_enabled(["celerp-inventory"])
    _Recovery(tmp_path, monkeypatch, real_database=True)
    user = await owner(real_engine)
    await company(real_engine, user, "Alpha Trading", "alpha")
    role = cli._parse_db_url(DATABASE_URL)
    assert cli._fix_ownership_statements(role["user"], role["dbname"]) is None
    try:
        if entry == "recovery":
            source = await backup_export.export_full()
            await company(real_engine, user, "Beta Trading", "beta")
            result = await backup_import.run_recovery(source)
            assert result.ok is True, result.error
        else:
            steps, dump = _update_steps(), tmp_path / "database.dump"
            await asyncio.to_thread(steps.preflight)
            await asyncio.to_thread(steps.dump, dump)
            await company(real_engine, user, "Beta Trading", "beta")
            await asyncio.to_thread(steps.restore, dump, "1.1.0")
        assert await _company_names(real_engine) == {"Alpha Trading"}
    finally:
        for kind in ("TABLES", "SEQUENCES"):
            await _execute(real_engine, f"ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON {kind} "
                                        f"FROM {role['user']}")


def _refusal_shown(update) -> str:
    """Why the update did not start, as the install owner's update card gets it; other users
    get only the reason code."""
    assert "detail" not in update.status(owner=False)["last_result"]
    return update.status(owner=True)["last_result"]["detail"]


@pytest.mark.parametrize("kind", ["view", "enum", "foreign key from another schema"])
async def test_an_update_of_a_database_holding_other_objects_stops_before_anything_changes(
        tmp_path, monkeypatch, real_engine, kind):
    from celerp import runtime
    from celerp.services import update
    from test_helpers import DATABASE_URL
    monkeypatch.setenv("CELERP_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.setattr(update, "installed_version", lambda: "1.0.0")
    steps = update.SupervisorSteps(
        {"server": {"api_port": 1, "ui_port": 2}, "database": {"url": DATABASE_URL}, "backup": {}},
        lambda root: {}, spawn_api=None, spawn_ui=None, wait_ready=None)
    user = await owner(real_engine)
    await company(real_engine, user, "Alpha Trading", "alpha")
    await _create(real_engine, kind)
    try:
        with pytest.raises(ValueError) as refused:
            steps.preflight()
        result, children = update.run_update("1.1.0", steps)
        assert (result["outcome"], result["reason"], children) == (update.FAILED, "backup_failed", ())
        shown = _refusal_shown(update)
        assert shown == str(refused.value).rstrip(".")
        assert shown.startswith("Celerp restores only its own tables") and UNSUPPORTED[kind][1] in shown, shown
        assert not update.dump_path().exists()
        assert not runtime.release_dir("1.1.0").exists()
        assert "in_progress" not in update.read_state()
        assert await _exists(real_engine, kind)
    finally:
        await _drop(real_engine, kind)


async def test_an_update_without_room_to_roll_back_stops_before_anything_changes(tmp_path, monkeypatch, real_engine):
    from celerp import runtime
    from celerp.services import update
    from test_helpers import DATABASE_URL
    monkeypatch.setenv("CELERP_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.setattr(update, "installed_version", lambda: "1.0.0")
    steps = update.SupervisorSteps(
        {"server": {"api_port": 1, "ui_port": 2}, "database": {"url": DATABASE_URL}, "backup": {}},
        lambda root: {}, spawn_api=None, spawn_ui=None, wait_ready=None)
    user = await owner(real_engine)
    await company(real_engine, user, "Alpha Trading", "alpha")
    _free_space(monkeypatch, 2**30)
    result, children = update.run_update("1.1.0", steps)
    assert (result["outcome"], result["reason"], children) == (update.FAILED, "backup_failed", ())
    assert _refusal_shown(update).startswith("Not enough free disk space to restore this backup")
    assert not runtime.release_dir("1.1.0").exists()
    assert "in_progress" not in update.read_state()
    assert await _company_names(real_engine) == {"Alpha Trading"}


# ── A database restore is all or nothing ─────────────────────────────────────

async def _restore_target(engine, tmp_path: Path) -> Path:
    """Dump a database with Alpha and a module table, then change it: Beta, and a module
    table the dump lacks whose foreign key points into a table the dump recreates."""
    import asyncio

    from celerp.services import backup
    from test_helpers import DATABASE_URL
    user = await owner(engine)
    await company(engine, user, "Alpha Trading", "alpha")
    await _execute(engine, "CREATE TABLE zz_source (company_id uuid REFERENCES companies(id))")
    await _execute(engine, "INSERT INTO zz_source SELECT id FROM companies")
    dump = tmp_path / "database.dump"
    dump.write_bytes(await asyncio.to_thread(backup.dump_database, DATABASE_URL))
    await _execute(engine, "DROP TABLE zz_source")
    await company(engine, user, "Beta Trading", "beta")
    await _execute(engine, "CREATE TABLE zz_extra (company_id uuid REFERENCES companies(id))")
    await _execute(engine, "INSERT INTO zz_extra SELECT id FROM companies")
    return dump


async def _restore(dump: Path, runner=None) -> None:
    import asyncio

    from celerp.services import backup
    from test_helpers import DATABASE_URL
    await asyncio.to_thread(backup.restore_database_file, dump, DATABASE_URL, runner=runner)


async def _assert_unchanged(engine, tables: set[str]) -> None:
    from sqlalchemy import text
    assert tables <= await _tables(engine)  # the fence may add its own instance_meta
    assert await _company_names(engine) == {"Alpha Trading", "Beta Trading"}
    async with engine.connect() as conn:
        assert (await conn.execute(text("SELECT count(*) FROM zz_extra"))).scalar() == 2


def _tool(command: list[str]) -> str:
    return Path(command[0]).stem


def _psql_never_runs(command, **kwargs):
    assert _tool(command) != "psql", "psql ran after pg_restore failed"
    return subprocess.run(command, **kwargs)


async def test_a_restore_replaces_the_database_exactly(tmp_path, real_engine):
    dump = await _restore_target(real_engine, tmp_path)
    try:
        await _restore(dump)
        assert "zz_extra" not in await _tables(real_engine)
        assert await _company_names(real_engine) == {"Alpha Trading"}
        from sqlalchemy import text
        async with real_engine.connect() as conn:
            assert (await conn.execute(text("SELECT count(*) FROM zz_source"))).scalar() == 1
        assert list(tmp_path.iterdir()) == [dump]
    finally:
        await _execute(real_engine, "DROP TABLE IF EXISTS zz_source, zz_extra")


def _fails_after_restoring_data(dump: Path):
    """A statement failing after the emptying and after every row was restored."""
    def runner(command, **kwargs):
        if _tool(command) == "psql":
            with Path(command[command.index("-f") + 1]).open("a") as script:
                script.write("SELECT 1/0;\n")
        return subprocess.run(command, **kwargs)
    return runner


def _cut_short(dump: Path):
    """pg_restore writes part of the script, then fails reading the rest of the dump."""
    dump.write_bytes(dump.read_bytes()[: dump.stat().st_size // 2])
    return _psql_never_runs


def _disk_full(dump: Path):
    """The disk fills while pg_restore writes the script."""
    def runner(command, **kwargs):
        if "-f" in command:
            command = [*command]
            command[command.index("-f") + 1] = "/dev/full"
        return _psql_never_runs(command, **kwargs)
    return runner


def _not_a_dump(dump: Path):
    dump.write_bytes(b"not a dump")
    return _psql_never_runs


@pytest.mark.parametrize("break_restore", [_fails_after_restoring_data, _cut_short, _disk_full, _not_a_dump],
                         ids=["a statement fails after the data", "the dump is cut short",
                              "the disk is full", "the dump is unreadable"])
async def test_a_failed_restore_changes_nothing(tmp_path, real_engine, break_restore):
    dump = await _restore_target(real_engine, tmp_path)
    try:
        tables = await _tables(real_engine)
        with pytest.raises(RuntimeError, match="failed"):
            await _restore(dump, break_restore(dump))
        await _assert_unchanged(real_engine, tables)
        assert list(tmp_path.iterdir()) == [dump]
    finally:
        await _execute(real_engine, "DROP TABLE IF EXISTS zz_source, zz_extra")


def _free_space(monkeypatch, free: int) -> None:
    import shutil
    monkeypatch.setattr(shutil, "disk_usage", lambda path: shutil._ntuple_diskusage(2 * free, free, free))


@pytest.mark.parametrize("short", [1, 0], ids=["one byte short", "exactly enough"])
async def test_a_restore_needs_ten_times_the_dump_and_1_gib_free_beside_it(tmp_path, monkeypatch, real_engine, short):
    """The script pg_restore writes beside the dump measured up to 6.7 times its size, on the
    disk the database usually lives on."""
    dump = await _restore_target(real_engine, tmp_path)
    try:
        tables = await _tables(real_engine)
        _free_space(monkeypatch, dump.stat().st_size * 10 + 2**30 - short)
        if short:
            with pytest.raises(ValueError, match="Not enough free disk space to restore this backup"):
                await _restore(dump)
            await _assert_unchanged(real_engine, tables)
        else:
            await _restore(dump)
            assert await _company_names(real_engine) == {"Alpha Trading"}
    finally:
        await _execute(real_engine, "DROP TABLE IF EXISTS zz_source, zz_extra")


async def test_a_restore_blocked_by_an_object_outside_public_changes_nothing(tmp_path, real_engine):
    """The dump recreates zz_status, which a table outside public uses, so its drop fails, and the
    error names that failure."""
    await _execute(real_engine, "CREATE TYPE zz_status AS ENUM ('open', 'done')")
    try:
        dump = await _restore_target(real_engine, tmp_path)
        await _execute(real_engine, "CREATE SCHEMA zz")
        await _execute(real_engine, "CREATE TABLE zz.jobs (status public.zz_status)")
        tables = await _tables(real_engine)
        with pytest.raises(RuntimeError, match=r"psql failed \(exit 3\): .*ERROR:  cannot drop type public.zz_status"):
            await _restore(dump)
        await _assert_unchanged(real_engine, tables)
    finally:
        await _execute(real_engine, "DROP SCHEMA IF EXISTS zz CASCADE")
        await _execute(real_engine, "DROP TABLE IF EXISTS zz_source, zz_extra")
        await _execute(real_engine, "DROP TYPE IF EXISTS zz_status")


@pytest.fixture
async def public_owner(real_engine):  # noqa: F811
    """A connection as the role that owns schema public. PostgreSQL 14 and older give it to
    the bootstrap superuser; where Celerp's role does not own it, ADMIN_DATABASE_URL names
    one that does, connected here to the test's own database."""
    import os

    from sqlalchemy import make_url
    from sqlalchemy.ext.asyncio import create_async_engine
    url = os.environ.get("ADMIN_DATABASE_URL")
    if url is None:
        yield real_engine
        return
    engine = create_async_engine(make_url(url).set(database=real_engine.url.database))
    yield engine
    await engine.dispose()


async def test_a_restore_leaves_the_public_schema_alone(tmp_path, real_engine, public_owner):
    """A backup of a database whose public schema comment was cleared carries that comment,
    which only the schema's owner may set; the restore replaces the tables and leaves the
    schema as it is."""
    from sqlalchemy import text
    comment = "SELECT obj_description('public'::regnamespace, 'pg_namespace')"
    async with real_engine.connect() as conn:
        before = (await conn.execute(text(comment))).scalar()
    await _execute(public_owner, "COMMENT ON SCHEMA public IS NULL")
    try:
        dump = await _restore_target(real_engine, tmp_path)
        await _execute(public_owner, "COMMENT ON SCHEMA public IS 'kept'")
        await _restore(dump)
        assert await _company_names(real_engine) == {"Alpha Trading"}
        async with real_engine.connect() as conn:
            assert (await conn.execute(text(comment))).scalar() == "kept"
    finally:
        await _execute(public_owner, f"COMMENT ON SCHEMA public IS {'NULL' if before is None else repr(before)}")
        await _execute(real_engine, "DROP TABLE IF EXISTS zz_source, zz_extra")


async def test_a_restore_leaves_other_schemas_alone(tmp_path, real_engine):
    from sqlalchemy import text
    dump = await _restore_target(real_engine, tmp_path)
    try:
        await _execute(real_engine, "CREATE SCHEMA zz")
        await _execute(real_engine, "CREATE TABLE zz.jobs (id int)")
        await _execute(real_engine, "INSERT INTO zz.jobs VALUES (1)")
        await _restore(dump)
        assert await _company_names(real_engine) == {"Alpha Trading"}
        async with real_engine.connect() as conn:
            assert (await conn.execute(text("SELECT count(*) FROM zz.jobs"))).scalar() == 1
    finally:
        await _execute(real_engine, "DROP SCHEMA IF EXISTS zz CASCADE")
        await _execute(real_engine, "DROP TABLE IF EXISTS zz_source, zz_extra")


# Objects outside public that depend on a public table: (create, lookup that holds while they exist).
OUTSIDE = {
    "foreign key": ("CREATE TABLE zz.links (company_id uuid REFERENCES public.companies(id))",
                    "SELECT count(*) FROM pg_constraint WHERE conrelid = 'zz.links'::regclass AND contype = 'f'"),
    "view": ("CREATE VIEW zz.names AS SELECT name FROM public.companies", "SELECT count(*) FROM zz.names"),
}


@pytest.mark.parametrize("kind", list(OUTSIDE))
async def test_a_restore_that_would_change_another_schema_changes_nothing(tmp_path, real_engine, kind):
    """Called directly, without the check a recovery or an update makes first, the restore's
    own transaction still refuses."""
    from sqlalchemy import text
    create, lookup = OUTSIDE[kind]
    dump = await _restore_target(real_engine, tmp_path)
    try:
        await _execute(real_engine, "CREATE SCHEMA zz")
        await _execute(real_engine, create)
        tables = await _tables(real_engine)
        with pytest.raises(RuntimeError, match=r"psql failed \(exit 1\): ERROR:  cannot drop desired"):
            await _restore(dump)
        await _assert_unchanged(real_engine, tables)
        async with real_engine.connect() as conn:
            assert (await conn.execute(text(lookup))).scalar() == (1 if kind == "foreign key" else 2)
    finally:
        await _execute(real_engine, "DROP SCHEMA IF EXISTS zz CASCADE")
        await _execute(real_engine, "DROP TABLE IF EXISTS zz_source, zz_extra")


async def test_a_restore_stopped_part_way_changes_nothing(tmp_path, real_engine):
    """psql is killed at its time limit while its transaction has already emptied part of
    the database; the server rolls the transaction back."""
    import asyncio

    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine
    from test_helpers import DATABASE_URL
    dump = await _restore_target(real_engine, tmp_path)
    blocker = create_async_engine(DATABASE_URL)
    try:
        tables = await _tables(real_engine)
        async with blocker.connect() as conn, conn.begin():
            await conn.execute(text("LOCK TABLE zz_extra IN ACCESS SHARE MODE"))

            def runner(command, **kwargs):
                return subprocess.run(command, **{**kwargs, "timeout": 3 if _tool(command) == "psql" else 60})

            with pytest.raises(RuntimeError, match="psql timed out"):
                await _restore(dump, runner)
        async with real_engine.connect() as conn:
            for _ in range(100):
                if not (await conn.execute(text(
                        "SELECT count(*) FROM pg_stat_activity WHERE application_name = 'psql'"))).scalar():
                    break
                await asyncio.sleep(0.1)
        await _assert_unchanged(real_engine, tables)
        await _restore(dump)
        assert await _company_names(real_engine) == {"Alpha Trading"}
        assert "zz_extra" not in await _tables(real_engine)
    finally:
        await blocker.dispose()
        await _execute(real_engine, "DROP TABLE IF EXISTS zz_source, zz_extra")


# Without a psql that runs nothing starts: no marker, no safety archive, no connector or cloud call.

# The error each kind of unusable psql gives.
PSQL = {
    "missing": "psql not found",
    "not executable": "psql could not run",
    "failing": "psql failed (exit 127)",
}


def _without_psql(monkeypatch, tmp_path: Path, psql: str = "missing") -> Path:
    from celerp.config import settings
    from celerp.services import backup
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in ("pg_dump", "pg_restore"):
        (bin_dir / tool).symlink_to(backup._find_pg_tool(tool))
    if psql != "missing":
        (bin_dir / "psql").write_text("#!/bin/sh\necho 'libpq.so.5: cannot open shared object file' >&2\nexit 127\n")
        (bin_dir / "psql").chmod(0o755 if psql == "failing" else 0o644)
    monkeypatch.setattr(settings, "pg_bin_dir", str(bin_dir))
    return bin_dir


def _record_connector_calls(monkeypatch) -> list[str]:
    from celerp.services import backup_import
    calls = []
    for name in ("_current_connectors", "_reconcile_connectors"):
        real = getattr(backup_import, name)

        async def _recorded(*a, _real=real, _name=name, **kw):
            calls.append(_name)
            return await _real(*a, **kw)
        monkeypatch.setattr(backup_import, name, _recorded)
    return calls


@pytest.mark.parametrize("psql", list(PSQL))
@pytest.mark.parametrize("entry", ["system recovery", "bootstrap recovery", "confirmed recovery"])
async def test_a_recovery_without_psql_changes_nothing_and_can_be_repeated(
        tmp_path, monkeypatch, code_config, real_engine, entry, psql):
    from celerp.config import settings
    from celerp.services import backup_export, backup_import
    _set_enabled(["celerp-inventory"])
    rec = _Recovery(tmp_path, monkeypatch, real_database=True)
    user = await owner(real_engine)
    await company(real_engine, user, "Alpha Trading", "alpha")
    source = await backup_export.export_full()
    await company(real_engine, user, "Beta Trading", "beta")
    connector_calls = _record_connector_calls(monkeypatch)
    if entry == "confirmed recovery":
        rec.fail_safety()
        pending = await backup_import.run_recovery(source)
        assert pending.needs_confirmation is True, pending.error

    async def _start():
        if entry == "system recovery":
            return await backup_import.run_recovery(source)
        if entry == "bootstrap recovery":
            return await backup_import.bootstrap_recovery(source)
        return await backup_import.continue_recovery(pending.confirmation_id, pending.archive_digest)

    staged = rec.staging()
    _without_psql(monkeypatch, tmp_path, psql)
    result = await _start()
    assert result.ok is False and PSQL[psql] in result.error, result.error
    assert backup_import.recovery_incomplete() is False
    assert connector_calls == []
    assert rec.safety_archives() == []
    rec.cloud_snapshot.assert_not_awaited()
    assert rec.staging() == staged
    assert await _company_names(real_engine) == {"Alpha Trading", "Beta Trading"}

    monkeypatch.setattr(settings, "pg_bin_dir", "")
    result = await _start()
    assert result.ok is True, result.error
    assert await _company_names(real_engine) == {"Alpha Trading"}


@pytest.mark.parametrize("psql", list(PSQL))
async def test_an_update_without_psql_stops_before_anything_changes(tmp_path, monkeypatch, real_engine, psql):
    """Nothing is recorded, so the next automatic update tries the same version again."""
    from celerp import runtime
    from celerp.config import settings
    from celerp.services import update
    from test_helpers import DATABASE_URL
    monkeypatch.setenv("CELERP_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.setattr(update, "installed_version", lambda: "1.0.0")
    _without_psql(monkeypatch, tmp_path, psql)
    steps = update.SupervisorSteps(
        {"server": {"api_port": 1, "ui_port": 2}, "database": {"url": DATABASE_URL}, "backup": {}},
        lambda root: {}, spawn_api=None, spawn_ui=None, wait_ready=None)
    user = await owner(real_engine)
    await company(real_engine, user, "Alpha Trading", "alpha")
    result, children = update.run_update("1.1.0", steps)
    assert (result["outcome"], result["reason"], children) == (update.FAILED, "backup_failed", ())
    assert not update.dump_path().exists()
    assert not runtime.release_dir("1.1.0").exists()
    assert update.read_state() == {}
    assert await _company_names(real_engine) == {"Alpha Trading"}

    monkeypatch.setattr(settings, "pg_bin_dir", "")
    monkeypatch.setattr(update, "self_update_blockers", lambda: [])
    monkeypatch.setattr(update, "refresh_check", lambda: {"latest": "1.1.0", "error": ""})
    monkeypatch.setattr(update, "request_update", lambda target: None)
    assert update.request_available(automatic=True) == "1.1.0"


@pytest.mark.parametrize("psql", list(PSQL))
async def test_an_update_rollback_after_a_failed_restore_can_be_repeated(tmp_path, monkeypatch, real_engine, psql):
    """The rollback an update runs (SupervisorSteps.restore): a failed attempt changes
    nothing, and the next start repeats it."""
    import asyncio

    from celerp.config import settings
    from celerp.services import update
    from test_helpers import DATABASE_URL
    dump = await _restore_target(real_engine, tmp_path)
    steps = update.SupervisorSteps(
        {"server": {"api_port": 1, "ui_port": 2}, "database": {"url": DATABASE_URL}, "backup": {}},
        lambda root: {}, spawn_api=None, spawn_ui=None, wait_ready=None)
    try:
        tables = await _tables(real_engine)
        _without_psql(monkeypatch, tmp_path, psql)
        with pytest.raises(RuntimeError, match=re.escape(PSQL[psql])):
            await asyncio.to_thread(steps.restore, dump, "1.1.0")
        await _assert_unchanged(real_engine, tables)
        monkeypatch.setattr(settings, "pg_bin_dir", "")
        await asyncio.to_thread(steps.restore, dump, "1.1.0")
        assert await _company_names(real_engine) == {"Alpha Trading"}
        assert "zz_extra" not in await _tables(real_engine)
    finally:
        await _execute(real_engine, "DROP TABLE IF EXISTS zz_source, zz_extra")
