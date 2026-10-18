# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Export a full local backup as .celerp-backup (unencrypted tar.gz).

``export_full()`` bundles a local pg_dump + the restore-owned file roots + meta.json. Cloud snapshots
are rebuilt into the same format by ``backup_repo.reassemble_snapshot``.
"""

from __future__ import annotations

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


def _build_archive(dump: Path, dirs: list[Path], meta: dict) -> Path:
    """Build a .celerp-backup tar.gz archive from the dump file at *dump*. Returns path to
    temp file; a failure removes it."""
    from celerp.services.backup import add_bytes_member, add_file_member, disk_full_as_error

    tmp = tempfile.NamedTemporaryFile(suffix=".celerp-backup", delete=False)
    tmp.close()
    try:
        with disk_full_as_error(), tarfile.open(tmp.name, "w:gz") as tar:
            add_file_member(tar, "database.dump", dump)

            add_bytes_member(tar, "meta.json", json.dumps(meta, indent=2).encode())

            for arcname, path in archive_members(dirs):
                tar.add(str(path), arcname=arcname)
    except BaseException:
        Path(tmp.name).unlink(missing_ok=True)
        raise
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

    with tempfile.TemporaryDirectory() as work:
        dump = Path(work) / "database.dump"
        await asyncio.to_thread(backup.dump_database, settings.database_url, dump)
        meta = await archive_meta()
        return await asyncio.to_thread(_build_archive, dump, list(restore_roots().values()), meta)


async def archive_meta() -> dict:
    """meta.json of a full backup."""
    import datetime

    from celerp.db import get_session_ctx
    from celerp.migrations.compatibility import running_version
    from celerp.modules.registry import load_set

    async with get_session_ctx() as session:
        enabled_modules = await load_set(session)
    return {
        "celerp_version": running_version(),
        "pg_version": _pg_version(),
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "enabled_modules": enabled_modules,
    }
