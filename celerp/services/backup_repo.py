# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Content-addressed cloud snapshot client.

One cloud backup path for both database and files. Every member of a backup (the
pg_dump and each attachment/upload file) is a content-addressed *blob* keyed by the
SHA-256 of its plaintext, encrypted client-side and stored once on the relay. A
*snapshot* is a small manifest referencing the blobs needed to rebuild a
``.celerp-backup``. Unchanged files share a hash and are never re-uploaded, so a
daily backup sends only what changed.

Restore downloads the manifest, fetches its blobs, reassembles a ``.celerp-backup``
and feeds the canonical ``run_import`` engine — the same importer as a local restore.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import tarfile
import tempfile
from pathlib import Path

import httpx

from celerp.config import settings
from celerp.gateway.state import get_session_token, relay_http_url, relay_session_headers
from celerp.services.backup import BackupResult, _parse_key, decrypt, dump_database, encrypt

log = logging.getLogger(__name__)

_CHUNK = 1024 * 1024  # 1 MiB read window for streaming hashes
# Each AES-256-GCM blob carries a 12-byte nonce + 16-byte tag over the plaintext.
_ENC_OVERHEAD = 28


def _attachment_dirs() -> list[Path]:
    return [settings.data_dir / "static" / "attachments", settings.data_dir / "ai_uploads"]


def _members() -> list[tuple[str, Path]]:
    """(arcname, path) for every backed-up file, arcname relative to the dir's parent
    so it round-trips through the same ``.celerp-backup`` layout as a local export."""
    out: list[tuple[str, Path]] = []
    for d in _attachment_dirs():
        if not d.exists():
            continue
        for p in sorted(d.rglob("*")):
            if p.is_file():
                try:
                    rel = str(p.relative_to(d.parent))
                except ValueError:
                    rel = p.name
                out.append((rel, p))
    return out


def _hash_file(path: Path) -> tuple[str, int]:
    h = hashlib.sha256()
    size = 0
    with path.open("rb") as fh:
        while chunk := fh.read(_CHUNK):
            h.update(chunk)
            size += len(chunk)
    return h.hexdigest(), size


async def _build_meta() -> dict:
    from celerp.services.backup_export import _pg_version, _read_company_enabled_modules, _version
    import datetime as _dt

    from celerp.config import read_config
    cfg = read_config()
    return {
        "celerp_version": _version(),
        "pg_version": _pg_version(),
        "created_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "company_name": cfg.get("company", {}).get("name", "unknown"),
        "enabled_modules": await _read_company_enabled_modules(),
    }


# ── Relay API ─────────────────────────────────────────────────────────────────

def _relay() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=relay_http_url(), headers=relay_session_headers(), timeout=60
    )


async def _missing(client: httpx.AsyncClient, hashes: set[str]) -> set[str]:
    r = await client.post("/repo/missing", json={"hashes": sorted(hashes)})
    r.raise_for_status()
    return set(r.json()["missing"])


async def _put_blob(client: httpx.AsyncClient, blob_hash: str, plaintext: bytes, key: bytes) -> int:
    """Encrypt and upload one blob via a presigned PUT. Returns stored (encrypted) size."""
    r = await client.post(f"/repo/blob/{blob_hash}/upload-url")
    r.raise_for_status()
    encrypted = await asyncio.to_thread(encrypt, plaintext, key)
    async with httpx.AsyncClient(timeout=300) as s3:
        put = await s3.put(r.json()["url"], content=encrypted)
        put.raise_for_status()
    return len(encrypted)


async def _get_blob(client: httpx.AsyncClient, blob_hash: str, key: bytes) -> bytes:
    """Download, decrypt and integrity-check one blob against its content hash."""
    r = await client.get(f"/repo/blob/{blob_hash}/download-url")
    r.raise_for_status()
    async with httpx.AsyncClient(timeout=300) as s3:
        dl = await s3.get(r.json()["url"])
        dl.raise_for_status()
    plaintext = await asyncio.to_thread(decrypt, dl.content, key)
    if hashlib.sha256(plaintext).hexdigest() != blob_hash:
        raise RuntimeError(f"Blob {blob_hash[:12]} failed integrity check")
    return plaintext


# ── Public API ────────────────────────────────────────────────────────────────

