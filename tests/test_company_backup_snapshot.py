# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A company backup is read from one consistent moment of the database: its own session in
one read-only repeatable-read transaction set to UTC, so a posting made while the backup is
being written is either wholly in it or wholly absent. The file format is unchanged, rows
still stream a batch at a time, and a backup that cannot be completed leaves no file behind."""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid

import pytest
from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from company_backup_support import manifest, members, token
from migration_support import auth, maker, real_client, real_engine  # noqa: F401
from test_company_backup import (
    _bk_cb,
    _bk_cloud,
    _bk_extra_ledger,
    _bk_local,
    _bk_local_file,
    _bk_point_at,
    _bk_restore_new,
    _bk_run,
    _bk_setup,
)

pytestmark = pytest.mark.asyncio

_CALLERS = ["settings", "migration"]
_PROVENANCE = {"prepared_by": "Example Accounting", "source_system": "fake_source"}


async def _download_params(engine, user, cid, caller: str) -> dict | None:
    """Query parameters for the Settings download or the migration completion download."""
    return None if caller == "settings" else {"run_id": str(await _bk_run(engine, user, cid))}


async def _chart(engine, cid) -> None:
    from celerp_accounting.models import Account
    async with maker(engine)() as s:
        s.add(Account(company_id=cid, code="1111", name="Cash", account_type="asset"))
        s.add(Account(company_id=cid, code="4100", name="Sales", account_type="revenue"))
        await s.commit()


async def _post_entry(client, tok: str, amount: float, key: str) -> str:
    """Post a balanced manual journal entry through the API; returns its id."""
    r = await client.post("/accounting/journal-entries", headers=auth(tok), json={
        "ts": "2026-01-15", "memo": f"sale {key}", "idempotency_token": key,
        "entries": [{"account": "1111", "debit": amount}, {"account": "4100", "credit": amount}],
    })
    assert r.status_code == 200, r.text
    return r.json()["je_id"]


async def _trial_balance(client, tok: str) -> dict:
    r = await client.get("/accounting/trial-balance", headers=auth(tok))
    assert r.status_code == 200, r.text
    return r.json()


def _entities(data: bytes, table: str) -> list[str]:
    return [json.loads(line)["entity_id"] for line in members(data)[f"tables/{table}.jsonl"].splitlines()]


def _pause_after_first_table(monkeypatch, cb, names: set[str]) -> tuple[asyncio.Event, asyncio.Event]:
    """Hold the export once it has read every row of the first of ``names`` it reaches."""
    paused, release = asyncio.Event(), asyncio.Event()
    real = cb._batches

    async def held(session, table, *args, **kwargs):
        async for batch in real(session, table, *args, **kwargs):
            yield batch
        if table.name in names and not paused.is_set():
            paused.set()
            await release.wait()

    monkeypatch.setattr(cb, "_batches", held)
    return paused, release


async def _replayed_states(engine, cid) -> tuple[dict, dict]:
    """The company's stored projection states, and the states its ledger replays to."""
    from celerp.models.projections import Projection
    from celerp.projections.engine import ProjectionEngine
    async with maker(engine)() as s:
        stored = {p.entity_id: p.state for p in (await s.execute(
            select(Projection).where(Projection.company_id == cid))).scalars()}
        await ProjectionEngine.rebuild(s, cid)
        await s.flush()
        replayed = {p.entity_id: p.state for p in (await s.execute(
            select(Projection).where(Projection.company_id == cid).execution_options(populate_existing=True)
        )).scalars()}
        await s.rollback()
    return stored, replayed


