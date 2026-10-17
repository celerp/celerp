# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Private, short-lived company backup files.

A backup being downloaded and an upload waiting to be restored hold a company's whole
business, so both live in folders only this process's user can open, as files only it
can read. A download is written in its company's folder, so deleting the company deletes
it too, and is deleted once it is sent. An upload is kept only while its
restore can still go on: beside it a small metadata file names the file the owner
chose, and, once the restore is done, only the company it opened, so a retry whose
response was lost can be answered without the backup itself. A backup taken of a company
before a copy replaced it is kept, by the company's lineage, until its owner deletes it.
The key of an export request's link is kept here too, as only this installation may read
the files a client uploads through it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO

from celerp.services.company_files import SENT, company_backups_dir

logger = logging.getLogger(__name__)

UPLOAD_TTL_SECONDS = 24 * 3600
RECEIPT_TTL_SECONDS = 90 * 24 * 3600
UPLOADS = "uploads"
KEPT = "kept"
EXPORT_REQUESTS = "export-requests"
UPLOAD_SUFFIX, EXPORT_SUFFIX, META_SUFFIX, KEPT_SUFFIX = ".upload", ".export", ".json", ".celerp-company"
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


def _write_json(path: Path, facts: dict) -> None:
    """Write ``facts`` to the metadata file ``path`` whole, or leave it as it was."""
    partial = path.with_suffix(META_SUFFIX + ".partial")
    partial.unlink(missing_ok=True)
    with private_file(partial) as out:
        out.write(json.dumps(facts).encode())
        out.flush()
        os.fsync(out.fileno())
    partial.replace(path)


def _write_meta(stage: Path, facts: dict) -> None:
    _write_json(_meta(stage), facts)


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


def bind_handoff(stage: Path, copy_id: str, proof: str) -> None:
    """Attach a handoff receipt to one privately staged, validated backup."""
    try:
        canonical = str(uuid.UUID(copy_id))
    except (ValueError, TypeError):
        raise ValueError("Invalid company copy link") from None
    if canonical != copy_id or not re.fullmatch(r"[0-9a-f]{64}", proof):
        raise ValueError("Invalid company copy link")
    facts = stage_facts(stage)
    if not stage.is_file() or not facts.get("owner"):
        raise ValueError("The company copy upload is unavailable")
    receipt = {"copy_id": copy_id, "proof": proof}
    if "handoff" in facts and facts["handoff"] != receipt:
        raise ValueError("A different handoff is already associated with this upload")
    _write_meta(stage, {**facts, "handoff": receipt})


def staged_handoff(stage: Path) -> dict | None:
    """The handoff bound to this upload once its link was verified, restored or not."""
    value = stage_facts(stage).get("handoff")
    if not isinstance(value, dict):
        return None
    try:
        canonical = str(uuid.UUID(value["copy_id"]))
    except (KeyError, TypeError, ValueError):
        return None
    if canonical != value["copy_id"] or not re.fullmatch(r"[0-9a-f]{64}", str(value.get("proof"))):
        return None
    return value


def remember_open(stage: Path, intent: dict) -> None:
    """Record that the owner chose to open this upload once its modules run."""
    _write_meta(stage, {**stage_facts(stage), "open": intent})


def take_open(stage: Path) -> dict | None:
    """The open recorded for this upload, removed so it is acted on once."""
    facts = stage_facts(stage)
    intent = facts.pop("open", None)
    if intent is None:
        return None
    _write_meta(stage, facts)
    return intent if isinstance(intent, dict) else None


def handoff_receipt(stage: Path) -> dict | None:
    """The handoff of an upload whose restore has finished: the receipt to report."""
    restored = stage_facts(stage).get("restored")
    if not isinstance(restored, dict) or not restored.get("company_id"):
        return None
    return staged_handoff(stage)


def pending_handoffs(*, reported: bool | None = None) -> list[tuple[Path, dict]]:
    """Read receipt records retained after a verified restore, never the backup content."""
    result = []
    for metadata in _private_dir(UPLOADS).glob("*.json"):
        stage = metadata.with_suffix(UPLOAD_SUFFIX)
        receipt = handoff_receipt(stage)
        if receipt and (reported is None or bool(receipt.get("reported")) == reported):
            result.append((stage, receipt))
    return sorted(result, key=lambda pair: pair[0].name)


def mark_handoff_reported(stage: Path) -> None:
    receipt = handoff_receipt(stage)
    if receipt is None:
        return
    facts = stage_facts(stage)
    _write_meta(stage, {**facts, "handoff": {**receipt, "reported": True}})


def acknowledge_handoff(copy_id: str) -> None:
    """Stop reporting a handoff after the cloud confirms attribution."""
    for stage, receipt in pending_handoffs():
        if receipt["copy_id"] == copy_id:
            facts = stage_facts(stage)
            facts.pop("handoff", None)
            _write_meta(stage, facts)


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
    kept = {k: facts[k] for k in ("owner", "mode", "created_at", "handoff") if k in facts}
    try:
        _write_meta(stage, {**kept, "restored": {"company_id": str(company_id)}})
    except OSError:
        logger.warning("Recording a finished company restore failed", exc_info=True)


def purge_expired_uploads() -> None:
    """Delete uploads, and their metadata, older than UPLOAD_TTL_SECONDS."""
    cutoff = time.time() - UPLOAD_TTL_SECONDS
    for f in _private_dir(UPLOADS).iterdir():
        try:
            if not f.is_file() or f.stat().st_mtime >= cutoff:
                continue
            if f.suffix == META_SUFFIX and handoff_receipt(f.with_suffix(UPLOAD_SUFFIX)):
                if f.stat().st_mtime >= time.time() - RECEIPT_TTL_SECONDS:
                    continue
            f.unlink(missing_ok=True)
        except FileNotFoundError:
            continue  # removed by another request meanwhile


