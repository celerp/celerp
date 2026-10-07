# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""System Recovery from a .celerp-backup archive: replaces the whole installation.

``prepare_recovery`` stages and checks the archive, ``make_safety_archive`` saves the
current installation, and ``commit_recovery`` is the one destructive engine that local
upload, cloud recovery point and bootstrap restore all run. Works without a Cloud
subscription.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import tarfile
import uuid
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath

log = logging.getLogger(__name__)


# Archives from before backups recorded their version.
_UNRECORDED_VERSIONS = (None, "", "unknown")


def _refuse_newer_archive(recorded) -> None:
    """Refuse a backup made by a newer Celerp than this one: an older copy cannot
    know that backup's schema or data. Legacy backups that never recorded a version
    restore; a recorded version that cannot be read is refused."""
    from packaging.version import InvalidVersion
    from celerp.migrations.compatibility import is_newer_than_running, running_version
    if recorded in _UNRECORDED_VERSIONS:
        return
    try:
        newer = is_newer_than_running(recorded)
    except (InvalidVersion, TypeError):
        raise ValueError(f"This backup records a Celerp version that cannot be read ({recorded!r}). "
                         f"Nothing was changed.")
    if newer:
        raise ValueError(
            f"This backup was made with Celerp {recorded}, which is newer than this copy "
            f"({running_version()}). Nothing was changed. Update Celerp, then restore it."
        )


@dataclass
class ImportMeta:
    celerp_version: str
    pg_version: str
    created_at: str
    company_name: str
    # Modules the source system had enabled at export time. Empty for old
    # backups (pre-2026-06-04). Used by the install pass to surface
    # missing modules to the user before they reach a broken dashboard.
    enabled_modules: list[str] = field(default_factory=list)


def _pg_major(version_text: str | None) -> int | None:
    """Major PostgreSQL version from a `pg_dump`/`pg_restore --version` string like
    'pg_dump (PostgreSQL) 17.2 (Ubuntu ...)'. None if unknown/unparseable."""
    if not version_text:
        return None
    m = re.search(r"PostgreSQL\)\s+(\d+)", version_text)
    return int(m.group(1)) if m else None


def _local_pg_restore_major() -> int | None:
    """Major version of the pg_restore that will run the restore, or None if it can't be
    determined (in which case the pre-check is skipped — we never block on inability to check)."""
    import subprocess
    from celerp.services.backup import _find_pg_tool
    try:
        out = subprocess.run([_find_pg_tool("pg_restore"), "--version"], capture_output=True, timeout=5)
        return _pg_major(out.stdout.decode(errors="replace"))
    except Exception:
        return None


def validate_archive(path: Path) -> ImportMeta:
    """Check archive structure and read meta.json.

    Raises ValueError on invalid archive or version incompatibility.
    """
    if not tarfile.is_tarfile(str(path)):
        raise ValueError("Not a valid .celerp-backup archive (not a tar.gz)")

    with tarfile.open(str(path), "r:gz") as tar:
        names = tar.getnames()

        if "database.dump" not in names:
            raise ValueError("Archive missing database.dump")
        if "meta.json" not in names:
            raise ValueError("Archive missing meta.json")

        # Security: check for path traversal and platform-ambiguous separators.
        # Celerp-generated tar names are POSIX paths; a backslash can become a
        # separator on Windows and must never acquire different extraction meaning.
        for name in names:
            if name.startswith("/") or "\\" in name or ".." in name:
                raise ValueError(f"Unsafe path in archive: {name}")

        meta_file = tar.extractfile("meta.json")
        if meta_file is None:
            raise ValueError("Cannot read meta.json from archive")
        meta_data = json.loads(meta_file.read())

    meta = ImportMeta(
        celerp_version=meta_data.get("celerp_version") or "unknown",
        pg_version=meta_data.get("pg_version", "unknown"),
        created_at=meta_data.get("created_at", "unknown"),
        company_name=meta_data.get("company_name", "unknown"),
        enabled_modules=list(meta_data.get("enabled_modules") or []),
    )

    # PostgreSQL forward-compatibility: pg_restore cannot read a backup made by a NEWER
    # pg_dump. Fail early with an actionable message instead of the cryptic
    # "unsupported version (1.16) in file header" pg_restore emits mid-restore. Only block
    # when we can affirmatively determine backup_major > local_major; if either version is
    # unknown we skip the pre-check and let pg_restore decide.
    backup_major = _pg_major(meta.pg_version)
    local_major = _local_pg_restore_major()
    if backup_major is not None and local_major is not None and backup_major > local_major:
        raise ValueError(
            f"This backup was created with PostgreSQL {backup_major}, but this system's "
            f"restore tools are PostgreSQL {local_major}. Install PostgreSQL {backup_major} "
            f"(client tools, and a matching server) to restore it - pg_restore cannot read a "
            f"backup from a newer PostgreSQL."
        )

    _refuse_newer_archive(meta_data.get("celerp_version"))
    return meta


