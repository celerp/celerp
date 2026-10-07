# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Which modules companies use and which modules are installed change one at a time.

A module is deleted or its data purged only while no company uses it. Each test
pauses one side partway (the destroyer right after its in-use check, or the writer
right before it commits), starts the other side, waits until that side is blocked
on a lock or has finished, then lets the first side finish. Whichever side goes
first, the result is one a serial run could produce: a company never uses a module
that is gone, and a module's tables are never dropped while a company uses it.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from celerp.models.company import Company
from migration_support import real_client, real_engine  # noqa: F401

pytestmark = pytest.mark.asyncio

_NAME = "acme-widgets"
_PREFIX = "acme_"
_TABLES = ("acme_widget", "acme_log")


def _factory(engine):
    return async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)


def _write_module(root: Path, name: str = _NAME) -> Path:
    pkg = root / name
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text(
        f'PLUGIN_MANIFEST = {{"name": "{name}", "version": "1.0.0", '
        f'"display_name": "Acme Widgets", "table_prefix": "{_PREFIX}"}}\n')
    return pkg


@pytest.fixture
def module_dir(tmp_path, monkeypatch) -> Path:
    """An installed, not running module owning the acme_ tables."""
    root = tmp_path / "modules"
    root.mkdir()
    _write_module(root)
    monkeypatch.setenv("MODULE_DIR", str(root))
    return root


async def _seed(engine) -> uuid.UUID:
    """One company that uses no module, and the module's tables holding data."""
    company_id = uuid.uuid4()
    async with _factory(engine)() as s:
        s.add(Company(id=company_id, name="RaceCo", slug=f"race-{company_id.hex[:8]}",
                      settings={"enabled_modules": []}))
        for table in _TABLES:
            await s.execute(text(f'CREATE TABLE "{table}" (id serial PRIMARY KEY, v text)'))
            await s.execute(text(f'INSERT INTO "{table}" (v) VALUES (\'kept\')'))
        await s.commit()
    return company_id


async def _uses(engine, name: str = _NAME) -> bool:
    async with engine.connect() as conn:
        rows = (await conn.execute(text("SELECT settings::text FROM companies"))).scalars().all()
    return any(name in (json.loads(r or "{}").get("enabled_modules") or []) for r in rows)


async def _tables(engine) -> set[str]:
    async with engine.connect() as conn:
        rows = (await conn.execute(text(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND tablename LIKE 'acme%'"
        ))).scalars().all()
    return set(rows)


def _installed(name: str = _NAME) -> bool:
    from celerp.modules.loader import module_search_path, resolve_module_path
    return resolve_module_path(name, module_search_path()) is not None


def _pause_after(monkeypatch, owner, name: str) -> tuple[asyncio.Event, asyncio.Event]:
    """Let the first call of ``owner.<name>`` finish, then hold its caller until released."""
    paused, release = asyncio.Event(), asyncio.Event()
    real = getattr(owner, name)

    async def _held(*args, **kwargs):
        result = await real(*args, **kwargs)
        if not paused.is_set():
            paused.set()
            await release.wait()
        return result

    monkeypatch.setattr(owner, name, _held)
    return paused, release


def _pause_before(monkeypatch, owner, name: str) -> tuple[asyncio.Event, asyncio.Event]:
    """Hold the first call of ``owner.<name>`` until released."""
    paused, release = asyncio.Event(), asyncio.Event()
    real = getattr(owner, name)

    async def _held(*args, **kwargs):
        if not paused.is_set():
            paused.set()
            await release.wait()
        return await real(*args, **kwargs)

    monkeypatch.setattr(owner, name, _held)
    return paused, release


async def _until_blocked_or_done(engine, task: asyncio.Task) -> None:
    """Wait until the other side is blocked on a lock, or has finished.

    pg_stat_activity is a per-transaction snapshot, so every poll opens its own."""
    for _ in range(400):
        if task.done():
            return
        async with engine.connect() as conn:
            waiting = (await conn.execute(text(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE datname = current_database() AND wait_event_type = 'Lock'"
            ))).scalar_one()
        if waiting:
            return
        await asyncio.sleep(0.05)
    raise AssertionError("the other side neither blocked nor finished")