@pytest.mark.parametrize("caller", _CALLERS)
async def test_export_snapshot_never_mixes_concurrent_write(real_engine, real_client, tmp_path, monkeypatch, caller):
    """A journal entry posted while a backup is being written is wholly absent from the backup,
    which began before it: never its ledger event without its projection. The restored copy's
    projections replay from its ledger and its trial balance equals the books when the backup began."""
    cb = _bk_cb()
    _bk_local(monkeypatch, tmp_path)
    monkeypatch.setattr(cb, "BATCH_ROWS", 2)
    user, cid, tok = await _bk_setup(real_engine)
    await _chart(real_engine, cid)
    for i in range(3):
        await _post_entry(real_client, tok, 100 + i, f"before-{i}")
    params = await _download_params(real_engine, user, cid, caller)
    before = await _trial_balance(real_client, tok)
    async with real_engine.connect() as conn:
        ledger_before = (await conn.execute(text("SELECT count(*) FROM ledger WHERE company_id = :c"),
                                            {"c": cid})).scalar_one()
    paused, release = _pause_after_first_table(monkeypatch, cb, {"ledger", "projections"})
    export = asyncio.create_task(real_client.get("/company-backups/download", params=params, headers=auth(tok)))
    try:
        await asyncio.wait_for(paused.wait(), timeout=20)
        # Posting never waits for a backup being written.
        during = await asyncio.wait_for(_post_entry(real_client, tok, 500, "during"), timeout=20)
    finally:
        release.set()
    r = await asyncio.wait_for(export, timeout=60)
    assert r.status_code == 200, r.text
    ledger, projections = _entities(r.content, "ledger"), _entities(r.content, "projections")
    assert during not in ledger and during not in projections, (during in ledger, during in projections)
    assert set(ledger) == set(projections)
    assert manifest(r.content)["tables"]["ledger"]["rows"] == ledger_before == len(ledger)

    new = await _bk_restore_new(real_client, tok, r.content)
    stored, replayed = await _replayed_states(real_engine, uuid.UUID(new))
    assert set(stored) == set(replayed)
    posted = {e: state for e, state in stored.items() if e.startswith("je:manual:")}
    assert len(posted) == 3 and posted == {e: replayed[e] for e in posted}
    after = await _trial_balance(real_client, await token(real_engine, user, uuid.UUID(new)))
    assert after["balanced"] and after == before


@pytest.mark.parametrize("caller", _CALLERS)
async def test_export_snapshot_transaction_is_repeatable_read_read_only_utc(real_engine, real_client, tmp_path,
                                                                           monkeypatch, caller):
    """The backup's transaction starts as REPEATABLE READ READ ONLY before any query, then sets UTC."""
    cb = _bk_cb()
    _bk_local(monkeypatch, tmp_path)
    user, cid, tok = await _bk_setup(real_engine)
    params = await _download_params(real_engine, user, cid, caller)
    statements: list[tuple[object, str]] = []

    def record(conn, cursor, statement, *args):
        statements.append((conn, statement))

    event.listen(real_engine.sync_engine, "before_cursor_execute", record)
    seen: dict = {}
    real = cb._batches

    async def spying(session, table, *args, **kwargs):
        if not seen:
            seen["conn"] = (await session.connection()).sync_connection
            seen["own"] = [s for c, s in statements if c is seen["conn"]]
            for name in ("transaction_isolation", "transaction_read_only", "TimeZone"):
                seen[name] = await session.scalar(text(f"SHOW {name}"))
        async for batch in real(session, table, *args, **kwargs):
            yield batch

    monkeypatch.setattr(cb, "_batches", spying)
    try:
        r = await real_client.get("/company-backups/download", params=params, headers=auth(tok))
    finally:
        event.remove(real_engine.sync_engine, "before_cursor_execute", record)
    assert r.status_code == 200, r.text
    assert (seen["transaction_isolation"], seen["transaction_read_only"], seen["TimeZone"]) == (
        "repeatable read", "on", "UTC")
    assert seen["own"][:2] == ["SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY",
                               "SET LOCAL TimeZone = 'UTC'"], seen["own"][:3]


@pytest.mark.parametrize("caller", _CALLERS)
async def test_export_snapshot_reads_through_one_dedicated_session(real_engine, real_client, tmp_path, monkeypatch,
                                                                   caller):
    """The company, its schema and every table are read through one session of the backup's own,
    never the request session that authenticated the download."""
    from celerp.db import get_session
    from celerp.main import app
    from celerp.models.company import Company
    cb = _bk_cb()
    _bk_local(monkeypatch, tmp_path)
    user, cid, tok = await _bk_setup(real_engine)
    url = _bk_local_file(tmp_path, cid, "photo.png", b"alpha-photo")
    await _bk_point_at(real_engine, cid, url)
    params = await _download_params(real_engine, user, cid, caller)

    request_sessions: list = []
    serve = app.dependency_overrides[get_session]

    async def recording():
        async for s in serve():
            request_sessions.append(s)
            yield s

    app.dependency_overrides[get_session] = recording
    reads: dict[str, list] = {"schema": [], "rows": [], "company": []}
    real_schema, real_batches, real_get = cb._schema, cb._batches, AsyncSession.get

    async def schema(session):
        reads["schema"].append(session)
        return await real_schema(session)

    async def batches(session, *args, **kwargs):
        reads["rows"].append(session)
        async for batch in real_batches(session, *args, **kwargs):
            yield batch

    async def get(self, entity, ident, *args, **kwargs):
        if entity is Company and str(ident) == str(cid):
            reads["company"].append(self)
        return await real_get(self, entity, ident, *args, **kwargs)

    monkeypatch.setattr(cb, "_schema", schema)
    monkeypatch.setattr(cb, "_batches", batches)
    monkeypatch.setattr(AsyncSession, "get", get)
    r = await real_client.get("/company-backups/download", params=params, headers=auth(tok))
    assert r.status_code == 200, r.text
    assert members(r.content)["attachments/photo.png"] == b"alpha-photo"
    used = {id(s) for s in reads["schema"] + reads["rows"]}
    assert len(used) == 1 and reads["rows"], used
    [snapshot] = reads["schema"][:1]
    assert any(s is snapshot for s in reads["company"])
    assert request_sessions and not any(s is snapshot for s in request_sessions)