async def _dispose_engine() -> None:
    """Dispose the SQLAlchemy engine connection pool before pg_restore."""
    try:
        from celerp.db import engine as _engine
        await _engine.dispose()
        log.info("Connection pool disposed before pg_restore")
    except Exception as pool_exc:
        log.warning("Pool dispose failed (non-fatal): %s", pool_exc)


async def _run_pg_restore(dump_path: Path, database_url: str) -> None:
    """Run pg_restore from the staged dump file off the event loop (blocking subprocess)."""
    import asyncio
    from celerp.services.backup import restore_database_file
    await asyncio.to_thread(restore_database_file, dump_path, database_url)


async def _reconcile_schema() -> None:
    """Bring the restored database up to the current schema; raises on failure.

    A restored dump can be stamped behind (or ahead of) its actual DDL - a
    develop-origin source database, for example - and raw ``alembic upgrade head``
    then re-applies DDL that already exists and dies on DuplicateColumn, leaving the
    schema stale while the running code queries newer columns. Run the same
    stamp-repair walker, grants, and develop-to-release reconcile the CLI's
    ``celerp migrate`` uses. With a stale schema every data read fails, so a failure
    here fails the recovery.
    """
    import asyncio
    from celerp.config import settings

    def _sync() -> None:
        from celerp.cli import (
            _apply_migrations,
            _migration_lock,
            _post_migration_grants,
            _reconcile_after_migrate,
        )
        # Serialize under the shared migration advisory lock, the same one
        # `celerp migrate`/`celerp start` hold, so a restore reconcile cannot
        # run concurrently with a service migration or a second restore.
        with _migration_lock(settings.database_url):
            _apply_migrations(settings.database_url)
            _post_migration_grants(settings.database_url)
            _reconcile_after_migrate(settings.database_url)

    try:
        await asyncio.to_thread(_sync)
    except Exception as exc:
        raise RuntimeError(
            f"The database was restored, but its schema could not be brought up to date: {exc}"
        ) from exc
    log.info("Schema reconcile completed after pg_restore")


def _is_protected_module_dir(
    module_root: Path,
    name: str,
    protected_names: frozenset[str],
) -> bool:
    """Whether *name* identifies a current first-party directory on this filesystem."""
    if name in protected_names:
        return True

    candidate = module_root / name
    if not candidate.exists():
        return False

    for protected_name in protected_names:
        protected = module_root / protected_name
        if not protected.exists():
            continue
        try:
            if candidate.samefile(protected):
                return True
        except OSError:
            continue
    return False


RESTORE_NOTICE_FILE = "restore-notice.json"


SAFETY_WARNING = "A safety backup could not be made before restoring."

# Response header on a successful whole-installation restore: every session
# ended with it, so the UI drops the browser's session cookies.
SESSION_ENDED_HEADER = "X-Session-Ended"


def missing_modules_sentence(missing: list[str]) -> str:
    """The one user-facing sentence for modules the source had but this install lacks."""
    names = ", ".join(str(m) for m in missing)
    return (
        f"{len(missing)} module(s) enabled on the source are not installed on this "
        f"server: {names}. Those features stay unavailable until the module packages "
        f"are installed."
    )


RECOVERY_STAGING_DIR = "recovery-staging"
RECOVERY_SAFETY_DIR = "recovery-safety"
# Present from the first change to the installation until the recovery finishes or is undone.
RECOVERY_MARKER = "recovery-in-progress.json"
# An archive restored without a safety archive, kept until its recovery finishes.
UNFINISHED_RECOVERY_ARCHIVE = "unfinished-recovery.celerp-backup"
# Safety archives kept in RECOVERY_SAFETY_DIR; older ones are removed.
SAFETY_KEEP = 3
# How long a recovery staged without a safety archive waits for the owner to continue.
CONFIRMATION_TTL = timedelta(minutes=15)
# A staging directory with no pending confirmation is an interrupted recovery once
# it is this old.
_ABANDONED_STAGING_AGE = timedelta(days=1)

