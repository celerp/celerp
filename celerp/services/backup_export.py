# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Export full backup as .celerp-backup (unencrypted tar.gz).

Two sources:
  - export_full(): local pg_dump + attachments + meta.json
  - export_from_cloud(backup_id): download encrypted → decrypt → repackage
"""

from __future__ import annotations

import io
import json
import logging
import tarfile
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)


def _version() -> str:
    """Return current celerp version string."""
    try:
        from importlib.metadata import version
        return version("celerp")
    except Exception:
        return "unknown"


def _pg_version() -> str:
    """Return pg_dump --version output."""
    import subprocess
    from celerp.services.backup import _find_pg_tool
    try:
        result = subprocess.run([_find_pg_tool("pg_dump"), "--version"], capture_output=True, timeout=5)
        return result.stdout.decode().strip()
    except Exception:
        return "unknown"


def _build_archive(dump: bytes, attachment_dirs: list[Path], meta: dict) -> Path:
    """Build a .celerp-backup tar.gz archive. Returns path to temp file."""
    tmp = tempfile.NamedTemporaryFile(suffix=".celerp-backup", delete=False)
    tmp.close()

    with tarfile.open(tmp.name, "w:gz") as tar:
        # database.dump
        info = tarfile.TarInfo(name="database.dump")
        info.size = len(dump)
        tar.addfile(info, io.BytesIO(dump))

        # meta.json
        meta_bytes = json.dumps(meta, indent=2).encode()
        info = tarfile.TarInfo(name="meta.json")
        info.size = len(meta_bytes)
        tar.addfile(info, io.BytesIO(meta_bytes))

        # Attachment directories
        for d in attachment_dirs:
            if not d.exists():
                continue
            for p in sorted(d.rglob("*")):
                if p.is_file():
                    try:
                        rel = str(p.relative_to(d.parent))
                    except ValueError:
                        rel = p.name
                    tar.add(str(p), arcname=rel)

    return Path(tmp.name)


async def _read_company_enabled_modules() -> list[str]:
    """Read the first available company's enabled modules from the DB.

    Returns [] if no company exists (fresh install exporting nothing useful)
    or on any DB error (we never want a bad DB to fail the export).

    This is a soft read: the export is allowed to proceed without the
    modules list. The restore side will fall back to the company row it
    just restored from the dump, so the worst case is "meta doesn't know
    what was enabled" — same as the pre-fix behavior.
    """
    try:
        from sqlalchemy import select
        from celerp.db import SessionLocal
        from celerp.models.company import Company
    except Exception:
        return []

    try:
        async with SessionLocal() as session:
            from celerp.models.company import Company as _Company
            result = await session.execute(select(_Company).limit(1))
            company = result.scalar_one_or_none()
            if company is None:
                return []
            settings_dict = company.settings or {}
            raw = settings_dict.get("enabled_modules") or []
            return list(raw) if isinstance(raw, list) else []
    except Exception as exc:
        log.warning("Could not read enabled_modules for export: %s", exc)
        return []


async def export_full() -> Path:
    """Export pg_dump + all attachments + meta.json as .celerp-backup.

    Works without Cloud subscription — pure local operation.
    Returns path to temp file.
    """
    from celerp.config import settings, read_config
    from celerp.services.backup import dump_database

    dump = dump_database(settings.database_url)

    cfg = read_config()
    company_name = cfg.get("company", {}).get("name", "unknown")
    enabled_modules = await _read_company_enabled_modules()

    meta = {
        "celerp_version": _version(),
        "pg_version": _pg_version(),
        "created_at": __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        ).isoformat(),
        "company_name": company_name,
        "enabled_modules": enabled_modules,
    }

    return _build_archive(
        dump,
        [settings.data_dir / "static" / "attachments", settings.data_dir / "ai_uploads"],
        meta,
    )


async def export_from_cloud(backup_id: str) -> Path:
    """Download encrypted backup from relay, decrypt, repackage as .celerp-backup.

    The decrypted blob is itself a complete .celerp-backup (built by
    export_full when the cloud backup was first uploaded), so it already
    carries the original meta.json with enabled_modules. We pass it through
    unchanged — the import side reads meta.json directly.
    """
    from celerp.config import settings
    from celerp.services.backup import _parse_key, decrypt, download_from_relay

    key = _parse_key(settings.backup_encryption_key)
    encrypted = await download_from_relay(backup_id)
    decrypted = decrypt(encrypted, key)

    # Write decrypted blob as .celerp-backup
    tmp = tempfile.NamedTemporaryFile(suffix=".celerp-backup", delete=False)
    tmp.write(decrypted)
    tmp.close()
    return Path(tmp.name)
