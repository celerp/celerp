# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""The migration scan store: uploaded source files waiting to be migrated.

An upload is streamed to disk under a fresh random token, bounded by the file
count, each source's per-file limit and an aggregate limit, recognised by content
and inspected read only. The scan lives at ``<data_dir>/migration_scans/<token>/``
(0700, files 0600) until it expires, is replaced by the same owner's next scan, or
is claimed by a run. A run records the scan's claim (``scan_claim``) in its committed
row before ``claim_for_run`` moves the files to ``<data_dir>/migration_runs/<run_id>/``,
so a claimed scan is always either still here or already in run storage.
Every rejection removes whatever was written. Updates to one token are serialized
by a file lock inside the token directory.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import secrets
import shutil
import time
import uuid
from collections.abc import AsyncIterable, AsyncIterator, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from fastapi import HTTPException

from celerp import config_store
from celerp.config import settings
from celerp.importers.adapters import registry
from celerp.importers.adapters.base import (
    Artifact,
    ArtifactSet,
    MappingQuestion,
    MigrationDecisions,
    ScanError,
    SourceAdapter,
    SourceRevisionError,
    SourceScan,
)
from celerp.importers.schema import CIFCoverageEntry, CIFMode

SCAN_TTL_SECONDS = 3600
MAX_ARTIFACTS = 5
MAX_AGGREGATE_BYTES = 2 * 1024**3

EXPIRED = "This scan has expired. Upload the file again."
BUSY = "This scan is being updated. Try again in a moment."
STORE_FAILED = "The file could not be stored. Try again."
SOURCE_MISSING = "The uploaded file for this migration is missing. Discard it and upload the file again."
_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{43}")
_STORED_RE = re.compile(r"artifact-\d+")
_SOURCE_KEY_MAX = 200
_WRITE_BYTES = 4096  # disk writes are block sized, so a failing disk is caught part way through a file
_HASH_CHUNK = 1024 * 1024  # a source can be gigabytes, so it is hashed a bounded chunk at a time

ScanOwner = tuple[str, "uuid.UUID | None"]


class ScanStoreError(HTTPException):
    """A scan request that cannot be served; the detail is shown to the user."""


class SourceMissingError(ScanStoreError):
    """A claimed scan's files are neither in the scan nor in the run's directory."""


@dataclass(frozen=True)
class UploadPart:
    """One multipart field, streamed: ``files`` parts carry a file, ``source`` the adapter key."""
    name: str
    filename: str | None
    chunks: AsyncIterator[bytes]


@dataclass(frozen=True)
class ScanSession:
    token: str
    owner: ScanOwner
    adapter_key: str
    artifacts: ArtifactSet
    scan: SourceScan
    decisions: MigrationDecisions | None
    expires_at: datetime


# ── Filesystem primitives (single points tests can fault) ────────────────────

def _now() -> float:
    return time.time()


def _open_artifact(path: Path):
    return os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb")


def _write_json(path: Path, payload: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    try:
        with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as fh:
            json.dump(payload, fh)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _remove_tree(path: Path) -> None:
    shutil.rmtree(path)


def _scans_root() -> Path:
    return Path(settings.data_dir) / "migration_scans"


def runs_root() -> Path:
    return Path(settings.data_dir) / "migration_runs"


def run_dir(run_id: uuid.UUID) -> Path:
    return runs_root() / str(run_id)


def remove_run_dir(run_id: uuid.UUID) -> None:
    """Delete a run's source files; raises OSError when the files cannot be removed."""
    path = run_dir(run_id)
    if path.exists():
        _remove_tree(path)


def _private_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)


