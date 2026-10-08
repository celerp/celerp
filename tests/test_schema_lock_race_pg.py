# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Company backups and schema changes on real Postgres.

Every schema change (a Celerp or module migration, the start-up table creation, a
module data purge, System Recovery replacing the database) holds the schema key alone; a company export or restore shares it
for its whole transaction and is refused at once while a change holds it. These tests
hold one side open on its own connection and drive the other.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import threading
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from company_backup_support import auth, confirm, download, manifest, members, read
from migration_support import real_client, real_engine  # noqa: F401
from test_company_backup import (  # noqa: F401
    _BK_MODULE,
    _bk_cb,
    _bk_drop,
    _bk_fake_module,
    _bk_local,
    _bk_modules_running,
    _bk_setup,
    _bk_sql,
)

pytestmark = pytest.mark.asyncio

SCHEMA_KEY = 4207320001
_WIDGETS = ("CREATE TABLE zz_widgets (id uuid primary key, "
            "company_id uuid not null references companies(id) on delete cascade, note text)")


def _split(key: int) -> tuple[int, int]:
    return key >> 32, key & 0xFFFFFFFF


async def _holders(engine, key: int = SCHEMA_KEY) -> list[str]:
    """The granted lock modes on *key* across every connection to this database."""
    hi, lo = _split(key)
    async with engine.connect() as conn:
        return sorted((await conn.execute(text(
            "SELECT mode FROM pg_locks WHERE locktype = 'advisory' AND granted "
            "AND database = (SELECT oid FROM pg_database WHERE datname = current_database()) "
            "AND classid = :hi AND objid = :lo AND objsubid = 1"), {"hi": hi, "lo": lo})).scalars())


def _holders_now(url: str, key: int = SCHEMA_KEY) -> list[str]:
    """``_holders`` from a thread of its own, on the database at *url*."""
    from celerp.db_url import sync_url
    engine = create_engine(sync_url(url), poolclass=NullPool)
    hi, lo = _split(key)
    try:
        with engine.connect() as conn:
            return sorted(conn.execute(text(
                "SELECT mode FROM pg_locks WHERE locktype = 'advisory' AND granted "
                "AND database = (SELECT oid FROM pg_database WHERE datname = current_database()) "
                "AND classid = :hi AND objid = :lo AND objsubid = 1"), {"hi": hi, "lo": lo}).scalars())
    finally:
        engine.dispose()


async def _own_keys(session) -> set[str]:
    """Which of the module-state and schema keys *session*'s connection holds."""
    from celerp.modules.registry import _MODULE_STATE_LOCK_KEY
    rows = (await session.execute(text(
        "SELECT classid, objid FROM pg_locks WHERE locktype = 'advisory' AND granted "
        "AND pid = pg_backend_pid() AND objsubid = 1"))).all()
    names = {_split(SCHEMA_KEY): "schema", _split(_MODULE_STATE_LOCK_KEY): "module-state"}
    return {names[(r.classid, r.objid)] for r in rows if (r.classid, r.objid) in names}


def _untimed(engine):
    """An engine on the same database with no statement or lock timeouts."""
    return create_async_engine(engine.url, poolclass=NullPool)


async def _with_widgets(engine, cid, note: str = "widget-marker") -> None:
    await _bk_sql(engine, _WIDGETS)
    await _bk_sql(engine, "INSERT INTO zz_widgets (id, company_id, note) VALUES (:i, :c, :n)",
                  i=uuid.uuid4(), c=cid, n=note)


class _Paused:
    """An export started and held as it reads zz_widgets, until released."""

    def __init__(self, monkeypatch, *, fail: bool = False):
        cb = _bk_cb()
        batches = cb._batches
        self.reached, self.release, self.fail = asyncio.Event(), asyncio.Event(), fail
        self.count = 0

        async def held(session, table, *args):
            if table.name == "zz_widgets":
                self.count += 1
                self.reached.set()
                await self.release.wait()
                if self.fail:
                    raise RuntimeError("export interrupted")
            async for batch in batches(session, table, *args):
                yield batch

        monkeypatch.setattr(cb, "_batches", held)

    def start(self, cid, out):
        return asyncio.create_task(_bk_cb().export_company_snapshot(cid, out))


async def _not_done(task, seconds: float = 1.5) -> bool:
    done, _ = await asyncio.wait({task}, timeout=seconds)
    return not done


