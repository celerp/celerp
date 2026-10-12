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
and feeds the canonical ``run_recovery`` engine, the same importer as a local restore.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
import logging
import tarfile
import tempfile
from pathlib import Path

import httpx

from celerp.config import settings
from celerp.services.backup import (
    _CHUNK,
    ENC_OVERHEAD,
    BackupResult,
    _parse_key,
    add_bytes_member,
    add_file_member,
    decrypt_file,
    disk_full_as_error,
    dump_database,
    encrypt_chunks,
)
from ui.i18n import t

log = logging.getLogger(__name__)


def _members() -> list[tuple[str, Path]]:
    """(arcname, path) for every backed-up file, in the local export's layout."""
    from celerp.services.backup_export import archive_members, restore_roots
    return archive_members(restore_roots().values())


def _hash_file(path: Path) -> tuple[str, int]:
    h = hashlib.sha256()
    size = 0
    with path.open("rb") as fh:
        while chunk := fh.read(_CHUNK):
            h.update(chunk)
            size += len(chunk)
    return h.hexdigest(), size


# ── Relay API ─────────────────────────────────────────────────────────────────

@asynccontextmanager
async def _relay():
    from celerp.gateway.state import fetch_relay_bearer, relay_http_url
    from celerp.services.cloud_entitlement import stored_api_key
    if settings.cloud_disconnected:
        raise RuntimeError(t("error.cloud_backup_disconnected"))
    api_key = await stored_api_key()
    if not api_key:
        raise RuntimeError(t("error.cloud_backup_no_account"))
    async with httpx.AsyncClient(base_url=relay_http_url(), timeout=60) as client:
        jwt = await fetch_relay_bearer(client, api_key=api_key)
        client.headers["Authorization"] = f"Bearer {jwt}"
        yield client


async def _missing(client: httpx.AsyncClient, hashes: set[str]) -> set[str]:
    r = await client.post("/repo/missing", json={"hashes": sorted(hashes)})
    r.raise_for_status()
    return set(r.json()["missing"])


async def _put_blob(client: httpx.AsyncClient, blob_hash: str, path: Path, key: bytes) -> int:
    """Encrypt the file at *path* and upload it via a presigned PUT, a chunk at a time.
    Returns stored (encrypted) size."""
    r = await client.post(f"/repo/blob/{blob_hash}/upload-url")
    r.raise_for_status()
    size = path.stat().st_size + ENC_OVERHEAD
    chunks = encrypt_chunks(path, key)

    async def body():
        while (data := await asyncio.to_thread(next, chunks, None)) is not None:
            yield data

    try:
        async with httpx.AsyncClient(timeout=300) as s3:
            # A presigned PUT needs the length up front; the body is streamed to it.
            put = await s3.put(r.json()["url"], content=body(), headers={"Content-Length": str(size)})
            put.raise_for_status()
    finally:
        chunks.close()
    return size


async def _get_blob(client: httpx.AsyncClient, blob_hash: str, key: bytes, dest: Path) -> None:
    """Download one blob, decrypt it to *dest* and check it against its content hash, a
    chunk at a time. A damaged blob removes *dest* and raises."""
    r = await client.get(f"/repo/blob/{blob_hash}/download-url")
    r.raise_for_status()
    encrypted = dest.with_name(f"{dest.name}.enc")
    try:
        async with httpx.AsyncClient(timeout=300) as s3, s3.stream("GET", r.json()["url"]) as dl:
            dl.raise_for_status()
            with encrypted.open("wb") as fh:
                async for data in dl.aiter_bytes(_CHUNK):
                    await asyncio.to_thread(fh.write, data)
        digest = await asyncio.to_thread(decrypt_file, encrypted, dest, key)
    finally:
        encrypted.unlink(missing_ok=True)
    if digest != blob_hash:
        dest.unlink(missing_ok=True)
        raise RuntimeError(t("error.backup_damaged"))


# ── Public API ────────────────────────────────────────────────────────────────

