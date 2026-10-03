# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Everything a company keeps outside the database goes with it when it is reset: its
attachment files on every storage backend, its migration source files, its assistant
uploads, its staged import files and its generated backups. Other companies keep theirs.
A file being written while the company is reset is either removed with the company or
never written. Races run on a real database, each pausing one side at a named point."""

from __future__ import annotations

import asyncio
import importlib
import io
import json
import time
import uuid

import pytest
from PIL import Image
from sqlalchemy import text

from company_backup_support import company, owner, token
from migration_support import auth, count, maker, real_client, real_engine  # noqa: F401

pytestmark = pytest.mark.asyncio

RESET = "/companies/me/reset"
SCOPES = ("migration run", "attachments", "assistant uploads", "staged imports", "company backups")


# ── Storage ──────────────────────────────────────────────────────────────────

def _local_files(monkeypatch, tmp_path):
    from celerp.config import settings
    from celerp.services import attachments
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(attachments, "_backend", attachments.LocalBackend())


class _NoSuchKey(Exception):
    response = {"Error": {"Code": "NoSuchKey"}}


class _Body:
    def __init__(self, data: bytes) -> None:
        self.data = data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def read(self, n: int = -1) -> bytes:
        return self.data if n < 0 else self.data[:n]


class _Bucket:
    """An in-memory S3 bucket speaking the calls the S3 backend makes."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.stored: list[str] = []
        self.failing = False

    def client(self, *_):
        return _BucketClient(self)


class _BucketClient:
    def __init__(self, bucket: _Bucket) -> None:
        self.bucket = bucket

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def put_object(self, Bucket, Key, Body, ContentType):
        self.bucket.objects[Key] = Body
        self.bucket.stored.append(Key)

    async def get_object(self, Bucket, Key):
        if Key not in self.bucket.objects:
            raise _NoSuchKey()
        data = self.bucket.objects[Key]
        return {"ContentLength": len(data), "Body": _Body(data)}

    async def delete_object(self, Bucket, Key):
        if self.bucket.failing:
            raise OSError("storage unavailable")
        self.bucket.objects.pop(Key, None)

    async def list_objects_v2(self, Bucket, Prefix, ContinuationToken=None):
        return {"Contents": [{"Key": k} for k in sorted(self.bucket.objects) if k.startswith(Prefix)],
                "IsTruncated": False}

    async def delete_objects(self, Bucket, Delete):
        if self.bucket.failing:
            return {"Errors": [{"Key": o["Key"]} for o in Delete["Objects"]]}
        for o in Delete["Objects"]:
            self.bucket.objects.pop(o["Key"], None)
        return {}


def _s3_files(monkeypatch, tmp_path) -> _Bucket:
    from celerp.config import settings
    from celerp.services import attachments
    bucket = _Bucket()
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(attachments, "_s3_client", bucket.client)
    monkeypatch.setattr(attachments, "_backend", attachments.S3Backend("https://s3.example.test", "bucket", "k", "s"))
    return bucket


def _png() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (64, 48), (200, 40, 40)).save(out, "PNG")
    return out.getvalue()


# ── Seeding and inspection ───────────────────────────────────────────────────

async def _seed(engine, cid, user_id, marker: str) -> dict:
    """One file of every kind for the company, as each writer leaves it on disk; returns
    the path of each, by kind."""
    from celerp.config import settings
    from celerp.models.migration import MigrationRun
    from celerp.services import migration_scan_store as store
    async with maker(engine)() as s:
        run = MigrationRun(company_id=cid, created_by_user_id=user_id, scan_claim_sha256=uuid.uuid4().hex * 2,
                           source_system="fake", source_artifact_sha256="0" * 64, adapter_version="1",
                           cif_version="1", mode="full_history", status="completed")
        s.add(run)
        await s.commit()
    data = settings.data_dir
    upload, stage = data / "ai_uploads" / f"ai_up_{uuid.uuid4().hex}", data / "import_staging" / f"imp_{uuid.uuid4().hex}"
    files = {
        "migration run": (store.run_dir(run.id) / "books.fake", marker),
        "attachments": (data / "static" / "attachments" / str(cid) / f"{marker}.pdf", marker),
        "assistant uploads": (upload.with_suffix(".bin"), marker),
        "staged imports": (stage.with_suffix(".csv"), f"name\n{marker}\n"),
        "company backups": (data / "company_backups" / str(cid) / f"{marker}.celerp-company", marker),
    }
    metas = {upload.with_suffix(".meta"): {"filename": "rows.csv", "content_type": "text/csv", "size": 3,
                                           "company_id": str(cid), "user_id": str(user_id)},
             stage.with_suffix(".meta"): {"company_id": str(cid), "created_at": time.time()}}
    for path, body in [*((p, json.dumps(m)) for p, m in metas.items()), *files.values()]:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    return {kind: path for kind, (path, _) in files.items()}