@pytest.fixture
async def widgets_company(real_engine, tmp_path, monkeypatch):  # noqa: F811
    _bk_local(monkeypatch, tmp_path)
    _bk_fake_module(tmp_path, monkeypatch)
    user, cid, tok = await _bk_setup(real_engine, settings={"enabled_modules": [_BK_MODULE]})
    await _with_widgets(real_engine, cid)
    yield user, cid, tok
    await _bk_drop(real_engine, "zz_widgets")


# ── 1. a schema change holding the key refuses backups at once ────────────────

async def test_schema_change_in_progress_refuses_export_and_restore(
        real_engine, real_client, widgets_company, tmp_path):  # noqa: F811
    _, cid, tok = widgets_company
    data = await download(real_client, tok)
    preview = await read(real_client, tok, data, mode="new_company")
    assert preview.status_code == 200, preview.text
    async with real_engine.connect() as holder:
        await holder.execute(text("SELECT pg_advisory_lock(:k)"), {"k": SCHEMA_KEY})
        try:
            r = await real_client.get("/company-backups/download", headers=auth(tok))
            assert r.status_code == 409, r.text
            assert "updating its database" in r.json()["detail"]
            out = tmp_path / "direct.celerp-company"
            with pytest.raises(_bk_cb().BackupError) as err:
                await _bk_cb().export_company_snapshot(cid, out)
            assert err.value.status_code == 409 and not out.exists()

            again = await read(real_client, tok, data, mode="new_company")
            assert again.status_code == 409, again.text
            before = await _company_count(real_engine)
            r = await real_client.post("/company-backups/restore", json=confirm(preview, "new_company"),
                                       headers=auth(tok))
            assert r.status_code == 409, r.text
            assert "Nothing was restored" in r.json()["detail"]
            assert await _company_count(real_engine) == before
        finally:
            await holder.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": SCHEMA_KEY})


async def _company_count(engine) -> int:
    async with engine.connect() as conn:
        return (await conn.execute(text("SELECT count(*) FROM companies"))).scalar_one()


# ── 2. a running backup blocks every schema change ───────────────────────────

