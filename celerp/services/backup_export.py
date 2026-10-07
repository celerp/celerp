# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Export a full local backup as .celerp-backup (unencrypted tar.gz).

``export_full()`` bundles a local pg_dump + the restore-owned file roots + meta.json. Cloud snapshots
are rebuilt into the same format by ``backup_repo.reassemble_snapshot``.
"""

from __future__ import annotations

import io
import json
import logging
import tarfile
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)


def _pg_version() -> str:
    """Return pg_dump --version output."""
    import subprocess
    from celerp.services.backup import _find_pg_tool
    try:
        result = subprocess.run([_find_pg_tool("pg_dump"), "--version"], capture_output=True, timeout=5)
        return result.stdout.decode().strip()
    except Exception:
        return "unknown"


def archive_members(dirs) -> list[tuple[str, Path]]:
    """(arcname, path) for every file under *dirs*, arcname relative to the dir's parent
    so local exports and cloud snapshots share one ``.celerp-backup`` layout."""
    out: list[tuple[str, Path]] = []
    for d in dirs:
        if not d.exists():
            continue
        for p in sorted(d.rglob("*")):
            # Skip regenerable Python bytecode: bundling it bloats the archive and
            # stale .pyc across machines/python versions is actively harmful on
            # restore (custom modules carry these).
            if "__pycache__" in p.parts or p.suffix == ".pyc":
                continue
            if p.is_file():
                out.append((str(p.relative_to(d.parent)), p))
    return out


def _build_archive(dump: bytes, dirs: list[Path], meta: dict) -> Path:
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

        for arcname, path in archive_members(dirs):
            tar.add(str(path), arcname=arcname)

    return Path(tmp.name)


def restore_roots() -> dict[str, Path]:
    """The file roots a whole-installation backup carries, keyed by their archive prefix.

    Attachments, AI uploads and custom module code live on disk, not in the database
    dump; a restore on another machine needs them to use the restored rows.
    """
    from celerp.config import settings
    return {
        "attachments": settings.data_dir / "static" / "attachments",
        "ai_uploads": settings.data_dir / "ai_uploads",
        "modules": settings.data_dir / "modules",
    }


async def export_full() -> Path:
    """Export pg_dump + all restore-owned files + meta.json as .celerp-backup.

    Works without Cloud subscription - pure local operation.
    Returns path to temp file.
    """
    import asyncio

    from celerp.config import settings
    from celerp.services import backup

    dump = await asyncio.to_thread(backup.dump_database, settings.database_url)
    meta = await archive_meta()
    return await asyncio.to_thread(_build_archive, dump, list(restore_roots().values()), meta)


async def archive_meta() -> dict:
    """meta.json of a full backup."""
    import datetime

    from celerp.config import read_config
    from celerp.db import get_session_ctx
    from celerp.migrations.compatibility import running_version
    from celerp.modules.registry import load_set

    async with get_session_ctx() as session:
        enabled_modules = await load_set(session)
    return {
        "celerp_version": running_version(),
        "pg_version": _pg_version(),
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "company_name": read_config().get("company", {}).get("name", "unknown"),
        "enabled_modules": enabled_modules,
    }
