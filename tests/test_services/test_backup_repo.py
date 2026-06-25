# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Tests for celerp/services/backup_repo.py — content-addressed cloud snapshots.

Covers:
  - dedup: identical file content uploads a single blob; an unchanged file is never
    re-uploaded on the next snapshot
  - round-trip: a snapshot reassembles into a valid .celerp-backup whose database dump
    and files match the originals
  - restore: restore_snapshot feeds the reassembled archive to run_import
  - write-pause (#161): emit_event returns 503 while a backup is in progress, and is
    unaffected otherwise
"""

from __future__ import annotations

import base64
import os
import secrets

os.environ.setdefault("ALLOW_INSECURE_JWT", "true")

import pytest

from celerp.config import settings
from celerp.services import backup_repo


# ── In-memory fake relay/blob store ───────────────────────────────────────────

class _FakeClient:
    """Stands in for the relay HTTP client used by run_snapshot/reassemble."""

    def __init__(self, store: dict, snapshots: list):
        self._store = store
        self._snapshots = snapshots

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, path, json=None):
        assert path == "/repo/snapshot"
        snap_id = f"snap-{len(self._snapshots)}"
        self._snapshots.append({
            "id": snap_id,
            "manifest_hash": json["manifest_hash"],
            "label": json.get("label"),
            "size_bytes": sum(b["size"] for b in json["blobs"]),
            "created_at": f"2026-06-25T00:0{len(self._snapshots)}:00Z",
        })
        return _FakeResp({"id": snap_id})

    async def get(self, path):
        assert path == "/repo/snapshots"
        return _FakeResp({"items": list(self._snapshots),
                          "total_bytes": 0, "quota_bytes": None})


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


@pytest.fixture
def fake_repo(monkeypatch, tmp_path):
    """Patch backup_repo's relay/blob layer with an in-memory store.

    Returns (store, snapshots, put_calls). ``store`` maps content-hash -> plaintext;
    ``put_calls`` records every uploaded hash (to assert dedup)."""
    store: dict[str, bytes] = {}
    snapshots: list[dict] = []
    put_calls: list[str] = []

    async def fake_missing(client, hashes):
        return {h for h in hashes if h not in store}

    async def fake_put(client, blob_hash, plaintext, key):
        store[blob_hash] = plaintext
        put_calls.append(blob_hash)
        return len(plaintext)

    async def fake_get(client, blob_hash, key):
        return store[blob_hash]

    monkeypatch.setattr(backup_repo, "_relay", lambda: _FakeClient(store, snapshots))
    monkeypatch.setattr(backup_repo, "_missing", fake_missing)
    monkeypatch.setattr(backup_repo, "_put_blob", fake_put)
    monkeypatch.setattr(backup_repo, "_get_blob", fake_get)
    monkeypatch.setattr(backup_repo, "dump_database", lambda url: b"PGDUMP-CUSTOM-FORMAT")

    async def fake_meta():
        return {"celerp_version": "1.0.0", "pg_version": "16", "created_at": "2026-06-25T00:00:00Z",
                "company_name": "TestCo", "enabled_modules": []}
    monkeypatch.setattr(backup_repo, "_build_meta", fake_meta)

    monkeypatch.setattr(backup_repo, "get_session_token", lambda: "tok")
    settings.backup_encryption_key = base64.b64encode(secrets.token_bytes(32)).decode()

    # data_dir/static/attachments + data_dir/ai_uploads, real files (hashed for real)
    att = tmp_path / "static" / "attachments"
    att.mkdir(parents=True)
    monkeypatch.setattr(backup_repo, "_attachment_dirs", lambda: [att])
    return store, snapshots, put_calls, att


# ── dedup ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_identical_content_uploads_one_blob(fake_repo):
    store, snapshots, put_calls, att = fake_repo
    (att / "a.txt").write_bytes(b"SAME CONTENT")
    (att / "b.txt").write_bytes(b"SAME CONTENT")  # identical → one blob

    result = await backup_repo.run_snapshot(label="t")
    assert result.ok, result.error
    # one shared file blob + db dump + manifest = 3 uploads, not 4
    same_hash = __import__("hashlib").sha256(b"SAME CONTENT").hexdigest()
    assert put_calls.count(same_hash) == 1
    assert len(put_calls) == 3


@pytest.mark.asyncio
async def test_unchanged_file_not_reuploaded(fake_repo):
    store, snapshots, put_calls, att = fake_repo
    (att / "keep.txt").write_bytes(b"UNCHANGED")
    await backup_repo.run_snapshot(label="first")
    put_calls.clear()

    # change nothing but the db dump differs each run (global) — only the db dump and
    # the new manifest should upload; the unchanged file must be skipped.
    (att / "new.txt").write_bytes(b"BRAND NEW")
    await backup_repo.run_snapshot(label="second")

    unchanged_hash = __import__("hashlib").sha256(b"UNCHANGED").hexdigest()
    assert unchanged_hash not in put_calls, "unchanged file was re-uploaded"
    new_hash = __import__("hashlib").sha256(b"BRAND NEW").hexdigest()
    assert new_hash in put_calls, "changed/new file should upload"


# ── round-trip ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_snapshot_round_trip_rebuilds_valid_archive(fake_repo):
    import tarfile
    from celerp.services.backup_import import validate_archive

    store, snapshots, put_calls, att = fake_repo
    (att / "doc.pdf").write_bytes(b"PDF-BYTES")
    await backup_repo.run_snapshot(label="rt")
    snap_id = snapshots[0]["id"]

    path = await backup_repo.reassemble_snapshot(snap_id)
    try:
        meta = validate_archive(path)  # raises if not a valid .celerp-backup
        assert meta.company_name == "TestCo"
        with tarfile.open(path, "r:gz") as tar:
            assert tar.extractfile("database.dump").read() == b"PGDUMP-CUSTOM-FORMAT"
            assert tar.extractfile("attachments/doc.pdf").read() == b"PDF-BYTES"
    finally:
        path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_restore_snapshot_invokes_run_import(fake_repo, monkeypatch):
    store, snapshots, put_calls, att = fake_repo
    (att / "f.txt").write_bytes(b"X")
    await backup_repo.run_snapshot(label="r")
    snap_id = snapshots[0]["id"]

    called = {}

    async def fake_run_import(path):
        from celerp.services.backup import BackupResult
        called["path"] = str(path)
        assert path.exists()
        return BackupResult(ok=True, size_bytes=0)

    monkeypatch.setattr("celerp.services.backup_import.run_import", fake_run_import)
    result = await backup_repo.restore_snapshot(snap_id)
    assert result.ok, result.error
    assert called["path"].endswith(".celerp-backup")


# ── write-pause (#161) ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_emit_event_blocked_while_backup_active():
    from fastapi import HTTPException
    from celerp.events.engine import emit_event
    from celerp.services.backup_state import writes_paused, is_active

    assert not is_active()
    with writes_paused():
        assert is_active()
        with pytest.raises(HTTPException) as ei:
            await emit_event(None, event_type="anything", data={})
        assert ei.value.status_code == 503
    assert not is_active()