_STAGED_ARCHIVE = "archive.celerp-backup"
_STAGED_DUMP = "database.dump"
_STAGED_FILES = "files"
_STAGED_RECORD = "staged.json"
_PENDING_RECORD = "pending.json"
_STAGING_ID = re.compile(r"[0-9a-f]{32}")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _rename(src: Path, dst: Path) -> None:
    """Same-filesystem rename used for every root swap and roll-back."""
    os.rename(src, dst)


@dataclass
class PreparedRecovery:
    """A recovery archive staged under data_dir and checked, ready to commit.

    ``files`` maps every staged file (relative to the staging directory) to its size.
    """
    id: str
    root: Path
    digest: str
    meta: ImportMeta
    files: dict[str, int]

    @property
    def archive(self) -> Path:
        return self.root / _STAGED_ARCHIVE

    @property
    def dump(self) -> Path:
        return self.root / _STAGED_DUMP


@dataclass
class SafetyResult:
    ok: bool
    path: Path | None = None
    error: str | None = None


def _staging_root() -> Path:
    from celerp.config import settings
    return settings.data_dir / RECOVERY_STAGING_DIR


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


def _remove_staging(root: Path) -> None:
    shutil.rmtree(root, ignore_errors=True)


def _purge_expired_staging() -> None:
    """Remove staged recoveries whose confirmation expired or that were abandoned."""
    staging = _staging_root()
    if not staging.is_dir():
        return
    now = _now()
    for root in staging.iterdir():
        try:
            pending = root / _PENDING_RECORD
            if pending.exists():
                expired = datetime.fromisoformat(json.loads(pending.read_text())["expires_at"]) <= now
            else:
                modified = datetime.fromtimestamp(root.stat().st_mtime, timezone.utc)
                expired = now - modified > _ABANDONED_STAGING_AGE
        except (OSError, ValueError, KeyError):
            expired = True
        if expired:
            _remove_staging(root)


def _staged_files(root: Path) -> dict[str, int]:
    files = root / _STAGED_FILES
    out = {_STAGED_DUMP: (root / _STAGED_DUMP).stat().st_size}
    for p in files.rglob("*"):
        if p.is_file():
            out[str(p.relative_to(root))] = p.stat().st_size
    return out


def _stage_members(archive: Path, root: Path) -> None:
    """Extract the dump and every restore-owned file into *root*, refusing unsafe entries."""
    from celerp.config import settings
    from celerp.modules.loader import first_party_names
    from celerp.services.backup_export import restore_roots

    keys = set(restore_roots())
    files = root / _STAGED_FILES
    for key in keys:
        (files / key).mkdir(parents=True, exist_ok=True)
    files_root = files.resolve()
    protected = first_party_names()
    module_root = settings.data_dir / "modules"
    with tarfile.open(str(archive), "r:gz") as tar:
        for member in tar.getmembers():
            if member.name == "meta.json" or member.isdir():
                continue
            if not member.isfile():
                raise ValueError(f"Unsupported entry in archive (links and devices are not allowed): {member.name}")
            if member.name == _STAGED_DUMP:
                dest = root / _STAGED_DUMP
            else:
                parts = PurePosixPath(member.name).parts
                if parts[0] not in keys:
                    continue
                if len(parts) < 2:
                    raise ValueError(f"Unsafe path in archive: {member.name}")
                if parts[0] == "modules" and _is_protected_module_dir(module_root, parts[1], protected):
                    # Bundled module directories belong to the application, not the backup.
                    continue
                dest = files.joinpath(*parts)
                if not dest.resolve().is_relative_to(files_root):
                    raise ValueError(f"Unsafe path in archive: {member.name}")
            dest.parent.mkdir(parents=True, exist_ok=True)
            src = tar.extractfile(member)
            if src is None:
                raise ValueError(f"Cannot read {member.name} from archive")
            with src, dest.open("wb") as out:
                shutil.copyfileobj(src, out)
    if not (root / _STAGED_DUMP).is_file():
        raise ValueError("Archive missing database.dump")