@pytest.mark.parametrize("failure", ["missing", "read_error"])
async def test_export_unreadable_attachment_removes_partial(real_engine, real_client, tmp_path, monkeypatch, failure):
    """An attachment that cannot be read stops the backup with a plain message on both downloads and
    from the service; the unfinished file is removed and no backup file is ever exposed."""
    from celerp.config import settings
    cb = _bk_cb()
    fake = _bk_cloud(monkeypatch, tmp_path)
    user, cid, tok = await _bk_setup(real_engine)
    url = fake.url(cid, "photo.png")
    fake.files[url] = b"cloud-photo"
    await _bk_point_at(real_engine, cid, url)
    folder = settings.data_dir / "company_backups" / "exports"
    seen_while_reading: list[list[str]] = []

    async def unreadable(company_id, read_url, max_bytes):
        seen_while_reading.append(sorted(p.name for p in folder.rglob("*") if p.is_file()))
        if failure == "read_error":
            raise OSError("cloud storage unavailable")
        return None

    monkeypatch.setattr(fake, "read", unreadable)
    for params in (None, {"run_id": str(await _bk_run(real_engine, user, cid))}):
        r = await real_client.get("/company-backups/download", params=params, headers=auth(tok))
        assert r.status_code == 409, (params, r.text)
        detail = r.json()["detail"]
        assert url in detail and detail.endswith("Nothing was backed up."), detail
        assert not [p for p in folder.rglob("*") if p.is_file()]
    out = tmp_path / "bk-out" / "books.celerp-company"
    with pytest.raises(cb.BackupError) as err:
        await cb.export_company_snapshot(cid, out)
    assert err.value.status_code == 409 and url in err.value.detail
    assert list(out.parent.iterdir()) == []
    assert len(seen_while_reading) == 3
    assert all(len(names) == 1 and names[0].endswith(".export.partial")
               for names in seen_while_reading[:2]), seen_while_reading


async def test_export_snapshot_streams_in_bounded_batches(real_engine, tmp_path, monkeypatch):
    """Rows are still read and written BATCH_ROWS at a time, in primary-key order, every row once."""
    cb = _bk_cb()
    _bk_local(monkeypatch, tmp_path)
    monkeypatch.setattr(cb, "BATCH_ROWS", 2)
    _, cid, _ = await _bk_setup(real_engine)
    await _bk_extra_ledger(real_engine, cid, 6, "batch")
    sizes: dict[str, list[int]] = {}
    real = cb._batches

    async def counting(session, table, *args, **kwargs):
        async for batch in real(session, table, *args, **kwargs):
            sizes.setdefault(table.name, []).append(len(batch))
            yield batch

    monkeypatch.setattr(cb, "_batches", counting)
    out = tmp_path / "bk-out" / "books.celerp-company"
    m = await cb.export_company_snapshot(cid, out)
    assert sizes["ledger"] == [2, 2, 2, 1] and sizes["projections"] == [2, 2, 2, 1], sizes
    assert all(n <= 2 for batch_sizes in sizes.values() for n in batch_sizes)
    data = out.read_bytes()
    keys = [json.loads(line)["idempotency_key"] for line in members(data)["tables/ledger.jsonl"].splitlines()]
    async with real_engine.connect() as conn:
        stored = (await conn.execute(text("SELECT idempotency_key FROM ledger WHERE company_id = :c ORDER BY id"),
                                     {"c": cid})).scalars().all()
    assert keys == stored and len(set(keys)) == m["tables"]["ledger"]["rows"] == 7