async def _harbor(engine):
    """Companies A and B with a file of every kind each; A's owner is ``boss``."""
    boss = await owner(engine)
    a = await company(engine, boss, "Harbor Goods Ltd", "alpha")
    b = await company(engine, boss, "Hillside Supply Co", "bravo")
    return boss, a, b, await _seed(engine, a, boss, "alpha"), await _seed(engine, b, boss, "bravo")


async def _reset(client, engine, boss, cid):
    return await client.post(RESET, json={"company_name": "Harbor Goods Ltd"},
                             headers=auth(await token(engine, boss, cid)))


def _left(paths: dict) -> set[str]:
    return {kind for kind, path in paths.items() if path.exists()}


def _company_files(tmp_path, cid) -> list[str]:
    """Every file on disk that names the company, in its path or in its metadata."""
    found = []
    for path in tmp_path.rglob("*"):
        if not path.is_file():
            continue
        if str(cid) in str(path.relative_to(tmp_path)):
            found.append(str(path.relative_to(tmp_path)))
        elif path.suffix == ".meta" and str(cid) in path.read_text(errors="replace"):
            found.append(str(path.relative_to(tmp_path)))
    return found


async def _tasks(engine) -> int:
    return await count(engine, "migration_cleanup_tasks")


async def _sweep(engine) -> int:
    from celerp.services import migrations
    async with maker(engine)() as s:
        return await migrations.sweep_cleanup_tasks(s)


# ── Pausing one side of a race ───────────────────────────────────────────────

def _pause_after(monkeypatch, module, name: str):
    """Pause the first call of ``module.name`` after it returns, until released."""
    reached, release = asyncio.Event(), asyncio.Event()
    real = getattr(module, name)

    async def paused(*args, **kwargs):
        out = await real(*args, **kwargs)
        if not reached.is_set():
            reached.set()
            await release.wait()
        return out

    monkeypatch.setattr(module, name, paused)
    return reached, release


def _pause_before(monkeypatch, module, name: str):
    """Pause the first call of ``module.name`` before it runs, until released."""
    reached, release = asyncio.Event(), asyncio.Event()
    real = getattr(module, name)

    async def paused(*args, **kwargs):
        if not reached.is_set():
            reached.set()
            await release.wait()
        return await real(*args, **kwargs)

    monkeypatch.setattr(module, name, paused)
    return reached, release


async def _until(reached: asyncio.Event, request: asyncio.Task) -> None:
    """Wait for the paused point, failing at once if the request ended before reaching it."""
    paused = asyncio.create_task(reached.wait())
    await asyncio.wait({paused, request}, return_when=asyncio.FIRST_COMPLETED)
    if not reached.is_set():
        paused.cancel()
        raise AssertionError(f"the request ended before the paused point: {request.result()!r}")