def _prepare_sync(path: Path) -> PreparedRecovery:
    _purge_expired_staging()
    meta = validate_archive(path)
    staging_id = uuid.uuid4().hex
    root = _staging_root() / staging_id
    root.mkdir(parents=True)
    try:
        shutil.copyfile(path, root / _STAGED_ARCHIVE)
        digest = _sha256(root / _STAGED_ARCHIVE)
        _stage_members(root / _STAGED_ARCHIVE, root)
        prepared = PreparedRecovery(id=staging_id, root=root, digest=digest, meta=meta,
                                    files=_staged_files(root))
        (root / _STAGED_RECORD).write_text(json.dumps({
            "digest": digest, "meta": asdict(meta), "files": prepared.files,
        }))
        return prepared
    except BaseException:
        _remove_staging(root)
        raise


async def prepare_recovery(path: Path) -> PreparedRecovery:
    """Stage and check a recovery archive before anything is changed.

    Validates the archive, copies it under data_dir and records its sha256, and
    extracts the database dump and every restore-owned file into a staging
    directory on the installation's filesystem, refusing links, devices and paths
    outside the restore roots. Raises ValueError for an archive that cannot be
    restored; nothing is left staged on failure.
    """
    import asyncio
    return await asyncio.to_thread(_prepare_sync, path)


def _load_prepared(root: Path) -> PreparedRecovery:
    record = json.loads((root / _STAGED_RECORD).read_text())
    return PreparedRecovery(id=root.name, root=root, digest=record["digest"],
                            meta=ImportMeta(**record["meta"]), files=record["files"])


def _keep_newest_safety_archives(safety_dir: Path) -> None:
    for old in sorted(safety_dir.glob("pre-recovery-*.celerp-backup"))[:-SAFETY_KEEP]:
        old.unlink(missing_ok=True)


async def make_safety_archive() -> SafetyResult:
    """Archive the whole current installation into data_dir/recovery-safety. Never raises.

    Uses the canonical full exporter, then re-validates the archive: one that does not
    validate is no safety archive. The newest SAFETY_KEEP archives are kept.
    """
    import asyncio
    from celerp.config import settings
    from celerp.services import backup_export

    partial: Path | None = None
    try:
        exported = await backup_export.export_full()
        try:
            await asyncio.to_thread(validate_archive, exported)
            safety_dir = settings.data_dir / RECOVERY_SAFETY_DIR
            safety_dir.mkdir(parents=True, exist_ok=True)
            final = safety_dir / f"pre-recovery-{_now():%Y%m%dT%H%M%S%fZ}.celerp-backup"
            partial = final.with_name(final.name + ".partial")
            await asyncio.to_thread(shutil.move, exported, partial)
            partial.replace(final)
        finally:
            exported.unlink(missing_ok=True)
        _keep_newest_safety_archives(safety_dir)
        log.info("Safety archive saved: %s", final)
        return SafetyResult(ok=True, path=final)
    except Exception as exc:
        if partial is not None:
            partial.unlink(missing_ok=True)
        log.warning("Safety archive failed: %s", exc)
        return SafetyResult(ok=False, error=str(exc) or repr(exc))


async def _cloud_safety_snapshot() -> None:
    """An optional extra safety copy in the cloud, when a cloud key is configured."""
    from celerp.config import settings
    from celerp.services import backup_repo
    if not settings.backup_encryption_key or settings.cloud_disconnected:
        return
    result = await backup_repo.run_snapshot(label="pre-recovery")
    if not result.ok:
        log.warning("Cloud safety snapshot failed (the local safety archive was made): %s", result.error)


def _verify_staged(prepared: PreparedRecovery) -> None:
    if _staged_files(prepared.root) != prepared.files:
        raise RuntimeError("The staged recovery files changed before they could be put in place.")


def _swap_roots(prepared: PreparedRecovery) -> None:
    """Replace each restore-owned root with its staged root by same-filesystem renames.

    Bundled module directories are moved back into the new module root. On any
    failure every root already swapped is put back as it was, then the error is raised.
    """
    from celerp.modules.loader import first_party_names
    from celerp.services.backup_export import restore_roots

    _verify_staged(prepared)
    protected = first_party_names()
    old = prepared.root / "old"
    old.mkdir(exist_ok=True)
    swapped: list[tuple[str, Path, bool]] = []
    try:
        for key, dest in restore_roots().items():
            existed = dest.exists() or dest.is_symlink()
            if existed:
                _rename(dest, old / key)
            swapped.append((key, dest, existed))
            dest.parent.mkdir(parents=True, exist_ok=True)
            _rename(prepared.root / _STAGED_FILES / key, dest)
            if key == "modules" and existed:
                for entry in sorted((old / key).iterdir()):
                    if _is_protected_module_dir(old / key, entry.name, protected):
                        _rename(entry, dest / entry.name)
    except Exception:
        _roll_back_roots(prepared.root, swapped, protected)
        raise


