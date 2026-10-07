# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Marketplace downloads waiting for Install, one stage per download.

Each download is staged under its own random reference (``mp_`` plus 32 hex
digits). A stage is a directory holding the package bytes and a details file with
the module slug and version, the official and paid flags, the SHA-256 of the
package and the time it was staged. The details file is written last, so a stage
without it is incomplete.

Reading a stage checks the package against its recorded SHA-256. A stage lasts
15 minutes; an expired, incomplete or missing stage means downloading again.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from celerp.config import settings

REF_RE = re.compile(r"^mp_[0-9a-f]{32}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
TTL_SECONDS = 15 * 60
_DIR_MODE = 0o700
_FILE_MODE = 0o600
_FIELDS = {"slug", "version", "is_official", "is_paid", "sha256", "created_at"}


class StageGone(Exception):
    """The stage is missing, incomplete or expired: download again."""


@dataclass(frozen=True)
class Stage:
    data: bytes
    slug: str
    version: str
    is_official: bool
    is_paid: bool


def stage_dir() -> Path:
    return Path(settings.data_dir) / "marketplace-downloads"


def stage_paths(ref: str) -> tuple[Path, Path] | None:
    """Return the (package, details) paths for a well-formed reference, else None."""
    if not isinstance(ref, str) or not REF_RE.fullmatch(ref):
        return None
    d = stage_dir() / ref
    return d / "package.zip", d / "details.json"


def _write_private(path: Path, data: bytes) -> None:
    """Write *data* to *path* whole or not at all, with owner-only permissions."""
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, _FILE_MODE)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _sweep_expired() -> None:
    """Remove every entry of the download area older than the stage lifetime."""
    cutoff = time.time() - TTL_SECONDS
    for entry in stage_dir().iterdir():
        try:
            if entry.lstat().st_mtime < cutoff:
                if entry.is_dir() and not entry.is_symlink():
                    shutil.rmtree(entry, ignore_errors=True)
                else:
                    entry.unlink(missing_ok=True)
        except OSError:
            continue


def write_stage(data: bytes, *, slug: str, version: str, is_official: bool,
                is_paid: bool, sha256: str) -> str:
    """Stage *data* with its details and return the new reference."""
    base = stage_dir()
    base.mkdir(mode=_DIR_MODE, parents=True, exist_ok=True)
    os.chmod(base, _DIR_MODE)
    _sweep_expired()
    ref = f"mp_{secrets.token_hex(16)}"
    package, details = stage_paths(ref)
    package.parent.mkdir(mode=_DIR_MODE)
    try:
        _write_private(package, data)
        _write_private(details, json.dumps({
            "slug": slug, "version": version, "is_official": is_official,
            "is_paid": is_paid, "sha256": sha256, "created_at": time.time(),
        }).encode())
    except BaseException:
        shutil.rmtree(package.parent, ignore_errors=True)
        raise
    return ref


def read_stage(ref: str) -> Stage:
    """Return the stage *ref* names. Raises ValueError for a malformed reference
    and StageGone for a missing, incomplete or expired stage (which is removed)."""
    paths = stage_paths(ref)
    if paths is None:
        raise ValueError("malformed reference")
    package, details = paths
    try:
        meta = json.loads(details.read_text(encoding="utf-8"))
        data = package.read_bytes()
    except (OSError, ValueError):
        raise StageGone
    if (not isinstance(meta, dict) or set(meta) != _FIELDS
            or not isinstance(meta["slug"], str) or not meta["slug"]
            or not isinstance(meta["version"], str) or not meta["version"]
            or not isinstance(meta["is_official"], bool)
            or not isinstance(meta["is_paid"], bool)
            or not isinstance(meta["sha256"], str) or not SHA256_RE.fullmatch(meta["sha256"])
            or not isinstance(meta["created_at"], (int, float)) or isinstance(meta["created_at"], bool)
            or hashlib.sha256(data).hexdigest() != meta["sha256"]):
        raise StageGone
    if time.time() - meta["created_at"] > TTL_SECONDS:
        remove_stage(ref)
        raise StageGone
    return Stage(data=data, slug=meta["slug"], version=meta["version"],
                 is_official=meta["is_official"], is_paid=meta["is_paid"])


def remove_stage(ref: str) -> None:
    """Remove the stage *ref* names, if any."""
    paths = stage_paths(ref)
    if paths is not None:
        shutil.rmtree(paths[0].parent, ignore_errors=True)
