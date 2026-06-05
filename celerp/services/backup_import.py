# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

"""Import from .celerp-backup archive (validate, version check, pg_restore + files).

Works without Cloud subscription — pure local operation.
"""

from __future__ import annotations

import json
import logging
import tarfile
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)


def _parse_version(v: str | None) -> tuple[int, int, int, str]:
    """Parse a celerp version string into a comparable tuple.

    Returns (major, minor, patch, pre_release). Garbage / None / "unknown"
    return (0, 0, 0, "") so the import proceeds (no false block on
    legacy archives).

    Accepts:
      - "1.2.3"                       → (1, 2, 3, "")
      - "1.1.19.dev158"                → (1, 1, 19, "dev158")   (setuptools_scm on dev)
      - "0.1.dev1"                     → (0, 1, 0,  "dev1")     (setuptools_scm on CI, no tags)
      - "1.0"                          → (1, 0, 0, "")
      - "" / None / "unknown" / "abc"  → (0, 0, 0, "")

    Tuple comparison is correct: (10, 0, 0, "") > (9, 0, 0, "") is True.
    (String compare would say "10" < "9" lexicographically — that's the
    old bug.)

    The parser walks the dotted components. The first 1-3 components
    that are pure integers become major/minor/patch. The first non-int
    component (or anything after it) is captured as pre-release. If the
    FIRST component isn't an int, the whole string is treated as
    garbage (we return zeros) — the dev versions setuptools_scm emits
    always start with a numeric major.
    """
    if not v or not isinstance(v, str):
        return (0, 0, 0, "")

    parts = v.split(".")

    # First component must be a pure int for the version to be parseable.
    try:
        major = int(parts[0])
    except (ValueError, IndexError):
        return (0, 0, 0, "")

    # Walk the rest, allowing up to 2 more numeric components before any
    # non-numeric one starts the pre-release tail.
    nums = [major]
    pre_parts: list[str] = []
    saw_non_int = False
    for p in parts[1:]:
        if not saw_non_int and len(nums) < 3:
            try:
                nums.append(int(p))
            except ValueError:
                saw_non_int = True
                pre_parts.append(p)
        else:
            if not saw_non_int:
                saw_non_int = True
            pre_parts.append(p)

    minor = nums[1] if len(nums) > 1 else 0
    patch = nums[2] if len(nums) > 2 else 0
    pre = ".".join(pre_parts)
    return (major, minor, patch, pre)


def _safe_test_version() -> str:
    """Return a version string guaranteed to be <= the current install.

    Used by tests that build .celerp-backup archives with a fixed
    celerp_version. Deriving at runtime (instead of hardcoding "1.0.0")
    means the test passes on:
      - Local dev (setuptools_scm gives 1.1.11.dev20 with tags)
      - CI (setuptools_scm gives 0.1.dev1 on a fresh checkout with no tags)
      - Packaged install (setuptools gives the published version)

    The returned version is one patch below the current major.minor so
    it's unambiguously "older" — the version policy treats it as silent.
    """
    try:
        from importlib.metadata import version
        current = version("celerp")
    except Exception:
        return "0.0.0"
    v = _parse_version(current)
    if v == (0, 0, 0, ""):
        return "0.0.0"
    # Same major.minor, patch - 1. Guarantees <= current.
    return f"{v[0]}.{v[1]}.{max(0, v[2] - 1)}"


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

        # Security: check for path traversal
        for name in names:
            if name.startswith("/") or ".." in name:
                raise ValueError(f"Unsafe path in archive: {name}")

        meta_file = tar.extractfile("meta.json")
        if meta_file is None:
            raise ValueError("Cannot read meta.json from archive")
        meta_data = json.loads(meta_file.read())

    meta = ImportMeta(
        celerp_version=meta_data.get("celerp_version", "unknown"),
        pg_version=meta_data.get("pg_version", "unknown"),
        created_at=meta_data.get("created_at", "unknown"),
        company_name=meta_data.get("company_name", "unknown"),
        enabled_modules=list(meta_data.get("enabled_modules") or []),
    )

    # Version compatibility check
    try:
        from importlib.metadata import version
        current = version("celerp")
    except Exception:
        current = "0.0.0"

    backup_v = _parse_version(meta.celerp_version)
    current_v = _parse_version(current)

    if backup_v > current_v and backup_v[0] > current_v[0]:
        # Backup is from a newer MAJOR version. The whole point of restore
        # is recovery — don't block the user. Warn loudly so the UI can
        # surface it, but proceed with the import. Schema migrations are
        # additive so a newer-major backup is usually safe to restore.
        log.warning(
            "Backup is from a newer major version (%s) than the current "
            "installation (%s). Restoring anyway — schema migrations are "
            "additive. If the backup uses removed features, some data may "
            "not round-trip cleanly.",
            meta.celerp_version, current,
        )
    elif backup_v[0] == current_v[0] and backup_v[1] != current_v[1]:
        # Same major, different minor. Existing behaviour: warn, proceed.
        log.warning(
            "Backup version %s differs from current %s (minor version mismatch). "
            "Schema migrations will run automatically after restore.",
            meta.celerp_version, current,
        )
    # else: same major, same or older minor (downgrade) → silent.
    # Schema migrations are additive in both directions.

    return meta