async def _race(engine, pause, first, other) -> tuple[object, object]:
    """Run ``first`` until ``pause`` holds it and run ``other`` meanwhile; both outcomes."""
    paused, release = pause
    task = asyncio.create_task(first())
    await asyncio.wait_for(paused.wait(), timeout=10)
    rival = asyncio.create_task(other())
    await _until_blocked_or_done(engine, rival)
    release.set()
    return tuple(await asyncio.wait_for(
        asyncio.gather(task, rival, return_exceptions=True), timeout=30))


def _refused(outcome, status: int) -> bool:
    return isinstance(outcome, HTTPException) and outcome.status_code == status


# ── the sides ───────────────────────────────────────────────────────────────

def _api_enable(engine, company_id):
    from celerp.routers.companies import enable_module

    async def run():
        async with _factory(engine)() as s:
            return await enable_module(module_name=_NAME, company_id=company_id, session=s)
    return run


def _cli_enable(engine):
    from celerp.cli import _enable_for_every_company

    url = engine.url.render_as_string(hide_password=False)
    return lambda: _enable_for_every_company(url, [_NAME])


def _delete(engine):
    from celerp.routers.companies import delete_module

    async def run():
        async with _factory(engine)() as s:
            return await delete_module(module_name=_NAME, session=s)
    return run


def _purge(engine, used_at_drop: list[bool], monkeypatch):
    """Purge, recording whether a company used the module when its tables were dropped."""
    from celerp.routers import companies

    real_drop = companies._drop_module_tables

    def _drop(sync_session, names):
        rows = sync_session.connection().execute(text("SELECT settings::text FROM companies")).scalars().all()
        used_at_drop.append(any(_NAME in (json.loads(r or "{}").get("enabled_modules") or []) for r in rows))
        return real_drop(sync_session, names)

    monkeypatch.setattr(companies, "_drop_module_tables", _drop)

    async def run():
        async with _factory(engine)() as s:
            return await companies.purge_module_data(module_name=_NAME, session=s)
    return run


def _hold_destroyer(monkeypatch):
    from celerp.routers import companies
    return _pause_after(monkeypatch, companies, "_refuse_while_in_use")


def _hold_writer_commit(monkeypatch):
    """Hold the first commit any session makes: the writer's, after it checked and changed."""
    return _pause_before(monkeypatch, AsyncSession, "commit")


# ── enable vs delete ────────────────────────────────────────────────────────

async def test_enable_first_then_delete_refuses_delete(committed_engine, module_dir, monkeypatch):
    company_id = await _seed(committed_engine)
    enabled, deleted = await _race(committed_engine, _hold_writer_commit(monkeypatch),
                                   _api_enable(committed_engine, company_id), _delete(committed_engine))
    assert not (await _uses(committed_engine) and not _installed()), "a company uses a deleted module"
    assert not isinstance(enabled, BaseException), enabled
    assert _refused(deleted, 409), deleted
    assert await _uses(committed_engine) and _installed()


async def test_delete_first_then_enable_refuses_enable(committed_engine, module_dir, monkeypatch):
    company_id = await _seed(committed_engine)
    deleted, enabled = await _race(committed_engine, _hold_destroyer(monkeypatch),
                                   _delete(committed_engine), _api_enable(committed_engine, company_id))
    assert not (await _uses(committed_engine) and not _installed()), "a company uses a deleted module"
    assert deleted == {"ok": True, "name": _NAME}, deleted
    assert _refused(enabled, 404), enabled
    assert not await _uses(committed_engine) and not _installed()


# ── enable vs purge ─────────────────────────────────────────────────────────

