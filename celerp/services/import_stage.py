# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Staged import files: uploaded CSV data kept on disk for one company between the
upload and the confirmed import, keyed by an opaque random reference.

Staging avoids round-tripping large CSV data through hidden form fields (Starlette
enforces a 1 MB multipart field limit that breaks large imports). A reference is only
ever matched against REF_RE and never used as a path fragment until it has matched.
Stages can hold customer, cost and tax data, so the directory and every file in it are
readable by the server's own user only, whatever the process umask.

Each stage is a ``<ref>.meta`` naming its company, written first, and a ``<ref>.csv``.
A stage's files therefore always carry their company, and deleting a company deletes
its stages."""

from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from pathlib import Path

from celerp.config import settings

logger = logging.getLogger(__name__)

REF_RE = re.compile(r"^imp_[0-9a-f]{32}$")
MAX_AGE_SECONDS = 24 * 3600
_DIR_MODE = 0o700
_FILE_MODE = 0o600


def stage_dir() -> Path:
    return Path(settings.data_dir) / "import_staging"


def stage_paths(ref: str) -> tuple[Path, Path] | None:
    """Return the (csv, meta) paths for a well-formed reference, else None."""
    if not isinstance(ref, str) or not REF_RE.fullmatch(ref):
        return None
    base = stage_dir()
    return base / f"{ref}.csv", base / f"{ref}.meta"


def _write_private(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically with owner-only permissions.

    The content goes to a temporary sibling first and replaces ``path`` in one
    step, so a reader never sees a partial file. The temporary name starts with
    the stage reference, so cleanup treats a leftover one as part of that stage.
    """
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, _FILE_MODE)
    try:
        os.fchmod(fd, _FILE_MODE)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def write_stage(company_id: str, csv_text: str) -> str:
    """Stage ``csv_text`` for ``company_id`` and return its reference."""
    if not company_id:
        raise ValueError("import staging requires a company")
    base = stage_dir()
    base.mkdir(mode=_DIR_MODE, parents=True, exist_ok=True)
    os.chmod(base, _DIR_MODE)
    cleanup_expired()
    ref = f"imp_{uuid.uuid4().hex}"
    csv_path, meta_path = stage_paths(ref)
    try:
        _write_private(meta_path, json.dumps({"company_id": str(company_id), "created_at": time.time()}))
        _write_private(csv_path, csv_text)
    except BaseException:
        delete_ref(ref)
        raise
    return ref


def read_stage(company_id: str, ref: str) -> str | None:
    """Stage content for ``ref`` if it belongs to ``company_id`` and is fresh."""
    paths = stage_paths(ref)
    if paths is None or not company_id:
        return None
    csv_path, meta_path = paths
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        created_at = float(meta["created_at"])
        owner = str(meta["company_id"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if time.time() - created_at > MAX_AGE_SECONDS:
        delete_ref(ref)
        return None
    if owner != str(company_id):
        return None
    try:
        return csv_path.read_text(encoding="utf-8")
    except OSError:
        return None


def delete_ref(ref: str) -> None:
    """Remove a stage pair. Malformed references are ignored."""
    paths = stage_paths(ref)
    if paths is None:
        return
    for path in paths:
        path.unlink(missing_ok=True)


def _stages() -> dict[str, list[Path]]:
    """Every stage on disk, by reference, with all of its files (leftover temporaries included)."""
    base = stage_dir()
    if not base.is_dir():
        return {}
    stages: dict[str, list[Path]] = {}
    for path in base.glob("imp_*"):
        ref = path.name.split(".", 1)[0]
        if REF_RE.fullmatch(ref):
            stages.setdefault(ref, []).append(path)
    return stages


def _meta(ref: str) -> dict | None:
    try:
        meta = json.loads((stage_dir() / f"{ref}.meta").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return meta if isinstance(meta, dict) else None


def _stage_created_at(ref: str, files: list[Path]) -> float:
    """When a stage was written: its metadata timestamp, or for a stage whose
    metadata is missing or unreadable, the newest modification time of its files."""
    try:
        return float((_meta(ref) or {})["created_at"])
    except (KeyError, ValueError, TypeError):
        mtimes = []
        for path in files:
            try:
                mtimes.append(path.stat().st_mtime)
            except OSError:
                continue
        return max(mtimes, default=0.0)


def cleanup_expired() -> int:
    """Delete stages older than the retention window and return how many.

    Files are grouped by stage reference, so a lone ``.csv`` or ``.meta`` left
    by an interrupted write expires like a complete stage. Every stage file
    that is kept is set to owner-only permissions.
    """
    removed = 0
    cutoff = time.time() - MAX_AGE_SECONDS
    for ref, files in _stages().items():
        if _stage_created_at(ref, files) < cutoff:
            for path in files:
                path.unlink(missing_ok=True)
            removed += 1
            continue
        for path in files:
            try:
                os.chmod(path, _FILE_MODE)
            except OSError:
                logger.warning("Could not restrict permissions of staged import %s", ref)
    return removed


def delete_company_stages(company_id) -> None:
    """Delete every stage of ``company_id``; stages already gone count as deleted. Raises
    when a file cannot be deleted."""
    for ref, files in _stages().items():
        if str((_meta(ref) or {}).get("company_id")) == str(company_id):
            # The metadata goes last, so a retry after a failure still finds the stage.
            for path in sorted(files, key=lambda p: p.name == f"{ref}.meta"):
                path.unlink(missing_ok=True)