def _roll_back_roots(root: Path, swapped: list[tuple[str, Path, bool]], protected: frozenset[str]) -> None:
    discard = root / "discard"
    discard.mkdir(exist_ok=True)
    for key, dest, existed in reversed(swapped):
        try:
            if key == "modules" and existed and dest.is_dir():
                for entry in sorted(dest.iterdir()):
                    kept = root / "old" / key / entry.name
                    if _is_protected_module_dir(dest, entry.name, protected) and not kept.exists():
                        _rename(entry, kept)
            if dest.exists():
                _rename(dest, discard / key)
            if existed:
                _rename(root / "old" / key, dest)
        except Exception:
            log.exception("Could not put back %s after a failed recovery", dest)


def _apply_modules(modules: list[str]) -> bool:
    """Make the enabled modules exactly *modules*; returns True when a restart was scheduled."""
    if not modules:
        return False
    from celerp.config import replace_enabled_modules
    if not replace_enabled_modules(modules):
        log.info("Enabled modules unchanged - skipping restart")
        return False
    from celerp.modules.requirements import schedule_restart
    return schedule_restart()


def _missing_module_warnings(modules: list[str]) -> list[str]:
    try:
        from celerp.modules.audit import audit_missing_modules
        missing = audit_missing_modules(modules)
    except Exception as audit_exc:
        log.warning("Module audit failed (non-fatal): %s", audit_exc)
        return []
    if not missing:
        return []
    log.warning("Recovery enabled %d modules that are not installed on this server: %s",
                len(missing), missing)
    return [missing_modules_sentence(missing)]


def _write_restore_notice(company_name: str | None, warnings: list[str],
                          safety_archive: str | None, restart_scheduled: bool) -> None:
    """Persist a one-shot restore notice for the login page.

    The post-restore restart can replace the page that showed the result (the
    desktop shell reloads the window to the respawned server), so the outcome and
    any warnings must survive it; the login page renders and then clears this file.
    """
    from celerp.config import settings
    try:
        path = settings.data_dir / RESTORE_NOTICE_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "company_name": company_name,
            "warnings": warnings,
            "safety_archive": safety_archive,
            "restart_scheduled": restart_scheduled,
        }))
    except Exception as exc:
        log.warning("Could not write restore notice: %s", exc)


async def _current_connectors() -> list[dict]:
    """Every connector of the installation, as the recovery marker records it to revoke."""
    import sqlalchemy as sa

    from celerp.db import get_session_ctx
    from celerp.models.connector_config import ConnectorConfig

    async with get_session_ctx() as session:
        configs = (await session.scalars(
            sa.select(ConnectorConfig).order_by(ConnectorConfig.id))).all()
        return [{"company_id": str(c.company_id), "connector": c.connector,
                 "webhook_ids": list(c.webhook_ids or []), "revision": None} for c in configs]


async def _reconcile_connectors() -> None:
    """Revoke the remote state of every connector the recovery marker still lists.

    The replacement discards every local connector config, so none may leave a live
    relay credential or store webhook behind. Each connector is attempted whatever
    happened to the others. The revision a disconnect is sent for is in the marker
    before the request leaves, so a retry after a lost response disconnects that same
    connection; a connection that changed since is read afresh. A connector leaves the
    marker only once its remote state is confirmed gone; while any is left this raises,
    and the marker keeps them for the next attempt.
    """
    from celerp.connectors.remote_state import (
        ConnectorRemoteCleanupError,
        ConnectorRemoteStateChangedError,
        connection_revision,
        revoke_connector_remote_state,
    )

    state = json.loads(_marker_path().read_text())
    for _ in range(2):
        for entry in list(state["connectors"]):
            try:
                if entry["revision"] is None:
                    entry["revision"] = await connection_revision(entry["connector"])
                    _write_marker(state)
                if entry["revision"] is not None:
                    await revoke_connector_remote_state(
                        entry["company_id"], entry["connector"],
                        webhook_ids=entry["webhook_ids"], revision=entry["revision"])
            except ConnectorRemoteStateChangedError:
                entry["revision"] = None
                _write_marker(state)
                continue
            except Exception:
                log.warning("Connector %s could not be disconnected", entry["connector"], exc_info=True)
                continue
            state["connectors"].remove(entry)
            _write_marker(state)
    if state["connectors"]:
        names = ", ".join(sorted({e["connector"] for e in state["connectors"]}))
        raise ConnectorRemoteCleanupError(f"Connections to other services could not be disconnected ({names})")