async def test_enable_first_then_purge_refuses_purge(committed_engine, module_dir, monkeypatch):
    company_id = await _seed(committed_engine)
    used_at_drop: list[bool] = []
    enabled, purged = await _race(committed_engine, _hold_writer_commit(monkeypatch),
                                  _api_enable(committed_engine, company_id),
                                  _purge(committed_engine, used_at_drop, monkeypatch))
    assert True not in used_at_drop, "the module's tables were dropped while a company used it"
    assert not isinstance(enabled, BaseException), enabled
    assert _refused(purged, 409), purged
    assert await _uses(committed_engine) and await _tables(committed_engine) == set(_TABLES)


async def test_purge_first_then_enable_waits_for_the_drop(committed_engine, module_dir, monkeypatch):
    company_id = await _seed(committed_engine)
    used_at_drop: list[bool] = []
    purged, enabled = await _race(committed_engine, _hold_destroyer(monkeypatch),
                                  _purge(committed_engine, used_at_drop, monkeypatch),
                                  _api_enable(committed_engine, company_id))
    assert used_at_drop == [False], "the module's tables were dropped while a company used it"
    assert purged == {"ok": True, "name": _NAME, "dropped": sorted(_TABLES)}, purged
    # Turning the module on after its data was purged is a later, deliberate choice.
    assert not isinstance(enabled, BaseException), enabled
    assert await _uses(committed_engine) and await _tables(committed_engine) == set()


# ── the all-company enable (celerp module install) vs delete and purge ──────

async def test_cli_enable_first_then_delete_refuses_delete(committed_engine, module_dir, monkeypatch):
    await _seed(committed_engine)
    enabled, deleted = await _race(committed_engine, _hold_writer_commit(monkeypatch),
                                   _cli_enable(committed_engine), _delete(committed_engine))
    assert not (await _uses(committed_engine) and not _installed()), "a company uses a deleted module"
    assert enabled == 1, enabled
    assert _refused(deleted, 409), deleted


async def test_delete_first_then_cli_enable_refuses_enable(committed_engine, module_dir, monkeypatch):
    await _seed(committed_engine)
    deleted, enabled = await _race(committed_engine, _hold_destroyer(monkeypatch),
                                   _delete(committed_engine), _cli_enable(committed_engine))
    assert not (await _uses(committed_engine) and not _installed()), "a company uses a deleted module"
    assert deleted == {"ok": True, "name": _NAME}, deleted
    assert isinstance(enabled, ValueError) and "not installed" in str(enabled), enabled
    assert not await _uses(committed_engine)


async def test_cli_enable_first_then_purge_refuses_purge(committed_engine, module_dir, monkeypatch):
    await _seed(committed_engine)
    used_at_drop: list[bool] = []
    enabled, purged = await _race(committed_engine, _hold_writer_commit(monkeypatch),
                                  _cli_enable(committed_engine),
                                  _purge(committed_engine, used_at_drop, monkeypatch))
    assert True not in used_at_drop, "the module's tables were dropped while a company used it"
    assert enabled == 1, enabled
    assert _refused(purged, 409), purged
    assert await _tables(committed_engine) == set(_TABLES)


async def test_purge_first_then_cli_enable_waits_for_the_drop(committed_engine, module_dir, monkeypatch):
    await _seed(committed_engine)
    used_at_drop: list[bool] = []
    purged, enabled = await _race(committed_engine, _hold_destroyer(monkeypatch),
                                  _purge(committed_engine, used_at_drop, monkeypatch),
                                  _cli_enable(committed_engine))
    assert used_at_drop == [False], "the module's tables were dropped while a company used it"
    assert purged == {"ok": True, "name": _NAME, "dropped": sorted(_TABLES)}, purged
    assert enabled == 1, enabled


