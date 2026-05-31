# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

"""Import from .celerp-backup archive (validate, version check, pg_restore + files).

Works without Cloud subscription — pure local operation.
"""

from __future__ import annotations

import json
import logging
import tarfile
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass
class ImportMeta:
    celerp_version: str
    pg_version: str
    created_at: str
    company_name: str


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
    )

    # Version compatibility check
    try:
        from importlib.metadata import version
        current = version("celerp")
    except Exception:
        current = "0.0.0"

    backup_ver = meta.celerp_version if meta.celerp_version != "unknown" else "0.0.0"
    backup_parts = backup_ver.split(".")
    current_parts = current.split(".")

    backup_major = backup_parts[0] if backup_parts else "0"
    current_major = current_parts[0] if current_parts else "0"

    if backup_major > current_major:
        raise ValueError(
            f"This backup is from a newer version ({meta.celerp_version}) "
            f"than the current installation ({current}). "
            "Update Celerp before importing."
        )

    backup_minor = int(backup_parts[1]) if len(backup_parts) > 1 else 0
    current_minor = int(current_parts[1]) if len(current_parts) > 1 else 0
    if backup_minor != current_minor:
        log.warning(
            "Backup version %s differs from current %s (minor version mismatch). "
            "Schema migrations will run automatically after restore.",
            meta.celerp_version, current,
        )

    return meta


async def run_import(path: Path):
    """Import from .celerp-backup: safety backup + pg_restore + extract files.

    Returns BackupResult.
    """
    from celerp.services.backup import BackupResult, run_backup, restore_database
    from celerp.config import settings

    try:
        meta = validate_archive(path)
        log.info("Importing backup from %s (company=%s, version=%s)",
                 path, meta.company_name, meta.celerp_version)

        # Safety backup first (if encryption key is available)
        if settings.backup_encryption_key:
            safety = await run_backup(label="pre-import-safety")
            if not safety.ok:
                log.warning("Safety backup failed before import: %s", safety.error)

        with tarfile.open(str(path), "r:gz") as tar:
            # Extract database.dump
            dump_file = tar.extractfile("database.dump")
            if dump_file is None:
                return BackupResult(ok=False, size_bytes=0, error="Cannot read database.dump")
            dump_bytes = dump_file.read()

        # Dispose connection pool BEFORE pg_restore so live connections don't
        # hold locks that block pg_restore from dropping/recreating tables.
        try:
            from celerp.db import engine as _engine
            await _engine.dispose()
            log.info("Connection pool disposed before pg_restore")
        except Exception as pool_exc:
            log.warning("Pool dispose failed (non-fatal): %s", pool_exc)

        # Run pg_restore in a thread executor — it's a blocking subprocess call.
        # Running it directly in an async function blocks the uvicorn event loop,
        # which prevents the response from being sent back and causes HTTPX timeouts.
        import asyncio
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None, lambda: restore_database(dump_bytes, settings.database_url)
        )

        # Reconcile schema — run any missing migrations after pg_restore
        try:
            from alembic.config import Config as _AlembicConfig
            from alembic import command as _alembic_cmd
            _cfg = _AlembicConfig("alembic.ini")
            await loop.run_in_executor(None, lambda: _alembic_cmd.upgrade(_cfg, "head"))
            log.info("Alembic upgrade head completed after pg_restore")
        except Exception as alembic_exc:
            log.warning("Alembic upgrade after pg_restore failed (non-fatal): %s", alembic_exc)

        # Extract files outside the tar context (already read dump above)
        with tarfile.open(str(path), "r:gz") as tar:
            from celerp.config import settings as _settings
            _ALLOWED_PREFIXES = ("attachments/", "ai_uploads/")
            for member in tar.getmembers():
                if member.name in ("database.dump", "meta.json"):
                    continue
                if member.name.startswith("/") or ".." in member.name:
                    continue
                if not any(member.name.startswith(p) for p in _ALLOWED_PREFIXES):
                    continue
                if member.isfile():
                    dest = (
                        _settings.data_dir / "static" / member.name
                        if member.name.startswith("attachments/")
                        else _settings.data_dir / member.name
                    )
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    src = tar.extractfile(member)
                    if src:
                        dest.write_bytes(src.read())

        return BackupResult(ok=True, size_bytes=len(dump_bytes))

    except ValueError as exc:
        log.error("Backup import validation error: %s", exc)
        return BackupResult(ok=False, size_bytes=0, error=str(exc))
    except Exception as exc:
        log.exception("Backup import failed unexpectedly")
        return BackupResult(ok=False, size_bytes=0, error=str(exc) or repr(exc))