async def test_export_snapshot_archive_format_unchanged(real_engine, real_client, tmp_path, monkeypatch):
    """The backup file keeps the format restore reads: the same members, manifest shape and
    hashes, and it restores as before."""
    cb = _bk_cb()
    _bk_local(monkeypatch, tmp_path)
    _, cid, tok = await _bk_setup(real_engine)
    url = _bk_local_file(tmp_path, cid, "photo.png", b"alpha-photo")
    await _bk_point_at(real_engine, cid, url)
    out = tmp_path / "bk-out" / "books.celerp-company"
    m = await cb.export_company_snapshot(cid, out, provenance=_PROVENANCE)
    data = out.read_bytes()
    parts = members(data)
    assert set(parts) == ({"manifest.json", "attachments/photo.png"}
                          | {f"tables/{t}.jsonl" for t in m["tables"]})
    assert json.loads(parts["manifest.json"]) == m
    assert set(m) == {"format", "format_version", "backup_id", "created_at", "company", "provenance",
                      "modules", "tables", "attachments"}
    assert (m["format"], m["format_version"], m["provenance"]) == ("celerp-company-backup", 1, _PROVENANCE)
    assert set(m["company"]) == {"id", "name", "settings"} and m["company"]["id"] == str(cid)
    assert set(m["modules"]) == {"enabled", "versions"}
    async with maker(real_engine)() as s:
        assert list(m["tables"]) == [t for t in await cb.classify(s) if t in m["tables"]]
    for name, meta in m["tables"].items():
        assert set(meta) == {"columns", "rows", "sha256"}
        body = parts[f"tables/{name}.jsonl"]
        assert hashlib.sha256(body).hexdigest() == meta["sha256"]
        lines = body.splitlines()
        assert len(lines) == meta["rows"] and all(isinstance(json.loads(line), dict) for line in lines)
    assert m["attachments"] == [{"url": url, "name": "photo.png", "size": len(b"alpha-photo"),
                                 "sha256": hashlib.sha256(b"alpha-photo").hexdigest()}]
    assert cb.read_backup(out).manifest == m
    await _bk_restore_new(real_client, tok, data)


async def test_download_routes_use_export_company_snapshot(real_engine, real_client, tmp_path, monkeypatch):
    """The Settings download and the migration completion download both back up through the
    snapshot export, handing it no request session; the old session-taking export is gone."""
    cb = _bk_cb()
    _bk_local(monkeypatch, tmp_path)
    user, cid, tok = await _bk_setup(real_engine)
    run = await _bk_run(real_engine, user, cid)
    calls: list[tuple] = []

    async def snapshot_export(company_id, out, *, provenance=None):
        calls.append((company_id, out, provenance))
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"backup")
        return {}

    monkeypatch.setattr(cb, "export_company_snapshot", snapshot_export, raising=False)
    for params in (None, {"run_id": str(run)}):
        r = await real_client.get("/company-backups/download", params=params, headers=auth(tok))
        assert r.status_code == 200, r.text
        assert r.content == b"backup"
    assert [(c[0], c[2]) for c in calls] == [(cid, None), (cid, _PROVENANCE)]
    assert all(c[1].name.endswith(".export") for c in calls)
    assert not hasattr(cb, "export_company")


async def test_export_snapshot_sqlite_dialect_uses_one_plain_transaction(tmp_path, monkeypatch):
    """On SQLite the backup reads through its own session in one plain transaction, with no
    PostgreSQL isolation or time zone statement."""
    import celerp.db
    cb = _bk_cb()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'books.sqlite'}")
    statements: list[str] = []
    event.listen(engine.sync_engine, "before_cursor_execute",
                 lambda conn, cursor, statement, *args: statements.append(statement))
    monkeypatch.setattr(celerp.db, "engine", engine)
    seen: dict = {}

    async def export(session, company_id, out, *, provenance=None):
        seen.update(bind=session.get_bind(), in_transaction=session.in_transaction(),
                    args=(company_id, out, provenance))
        await session.execute(text("SELECT 1"))
        return {"written": True}

    monkeypatch.setattr(cb, "_export_company", export, raising=False)
    out = tmp_path / "books.celerp-company"
    try:
        assert await cb.export_company_snapshot("company-1", out, provenance=_PROVENANCE) == {"written": True}
    finally:
        await engine.dispose()
    assert seen["bind"] is engine.sync_engine and seen["in_transaction"]
    assert seen["args"] == ("company-1", out, _PROVENANCE)
    assert statements == ["SELECT 1"]