async def test_a_change_queued_behind_a_long_holder_waits_instead_of_failing(
        committed_engine, module_dir):
    """A request connection gives up on a lock after a few seconds; a module change
    queued behind a long one (a company restore holds the modules for its whole run)
    must wait for it, not fail."""
    from sqlalchemy.ext.asyncio import create_async_engine
    from celerp.modules import registry
    from celerp.routers.companies import enable_module

    company_id = await _seed(committed_engine)
    impatient = create_async_engine(committed_engine.url, connect_args={"server_settings": {"lock_timeout": "200"}})
    try:
        async with _factory(committed_engine)() as holder, _factory(impatient)() as s:
            await holder.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": registry._MODULE_STATE_LOCK_KEY})
            enable = asyncio.create_task(enable_module(module_name=_NAME, company_id=company_id, session=s))
            await _until_blocked_or_done(committed_engine, enable)
            await asyncio.sleep(0.5)
            await holder.commit()
            outcome = (await asyncio.wait_for(asyncio.gather(enable, return_exceptions=True), timeout=30))[0]
    finally:
        await impatient.dispose()
    assert not isinstance(outcome, BaseException), outcome
    assert await _uses(committed_engine)


# ── a company backup restore that turns the module on vs delete and purge ───
#
# A restore needs every module the backup uses to be running here, and delete and
# purge refuse a running module, so a destroyer can never get past its check while
# such a restore is possible. What is left to prove is the other order: a restore
# that is about to commit a company using the module makes the destroyer wait for it
# and then refuse.

@pytest_asyncio.fixture
async def restorable(tmp_path, monkeypatch, module_dir, real_engine):
    """The module installed and running, attachments stored under tmp_path; its tables,
    which live outside the worker database's truncated set, are dropped afterwards."""
    from celerp.config import settings
    from celerp.modules import loader
    from celerp.services import attachments

    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    monkeypatch.setattr(attachments, "_backend", attachments.LocalBackend())
    monkeypatch.setattr(loader, "_loaded", [*loader._loaded, {"name": _NAME, "version": "1.0.0"}])
    yield
    tables = await _tables(real_engine)
    async with real_engine.begin() as conn:
        for table in tables:
            await conn.execute(text(f'DROP TABLE "{table}"'))


async def _restore_race(real_engine, real_client, monkeypatch, destroyer):
    from company_backup_support import company, download, owner, restore, token
    from celerp.services import company_backup

    user = await owner(real_engine)
    cid = await company(real_engine, user, "Alpha Trading", "alpha-marker",
                        settings={"enabled_modules": [_NAME]})
    tok = await token(real_engine, user, cid)
    data = await download(real_client, tok)
    async with real_engine.begin() as conn:
        await conn.execute(text('UPDATE companies SET settings = CAST(:s AS json) WHERE id = :c'),
                           {"s": json.dumps({"enabled_modules": []}), "c": cid})
        for table in _TABLES:
            await conn.execute(text(f'CREATE TABLE "{table}" (id serial PRIMARY KEY, v text)'))
            await conn.execute(text(f'INSERT INTO "{table}" (v) VALUES (\'kept\')'))
    return await _race(
        real_engine, _pause_before(monkeypatch, company_backup, "provision_restored_company"),
        lambda: restore(real_client, tok, data, mode="new_company"), destroyer)


async def test_restore_first_then_delete_refuses_delete(real_engine, real_client, restorable, monkeypatch):
    restored, deleted = await _restore_race(real_engine, real_client, monkeypatch, _delete(real_engine))
    assert restored.status_code == 201, restored.text
    assert _refused(deleted, 409), deleted
    assert await _uses(real_engine) and _installed()


async def test_restore_first_then_purge_refuses_purge(real_engine, real_client, restorable, monkeypatch):
    used_at_drop: list[bool] = []
    restored, purged = await _restore_race(real_engine, real_client, monkeypatch,
                                           _purge(real_engine, used_at_drop, monkeypatch))
    assert restored.status_code == 201, restored.text
    assert True not in used_at_drop, "the module's tables were dropped while a company used it"
    assert _refused(purged, 409), purged
    assert await _uses(real_engine) and await _tables(real_engine) == set(_TABLES)