async def run_snapshot(label: str | None = None) -> BackupResult:
    """Back up the database + files as a deduplicated snapshot. Never raises."""
    if not settings.backup_encryption_key:
        return BackupResult(ok=False, size_bytes=0, error="BACKUP_ENCRYPTION_KEY is not configured")
    if not get_session_token():
        return BackupResult(ok=False, size_bytes=0, error="Relay not connected - skipping backup")

    try:
        from celerp.services.backup_state import writes_paused

        key = _parse_key(settings.backup_encryption_key)

        # Pause writes for the clean point; blocking work runs off the event loop so
        # reads stay served throughout.
        with writes_paused():
            # Hash every member first (streamed) so we only ever upload what's missing.
            dump = await asyncio.to_thread(dump_database, settings.database_url)
            db_hash = hashlib.sha256(dump).hexdigest()
            files = []  # manifest entries
            by_hash: dict[str, tuple[str, Path] | None] = {db_hash: None}  # None ⇒ db dump
            for arcname, path in _members():
                fhash, size = await asyncio.to_thread(_hash_file, path)
                files.append({"path": arcname, "hash": fhash, "size": size})
                by_hash.setdefault(fhash, (arcname, path))

            manifest = {"meta": await _build_meta(), "db_dump": db_hash, "files": files}
            manifest_bytes = json.dumps(manifest, indent=2).encode()
            manifest_hash = hashlib.sha256(manifest_bytes).hexdigest()

            referenced = set(by_hash) | {manifest_hash}
            uploaded = 0
            async with _relay() as client:
                missing = await _missing(client, referenced)
                sizes: dict[str, int] = {}
                for h in missing:
                    if h == manifest_hash:
                        plaintext = manifest_bytes
                    elif h == db_hash:
                        plaintext = dump
                    else:
                        plaintext = await asyncio.to_thread(by_hash[h][1].read_bytes)  # type: ignore[index]
                    sizes[h] = await _put_blob(client, h, plaintext, key)
                    uploaded += sizes[h]

                # size matters only for newly-uploaded blobs (quota); the relay keeps
                # the stored size for blobs it already has, so 1 is a harmless placeholder.
                blobs = [{"hash": h, "size": sizes.get(h, 1)} for h in referenced]
                r = await client.post(
                    "/repo/snapshot",
                    json={"manifest_hash": manifest_hash, "blobs": blobs, "label": label},
                )
                r.raise_for_status()

        log.info("Snapshot committed: %d blobs referenced, %d bytes uploaded", len(referenced), uploaded)
        return BackupResult(ok=True, size_bytes=uploaded)
    except Exception as exc:  # never raise — surfaced in result
        return BackupResult(ok=False, size_bytes=0, error=str(exc))


async def list_snapshots() -> dict:
    """Return the relay's snapshot list payload (items + totals). Raises on error."""
    async with _relay() as client:
        r = await client.get("/repo/snapshots")
        r.raise_for_status()
        return r.json()


async def reassemble_snapshot(snapshot_id: str) -> Path:
    """Download a snapshot and rebuild it as a ``.celerp-backup`` temp file."""
    key = _parse_key(settings.backup_encryption_key)
    async with _relay() as client:
        r = await client.get("/repo/snapshots")
        r.raise_for_status()
        snap = next((s for s in r.json()["items"] if s["id"] == snapshot_id), None)
        if snap is None:
            raise RuntimeError(f"Snapshot {snapshot_id} not found")

        manifest = json.loads(await _get_blob(client, snap["manifest_hash"], key))

        tmp = tempfile.NamedTemporaryFile(suffix=".celerp-backup", delete=False)
        tmp.close()
        with tarfile.open(tmp.name, "w:gz") as tar:
            dump = await _get_blob(client, manifest["db_dump"], key)
            info = tarfile.TarInfo(name="database.dump")
            info.size = len(dump)
            tar.addfile(info, io.BytesIO(dump))

            meta_bytes = json.dumps(manifest["meta"], indent=2).encode()
            info = tarfile.TarInfo(name="meta.json")
            info.size = len(meta_bytes)
            tar.addfile(info, io.BytesIO(meta_bytes))

            for entry in manifest["files"]:
                data = await _get_blob(client, entry["hash"], key)
                info = tarfile.TarInfo(name=entry["path"])
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))

    return Path(tmp.name)


async def restore_snapshot(snapshot_id: str) -> BackupResult:
    """Reassemble a cloud snapshot and restore it through the canonical importer."""
    if not settings.backup_encryption_key:
        return BackupResult(ok=False, size_bytes=0, error="BACKUP_ENCRYPTION_KEY is not configured")
    try:
        from celerp.services.backup_import import run_import
        path = await reassemble_snapshot(snapshot_id)
        try:
            await run_import(path)
        finally:
            path.unlink(missing_ok=True)
        return BackupResult(ok=True, size_bytes=0)
    except Exception as exc:
        return BackupResult(ok=False, size_bytes=0, error=str(exc))