async def _safety_backup(label: str):
    """Run a pre-import safety backup if the encryption key is configured.

    Module-level helper so tests can stub it cleanly.
    """
    from celerp.config import settings
    from celerp.services.backup import run_backup
    if not settings.backup_encryption_key:
        from celerp.services.backup import BackupResult
        return BackupResult(ok=True, size_bytes=0)
    return await run_backup(label=label)


async def _dispose_engine() -> None:
    """Dispose the SQLAlchemy engine connection pool before pg_restore."""
    try:
        from celerp.db import engine as _engine
        await _engine.dispose()
        log.info("Connection pool disposed before pg_restore")
    except Exception as pool_exc:
        log.warning("Pool dispose failed (non-fatal): %s", pool_exc)


async def _run_pg_restore(dump_bytes: bytes, database_url: str) -> None:
    """Run pg_restore in a thread executor (blocking subprocess)."""
    import asyncio
    from celerp.services.backup import restore_database
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(
        None, lambda: restore_database(dump_bytes, database_url)
    )


async def _run_alembic() -> None:
    """Reconcile schema after pg_restore via alembic upgrade head."""
    try:
        import asyncio
        from celerp.alembic_config import build_alembic_config as _build_alembic_config
        from alembic import command as _alembic_cmd
        _cfg = _build_alembic_config()
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, lambda: _alembic_cmd.upgrade(_cfg, "head"))
        log.info("Alembic upgrade head completed after pg_restore")
    except Exception as alembic_exc:
        log.warning("Alembic upgrade after pg_restore failed (non-fatal): %s", alembic_exc)


async def _extract_files(path: Path) -> None:
    """Extract attachments/ and ai_uploads/ from the archive into data_dir."""
    from celerp.config import settings
    _ALLOWED_PREFIXES = ("attachments/", "ai_uploads/")
    with tarfile.open(str(path), "r:gz") as tar:
        for member in tar.getmembers():
            if member.name in ("database.dump", "meta.json"):
                continue
            if member.name.startswith("/") or ".." in member.name:
                continue
            if not any(member.name.startswith(p) for p in _ALLOWED_PREFIXES):
                continue
            if member.isfile():
                dest = (
                    settings.data_dir / "static" / member.name
                    if member.name.startswith("attachments/")
                    else settings.data_dir / member.name
                )
                dest.parent.mkdir(parents=True, exist_ok=True)
                src = tar.extractfile(member)
                if src:
                    dest.write_bytes(src.read())