async def _clear_restored_connector_state(session) -> None:
    import sqlalchemy as sa

    from celerp.connectors.ownership import record_connector_reset
    from celerp.models.company import Company
    from celerp.models.connector_config import ConnectorConfig, OutboundQueue

    connectors = set((await session.scalars(
        sa.select(ConnectorConfig.connector)
    )).all())
    connectors.update((await session.scalars(
        sa.select(OutboundQueue.connector)
    )).all())
    company_ids = (await session.scalars(sa.select(Company.id))).all()
    for company_id in company_ids:
        for connector in connectors:
            record_connector_reset(session, company_id, connector)
    await session.execute(sa.delete(OutboundQueue))
    await session.execute(sa.delete(ConnectorConfig))


def _failed(error: str, **kw):
    from celerp.services.backup import BackupResult
    return BackupResult(ok=False, size_bytes=0, error=error, **kw)


def _commit_failure(exc: Exception, safety_archive: str | None, restored: bool) -> str:
    detail = (str(exc) or repr(exc)).rstrip(".")
    if restored:
        return (f"System Recovery did not complete: {detail}. The installation was put back "
                f"as it was before, from the safety archive {safety_archive}; connections to "
                "other services must be connected again and everyone must sign in again.")
    saved = (f" The installation as it was before is saved in the safety archive {safety_archive}."
             if safety_archive else "")
    return f"System Recovery did not complete: {detail}.{saved} {MAINTENANCE_MESSAGE}"


MAINTENANCE_MESSAGE = ("Celerp stays unavailable until the recovery is finished; "
                       "restart Celerp to try again.")


def _marker_path() -> Path:
    from celerp.config import settings
    return settings.data_dir / RECOVERY_MARKER


def recovery_incomplete() -> bool:
    """Whether a destructive recovery started and has not finished or been undone.

    While true the installation serves nothing but its liveness and readiness
    probes: its database, files and modules may not agree, and no session from
    before the replacement may be honoured.
    """
    return _marker_path().exists()


def _mark_recovery_started(target: Path, connectors: list[dict]) -> None:
    """Durably record that the installation is being replaced, what *target* archive
    brings it back to a whole state if the replacement does not finish, and the
    *connectors* whose remote state must be revoked before it is replaced. The marker
    records this copy's version: an older copy never finishes a newer copy's recovery."""
    from celerp.migrations.compatibility import running_version
    _write_marker({"target": str(target), "connectors": connectors, "celerp_version": running_version()})


def _write_marker(state: dict) -> None:
    path = _marker_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".partial")
    with partial.open("w") as out:
        json.dump(state, out)
        out.flush()
        os.fsync(out.fileno())
    partial.replace(path)
    _fsync_dir(path.parent)


def _mark_recovery_finished() -> None:
    from celerp.config import settings
    _marker_path().unlink(missing_ok=True)
    (settings.data_dir / RECOVERY_SAFETY_DIR / UNFINISHED_RECOVERY_ARCHIVE).unlink(missing_ok=True)
    _fsync_dir(_marker_path().parent)


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _keep_for_retry(prepared: PreparedRecovery) -> Path:
    """Move the staged archive out of staging so an unfinished recovery can be retried."""
    from celerp.config import settings
    safety_dir = settings.data_dir / RECOVERY_SAFETY_DIR
    safety_dir.mkdir(parents=True, exist_ok=True)
    kept = safety_dir / UNFINISHED_RECOVERY_ARCHIVE
    _rename(prepared.root / _STAGED_ARCHIVE, kept)
    return kept