async def test_running_export_blocks_migration_lock(real_engine, widgets_company, tmp_path, monkeypatch):  # noqa: F811
    _, cid, _ = widgets_company
    paused = _Paused(monkeypatch)
    task = paused.start(cid, tmp_path / "a.celerp-company")
    await asyncio.wait_for(paused.reached.wait(), 15)
    try:
        assert await _holders(real_engine) == ["ShareLock"]
        async with real_engine.connect() as conn:
            assert (await conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": SCHEMA_KEY})).scalar_one() is False
    finally:
        paused.release.set()
    await asyncio.wait_for(task, 15)
    assert await _holders(real_engine) == []


async def test_running_export_blocks_startup_table_creation(real_engine, widgets_company, tmp_path, monkeypatch):  # noqa: F811
    from celerp.db import create_tables

    _, cid, _ = widgets_company
    paused = _Paused(monkeypatch)
    task = paused.start(cid, tmp_path / "a.celerp-company")
    await asyncio.wait_for(paused.reached.wait(), 15)
    untimed = _untimed(real_engine)
    try:
        creating = asyncio.create_task(create_tables(untimed))
        try:
            assert await _not_done(creating)
        finally:
            paused.release.set()
        await asyncio.wait_for(task, 15)
        await asyncio.wait_for(creating, 15)
    finally:
        await untimed.dispose()


def _purgeable_module(tmp_path) -> str:
    """A second installed module owning zy_things, not running and used by no company."""
    pkg = tmp_path / "bk-modules" / "zy-things"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text(
        'PLUGIN_MANIFEST = {"name": "zy-things", "version": "1.0.0", "table_prefix": "zy_", '
        '"company_backup": {"zy_things": "exclude"}}\n')
    return "zy-things"


async def test_running_export_refuses_module_data_purge(real_engine, real_client, widgets_company, tmp_path,  # noqa: F811
                                                       monkeypatch):
    _, cid, tok = widgets_company
    name = _purgeable_module(tmp_path)
    await _bk_sql(real_engine, "CREATE TABLE zy_things (id uuid primary key)")
    try:
        paused = _Paused(monkeypatch)
        task = paused.start(cid, tmp_path / "a.celerp-company")
        await asyncio.wait_for(paused.reached.wait(), 15)
        try:
            r = await real_client.post(f"/companies/me/modules/{name}/purge-data", headers=auth(tok))
            assert r.status_code == 409, r.text
            assert "Nothing was deleted" in r.json()["detail"]
        finally:
            paused.release.set()
        await asyncio.wait_for(task, 15)
        async with real_engine.connect() as conn:
            assert (await conn.execute(text("SELECT to_regclass('zy_things')"))).scalar_one() is not None
        r = await real_client.post(f"/companies/me/modules/{name}/purge-data", headers=auth(tok))
        assert r.status_code == 200 and r.json()["dropped"] == ["zy_things"], r.text
    finally:
        await _bk_drop(real_engine, "zy_things")


# ── 3. backups share the key ──────────────────────────────────────────────────

async def test_two_exports_run_together(real_engine, widgets_company, tmp_path, monkeypatch):  # noqa: F811
    """Both exports hold the key shared at once; each then finishes in turn."""
    cb = _bk_cb()
    batches = cb._batches
    reading, gates = [], [asyncio.Event(), asyncio.Event()]

    async def held(session, table, *args):
        if table.name == "zz_widgets":
            gate = gates[len(reading)]
            reading.append(table.name)
            await gate.wait()
        async for batch in batches(session, table, *args):
            yield batch

    monkeypatch.setattr(cb, "_batches", held)
    _, cid, _ = widgets_company
    first = asyncio.create_task(cb.export_company_snapshot(cid, tmp_path / "a.celerp-company"))
    try:
        while len(reading) < 1:
            await asyncio.sleep(0.05)
        second = asyncio.create_task(cb.export_company_snapshot(cid, tmp_path / "b.celerp-company"))
        while len(reading) < 2:
            await asyncio.sleep(0.05)
        assert await _holders(real_engine) == ["ShareLock", "ShareLock"]
        gates[0].set()
        await asyncio.wait_for(first, 15)
    finally:
        for gate in gates:
            gate.set()
    await asyncio.wait_for(second, 15)
    for name in ("a", "b"):
        data = (tmp_path / f"{name}.celerp-company").read_bytes()
        assert b"widget-marker" in members(data)["tables/zz_widgets.jsonl"]


# ── 4. an error or a rollback lets the key go ─────────────────────────────────

async def test_failed_export_releases_the_key(real_engine, widgets_company, tmp_path, monkeypatch):  # noqa: F811
    _, cid, _ = widgets_company
    paused = _Paused(monkeypatch, fail=True)
    out = tmp_path / "a.celerp-company"
    task = paused.start(cid, out)
    await asyncio.wait_for(paused.reached.wait(), 15)
    assert await _holders(real_engine) == ["ShareLock"]
    paused.release.set()
    with pytest.raises(RuntimeError):
        await asyncio.wait_for(task, 15)
    assert await _holders(real_engine) == [] and not out.exists()


async def test_refused_restore_releases_the_key(real_engine, real_client, widgets_company, monkeypatch):  # noqa: F811
    cb = _bk_cb()
    _, _, tok = widgets_company
    data = await download(real_client, tok)
    preview = await read(real_client, tok, data, mode="new_company")
    assert preview.status_code == 200, preview.text
    seen: list[list[str]] = []

    async def refusing(session, *args, **kw):
        seen.append(await _holders(real_engine))
        raise cb.BackupError(409, "Refused for the test.")

    monkeypatch.setattr(cb, "_plan", refusing)
    r = await real_client.post("/company-backups/restore", json=confirm(preview, "new_company"), headers=auth(tok))
    assert r.status_code == 409, r.text
    assert seen == [["ShareLock"]]
    assert await _holders(real_engine) == []


# ── 5. module-state first, schema second ─────────────────────────────────────

def _record_order(monkeypatch, target, holder_name: str, enum_target, enum_name: str) -> list[set[str]]:
    seen: list[set[str]] = []
    hold, enum = getattr(target, holder_name), getattr(enum_target, enum_name)

    async def recording_hold(session):
        seen.append(await _own_keys(session))
        await hold(session)
        seen.append(await _own_keys(session))

    async def recording_enum(session, *args, **kw):
        seen.append(await _own_keys(session))
        return await enum(session, *args, **kw)

    monkeypatch.setattr(target, holder_name, recording_hold)
    monkeypatch.setattr(enum_target, enum_name, recording_enum)
    return seen


async def test_purge_takes_module_state_then_schema(real_engine, real_client, widgets_company, tmp_path,  # noqa: F811
                                                    monkeypatch):
    from celerp.modules import registry
    from celerp.routers import companies

    _, _, tok = widgets_company
    name = _purgeable_module(tmp_path)
    await _bk_sql(real_engine, "CREATE TABLE zy_things (id uuid primary key)")
    try:
        seen = _record_order(monkeypatch, registry, "hold_module_state", companies, "_module_tables_with_row_counts")
        r = await real_client.post(f"/companies/me/modules/{name}/purge-data", headers=auth(tok))
        assert r.status_code == 200, r.text
        assert seen == [set(), {"module-state"}, {"module-state", "schema"}]
    finally:
        await _bk_drop(real_engine, "zy_things")


async def test_restore_takes_module_state_then_schema(real_engine, real_client, widgets_company, monkeypatch):  # noqa: F811
    cb = _bk_cb()
    _, _, tok = widgets_company
    data = await download(real_client, tok)
    preview = await read(real_client, tok, data, mode="new_company")
    assert preview.status_code == 200, preview.text
    seen = _record_order(monkeypatch, cb, "hold_module_state", cb, "_classify")
    r = await real_client.post("/company-backups/restore", json=confirm(preview, "new_company"), headers=auth(tok))
    assert r.status_code == 201, r.text
    assert seen == [set(), {"module-state"}, {"module-state", "schema"}]


async def test_export_takes_no_module_state_lock(real_engine, widgets_company, tmp_path, monkeypatch):  # noqa: F811
    cb = _bk_cb()
    _, cid, _ = widgets_company
    seen: list[set[str]] = []
    classify = cb._classify

    async def recording(session, *args, **kw):
        seen.append(await _own_keys(session))
        return await classify(session, *args, **kw)

    monkeypatch.setattr(cb, "_classify", recording)
    await cb.export_company_snapshot(cid, tmp_path / "a.celerp-company")
    assert seen == [{"schema"}]


# ── 6. a module migration changing a carried type waits for the backup ───────

_RENAME_MOOD = """
from alembic import op


def upgrade():
    op.execute("ALTER TYPE zz_mood RENAME VALUE 'calm' TO 'serene'")
"""


async def test_module_type_migration_waits_for_running_export(real_engine, tmp_path, monkeypatch):  # noqa: F811
    from celerp.modules import loader
    from celerp.modules.migrations_runner import run_migration_phase

    _bk_local(monkeypatch, tmp_path)
    _bk_fake_module(tmp_path, monkeypatch)
    _, cid, _ = await _bk_setup(real_engine, settings={"enabled_modules": [_BK_MODULE]})
    base = tmp_path / "migrating"
    pkg = base / "zx-moods"
    (pkg / "inner" / "migrations").mkdir(parents=True)
    (pkg / "__init__.py").write_text(
        'PLUGIN_MANIFEST = {"name": "zx-moods", "version": "1.0.0", '
        '"migrations": "inner.migrations", "table_prefix": "zx_"}\n')
    (pkg / "inner" / "__init__.py").write_text("")
    (pkg / "inner" / "migrations" / "m_001.py").write_text(_RENAME_MOOD)
    await _bk_sql(real_engine, "CREATE TYPE zz_mood AS ENUM ('calm', 'busy')")
    await _bk_sql(real_engine, "CREATE TABLE zz_widgets (id uuid primary key, "
                               "company_id uuid not null references companies(id) on delete cascade, mood zz_mood)")
    try:
        await _bk_sql(real_engine, "INSERT INTO zz_widgets (id, company_id, mood) VALUES (:i, :c, 'calm')",
                      i=uuid.uuid4(), c=cid)
        paused = _Paused(monkeypatch)
        out = tmp_path / "a.celerp-company"
        task = paused.start(cid, out)
        await asyncio.wait_for(paused.reached.wait(), 15)
        migrating = asyncio.create_task(run_migration_phase(real_engine, loader.admit_modules(str(base), {"zx-moods"})))
        try:
            assert await _not_done(migrating)
            async with real_engine.connect() as conn:
                labels = (await conn.execute(text(
                    "SELECT enumlabel FROM pg_enum WHERE enumtypid = 'zz_mood'::regtype ORDER BY enumsortorder"))).scalars().all()
            assert labels == ["calm", "busy"]
        finally:
            paused.release.set()
        await asyncio.wait_for(task, 15)
        admission = await asyncio.wait_for(migrating, 30)
        assert "zx-moods" in {m.name for m in admission.admitted}
        assert b'"calm"' in members(out.read_bytes())["tables/zz_widgets.jsonl"]
        async with real_engine.connect() as conn:
            assert (await conn.execute(text(
                "SELECT mood::text FROM zz_widgets WHERE company_id = :c"), {"c": cid})).scalar_one() == "serene"
    finally:
        await _bk_drop(real_engine, "zz_widgets")
        async with real_engine.begin() as conn:
            await conn.execute(text("DROP TYPE IF EXISTS zz_mood"))
            for t in (await conn.execute(text(
                    "SELECT tablename FROM pg_tables WHERE schemaname = current_schema() AND tablename LIKE 'zx\\_%'"))).scalars():
                await conn.execute(text(f'DROP TABLE IF EXISTS "{t}"'))


# ── 7. module installs during an export do not change what it records ────────

@pytest.mark.parametrize("change", ["deleted", "reinstalled"])
async def test_module_changed_mid_export_keeps_recorded_version(real_engine, widgets_company, tmp_path, monkeypatch,  # noqa: F811
                                                                change):
    from celerp.modules.importer import remove_module_dir
    from celerp.modules import loader

    from test_company_settings_race_pg import _hold_first_call

    _, cid, _ = widgets_company
    monkeypatch.setattr(loader, "_loaded", [m for m in loader._loaded if m["name"] != _BK_MODULE])
    # Held once the export has classified the tables, before it writes the manifest.
    reached, release = _hold_first_call(monkeypatch, _bk_cb(), "_unchanged")
    out = tmp_path / "a.celerp-company"
    task = asyncio.create_task(_bk_cb().export_company_snapshot(cid, out))
    await asyncio.wait_for(reached.wait(), 15)
    try:
        await asyncio.to_thread(remove_module_dir, _BK_MODULE)
        if change == "reinstalled":
            _bk_fake_module(tmp_path, monkeypatch, version="3.0.0", backup={"zz_widgets": "exclude"})
            monkeypatch.setattr(loader, "_loaded", [m for m in loader._loaded if m["name"] != _BK_MODULE])
    finally:
        release.set()
    await asyncio.wait_for(task, 15)
    m = manifest(out.read_bytes())
    assert m["modules"]["versions"][_BK_MODULE] == "2.0.0"
    assert _BK_MODULE in m["modules"]["enabled"]
    assert m["tables"]["zz_widgets"]["rows"] == 1


@pytest.mark.parametrize("version", ["", "   "])
async def test_owner_without_valid_version_refuses_export(real_engine, tmp_path, monkeypatch, version):  # noqa: F811
    cb = _bk_cb()
    _bk_local(monkeypatch, tmp_path)
    _bk_fake_module(tmp_path, monkeypatch, version=version)
    _, cid, _ = await _bk_setup(real_engine, settings={"enabled_modules": [_BK_MODULE]})
    await _with_widgets(real_engine, cid)
    out = tmp_path / "a.celerp-company"
    try:
        with pytest.raises(cb.BackupError) as err:
            await cb.export_company_snapshot(cid, out)
        assert err.value.status_code == 409
        assert _BK_MODULE in err.value.detail and "version" in err.value.detail
        assert not out.exists()
    finally:
        await _bk_drop(real_engine, "zz_widgets")


async def test_owner_installed_twice_with_different_prefixes_refuses_export(real_engine, tmp_path, monkeypatch):  # noqa: F811
    """The copy of the module whose manifest is read declares another table prefix than
    the copy the table prefixes were taken from, so who owns zz_widgets is not one answer."""
    cb = _bk_cb()
    _bk_local(monkeypatch, tmp_path)
    _bk_fake_module(tmp_path, monkeypatch)
    earlier = tmp_path / "earlier-modules" / _BK_MODULE
    earlier.mkdir(parents=True)
    (earlier / "__init__.py").write_text(
        f'PLUGIN_MANIFEST = {{"name": "{_BK_MODULE}", "version": "2.0.0", "table_prefix": "zw_"}}\n')
    monkeypatch.setenv("MODULE_DIR", f"{earlier.parent},{os.environ['MODULE_DIR']}")
    _, cid, _ = await _bk_setup(real_engine, settings={"enabled_modules": [_BK_MODULE]})
    await _with_widgets(real_engine, cid)
    out = tmp_path / "a.celerp-company"
    try:
        with pytest.raises(cb.BackupError) as err:
            await cb.export_company_snapshot(cid, out)
        assert err.value.status_code == 409 and "more than once" in err.value.detail
        assert not out.exists()
    finally:
        await _bk_drop(real_engine, "zz_widgets")


# ── 8. System Recovery replaces the database holding the key alone ───────────

def _url(engine) -> str:
    return engine.url.render_as_string(hide_password=False)


class _Recovery:
    """System Recovery's database restore (``restore_database_file``), run off the event
    loop with pg_restore and psql stood in for: each run records the key's holders, then
    waits for ``release`` before answering, or failing with *failure*."""

    def __init__(self, url: str, *, failure: str | None = None):
        from celerp.services.backup import restore_database_file

        self.started, self.release = threading.Event(), threading.Event()
        self.seen: list[list[str]] = []

        def runner(command, **kwargs):
            self.seen.append(_holders_now(url))
            self.started.set()
            self.release.wait(30)
            if failure == "timeout":
                raise subprocess.TimeoutExpired(command, 600)
            return subprocess.CompletedProcess(command, 1 if failure else 0, b"",
                                               b"ERROR: restore failed" if failure else b"")

        self.task = asyncio.create_task(asyncio.to_thread(
            restore_database_file, Path("database.dump"), url, runner=runner))

    async def reached(self, seconds: float = 15) -> bool:
        return await asyncio.to_thread(self.started.wait, seconds)


@pytest.fixture
async def scratch_url(real_engine):  # noqa: F811
    """A database of its own, so a restore may empty its whole schema."""
    name = f"zz_recovery_{uuid.uuid4().hex[:10]}"
    async with real_engine.connect() as conn:
        conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
        await conn.execute(text(f'CREATE DATABASE "{name}"'))
    yield _url(real_engine).rpartition("/")[0] + "/" + name
    async with real_engine.connect() as conn:
        conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
        await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))