async def _activate_modules(modules: list[str]) -> None:
    """After a successful restore, update config.toml and request a restart.

    The destination may not have the same set of enabled modules as the
    source. We call celerp.config.set_enabled_modules() (idempotent) so
    config.toml reflects what was enabled at export time, and we write
    the .restart_requested sentinel so the celerp start process manager
    respawns with the new module set. The loader will skip modules that
    don't have on-disk packages; the missing-modules warning surfaces
    those to the user.

    No-op if modules is empty (backwards compat with old archives).
    """
    if not modules:
        return
    try:
        from celerp.config import set_enabled_modules as _set_enabled_modules
        _set_enabled_modules(modules)
        log.info("config.toml updated with %d enabled modules: %s",
                 len(modules), modules)
    except Exception as exc:
        log.warning("Failed to update config.toml with enabled modules: %s", exc)
        return

    # Request a restart so the loader picks up the new modules.
    # _send_sigterm writes the sentinel + sleeps 0.2s then SIGTERMs self.
    # We schedule it via call_later so the HTTP response flushes first.
    try:
        import asyncio
        from celerp.routers.system import _restart_sentinel_path, _send_sigterm
        sentinel = _restart_sentinel_path()
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        sentinel.touch()
        log.info("Restart sentinel written: %s", sentinel)
        loop = asyncio.get_event_loop()
        loop.call_later(0.5, _send_sigterm)
        log.info("Restart scheduled (SIGTERM in 0.5s)")
    except Exception as exc:
        log.warning("Failed to schedule restart: %s", exc)


async def run_import(path: Path):
    """Import from .celerp-backup: safety backup + pg_restore + extract files.

    Returns BackupResult with a `warnings` field listing modules the source
    had enabled that aren't installed on the destination (read-only diff,
    doesn't fail the import).
    """
    from celerp.services.backup import BackupResult
    from celerp.config import settings

    try:
        meta = validate_archive(path)
        log.info("Importing backup from %s (company=%s, version=%s)",
                 path, meta.company_name, meta.celerp_version)

        # Safety backup first (if encryption key is available)
        safety = await _safety_backup("pre-import-safety")
        if not safety.ok:
            log.warning("Safety backup failed before import: %s", safety.error)

        with tarfile.open(str(path), "r:gz") as tar:
            dump_file = tar.extractfile("database.dump")
            if dump_file is None:
                return BackupResult(ok=False, size_bytes=0, error="Cannot read database.dump")
            dump_bytes = dump_file.read()

        # Dispose connection pool BEFORE pg_restore so live connections don't
        # hold locks that block pg_restore from dropping/recreating tables.
        await _dispose_engine()

        # Run pg_restore in a thread executor — it's a blocking subprocess call.
        # Running it directly in an async function blocks the uvicorn event loop,
        # which prevents the response from being sent back and causes HTTPX timeouts.
        await _run_pg_restore(dump_bytes, settings.database_url)

        # Reconcile schema — run any missing migrations after pg_restore
        await _run_alembic()

        # Extract files outside the tar context (already read dump above)
        await _extract_files(path)

        # Activate pass: update config.toml with the source's enabled modules
        # and request a restart so the loader picks them up. Server-side
        # because we have no user token yet (bootstrap restore).
        if meta.enabled_modules:
            await _activate_modules(meta.enabled_modules)

        # Audit pass: surface modules that were enabled on the source but
        # have no on-disk package on the destination. Read-only — the import
        # succeeds; the user just gets warned so the dashboard 404 is no
        # longer a surprise.
        warnings: list[str] = []
        if meta.enabled_modules:
            try:
                from celerp.modules.audit import audit_missing_modules
                warnings = audit_missing_modules(meta.enabled_modules)
                if warnings:
                    log.warning(
                        "Imported backup enabled %d modules that are not "
                        "installed on this server: %s",
                        len(warnings), warnings,
                    )
            except Exception as audit_exc:
                log.warning("Module audit failed (non-fatal): %s", audit_exc)

        return BackupResult(ok=True, size_bytes=len(dump_bytes), warnings=warnings)

    except ValueError as exc:
        log.error("Backup import validation error: %s", exc)
        return BackupResult(ok=False, size_bytes=0, error=str(exc))
    except Exception as exc:
        log.exception("Backup import failed unexpectedly")
        return BackupResult(ok=False, size_bytes=0, error=str(exc) or repr(exc))