async def _replace_installation(prepared: PreparedRecovery) -> tuple[list[str], bool]:
    """Replace the database, file roots and enabled modules with *prepared*'s.

    Clears the restored connectors, records the restore for Celerp Cloud
    (``payments.record_recovery``) and ends every session. Returns the enabled
    modules and whether a restart was scheduled. The caller holds the recovery locks and the recovery marker.
    """
    import asyncio
    import sqlalchemy as sa
    from celerp.config import settings
    from celerp.db import get_session_ctx
    from celerp.models.company import Company
    from celerp.modules.registry import load_set
    from celerp.services import payments, session_tracker

    await _dispose_engine()
    await _run_pg_restore(prepared.dump, settings.database_url)
    await _reconcile_schema()
    async with get_session_ctx() as session:
        await _clear_restored_connector_state(session)
        # The restored companies take online payments again, closings from before the
        # restore can no longer finish, companies it did not bring back stay closed, and
        # every payment it ever recorded is delivered again.
        payments.record_recovery(session, (await session.scalars(sa.select(Company.id))).all())
        # Backups without module metadata take the set from every restored company.
        modules = prepared.meta.enabled_modules or await load_set(session)
        # No session from before the replacement stays valid; this also
        # commits the connector cleanup and the recorded restore.
        await session_tracker.end_all_sessions(session)
    await asyncio.to_thread(_swap_roots, prepared)
    return modules, _apply_modules(modules)


async def _replace_from(target: Path) -> None:
    """Revoke the connectors the recovery marker still lists, replace the installation
    with the archive *target*, then clear the marker."""
    import asyncio
    # Checked before the irreversible connector revoke: an archive this copy cannot
    # restore leaves everything as it is.
    prepared = await prepare_recovery(target)
    try:
        await _reconcile_connectors()
        await _replace_installation(prepared)
    finally:
        await asyncio.to_thread(_remove_staging, prepared.root)
    _mark_recovery_finished()


async def finish_incomplete_recovery() -> None:
    """At startup, bring back to a whole state an installation whose recovery did not finish.

    Replaces it with the archive the marker names: the safety archive of the
    installation as it was, or, when the owner restored without one, the archive
    being restored. On failure the marker stays and the installation stays
    unavailable; the next start tries again.
    """
    if not recovery_incomplete():
        return
    try:
        marker = json.loads(_marker_path().read_text())
        target = Path(marker["target"])
        _refuse_newer_archive(marker.get("celerp_version"))
        async with _recovery_locks():
            await _replace_from(target)
        log.info("Unfinished System Recovery completed from %s", target)
    except Exception:
        log.exception("Unfinished System Recovery could not be completed; Celerp stays unavailable")


async def commit_recovery(prepared: PreparedRecovery, safety_archive: Path | None):
    """The one destructive recovery engine; the caller holds the recovery locks.

    Under a durable recovery marker, revokes the current connectors' remote state
    (`_reconcile_connectors`), then replaces the installation (`_replace_installation`).
    When either fails, the installation is put back from the safety archive, once every
    connector is revoked; with no safety archive, or when putting it back fails too, the
    marker stays and the installation serves nothing until a start finishes the
    recovery. The staging directory is removed either way.
    """
    import asyncio
    from celerp.services.backup import BackupResult

    safety = str(safety_archive) if safety_archive else None
    try:
        # Marked before the first remote revoke: a revoke cannot be undone, so from here a
        # failure is a started recovery, finished or put back like any other.
        _mark_recovery_started(safety_archive or await asyncio.to_thread(_keep_for_retry, prepared),
                               await _current_connectors())
        try:
            await _reconcile_connectors()
            modules, restart_scheduled = await _replace_installation(prepared)
        except Exception as exc:
            log.exception("System Recovery failed")
            restored = False
            if safety_archive is not None:
                try:
                    await _replace_from(safety_archive)
                    restored = True
                except Exception:
                    log.exception("The installation could not be put back from %s", safety_archive)
            return _failed(_commit_failure(exc, safety, restored), safety_archive=safety)
        _mark_recovery_finished()
        # Celerp Cloud learns of the restore now; until it has, no company can be reset,
        # and the reconciliation at start and every few minutes keeps trying.
        from celerp.services.payments import reconcile_payments
        await reconcile_payments()
        warnings = _missing_module_warnings(modules)
        _write_restore_notice(prepared.meta.company_name, warnings, safety, restart_scheduled)
        return BackupResult(ok=True, size_bytes=prepared.files[_STAGED_DUMP], warnings=warnings,
                            restart_scheduled=restart_scheduled, safety_archive=safety)
    finally:
        await asyncio.to_thread(_remove_staging, prepared.root)


