# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A migration source can be gigabytes, so the server never reads one whole into
memory to check it: files are hashed in bounded chunks, and a size change is
caught from the file's metadata before any hashing."""

from __future__ import annotations

import hashlib
import sys
import uuid
from pathlib import Path

import pytest

from migration_support import (
    fake_bytes,
    load_run,
    migrate_as_owner,
    migration_env,  # noqa: F401 - fixture
    real_client,  # noqa: F401 - fixture
    real_engine,  # noqa: F401 - fixture
    save_decisions,
    scan_upload,
)
from test_helpers import register_admin

BOOTSTRAP = ("bootstrap", None)
CHANGED = "The uploaded file changed after it was scanned. Upload it again."


def _refuse_server_whole_reads(monkeypatch):
    """Whole-file reads are refused when the server makes them; the fake test source
    parser still reads its small fixture."""
    real = Path.read_bytes

    def read_bytes(self):
        caller = sys._getframe(1).f_globals.get("__name__", "")
        if caller.startswith(("celerp.services", "celerp.routers")):
            raise AssertionError(f"whole-file read by {caller}")
        return real(self)
    monkeypatch.setattr(Path, "read_bytes", read_bytes)


async def _bootstrap_scan(client, env) -> tuple[str, Path]:
    r = await scan_upload(client, fake_bytes())
    assert r.status_code == 200, r.text
    token = r.json()["scan_token"]
    assert (await save_decisions(client, token)).status_code == 200
    directory = env["data_dir"] / "migration_scans" / token
    return token, next(directory.glob("artifact-*"))


@pytest.mark.asyncio
async def test_scan_and_start_never_read_a_whole_source(real_client, real_engine, migration_env, monkeypatch):
    from celerp.services import migrations

    migrations._sample_sha256.cache_clear()
    _refuse_server_whole_reads(monkeypatch)
    try:
        token = await register_admin(real_client)
        run_id = await migrate_as_owner(real_client, token)
    finally:
        migrations._sample_sha256.cache_clear()
    assert (await load_run(real_engine, uuid.UUID(run_id))).status == "running"


def test_file_hash_reads_bounded_chunks(tmp_path, monkeypatch):
    from celerp.services import migration_scan_store as store

    monkeypatch.setattr(store, "_HASH_CHUNK", 4096)
    data = bytes(range(256)) * 50  # several chunks plus a partial one
    path = tmp_path / "source"
    path.write_bytes(data)
    reads: list[int] = []
    real_open = Path.open

    class Recorder:
        def __init__(self, fh):
            self._fh = fh

        def read(self, size=-1):
            reads.append(size)
            return self._fh.read(size)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self._fh.close()

    monkeypatch.setattr(Path, "open", lambda self, *a, **k: Recorder(real_open(self, *a, **k)))
    assert store.file_sha256(path) == hashlib.sha256(data).hexdigest()
    assert reads and all(0 < size <= 4096 for size in reads), reads


@pytest.mark.asyncio
async def test_a_same_size_change_is_detected_by_streaming_hash(real_client, real_engine, migration_env,
                                                                monkeypatch):
    from celerp.services import migration_scan_store as store

    token, artifact = await _bootstrap_scan(real_client, migration_env)
    data = artifact.read_bytes()
    artifact.write_bytes(data[:-1] + bytes([data[-1] ^ 1]))
    _refuse_server_whole_reads(monkeypatch)
    with pytest.raises(store.ScanStoreError) as exc:
        store.verify_unchanged(token, owner=BOOTSTRAP)
    assert exc.value.status_code == 409 and exc.value.detail == CHANGED
    assert not artifact.parent.exists()


@pytest.mark.asyncio
async def test_a_grown_source_is_refused_before_hashing(real_client, real_engine, migration_env, monkeypatch):
    """A file that grew after the scan, even to gigabytes, is refused from its size alone."""
    from celerp.services import migration_scan_store as store

    token, artifact = await _bootstrap_scan(real_client, migration_env)
    with artifact.open("r+b") as fh:
        fh.truncate(3 * 1024**3)  # sparse, so the test itself stays cheap

    def never(path):
        raise AssertionError("a size change must be refused without hashing")
    monkeypatch.setattr(store, "file_sha256", never, raising=False)
    _refuse_server_whole_reads(monkeypatch)
    with pytest.raises(store.ScanStoreError) as exc:
        store.verify_unchanged(token, owner=BOOTSTRAP)
    assert exc.value.status_code == 409 and exc.value.detail == CHANGED
    assert not artifact.parent.exists()
