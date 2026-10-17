# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A backup holds a bounded amount of memory however large the database is.

A 64 MiB fake pg_dump goes through the local export, the cloud snapshot upload and the
cloud snapshot download; each path's Python allocations stay well under the dump size,
because the dump is written to disk and read, encrypted and uploaded in chunks."""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import sys
import tarfile
import tracemalloc
from pathlib import Path

import httpx
import pytest

from celerp.config import settings
from celerp.migrations.compatibility import running_version
from celerp.services import backup, backup_export, backup_repo

_MIB = 1024 * 1024
_DUMP_MIB = 64
_BOUND = 24 * _MIB
_BLOCK = bytes(range(256)) * 4096  # 1 MiB

_FAKE_PG_DUMP = f"""#!{sys.executable}
import sys
dest = next((a[len("--file="):] for a in sys.argv[1:] if a.startswith("--file=")), None)
block = bytes(range(256)) * 4096
out = open(dest, "wb") if dest else sys.stdout.buffer
for _ in range({_DUMP_MIB}):
    out.write(block)
out.close()
"""


def _dump_hash() -> str:
    h = hashlib.sha256()
    for _ in range(_DUMP_MIB):
        h.update(_BLOCK)
    return h.hexdigest()


@pytest.fixture
def big_dump(monkeypatch, tmp_path):
    """pg_dump writes a 64 MiB dump; the restore-owned roots live under tmp_path."""
    tool = tmp_path / "bin" / "pg_dump"
    tool.parent.mkdir()
    tool.write_text(_FAKE_PG_DUMP)
    tool.chmod(0o755)
    monkeypatch.setattr(backup, "_find_pg_tool", lambda name: str(tool))
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    att = tmp_path / "data" / "static" / "attachments"
    att.mkdir(parents=True)
    (att / "a.txt").write_bytes(b"attachment")

    async def fake_meta():
        return {"celerp_version": running_version(), "pg_version": "16",
                "created_at": "2026-10-12T00:00:00Z", "enabled_modules": []}
    monkeypatch.setattr(backup_export, "archive_meta", fake_meta)
    monkeypatch.setattr(settings, "backup_encryption_key",
                        base64.b64encode(secrets.token_bytes(32)).decode())
    return tmp_path


class _ObjectStore(httpx.AsyncBaseTransport):
    """Presigned-URL object store: a PUT body is written to disk as it arrives, a GET
    streams the stored file back. Nothing is held whole in memory."""

    def __init__(self, root: Path):
        self.root = root
        self.puts: dict[str, dict] = {}

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        blob = request.url.path.rsplit("/", 1)[-1]
        path = self.root / blob
        if request.method == "PUT":
            size = 0
            with path.open("wb") as fh:
                async for chunk in request.stream:
                    fh.write(chunk)
                    size += len(chunk)
            self.puts[blob] = {"size": size, "headers": dict(request.headers)}
            return httpx.Response(200)
        return httpx.Response(200, stream=_FileStream(path))


class _FileStream(httpx.AsyncByteStream):
    def __init__(self, path: Path):
        self.path = path

    async def __aiter__(self):
        with self.path.open("rb") as fh:
            while chunk := fh.read(256 * 1024):
                yield chunk


class _Relay:
    """The relay API: upload/download URLs pointing at the object store."""

    def __init__(self, snapshots: list):
        self.snapshots = snapshots

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, path, json=None):
        if path == "/repo/missing":
            return _Resp({"missing": json["hashes"]})
        if path.endswith("/upload-url"):
            return _Resp({"url": f"https://store.test/{path.split('/')[3]}"})
        assert path == "/repo/snapshot"
        self.snapshots.append({"id": "snap-0", "manifest_hash": json["manifest_hash"]})
        return _Resp({"id": "snap-0"})

    async def get(self, path):
        if path == "/repo/snapshots":
            return _Resp({"items": list(self.snapshots)})
        assert path.endswith("/download-url")
        return _Resp({"url": f"https://store.test/{path.split('/')[3]}"})


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


@pytest.fixture
def object_store(monkeypatch, tmp_path):
    store = _ObjectStore(tmp_path / "store")
    store.root.mkdir()

    class _Client(httpx.AsyncClient):
        def __init__(self, **kwargs):
            super().__init__(transport=store, **kwargs)

    monkeypatch.setattr(backup_repo.httpx, "AsyncClient", _Client)
    return store


async def _peak(coro):
    tracemalloc.start()
    try:
        result = await coro
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    return result, peak


async def test_export_full_peak_memory_is_bounded(big_dump):
    """Red statement: dump_database returned the whole pg_dump stdout and the archive
    copied it into a BytesIO member, so the peak was at least twice the dump."""
    archive, peak = await _peak(backup_export.export_full())
    try:
        with tarfile.open(archive, "r:gz") as tar:
            member = tar.getmember("database.dump")
            assert member.size == _DUMP_MIB * _MIB
            h = hashlib.sha256()
            with tar.extractfile(member) as fh:
                while chunk := fh.read(_MIB):
                    h.update(chunk)
            assert h.hexdigest() == _dump_hash()
            assert "attachments/a.txt" in tar.getnames()
    finally:
        archive.unlink(missing_ok=True)
    assert peak < _BOUND, peak


async def test_run_snapshot_upload_peak_memory_is_bounded(big_dump, object_store, monkeypatch):
    """Red statement: the snapshot held the dump as bytes and encrypted it into a second
    bytes object before the PUT. The upload carries an explicit length (a presigned PUT
    needs one) and is the same nonce + ciphertext + tag the restore side reads."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    snapshots: list = []
    monkeypatch.setattr(backup_repo, "_relay", lambda: _Relay(snapshots))
    result, peak = await _peak(backup_repo.run_snapshot(label="t"))
    assert result.ok, result.error
    assert peak < _BOUND, peak
    dump_hash = _dump_hash()
    put = object_store.puts[dump_hash]
    assert put["size"] == _DUMP_MIB * _MIB + 28
    assert put["headers"]["content-length"] == str(_DUMP_MIB * _MIB + 28)
    assert "transfer-encoding" not in put["headers"]
    blob = (object_store.root / dump_hash).read_bytes()
    key = base64.b64decode(settings.backup_encryption_key)
    plain = AESGCM(key).decrypt(blob[:12], blob[12:], None)
    assert hashlib.sha256(plain).hexdigest() == dump_hash


