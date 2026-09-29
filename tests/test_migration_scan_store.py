# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The migration scan store: bounded streaming uploads, source detection, the
scan token, and how long an uploaded source file is kept."""

from __future__ import annotations

import json
import re
import stat
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select, text

from migration_support import (
    FAKE_KEY,
    code_config,  # noqa: F401 - fixture
    count,
    fake_bytes,
    fake_spec,
    load_run,
    maker,
    migration_env,  # noqa: F401 - fixture
    multipart,
    real_engine,  # noqa: F401 - fixture
    save_decisions,
    scan_upload,
    sha256,
    staged_run,
    upload_parts,
)

EXPIRED = "This scan has expired. Upload the file again."


def _scan_dirs(env) -> set[str]:
    root = env["data_dir"] / "migration_scans"
    return {p.name for p in root.iterdir()} if root.exists() else set()


def _run_dir(env, run_id):
    return env["data_dir"] / "migration_runs" / str(run_id)


def test_app_imports_without_posix_only_modules():
    """The app starts on Windows, where the POSIX-only fcntl, termios, pwd and grp do not exist."""
    blocked = ("fcntl", "termios", "pwd", "grp")
    code = ("import sys\n"
            f"sys.modules.update(dict.fromkeys({blocked!r}))\n"
            "import celerp.main\n")
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr


@pytest.mark.asyncio
async def test_migration_scan_streams_and_bounds_upload(client, session, migration_env, monkeypatch):
    from celerp.importers.adapters.base import ArtifactSpec
    from celerp.middleware import MaxBodySizeMiddleware
    from celerp.models.company import Company, User
    from celerp.models.migration import MigrationRun
    from celerp.services import migration_scan_store as store

    adapter = migration_env["adapter"]
    data = fake_bytes()
    r = await scan_upload(client, data)
    assert r.status_code == 200, r.text
    scan = r.json()["scan"]
    assert scan["size_bytes"] == len(data)
    assert scan["sha256_short"] == sha256(data)[:12]
    stored = json.loads((migration_env["data_dir"] / "migration_scans" / r.json()["scan_token"] / "scan.json").read_text())
    assert [a["sha256"] for a in stored["artifacts"]] == [sha256(data)]
    baseline = _scan_dirs(migration_env)
    inspected = adapter.inspect_calls

    async def rejected(files, status, detail=None, source=FAKE_KEY):
        resp = await client.post("/migrations/bootstrap/scan", files=files or None, data={"source": source})
        assert resp.status_code == status, resp.text
        if detail is not None:
            assert resp.json()["detail"] == detail
        assert _scan_dirs(migration_env) == baseline, "a rejected upload left files behind"
        assert adapter.inspect_calls == inspected, "a rejected upload reached the source parser"

    await rejected([], 422, "Choose a file to upload.")
    await rejected(multipart(("empty.fake", b"")), 422, "The uploaded file is empty.")
    await rejected(multipart(*[(f"f{i}.fake", data) for i in range(store.MAX_ARTIFACTS + 1)]), 413,
                   f"Upload at most {store.MAX_ARTIFACTS} files.")

    specs = adapter.artifact_specs
    monkeypatch.setattr(adapter, "artifact_specs", (ArtifactSpec("source_file", "Source file", (".fake",), 100),))
    await rejected(multipart(("big.fake", data + b" " * 200)), 413, "This file is larger than this source allows.")
    monkeypatch.setattr(adapter, "artifact_specs", specs)

    monkeypatch.setattr(store, "MAX_AGGREGATE_BYTES", len(data) + 10)
    await rejected(multipart(("a.fake", data), ("b.fake", data)), 413, "The upload is larger than the allowed total.")
    monkeypatch.setattr(store, "MAX_AGGREGATE_BYTES", 2 * 1024**3)

    real_open = store._open_artifact

    class _FailingFile:
        def __init__(self, path):
            self._fh = real_open(path)
            self.writes = 0

        def write(self, chunk):
            self.writes += 1
            if self.writes > 1:
                raise OSError("disk full")
            return self._fh.write(chunk)

        def close(self):
            self._fh.close()

    monkeypatch.setattr(store, "_open_artifact", _FailingFile)
    await rejected(multipart(("books.fake", data + b" " * 5000)), 500, "The file could not be stored. Try again.")
    monkeypatch.setattr(store, "_open_artifact", real_open)
    assert (await session.execute(select(func.count()).select_from(Company))).scalar_one() == 0
    assert (await session.execute(select(func.count()).select_from(User))).scalar_one() == 0
    assert (await session.execute(select(func.count()).select_from(MigrationRun))).scalar_one() == 0

    # The generic body cap lets only the two scan uploads stream through to their own route caps.
    reached: list[str] = []

    async def inner(scope, receive, send):
        reached.append(scope["path"])

    capped = MaxBodySizeMiddleware(inner, 10)
    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    for path in ("/migrations/bootstrap/scan", "/migrations/scan", "/migrations/scan/decisions"):
        scope = {"type": "http", "path": path, "headers": [(b"content-length", b"1000")]}
        await capped(scope, receive, send)
    assert reached == ["/migrations/bootstrap/scan", "/migrations/scan"]
    assert any(m.get("status") == 413 for m in sent)


@pytest.mark.asyncio
async def test_migration_rejects_unknown_or_mismatched_source(client, migration_env):
    from celerp.importers.adapters import registry
    from celerp.importers.adapters.base import Artifact

    r = await client.get("/migrations/sources")
    assert r.status_code == 200
    by_key = {s["key"]: s for s in r.json()}
    assert set(by_key) == {FAKE_KEY, "manager_io"}
    assert by_key["manager_io"]["display_name"] == "Manager.io"
    assert by_key[FAKE_KEY]["artifacts"][0]["extensions"] == [".fake"]

    cases = [
        (fake_bytes(), "not_a_source", "This source is not available."),
        (fake_bytes(), "manager_io", "This file is not a Manager.io file."),
        (b"PK\x03\x04 a spreadsheet", None, "Celerp cannot read this file yet."),
        (fake_bytes(fake_spec(unreadable=True)), FAKE_KEY, "This file is damaged."),
    ]
    for data, source, detail in cases:
        r = await scan_upload(client, data, source=source)
        assert r.status_code == 422, r.text
        assert r.json()["detail"] == detail
        assert _scan_dirs(migration_env) == set()

    assert registry.get_adapter("not_a_source") is None
    path = migration_env["data_dir"] / "probe.fake"
    path.write_bytes(fake_bytes())
    probe = [Artifact(path, "probe.fake", path.stat().st_size, sha256(path.read_bytes()))]
    assert registry.detect_adapter(probe) is migration_env["adapter"]


@pytest.mark.asyncio
async def test_scan_token_is_scoped_expiring_and_tamper_safe(client, session, migration_env, code_config, monkeypatch):
    from celerp.models.migration import MigrationRun
    from celerp.services import migration_scan_store as store

    data = fake_bytes()
    r = await scan_upload(client, data, headers={"X-Setup-Code": code_config})
    assert r.status_code == 200, r.text
    token = r.json()["scan_token"]
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", token)
    assert token not in json.dumps(dict(r.headers))
    directory = migration_env["data_dir"] / "migration_scans" / token
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    files = [p for p in directory.iterdir() if p.is_file()]
    assert files and all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in files)
    raw = (directory / "scan.json").read_text()
    for secret in (code_config, "access_token", "refresh_token"):
        assert secret not in raw

    bootstrap = ("bootstrap", None)
    assert store.load_scan(token, owner=bootstrap).token == token
    for bad_token, owner in ((token, ("user", uuid.uuid4())), ("../" + token[3:], bootstrap), ("x" * 43, bootstrap)):
        with pytest.raises(store.ScanStoreError) as exc:
            store.load_scan(bad_token, owner=owner)
        assert (exc.value.status_code, exc.value.detail) == (410, EXPIRED)

    # Replacing the file issues a new token and removes the previous scan.
    r = await scan_upload(client, data, headers={"X-Setup-Code": code_config})
    new_token = r.json()["scan_token"]
    assert new_token != token and not directory.exists()
    with pytest.raises(store.ScanStoreError):
        store.load_scan(token, owner=bootstrap)
    token, directory = new_token, migration_env["data_dir"] / "migration_scans" / new_token

    # Concurrent updates to one token are serialized by the per-token lock.
    real_write = store._write_json
    active, overlaps = [0], [0]
    guard = threading.Lock()

    def slow_write(path, payload):
        with guard:
            active[0] += 1
            overlaps[0] = max(overlaps[0], active[0])
        time.sleep(0.2)
        real_write(path, payload)
        with guard:
            active[0] -= 1

    monkeypatch.setattr(store, "_write_json", slow_write)
    from celerp.importers.adapters.base import MigrationDecisions
    threads = [threading.Thread(target=store.save_decisions, args=(token,),
                                kwargs={"owner": bootstrap, "decisions": MigrationDecisions(mode="full_history")})
               for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert overlaps[0] == 1
    monkeypatch.setattr(store, "_write_json", real_write)

    # A token held by another request past the lock budget is refused, and the scan is kept.
    from celerp import config_store
    release = config_store.hold_lock(str(directory / ".lock"))
    try:
        with pytest.raises(store.ScanStoreError) as exc:
            store.save_decisions(token, owner=bootstrap, decisions=MigrationDecisions(mode="full_history"))
    finally:
        release()
    assert (exc.value.status_code, exc.value.detail) == (409, store.BUSY)
    assert store.load_scan(token, owner=bootstrap).token == token
    assert not (directory / ".lock").exists()

    # A stored path pointing outside the scan directory is refused and the scan removed.
    payload = json.loads((directory / "scan.json").read_text())
    good = json.loads(json.dumps(payload))
    payload["artifacts"][0]["stored_name"] = "../../outside"
    (directory / "scan.json").write_text(json.dumps(payload))
    with pytest.raises(store.ScanStoreError) as exc:
        store.load_scan(token, owner=bootstrap)
    assert exc.value.status_code == 410 and not directory.exists()

    # An artifact changed after the scan is refused when the run claims it.
    r = await scan_upload(client, data, headers={"X-Setup-Code": code_config})
    token = r.json()["scan_token"]
    directory = migration_env["data_dir"] / "migration_scans" / token
    artifact = directory / good["artifacts"][0]["stored_name"]
    artifact.write_bytes(artifact.read_bytes() + b"tampered")
    with pytest.raises(store.ScanStoreError) as exc:
        store.claim_for_run(token, owner=bootstrap, run_id=uuid.uuid4())
    assert exc.value.status_code == 409 and not directory.exists()

    # Expiry.
    r = await scan_upload(client, data, headers={"X-Setup-Code": code_config})
    token = r.json()["scan_token"]
    now = time.time()
    monkeypatch.setattr(store, "_now", lambda: now + store.SCAN_TTL_SECONDS + 1)
    with pytest.raises(store.ScanStoreError) as exc:
        store.load_scan(token, owner=bootstrap)
    assert (exc.value.status_code, exc.value.detail) == (410, EXPIRED)
    monkeypatch.setattr(store, "_now", time.time)

    # Neither the run row nor the run's files hold the scan token, the setup code or auth tokens.
    r = await scan_upload(client, data, headers={"X-Setup-Code": code_config})
    token = r.json()["scan_token"]
    assert (await save_decisions(client, token)).status_code == 200
    r = await client.post("/migrations/bootstrap/start", headers={"X-Setup-Code": code_config}, json={
        "scan_token": token, "company_name": "Moved Co", "name": "Owner",
        "email": "owner@example.com", "password": "ownerpw123"})
    assert r.status_code == 201, r.text
    body = r.json()
    row = (await session.execute(text(
        "SELECT row_to_json(m)::text FROM migration_runs m WHERE id = :id"), {"id": body["run_id"]})).scalar_one()
    run_files = "".join(p.read_text(errors="ignore") for p in _run_dir(migration_env, body["run_id"]).rglob("*.json"))
    for secret in (token, code_config, body["access_token"], body["refresh_token"]):
        assert secret not in row and secret not in run_files
    assert (await session.execute(select(func.count()).select_from(MigrationRun))).scalar_one() == 1


@pytest.mark.asyncio
async def test_migration_source_retention_and_cleanup(real_engine, migration_env, monkeypatch):
    from celerp.services import migration_scan_store as store
    from celerp.services import migrations

    owner = ("user", uuid.uuid4())

    # Unusable scan: nothing is kept.
    with pytest.raises(store.ScanStoreError) as exc:
        await store.create_scan(upload_parts(("bad.fake", fake_bytes(fake_spec(unreadable=True)))), owner=owner)
    assert exc.value.status_code == 422 and _scan_dirs(migration_env) == set()

    # A valid scan survives Back within its TTL, then expires and is purged.
    scan = await store.create_scan(upload_parts(("books.fake", fake_bytes())), owner=owner)
    store.save_decisions(scan.token, owner=owner, decisions=migrations.validate_decisions(scan, {"mode": "full_history"}))
    again = store.load_scan(scan.token, owner=owner)
    assert again.decisions is not None and all(a.path.exists() for a in again.artifacts)
    now = time.time()
    monkeypatch.setattr(store, "_now", lambda: now + store.SCAN_TTL_SECONDS + 1)
    assert store.purge_expired() == 1
    assert _scan_dirs(migration_env) == set()
    monkeypatch.setattr(store, "_now", time.time)

    async def view(run_id):
        async with maker(real_engine)() as s:
            run = await migrations.get_run_for_company(s, run_id, (await load_run(real_engine, run_id)).company_id)
            return await migrations.run_view(s, run)

    async def finish(run_id):
        async with maker(real_engine)() as s:
            await migrations.finalize(s, await migrations.get_run_for_company(
                s, run_id, (await load_run(real_engine, run_id)).company_id))

    # Completion deletes the source.
    done, _, _ = await staged_run(real_engine, email="done@example.com")
    assert _run_dir(migration_env, done).exists()
    await migrations.run_migration(done)
    await finish(done)
    assert not _run_dir(migration_env, done).exists()
    assert (await view(done))["source_deleted"] is True

    # Discard deletes the source.
    dropped, _, _ = await staged_run(real_engine, email="dropped@example.com")
    async with maker(real_engine)() as s:
        run = await migrations.get_run_for_company(s, dropped, (await load_run(real_engine, dropped)).company_id)
        await migrations.discard(s, run)
    assert not _run_dir(migration_env, dropped).exists()

    # A failed run keeps its source only within the retention limit shown to the user.
    migration_env["sink"].fail_on = {"loc-1": 1}
    failed, _, _ = await staged_run(real_engine, email="failed@example.com")
    await migrations.run_migration(failed)
    run = await load_run(real_engine, failed)
    assert run.status == "failed"
    shown = await view(failed)
    assert shown["retention_until"] == (run.heartbeat_at + timedelta(days=migrations.RETENTION_DAYS)).isoformat()
    async with maker(real_engine)() as s:
        await migrations.purge_run_sources(s)
    assert _run_dir(migration_env, failed).exists()
    async with real_engine.begin() as conn:
        await conn.execute(text("UPDATE migration_runs SET heartbeat_at = :t WHERE id = :id"), {
            "t": datetime.now(timezone.utc) - timedelta(days=migrations.RETENTION_DAYS + 1), "id": failed})
    async with maker(real_engine)() as s:
        assert await migrations.purge_run_sources(s) == 1
    assert not _run_dir(migration_env, failed).exists()
    assert (await view(failed))["source_deleted"] is True

    # A deletion that fails is recorded on the run, never reported as deleted, and retried.
    stuck, _, _ = await staged_run(real_engine, email="stuck@example.com")
    await migrations.run_migration(stuck)

    def refuse(path):
        raise OSError("device busy")

    real_remove = store._remove_tree
    monkeypatch.setattr(store, "_remove_tree", refuse)
    await finish(stuck)
    run = await load_run(real_engine, stuck)
    assert run.status == "completed"
    assert "device busy" in run.source_summary["source_cleanup"]
    shown = await view(stuck)
    assert shown["source_deleted"] is False
    monkeypatch.setattr(store, "_remove_tree", real_remove)
    async with maker(real_engine)() as s:
        assert await migrations.purge_run_sources(s) == 1
    run = await load_run(real_engine, stuck)
    assert "source_cleanup" not in run.source_summary
    assert not _run_dir(migration_env, stuck).exists()
    assert await count(real_engine, "migration_runs") == 3


@pytest.mark.asyncio
async def test_save_decisions_storage_failure_is_reported_and_keeps_the_scan(migration_env, monkeypatch):
    from celerp import config_store
    from celerp.importers.adapters.base import MigrationDecisions
    from celerp.services import migration_scan_store as store

    owner = ("user", uuid.uuid4())
    scan = await store.create_scan(upload_parts(("books.fake", fake_bytes())), owner=owner)
    directory = migration_env["data_dir"] / "migration_scans" / scan.token
    before = (directory / "scan.json").read_bytes()
    decisions = MigrationDecisions(mode="full_history")

    def refuse(*args, **kwargs):
        raise OSError("disk full")

    real_replace = store.os.replace
    for target, name, replacement in ((store.os, "replace", refuse),
                                      (config_store, "hold_lock", refuse)):
        with monkeypatch.context() as patched, pytest.raises(store.ScanStoreError) as exc:
            patched.setattr(target, name, replacement)
            store.save_decisions(scan.token, owner=owner, decisions=decisions)
        assert (exc.value.status_code, exc.value.detail) == (500, "The file could not be stored. Try again."), name
        assert (directory / "scan.json").read_bytes() == before, name
        kept = sorted(p.name for p in directory.iterdir())
        assert kept == sorted([a.path.name for a in scan.artifacts] + ["scan.json"]), name
        assert store.load_scan(scan.token, owner=owner).decisions is None
    assert store.os.replace is real_replace

    # A scan removed while the request waits for its lock is reported as expired, not as a crash.
    real_hold = config_store.hold_lock

    def removed_first(path, *args, **kwargs):
        store._discard(directory)
        return real_hold(path, *args, **kwargs)

    monkeypatch.setattr(config_store, "hold_lock", removed_first)
    with pytest.raises(store.ScanStoreError) as exc:
        store.save_decisions(scan.token, owner=owner, decisions=decisions)
    assert (exc.value.status_code, exc.value.detail) == (410, EXPIRED)