def _start_failed(prepared: PreparedRecovery, exc: Exception):
    """The recovery locks could not be taken or the safety step broke: nothing was restored."""
    log.exception("System Recovery failed before the restore began")
    _remove_staging(prepared.root)
    return _failed(f"System Recovery did not start: {str(exc) or repr(exc)}")


@asynccontextmanager
async def _recovery_locks():
    """Exclude connector work, then pause writes, for a coherent safety archive and restore."""
    from celerp.connectors.ownership import connector_maintenance_guard
    from celerp.services.backup_state import writes_paused
    async with connector_maintenance_guard():
        with writes_paused():
            yield


async def _prepare_or_fail(path: Path):
    """(prepared, None) or (None, failed result) for an archive that cannot be restored."""
    try:
        return await prepare_recovery(path), None
    except ValueError as exc:
        log.error("Recovery archive refused: %s", exc)
        return None, _failed(str(exc))
    except Exception as exc:
        log.exception("Recovery archive could not be staged")
        return None, _failed(f"The backup could not be prepared for restoring: {str(exc) or repr(exc)}")


async def run_recovery(path: Path):
    """System Recovery from a .celerp-backup: replaces the whole installation.

    The archive is staged and checked first; then, holding the recovery locks, a
    local safety archive of the current installation is made before anything is
    overwritten. When no safety archive can be made nothing is changed: the result
    has ``needs_confirmation`` and the staged recovery waits CONFIRMATION_TTL for
    ``continue_recovery``.
    """
    prepared, failure = await _prepare_or_fail(path)
    if failure is not None:
        return failure
    try:
        async with _recovery_locks():
            safety = await make_safety_archive()
            if safety.ok:
                await _cloud_safety_snapshot()
                return await commit_recovery(prepared, safety.path)
    except Exception as exc:
        return _start_failed(prepared, exc)

    expires_at = _now() + CONFIRMATION_TTL
    (prepared.root / _PENDING_RECORD).write_text(json.dumps({
        "digest": prepared.digest, "expires_at": expires_at.isoformat(),
    }))
    detail = (safety.error or "").rstrip(".")
    return _failed(f"{SAFETY_WARNING} {detail}.", needs_confirmation=True,
                   confirmation_id=prepared.id, archive_digest=prepared.digest)


async def continue_recovery(confirmation_id: str, digest: str):
    """Restore a staged recovery without a safety archive, on the owner's explicit confirmation.

    The confirmation must name the staged recovery and the sha256 of its staged
    archive, within CONFIRMATION_TTL; the staged archive must still have that digest.
    """
    import asyncio
    if not isinstance(confirmation_id, str) or not _STAGING_ID.fullmatch(confirmation_id):
        return _failed("This recovery is not available. Start the recovery again.")
    root = _staging_root() / confirmation_id
    pending_path = root / _PENDING_RECORD
    try:
        pending = json.loads(pending_path.read_text())
        expires_at = datetime.fromisoformat(pending["expires_at"])
    except (OSError, ValueError, KeyError):
        return _failed("This recovery is not available. Start the recovery again.")
    if _now() >= expires_at:
        _remove_staging(root)
        return _failed("The confirmation to restore without a safety copy has expired. "
                       "Start the recovery again.")
    if digest != pending["digest"]:
        return _failed("This confirmation does not match the staged backup.")
    try:
        prepared = _load_prepared(root)
        intact = prepared.digest == digest and await asyncio.to_thread(_sha256, prepared.archive) == digest
    except (OSError, ValueError, KeyError, TypeError):
        intact = False
    if not intact:
        _remove_staging(root)
        return _failed("The staged backup changed after it was checked. Start the recovery again.")
    # The confirmation is used once.
    pending_path.unlink()
    try:
        async with _recovery_locks():
            return await commit_recovery(prepared, None)
    except Exception as exc:
        return _start_failed(prepared, exc)


async def bootstrap_recovery(path: Path):
    """Restore into an installation with no users yet: nothing to protect, so no safety archive."""
    prepared, failure = await _prepare_or_fail(path)
    if failure is not None:
        return failure
    try:
        async with _recovery_locks():
            return await commit_recovery(prepared, None)
    except Exception as exc:
        return _start_failed(prepared, exc)