async def test_system_recovery_holds_the_schema_key_alone_while_it_replaces_the_database(scratch_url):
    recovery = _Recovery(scratch_url)
    try:
        assert await recovery.reached()
    finally:
        recovery.release.set()
    await asyncio.wait_for(recovery.task, 15)
    assert recovery.seen == [["ExclusiveLock"]] * 3
    assert await asyncio.to_thread(_holders_now, scratch_url) == []


async def test_company_backup_during_system_recovery_is_refused_and_writes_nothing(
        real_engine, real_client, widgets_company, tmp_path):  # noqa: F811
    _, cid, tok = widgets_company
    files_before = sorted(p for p in tmp_path.rglob("*") if p.is_file())  # the download's private folder may exist
    out = tmp_path / "during.celerp-company"
    recovery = _Recovery(_url(real_engine))
    try:
        assert await recovery.reached()
        r = await real_client.get("/company-backups/download", headers=auth(tok))
        try:
            await _bk_cb().export_company_snapshot(cid, out)
            direct = None
        except _bk_cb().BackupError as exc:
            direct = exc
    finally:
        recovery.release.set()
        await asyncio.wait_for(recovery.task, 15)
    assert r.status_code == 409, r.status_code
    assert r.json()["detail"].startswith("Celerp is updating its database. Nothing was backed up.")
    assert direct is not None and direct.status_code == 409
    assert direct.detail.startswith("Celerp is updating its database. Nothing was backed up.")
    assert sorted(p for p in tmp_path.rglob("*") if p.is_file()) == files_before