def _sent_record(company_id, copy_id) -> Path:
    folder = _private_dir(SENT) / str(uuid.UUID(str(company_id)))
    folder.mkdir(exist_ok=True)
    os.chmod(folder, 0o700)
    return folder / f"{uuid.UUID(str(copy_id))}{META_SUFFIX}"


def remember_sent_copy(company_id, copy_id, facts: dict) -> None:
    """Keep what this installation alone knows about a copy it sent: the link's key
    and who it went to. The cloud never stores the key, so Copy link needs this."""
    _write_json(_sent_record(company_id, copy_id), facts)


def sent_copy(company_id, copy_id) -> dict:
    """The facts kept for a sent copy; empty when this installation has none."""
    try:
        facts = json.loads(_sent_record(company_id, copy_id).read_text())
    except (OSError, ValueError):
        return {}
    return facts if isinstance(facts, dict) else {}


def remember_sender(firm_name: str, reply_to: str) -> None:
    """Keep the firm name and reply address of the last confirmed send, so the next send
    from any company on this installation starts with them."""
    _write_json(_private_dir(SENT) / f"sender{META_SUFFIX}", {"firm_name": firm_name, "reply_to": reply_to})


def last_sender() -> dict:
    """The last send's firm name and reply address; empty strings when there was none."""
    try:
        facts = json.loads((_private_dir(SENT) / f"sender{META_SUFFIX}").read_text())
    except (OSError, ValueError):
        facts = {}
    facts = facts if isinstance(facts, dict) else {}
    return {k: facts[k] if isinstance(facts.get(k), str) else "" for k in ("firm_name", "reply_to")}


def forget_sent_copy(company_id, copy_id) -> None:
    _sent_record(company_id, copy_id).unlink(missing_ok=True)


def export_requests_dir() -> Path:
    """The private folder holding the keys of export requests sent from here, and the files
    being read from them."""
    return _private_dir(EXPORT_REQUESTS)


def _export_request_record(operation_id) -> Path:
    try:
        canonical = str(uuid.UUID(str(operation_id)))
    except ValueError:
        raise ValueError("Invalid export request") from None
    return export_requests_dir() / f"{canonical}{META_SUFFIX}"


def remember_export_request_key(operation_id, key: str) -> None:
    """Keep the key of an export request's link. The cloud never stores it, so only this
    installation can read the files the client uploads."""
    _write_json(_export_request_record(operation_id), {"key": key})


def export_request_key(operation_id) -> str | None:
    """The key kept for the export request sent as ``operation_id``; None when there is none."""
    try:
        facts = json.loads(_export_request_record(operation_id).read_text())
    except (OSError, ValueError):
        return None
    key = facts.get("key") if isinstance(facts, dict) else None
    return key if isinstance(key, str) else None


def forget_export_request_key(operation_id) -> None:
    _export_request_record(operation_id).unlink(missing_ok=True)


def _kept_dir(lineage) -> Path:
    folder = _private_dir(KEPT) / str(uuid.UUID(str(lineage)))
    folder.mkdir(exist_ok=True)
    os.chmod(folder, 0o700)
    return folder


def new_kept(lineage) -> tuple[str, Path]:
    """A new kept backup's id and where to write it, among its lineage's."""
    kept_id = uuid.uuid4().hex
    return kept_id, _kept_dir(lineage) / f"{kept_id}{KEPT_SUFFIX}"


def remember_kept(lineage, kept_id: str, facts: dict) -> None:
    _write_json(_kept_dir(lineage) / f"{kept_id}{META_SUFFIX}", facts)


def kept_path(lineage, kept_id: str) -> Path | None:
    """The kept backup ``kept_id`` of ``lineage``; None when there is none."""
    if not re.fullmatch(r"[0-9a-f]{32}", kept_id or ""):
        return None
    path = _kept_dir(lineage) / f"{kept_id}{KEPT_SUFFIX}"
    return path if path.is_file() and path.with_suffix(META_SUFFIX).is_file() else None


def kept_backups(lineage) -> list[dict]:
    """Every kept backup of ``lineage`` with its facts, the newest kept first."""
    found = []
    for meta in _kept_dir(lineage).glob(f"*{META_SUFFIX}"):
        try:
            facts = json.loads(meta.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(facts, dict) and kept_path(lineage, meta.stem) is not None:
            found.append({**facts, "id": meta.stem})
    return sorted(found, key=lambda f: str(f.get("kept_at", "")), reverse=True)


def discard_kept(lineage, kept_id: str) -> None:
    folder = _kept_dir(lineage)
    for suffix in (KEPT_SUFFIX, META_SUFFIX):
        (folder / f"{kept_id}{suffix}").unlink(missing_ok=True)


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
    # A kept backup a stopped process left half made has no metadata yet.
    for f in _private_dir(KEPT).glob("*/*"):
        if f.name.endswith(".partial") or (f.suffix == KEPT_SUFFIX and not f.with_suffix(META_SUFFIX).exists()):
            f.unlink(missing_ok=True)
    # Export request keys stay; files a stopped read left behind do not.
    for f in export_requests_dir().iterdir():
        if f.is_dir():
            shutil.rmtree(f, ignore_errors=True)
        elif f.name.endswith(".partial"):
            f.unlink(missing_ok=True)