async def test_download_reassemble_peak_memory_is_bounded(big_dump, object_store, monkeypatch):
    """Red statement: each blob was downloaded whole and decrypted into a second bytes
    object, so reassembling a snapshot held the dump twice."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    key = base64.b64decode(settings.backup_encryption_key)

    def put(plain: bytes) -> str:
        h = hashlib.sha256(plain).hexdigest()
        nonce = secrets.token_bytes(12)
        (object_store.root / h).write_bytes(nonce + AESGCM(key).encrypt(nonce, plain, None))
        return h

    dump_hash = put(_BLOCK * _DUMP_MIB)
    file_hash = put(b"attachment")
    manifest = {"meta": {"celerp_version": running_version()}, "db_dump": dump_hash,
                "files": [{"path": "attachments/a.txt", "hash": file_hash, "size": 10}]}
    manifest_hash = put(json.dumps(manifest).encode())
    monkeypatch.setattr(backup_repo, "_relay",
                        lambda: _Relay([{"id": "snap-0", "manifest_hash": manifest_hash}]))
    archive, peak = await _peak(backup_repo.reassemble_snapshot("snap-0"))
    try:
        with tarfile.open(archive, "r:gz") as tar:
            assert tar.getmember("database.dump").size == _DUMP_MIB * _MIB
            assert tar.extractfile("attachments/a.txt").read() == b"attachment"
    finally:
        archive.unlink(missing_ok=True)
    assert peak < _BOUND, peak


async def test_reassemble_failure_removes_temp_archive(big_dump, object_store, monkeypatch, tmp_path):
    """Red statement: a damaged blob raised out of the tar block and left the
    NamedTemporaryFile(delete=False) archive behind."""
    import tempfile
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    key = base64.b64decode(settings.backup_encryption_key)

    def put(plain: bytes, damage: bool = False) -> str:
        h = hashlib.sha256(plain).hexdigest()
        nonce = secrets.token_bytes(12)
        blob = bytearray(nonce + AESGCM(key).encrypt(nonce, plain, None))
        if damage:
            blob[-1] ^= 1
        (object_store.root / h).write_bytes(bytes(blob))
        return h

    manifest = {"meta": {}, "db_dump": put(b"DUMP"),
                "files": [{"path": "attachments/a.txt", "hash": put(b"x", damage=True), "size": 1}]}
    manifest_hash = put(json.dumps(manifest).encode())
    monkeypatch.setattr(backup_repo, "_relay",
                        lambda: _Relay([{"id": "snap-0", "manifest_hash": manifest_hash}]))
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    with pytest.raises(Exception):
        await backup_repo.reassemble_snapshot("snap-0")
    assert list(scratch.iterdir()) == []



async def test_decrypt_source_truncated_during_read_fails_closed(tmp_path, monkeypatch):
    """A source truncated after its size check must not leave the decrypt loop spinning."""
    from types import SimpleNamespace

    plain = tmp_path / "plain"
    encrypted = tmp_path / "encrypted"
    restored = tmp_path / "restored"
    plain.write_bytes(b"abc")
    key = b"x" * 32
    encrypted.write_bytes(b"".join(backup.encrypt_chunks(plain, key)))
    real_stat = Path.stat

    def changed_stat(path, *args, **kwargs):
        result = real_stat(path, *args, **kwargs)
        if path == encrypted:
            return SimpleNamespace(st_size=result.st_size + 17)
        return result

    with monkeypatch.context() as patcher:
        patcher.setattr(Path, "stat", changed_stat)
        with pytest.raises(RuntimeError):
            backup.decrypt_file(encrypted, restored, key, chunk=2)
    assert not restored.exists()