async def run_snapshot(label: str | None = None) -> BackupResult:
    """Back up the database + files as a deduplicated snapshot. Never raises."""
    if not settings.backup_encryption_key:
        return BackupResult(ok=False, size_bytes=0, error="BACKUP_ENCRYPTION_KEY is not configured")
    if settings.cloud_disconnected:
        return BackupResult(ok=False, size_bytes=0, error=t("error.cloud_backup_disconnected"))

    try:
        from celerp.services import backup_export
        from celerp.services.backup_state import writes_paused

        key = _parse_key(settings.backup_encryption_key)

        # Pause writes for the clean point; blocking work runs off the event loop so
        # reads stay served throughout.
        with writes_paused(), disk_full_as_error(), tempfile.TemporaryDirectory() as work:
            # Hash every member first (streamed) so we only ever upload what's missing.
            dump = Path(work) / "database.dump"
            await asyncio.to_thread(dump_database, settings.database_url, dump)
            db_hash, _ = await asyncio.to_thread(_hash_file, dump)
            files = []  # manifest entries
            by_hash: dict[str, Path] = {db_hash: dump}
            for arcname, path in _members():
                fhash, size = await asyncio.to_thread(_hash_file, path)
                files.append({"path": arcname, "hash": fhash, "size": size})
                by_hash.setdefault(fhash, path)

            manifest = {"meta": await backup_export.archive_meta(), "db_dump": db_hash, "files": files}
            manifest_path = Path(work) / "manifest.json"
            manifest_path.write_bytes(json.dumps(manifest, indent=2).encode())
            manifest_hash, _ = _hash_file(manifest_path)
            by_hash[manifest_hash] = manifest_path

            uploaded = 0
            async with _relay() as client:
                missing = await _missing(client, set(by_hash))
                sizes: dict[str, int] = {}
                for h in missing:
                    sizes[h] = await _put_blob(client, h, by_hash[h], key)
                    uploaded += sizes[h]

                # size matters only for newly-uploaded blobs (quota); the relay keeps
                # the stored size for blobs it already has, so 1 is a harmless placeholder.
                blobs = [{"hash": h, "size": sizes.get(h, 1)} for h in by_hash]
                r = await client.post(
                    "/repo/snapshot",
                    json={"manifest_hash": manifest_hash, "blobs": blobs, "label": label},
                )
                r.raise_for_status()

        log.info("Snapshot committed: %d blobs referenced, %d bytes uploaded", len(by_hash), uploaded)
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
            raise RuntimeError(t("error.backup_snapshot_missing"))

        tmp = tempfile.NamedTemporaryFile(suffix=".celerp-backup", delete=False)
        tmp.close()
        try:
            with disk_full_as_error(), tempfile.TemporaryDirectory() as work:
                blob = Path(work) / "blob"
                await _get_blob(client, snap["manifest_hash"], key, blob)
                manifest = json.loads(blob.read_bytes())
                with tarfile.open(tmp.name, "w:gz") as tar:
                    await _get_blob(client, manifest["db_dump"], key, blob)
                    add_file_member(tar, "database.dump", blob)

                    add_bytes_member(tar, "meta.json", json.dumps(manifest["meta"], indent=2).encode())

                    for entry in manifest["files"]:
                        await _get_blob(client, entry["hash"], key, blob)
                        add_file_member(tar, entry["path"], blob)
        except BaseException:
            Path(tmp.name).unlink(missing_ok=True)
            raise

    return Path(tmp.name)


async def restore_snapshot(snapshot_id: str) -> BackupResult:
    """Reassemble a cloud snapshot and restore it through the canonical importer."""
    if not settings.backup_encryption_key:
        return BackupResult(ok=False, size_bytes=0, error="BACKUP_ENCRYPTION_KEY is not configured")
    try:
        from celerp.services import backup_import
        path = await reassemble_snapshot(snapshot_id)
        try:
            return await backup_import.run_recovery(path)
        finally:
            path.unlink(missing_ok=True)
    except Exception as exc:
        return BackupResult(ok=False, size_bytes=0, error=str(exc))