def _discard(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


@contextmanager
def _token_lock(directory: Path) -> Iterator[None]:
    """Hold the token's lock, the same exclusive-create lock file protocol as the
    config and update locks, so it behaves identically on every platform."""
    try:
        release = config_store.hold_lock(str(directory / ".lock"))
    except FileNotFoundError as exc:  # the scan was removed while this request waited
        raise ScanStoreError(410, EXPIRED) from exc
    except OSError as exc:
        raise ScanStoreError(500, STORE_FAILED) from exc
    if release is None:
        raise ScanStoreError(409, BUSY)
    try:
        yield
    finally:
        release()


# ── Serialization ────────────────────────────────────────────────────────────

def _owner_json(owner: ScanOwner) -> list:
    kind, ident = owner
    return [kind, str(ident) if ident is not None else None]


def _scan_json(scan: SourceScan) -> dict:
    return {
        "source_system": scan.source_system,
        "source_schema_version": scan.source_schema_version,
        "company_name": scan.company_name,
        "base_currency": scan.base_currency,
        "period_start": scan.period_start.isoformat() if scan.period_start else None,
        "period_end": scan.period_end.isoformat() if scan.period_end else None,
        "currencies": list(scan.currencies),
        "object_counts": dict(scan.object_counts),
        "features": list(scan.features),
        "coverage": [c.model_dump(mode="json") for c in scan.coverage],
        "questions": [{**asdict(q), "options": list(q.options)} for q in scan.questions],
        "lock_date": scan.lock_date.isoformat() if scan.lock_date else None,
    }


def _scan_from_json(data: dict) -> SourceScan:
    def _date(value):
        return date.fromisoformat(value) if value else None

    return SourceScan(
        source_system=data["source_system"],
        source_schema_version=data["source_schema_version"],
        company_name=data["company_name"],
        base_currency=data["base_currency"],
        period_start=_date(data["period_start"]),
        period_end=_date(data["period_end"]),
        currencies=tuple(data["currencies"]),
        object_counts={str(k): int(v) for k, v in data["object_counts"].items()},
        features=tuple(data["features"]),
        coverage=tuple(CIFCoverageEntry(**c) for c in data["coverage"]),
        questions=tuple(MappingQuestion(**{**q, "options": tuple(q["options"])}) for q in data["questions"]),
        lock_date=_date(data["lock_date"]),
    )


def decisions_json(decisions: MigrationDecisions) -> dict:
    """The stored and displayed form of a decision set."""
    return {
        "mode": str(decisions.mode),
        "cutover_date": decisions.cutover_date.isoformat() if decisions.cutover_date else None,
        "mappings": dict(decisions.mappings),
        "prepared_by": decisions.prepared_by,
    }


def decisions_from_json(data: dict | None) -> MigrationDecisions | None:
    if data is None:
        return None
    cutover = data.get("cutover_date")
    return MigrationDecisions(
        mode=CIFMode(data["mode"]),
        cutover_date=date.fromisoformat(cutover) if cutover else None,
        mappings=dict(data.get("mappings") or {}),
        prepared_by=data.get("prepared_by"),
    )


def _session_from_json(token: str, directory: Path, data: dict) -> ScanSession:
    artifacts = []
    for a in data["artifacts"]:
        if not isinstance(a.get("stored_name"), str) or not _STORED_RE.fullmatch(a["stored_name"]):
            raise ValueError("stored artifact name is not a scan artifact")
        path = directory / a["stored_name"]
        if not path.is_file():
            raise ValueError("stored artifact is missing")
        artifacts.append(Artifact(path, str(a["original_name"]), int(a["size_bytes"]), str(a["sha256"])))
    kind, ident = data["owner"]
    return ScanSession(
        token=token,
        owner=(kind, uuid.UUID(ident) if ident else None),
        adapter_key=data["adapter_key"],
        artifacts=artifacts,
        scan=_scan_from_json(data["scan"]),
        decisions=decisions_from_json(data.get("decisions")),
        expires_at=datetime.fromtimestamp(float(data["expires_at"]), timezone.utc),
    )


# ── Upload ───────────────────────────────────────────────────────────────────

def _sanitize_name(filename: str | None) -> str:
    base = re.split(r"[\\/]", filename or "")[-1]
    base = "".join(ch for ch in base if ch.isprintable()).strip()
    return base[:255] or "upload"


def _per_file_limit(adapter: SourceAdapter | None) -> int:
    adapters = (adapter,) if adapter is not None else registry.list_adapters()
    return max((spec.max_bytes for a in adapters for spec in a.artifact_specs), default=0)


async def _read_text(chunks: AsyncIterator[bytes]) -> str:
    data = b""
    async for chunk in chunks:
        data += chunk
        if len(data) > _SOURCE_KEY_MAX:
            raise ScanStoreError(422, "This source is not available.")
    return data.decode("utf-8", errors="replace").strip()


async def _stream_file(part: UploadPart, path: Path, *, per_file: int, remaining: int) -> Artifact:
    digest, size = hashlib.sha256(), 0
    try:
        fh = _open_artifact(path)
    except OSError as exc:
        raise ScanStoreError(500, STORE_FAILED) from exc
    try:
        async for chunk in part.chunks:
            size += len(chunk)
            if size > per_file:
                raise ScanStoreError(413, "This file is larger than this source allows.")
            if size > remaining:
                raise ScanStoreError(413, "The upload is larger than the allowed total.")
            digest.update(chunk)
            try:
                for start in range(0, len(chunk), _WRITE_BYTES):
                    fh.write(chunk[start:start + _WRITE_BYTES])
            except OSError as exc:
                raise ScanStoreError(500, STORE_FAILED) from exc
    finally:
        fh.close()
    if size == 0:
        raise ScanStoreError(422, "The uploaded file is empty.")
    return Artifact(path, _sanitize_name(part.filename), size, digest.hexdigest())


async def _receive(files: AsyncIterable[UploadPart], directory: Path) -> tuple[ArtifactSet, SourceAdapter | None]:
    artifacts: ArtifactSet = []
    adapter: SourceAdapter | None = None
    total = 0
    async for part in files:
        if part.name == "source":
            key = await _read_text(part.chunks)
            if key:
                adapter = registry.get_adapter(key)
                if adapter is None:
                    raise ScanStoreError(422, "This source is not available.")
        elif part.name == "files" and part.filename:
            if len(artifacts) >= MAX_ARTIFACTS:
                raise ScanStoreError(413, f"Upload at most {MAX_ARTIFACTS} files.")
            path = directory / f"artifact-{len(artifacts) + 1}"
            artifact = await _stream_file(part, path, per_file=_per_file_limit(adapter),
                                          remaining=MAX_AGGREGATE_BYTES - total)
            total += artifact.size_bytes
            artifacts.append(artifact)
        else:
            async for _ in part.chunks:
                pass
    if not artifacts:
        raise ScanStoreError(422, "Choose a file to upload.")
    return artifacts, adapter


def _recognise(artifacts: ArtifactSet, chosen: SourceAdapter | None) -> SourceAdapter:
    """The adapter that reads the upload. A recognised source saved at a file format
    revision its adapter does not read is refused with that adapter's own message."""
    try:
        if chosen is not None:
            if not chosen.detect(artifacts).matched:
                raise ScanStoreError(422, f"This file is not a {chosen.display_name} file.")
            return chosen
        adapter = registry.detect_adapter(artifacts)
    except SourceRevisionError as exc:
        raise ScanStoreError(422, str(exc)) from exc
    if adapter is None:
        raise ScanStoreError(422, "Celerp cannot read this file yet.")
    return adapter


def _owned_tokens(owner: ScanOwner) -> list[str]:
    root = _scans_root()
    if not root.exists():
        return []
    wanted = _owner_json(owner)
    tokens = []
    for directory in root.iterdir():
        try:
            if json.loads((directory / "scan.json").read_text())["owner"] == wanted:
                tokens.append(directory.name)
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return tokens


async def create_scan(files: AsyncIterable[UploadPart], *, owner: ScanOwner) -> ScanSession:
    """Store and inspect an upload. The owner's previous scan is replaced on success."""
    purge_expired()
    token = secrets.token_urlsafe(32)
    root = _scans_root()
    _private_dir(root)
    directory = root / token
    directory.mkdir(mode=0o700)
    try:
        artifacts, chosen = await _receive(files, directory)
        adapter = _recognise(artifacts, chosen)
        try:
            scan = await asyncio.to_thread(adapter.inspect, artifacts)
        except (ScanError, SourceRevisionError) as exc:
            raise ScanStoreError(422, str(exc)) from exc
        previous = _owned_tokens(owner)
        expires_at = _now() + SCAN_TTL_SECONDS
        try:
            _write_json(directory / "scan.json", {
                "owner": _owner_json(owner),
                "adapter_key": adapter.key,
                "artifacts": [{"original_name": a.original_name, "stored_name": a.path.name,
                               "size_bytes": a.size_bytes, "sha256": a.sha256} for a in artifacts],
                "expires_at": expires_at,
                "scan": _scan_json(scan),
                "decisions": None,
            })
        except OSError as exc:
            raise ScanStoreError(500, STORE_FAILED) from exc
    except BaseException:
        _discard(directory)
        raise
    for old in previous:
        _discard(root / old)
    return ScanSession(token, owner, adapter.key, artifacts, scan, None,
                       datetime.fromtimestamp(expires_at, timezone.utc))


# ── Token access ─────────────────────────────────────────────────────────────

def _directory(token: str) -> Path:
    if not isinstance(token, str) or not _TOKEN_RE.fullmatch(token):
        raise ScanStoreError(410, EXPIRED)
    directory = _scans_root() / token
    if not directory.is_dir():
        raise ScanStoreError(410, EXPIRED)
    return directory


def _read(token: str, directory: Path, owner: ScanOwner) -> ScanSession:
    try:
        data = json.loads((directory / "scan.json").read_text())
        session = _session_from_json(token, directory, data)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        _discard(directory)
        raise ScanStoreError(410, EXPIRED) from exc
    if data["owner"] != _owner_json(owner):
        raise ScanStoreError(410, EXPIRED)
    if float(data["expires_at"]) <= _now():
        _discard(directory)
        raise ScanStoreError(410, EXPIRED)
    return session


def load_scan(token: str, *, owner: ScanOwner) -> ScanSession:
    return _read(token, _directory(token), owner)


def save_decisions(token: str, *, owner: ScanOwner, decisions: MigrationDecisions) -> ScanSession:
    directory = _directory(token)
    with _token_lock(directory):
        _read(token, directory, owner)
        path = directory / "scan.json"
        try:
            data = json.loads(path.read_text())
            data["decisions"] = decisions_json(decisions)
            _write_json(path, data)
        except OSError as exc:
            raise ScanStoreError(500, STORE_FAILED) from exc
        return _read(token, directory, owner)


def file_sha256(path: Path) -> str:
    """The SHA-256 of a stored file, read a bounded chunk at a time."""
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(_HASH_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def scan_claim(token: str) -> str:
    """The durable, non-secret identity a run keeps of the scan it was started from."""
    return hashlib.sha256(token.encode()).hexdigest()


def find_scan_token(claim: str) -> str | None:
    """The token of the stored scan whose claim is *claim*, if it is still stored."""
    root = _scans_root()
    if not root.exists():
        return None
    return next((d.name for d in root.iterdir() if scan_claim(d.name) == claim), None)


def artifact_changed(artifact: Artifact) -> bool:
    """Whether a stored file is no longer the one scanned: the size first, then the hash.
    Blocking; async callers run it in a worker thread."""
    return artifact.path.stat().st_size != artifact.size_bytes or file_sha256(artifact.path) != artifact.sha256


def verify_unchanged(token: str, *, owner: ScanOwner) -> ScanSession:
    """The scan, after checking its files are the ones that were scanned; a changed file
    discards the scan. A size change is caught before any hashing."""
    directory = _directory(token)
    with _token_lock(directory):
        session = _read(token, directory, owner)
        for artifact in session.artifacts:
            try:
                changed = artifact_changed(artifact)
            except OSError as exc:
                raise ScanStoreError(500, STORE_FAILED) from exc
            if changed:
                _discard(directory)
                raise ScanStoreError(409, "The uploaded file changed after it was scanned. Upload it again.")
    return session


def claim_for_run(token: str | None, *, run_id: uuid.UUID, stored_names: list[str]) -> None:
    """Move a claimed scan's files into the run's directory, then remove the scan.

    Idempotent: a file already in the run's directory stays there, so a start that died
    part way is finished by calling again. Raises ``SourceMissingError`` when a file is
    in neither place. The claim, not the scan's expiry, decides whether the files belong
    to the run, and they were verified when the claim was made."""
    target = run_dir(run_id)
    directory = _scans_root() / token if token and _TOKEN_RE.fullmatch(token) else None
    moves = []
    for name in stored_names:
        if not _STORED_RE.fullmatch(name):
            raise ValueError("stored artifact name is not a scan artifact")
        if (target / name).is_file():
            continue
        if directory is None or not (directory / name).is_file():
            raise SourceMissingError(410, SOURCE_MISSING)
        moves.append(name)
    if moves:
        _private_dir(runs_root())
        target.mkdir(mode=0o700, exist_ok=True)
    for name in moves:
        os.replace(directory / name, target / name)
    if directory is not None:
        _discard(directory)


def delete_scan(token: str) -> None:
    if isinstance(token, str) and _TOKEN_RE.fullmatch(token):
        _discard(_scans_root() / token)


def purge_expired() -> int:
    """Delete expired or unreadable scans; an upload still being written is left alone."""
    root = _scans_root()
    if not root.exists():
        return 0
    purged = 0
    now = _now()
    for directory in root.iterdir():
        try:
            expired = float(json.loads((directory / "scan.json").read_text())["expires_at"]) <= now
        except FileNotFoundError:
            expired = directory.stat().st_mtime + SCAN_TTL_SECONDS <= now
        except (OSError, ValueError, KeyError, TypeError):
            expired = True
        if expired:
            _discard(directory)
            purged += 1
    return purged