async def _waits_for_a_lock(engine) -> None:
    """Return once some connection is waiting for a lock: the reset waiting for a holder."""
    for _ in range(200):
        async with engine.connect() as conn:
            waiting = (await conn.execute(text(
                "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                "AND wait_event_type = 'Lock'"))).scalar_one()
        if waiting:
            return
        await asyncio.sleep(0.05)
    raise AssertionError("the reset never waited for the writer holding the company")


# ── Reset and its cleanup ────────────────────────────────────────────────────

async def test_reset_deletes_every_file_kept_for_the_company_and_no_other(real_engine, real_client, tmp_path,
                                                                          monkeypatch):
    _local_files(monkeypatch, tmp_path)
    boss, a, b, files_a, files_b = await _harbor(real_engine)

    r = await _reset(real_client, real_engine, boss, a)

    assert r.status_code == 200, r.text
    assert _left(files_a) == set()
    assert _company_files(tmp_path, a) == []
    assert _left(files_b) == set(SCOPES)
    assert await _tasks(real_engine) == 0


@pytest.mark.parametrize("scope", SCOPES)
async def test_a_kind_of_file_that_cannot_be_deleted_is_retried_until_it_is(real_engine, real_client, tmp_path,
                                                                             monkeypatch, scope):
    _local_files(monkeypatch, tmp_path)
    boss, a, b, files_a, files_b = await _harbor(real_engine)
    module, name = {"migration run": ("celerp.services.migration_scan_store", "remove_run_dir"),
                    "attachments": ("celerp.services.attachments", "delete_company_files"),
                    "assistant uploads": ("celerp.ai.files", "delete_company_uploads"),
                    "staged imports": ("celerp.services.import_stage", "delete_company_stages"),
                    "company backups": ("celerp.services.company_files", "_remove_backups")}[scope]
    module = importlib.import_module(module)

    def unavailable(*_):
        raise OSError("storage unavailable")

    async def unavailable_async(*_):
        raise OSError("storage unavailable")

    with monkeypatch.context() as m:
        m.setattr(module, name, unavailable_async if asyncio.iscoroutinefunction(getattr(module, name, None))
                  else unavailable, raising=False)
        r = await _reset(real_client, real_engine, boss, a)

        # The company is gone; every other kind of file went; the failed kind and its
        # cleanup task stay, and a sweep while the failure lasts keeps both.
        assert r.status_code == 200, r.text
        assert await count(real_engine, "companies", "id = :c", c=str(a)) == 0
        assert _left(files_a) == {scope}
        assert await _tasks(real_engine) == 1
        assert await _sweep(real_engine) == 1
        assert _left(files_a) == {scope}

    assert await _sweep(real_engine) == 0
    assert _left(files_a) == set()
    assert await _tasks(real_engine) == 0
    assert _left(files_b) == set(SCOPES)


@pytest.mark.parametrize("backend", ["local", "s3", "already gone", "s3 failing then back"])
async def test_attachment_cleanup_on_each_storage_backend(real_engine, tmp_path, monkeypatch, backend):
    from celerp.config import settings
    from celerp.models.migration import MigrationCleanupTask
    from celerp.services import migrations
    bucket = _s3_files(monkeypatch, tmp_path) if backend.startswith("s3") else None
    if bucket is None:
        _local_files(monkeypatch, tmp_path)
    a, b = uuid.uuid4(), uuid.uuid4()
    if bucket is not None:
        bucket.objects.update({f"attachments/{a}/one.png": b"a", f"attachments/{a}/one_thumb.jpg": b"a",
                               f"attachments/{b}/two.png": b"b"})
    if backend != "already gone":
        for cid in (a, b):  # local files: the local backend, or files kept from before S3 was set up
            folder = settings.data_dir / "static" / "attachments" / str(cid)
            folder.mkdir(parents=True)
            (folder / "old.pdf").write_bytes(b"x")
    if bucket is not None and backend.endswith("back"):
        bucket.failing = True
    async with maker(real_engine)() as s:
        task = MigrationCleanupTask(company_id=a, run_ids=[])
        s.add(task)
        await s.commit()
        done = await migrations.run_cleanup_task(s, task.id)

    if bucket is not None and bucket.failing:
        assert done is False and await _tasks(real_engine) == 1
        assert f"attachments/{a}/one.png" in bucket.objects
        bucket.failing = False
        assert await _sweep(real_engine) == 0
    else:
        assert done is True
    assert await _tasks(real_engine) == 0
    assert _company_files(tmp_path, a) == []
    if bucket is not None:
        assert sorted(bucket.objects) == [f"attachments/{b}/two.png"]
    if backend != "already gone":
        assert (settings.data_dir / "static" / "attachments" / str(b) / "old.pdf").exists()


# ── Attachments ──────────────────────────────────────────────────────────────

async def _upload_photo(client, engine, boss, cid):
    return await client.post("/items/item:1/attachments", files={"file": ("photo.png", _png(), "image/png")},
                             headers=auth(await token(engine, boss, cid)))


async def test_a_reset_waits_for_a_file_being_attached_then_deletes_it(real_engine, real_client, tmp_path,
                                                                       monkeypatch):
    from celerp.services import attachments
    _local_files(monkeypatch, tmp_path)
    boss, a, b, files_a, files_b = await _harbor(real_engine)
    reached, release = _pause_after(monkeypatch, attachments, "hold_company")
    upload = asyncio.create_task(_upload_photo(real_client, real_engine, boss, a))
    await _until(reached, upload)

    reset = asyncio.create_task(_reset(real_client, real_engine, boss, a))
    await _waits_for_a_lock(real_engine)
    assert not reset.done()
    release.set()

    assert (await upload).status_code == 200
    assert (await reset).status_code == 200
    assert _company_files(tmp_path, a) == []
    assert _left(files_b) == set(SCOPES)


async def test_a_file_attached_after_the_reset_is_never_stored(real_engine, real_client, tmp_path, monkeypatch):
    from celerp.services import attachments
    _local_files(monkeypatch, tmp_path)
    boss, a, b, files_a, files_b = await _harbor(real_engine)
    reached, release = _pause_before(monkeypatch, attachments, "hold_company")
    upload = asyncio.create_task(_upload_photo(real_client, real_engine, boss, a))
    await _until(reached, upload)

    assert (await _reset(real_client, real_engine, boss, a)).status_code == 200
    release.set()

    r = await upload
    assert r.status_code == 404
    assert _company_files(tmp_path, a) == []
    assert _left(files_b) == set(SCOPES)


@pytest.mark.parametrize("backend", ["local", "s3"])
async def test_a_stored_file_whose_record_fails_is_deleted_with_its_thumbnail(real_engine, real_client, tmp_path,
                                                                              monkeypatch, backend):
    import celerp_inventory.routes_attachments as routes
    bucket = _s3_files(monkeypatch, tmp_path) if backend == "s3" else None
    if bucket is None:
        _local_files(monkeypatch, tmp_path)
    boss = await owner(real_engine)
    a = await company(real_engine, boss, "Harbor Goods Ltd", "alpha")

    async def record_fails(*_args, **_kwargs):
        raise OSError("database unavailable")

    monkeypatch.setattr(routes, "_patch_item_attachments", record_fails)
    try:
        r = await _upload_photo(real_client, real_engine, boss, a)
        assert r.status_code >= 500
    except OSError:
        pass

    if bucket is not None:
        # The original and its thumbnail were both stored, then both deleted.
        assert len(bucket.stored) == 2 and any(k.endswith("_thumb.jpg") for k in bucket.stored)
        assert bucket.objects == {}
    assert _company_files(tmp_path, a) == []
    async with real_engine.connect() as conn:
        state = (await conn.execute(text("SELECT state FROM projections WHERE company_id = :c AND entity_id = 'item:1'"),
                                    {"c": str(a)})).scalar_one()
    assert not (state if isinstance(state, dict) else json.loads(state)).get("attachments")


# ── Assistant uploads ────────────────────────────────────────────────────────

def _connected(monkeypatch):
    """The installation has a Connect session, which the assistant requires."""
    import celerp.session_gate
    monkeypatch.setattr(celerp.session_gate, "get_session_token", lambda: "test-session-token")


async def _ai_upload(client, engine, boss, cid):
    return await client.post("/ai/upload", files={"files": ("rows.csv", b"a,b\n1,2\n", "text/csv")},
                             headers=auth(await token(engine, boss, cid)))


async def test_a_reset_waits_for_an_assistant_upload_then_deletes_it(real_engine, real_client, tmp_path,
                                                                     monkeypatch):
    import celerp_ai.routes as ai_routes
    _local_files(monkeypatch, tmp_path)
    _connected(monkeypatch)
    boss, a, b, files_a, files_b = await _harbor(real_engine)
    reached, release = _pause_after(monkeypatch, ai_routes, "hold_company")
    upload = asyncio.create_task(_ai_upload(real_client, real_engine, boss, a))
    await _until(reached, upload)

    reset = asyncio.create_task(_reset(real_client, real_engine, boss, a))
    await _waits_for_a_lock(real_engine)
    assert not reset.done()
    release.set()

    assert (await upload).status_code == 201
    assert (await reset).status_code == 200
    assert _company_files(tmp_path, a) == []
    assert _left(files_b) == set(SCOPES)


async def test_an_assistant_upload_after_the_reset_is_never_stored(real_engine, real_client, tmp_path,
                                                                   monkeypatch):
    import celerp_ai.routes as ai_routes
    _local_files(monkeypatch, tmp_path)
    _connected(monkeypatch)
    boss, a, b, files_a, files_b = await _harbor(real_engine)
    reached, release = _pause_before(monkeypatch, ai_routes, "hold_company")
    upload = asyncio.create_task(_ai_upload(real_client, real_engine, boss, a))
    await _until(reached, upload)

    assert (await _reset(real_client, real_engine, boss, a)).status_code == 200
    release.set()

    r = await upload
    assert r.status_code == 404
    assert _company_files(tmp_path, a) == []


@pytest.mark.parametrize("status", ["pending", "running", "completed", "failed"])
async def test_a_reset_waits_until_the_assistant_has_finished_reading_files(real_engine, real_client, tmp_path,
                                                                            monkeypatch, status):
    from celerp.models.ai import AIBatchJob
    _local_files(monkeypatch, tmp_path)
    boss, a, b, files_a, files_b = await _harbor(real_engine)
    async with maker(real_engine)() as s:
        s.add(AIBatchJob(company_id=a, user_id=boss, status=status, total_files=1, query="read these",
                         file_ids=[]))
        await s.commit()

    r = await _reset(real_client, real_engine, boss, a)

    if status in ("pending", "running"):
        assert r.status_code == 409
        assert r.json()["detail"] == ("Wait for the assistant to finish reading files before resetting this "
                                      "company. Nothing was deleted.")
        assert await count(real_engine, "companies", "id = :c", c=str(a)) == 1
        assert _left(files_a) == set(SCOPES)
    else:
        assert r.status_code == 200, r.text
        assert _left(files_a) == set()


# ── Staged imports ───────────────────────────────────────────────────────────

def _company_from_api(monkeypatch, client, hook=None):
    """The UI's company lookup, answered by the API under test; ``hook`` runs before the
    second lookup, the one made once the stage is written."""
    from ui import api_client as api
    from ui.routes import csv_import
    calls = []

    async def company_id(tok: str) -> str:
        calls.append(tok)
        if len(calls) == 2 and hook is not None:
            await hook()
        r = await client.get("/companies/me", headers=auth(tok))
        if r.status_code != 200:
            raise api.APIError(r.status_code, r.json().get("detail", ""))
        return str(r.json()["id"])

    monkeypatch.setattr(csv_import, "_company_id", company_id)


async def test_an_import_staged_while_the_company_is_reset_deletes_itself(real_engine, real_client, tmp_path,
                                                                         monkeypatch):
    from ui import api_client as api
    from ui.routes import csv_import
    _local_files(monkeypatch, tmp_path)
    boss, a, b, files_a, files_b = await _harbor(real_engine)
    tok = await token(real_engine, boss, a)

    async def reset_now():
        assert (await _reset(real_client, real_engine, boss, a)).status_code == 200

    _company_from_api(monkeypatch, real_client, reset_now)
    with pytest.raises(api.APIError) as exc:
        await csv_import.stash_import_csv(tok, "name\nlate\n")

    assert exc.value.status == 401
    assert _company_files(tmp_path, a) == []
    assert _left(files_b) == set(SCOPES)


async def test_an_import_staged_before_the_reset_is_deleted_by_it(real_engine, real_client, tmp_path,
                                                                 monkeypatch):
    from ui.routes import csv_import
    _local_files(monkeypatch, tmp_path)
    boss, a, b, files_a, files_b = await _harbor(real_engine)
    _company_from_api(monkeypatch, real_client)
    ref = await csv_import.stash_import_csv(await token(real_engine, boss, a), "name\nearly\n")
    staged = sorted(p.name for p in (tmp_path / "import_staging").glob(f"{ref}.*"))
    assert staged == [f"{ref}.csv", f"{ref}.meta"]

    assert (await _reset(real_client, real_engine, boss, a)).status_code == 200

    assert list((tmp_path / "import_staging").glob(f"{ref}.*")) == []
    assert _company_files(tmp_path, a) == []


# ── Backups ──────────────────────────────────────────────────────────────────

async def test_a_backup_finished_after_the_reset_is_never_published(real_engine, real_client, tmp_path,
                                                                    monkeypatch):
    from celerp.config import settings
    from celerp.services import company_backup as cb
    _local_files(monkeypatch, tmp_path)
    boss, a, b, files_a, files_b = await _harbor(real_engine)
    out = settings.data_dir / "company_backups" / str(a) / f"late{cb.EXTENSION}"
    reached, release = _pause_after(monkeypatch, cb, "_export_company")
    export = asyncio.create_task(cb.export_company_snapshot(a, out))
    await _until(reached, export)

    assert (await _reset(real_client, real_engine, boss, a)).status_code == 200
    release.set()

    with pytest.raises(cb.BackupError) as exc:
        await export
    assert exc.value.status_code == 404
    assert _company_files(tmp_path, a) == []
    assert _left(files_b) == set(SCOPES)


async def test_a_reset_waits_for_a_backup_being_published_then_deletes_it(real_engine, real_client, tmp_path,
                                                                          monkeypatch):
    from celerp.config import settings
    from celerp.services import company_backup as cb
    _local_files(monkeypatch, tmp_path)
    boss, a, b, files_a, files_b = await _harbor(real_engine)
    out = settings.data_dir / "company_backups" / str(a) / f"early{cb.EXTENSION}"
    reached, release = _pause_after(monkeypatch, cb, "hold_company")
    export = asyncio.create_task(cb.export_company_snapshot(a, out))
    await _until(reached, export)

    reset = asyncio.create_task(_reset(real_client, real_engine, boss, a))
    await _waits_for_a_lock(real_engine)
    assert not reset.done()
    release.set()

    assert isinstance(await export, dict)
    assert (await reset).status_code == 200
    assert _company_files(tmp_path, a) == []
    assert _left(files_b) == set(SCOPES)