async def test_system_recovery_waits_for_a_running_company_backup(
        real_engine, widgets_company, tmp_path, monkeypatch):  # noqa: F811
    _, cid, _ = widgets_company
    paused = _Paused(monkeypatch)
    out = tmp_path / "a.celerp-company"
    export = paused.start(cid, out)
    await asyncio.wait_for(paused.reached.wait(), 15)
    recovery = _Recovery(_url(real_engine))
    try:
        replaced_during_backup = await recovery.reached(1.5)
    finally:
        paused.release.set()
    try:
        await asyncio.wait_for(export, 15)
        assert await recovery.reached()
    finally:
        recovery.release.set()
    await asyncio.wait_for(recovery.task, 15)
    assert not replaced_during_backup
    assert recovery.seen == [["ExclusiveLock"]] * 3
    assert b"widget-marker" in members(out.read_bytes())["tables/zz_widgets.jsonl"]
    assert await _holders(real_engine) == []


@pytest.mark.parametrize("failure", ["error", "timeout"], ids=["pg_restore fails", "pg_restore times out"])
async def test_failed_system_recovery_restore_releases_the_schema_key(scratch_url, failure):
    recovery = _Recovery(scratch_url, failure=failure)
    recovery.release.set()
    with pytest.raises(RuntimeError, match="pg_restore"):
        await asyncio.wait_for(recovery.task, 15)
    assert recovery.seen == [["ExclusiveLock"]]
    assert await asyncio.to_thread(_holders_now, scratch_url) == []
