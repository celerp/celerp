# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Private, short-lived company backup files.

A backup being downloaded and an upload waiting to be restored hold a company's whole
business, so both live in folders only this process's user can open, as files only it
can read. A download is written in its company's folder, so deleting the company deletes
it too, and is deleted once it is sent. An upload is kept only while its
restore can still go on: beside it a small metadata file names the file the owner
chose, and, once the restore is done, only the company it opened, so a retry whose
response was lost can be answered without the backup itself.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO

from celerp.services.company_files import company_backups_dir

logger = logging.getLogger(__name__)

UPLOAD_TTL_SECONDS = 24 * 3600
UPLOADS = "uploads"
UPLOAD_SUFFIX, EXPORT_SUFFIX, META_SUFFIX = ".upload", ".export", ".json"
_CHUNK = 1024 * 1024


class UploadTooLarge(Exception):
    """The upload passed the limit it was written under."""


def _private_dir(name: str) -> Path:
    root = company_backups_dir()
    folder = root / name
    folder.mkdir(parents=True, exist_ok=True)
    for f in (root, folder):
        os.chmod(f, 0o700)
    return folder


def private_file(path: Path):
    """A new file at ``path`` only this process's user can read, open for writing."""
    return os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb")


def export_path(company_id) -> Path:
    """Where to write a backup of ``company_id`` that is about to be downloaded."""
    return _private_dir(str(uuid.UUID(str(company_id)))) / f"{uuid.uuid4().hex}{EXPORT_SUFFIX}"


def stage_path(owner: str, token: str) -> Path | None:
    """The staged upload of ``owner`` under ``token``; None for a token no upload can have."""
    if not token or not token.isascii() or not token.isalnum():
        return None
    return _private_dir(UPLOADS) / f"{owner}-{token}{UPLOAD_SUFFIX}"


def _meta(stage: Path) -> Path:
    return stage.with_suffix(META_SUFFIX)


def _write_meta(stage: Path, facts: dict) -> None:
    partial = stage.with_suffix(META_SUFFIX + ".partial")
    partial.unlink(missing_ok=True)
    with private_file(partial) as out:
        out.write(json.dumps(facts).encode())
    partial.replace(_meta(stage))


def stage_upload(source: BinaryIO, stage: Path, *, limit: int, owner: str, mode: str,
                 file_name: str | None) -> None:
    """Write an upload to ``stage``, refusing it once it passes ``limit`` bytes, then its
    metadata."""
    digest, size = hashlib.sha256(), 0
    with private_file(stage) as out:
        while chunk := source.read(_CHUNK):
            size += len(chunk)
            if size > limit:
                raise UploadTooLarge
            digest.update(chunk)
            out.write(chunk)
    name = "".join(c for c in Path(file_name or "").name if c.isprintable())[:255]
    _write_meta(stage, {"file_name": name, "owner": owner, "mode": mode, "sha256": digest.hexdigest(),
                        "created_at": datetime.now(timezone.utc).isoformat()})


def stage_facts(stage: Path) -> dict:
    """The upload's metadata, empty when it has none."""
    try:
        facts = json.loads(_meta(stage).read_text())
    except (OSError, ValueError):
        return {}
    return facts if isinstance(facts, dict) else {}


def restored_company(stage: Path) -> str | None:
    """The company a finished restore of this upload opened."""
    return (stage_facts(stage).get("restored") or {}).get("company_id")


def discard_stage(stage: Path) -> None:
    stage.unlink(missing_ok=True)
    _meta(stage).unlink(missing_ok=True)


def finish_stage(stage: Path, company_id) -> None:
    """The restore is done: delete the backup and keep only which company it opened."""
    facts = stage_facts(stage)
    stage.unlink(missing_ok=True)
    kept = {k: facts[k] for k in ("owner", "mode", "created_at") if k in facts}
    try:
        _write_meta(stage, {**kept, "restored": {"company_id": str(company_id)}})
    except OSError:
        logger.warning("Recording a finished company restore failed", exc_info=True)


def purge_expired_uploads() -> None:
    """Delete uploads, and their metadata, older than UPLOAD_TTL_SECONDS."""
    cutoff = time.time() - UPLOAD_TTL_SECONDS
    for f in _private_dir(UPLOADS).iterdir():
        try:
            if f.is_file() and f.stat().st_mtime < cutoff:
                f.unlink(missing_ok=True)
        except FileNotFoundError:
            continue  # removed by another request meanwhile


def _is_uuid(name: str) -> bool:
    try:
        uuid.UUID(name)
    except ValueError:
        return False
    return True


def sweep_transient_files() -> None:
    """At startup, when no restore or download is in progress: delete every expired upload,
    metadata whose upload is gone and was never restored, half-written files, and every
    download a stopped process left behind."""
    purge_expired_uploads()
    for f in _private_dir(UPLOADS).iterdir():
        if f.name.endswith(".partial"):
            f.unlink(missing_ok=True)
        elif f.suffix == META_SUFFIX:
            stage = f.with_suffix(UPLOAD_SUFFIX)
            if not stage.exists() and restored_company(stage) is None:
                f.unlink(missing_ok=True)
    for f in company_backups_dir().iterdir():
        if f.is_dir() and _is_uuid(f.name):
            shutil.rmtree(f, ignore_errors=True)
