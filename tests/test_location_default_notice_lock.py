# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Every writer of the runtime-state row reads it locked: the default-location notice locks it with
a statement that names it, two writers in two sessions keep each other's change in either commit
order, and a writer does not write back a stale copy its session read earlier."""
from __future__ import annotations

import asyncio
import importlib.util
import os
import pathlib
import uuid

from sqlalchemy import create_engine, text

_SPEC = importlib.util.spec_from_file_location(
    "test_migrations.conftest", pathlib.Path(__file__).resolve().parent / "test_migrations" / "conftest.py")
_mc = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_mc)
throwaway_db, upgrade_to = _mc.throwaway_db, _mc.upgrade_to


async def _race(async_url):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool
    from celerp.db import create_tables
    from celerp.json_text import dump_json
    from celerp.services.location_default_notice import run_location_default_notice
    # as the app's own engines (celerp/db.py): json stored as UTF-8 text
    engine = create_async_engine(async_url, poolclass=NullPool, json_serializer=dump_json)
    try:
        await create_tables(engine)
        mk = async_sessionmaker(engine, expire_on_commit=False)
        async with mk() as a, mk() as b:
            await run_location_default_notice(a)          # holds the row lock, not committed
            second = asyncio.create_task(run_location_default_notice(b))
            await asyncio.sleep(1.5)
            waited = not second.done()
            locks = (await (await a.connection()).exec_driver_sql(
                "SELECT count(*) FROM pg_locks WHERE NOT granted")).scalar()
            await a.commit()
            await asyncio.wait_for(second, 20)
            await b.commit()
        return waited, locks
    finally:
        await engine.dispose()


def test_two_starts_tell_once_and_the_second_waits():
    """Review r4 probe P2, adopted as a repo test: the second session's select waits on the
    row the first holds (an ungranted lock in pg_locks), then reads it as committed."""
    with throwaway_db("p2test") as (async_url, sync_url):
        upgrade_to(sync_url, "y8d9e0f1a2b3")
        engine = create_engine(sync_url)
        try:
            with engine.begin() as conn:
                cid = uuid.uuid4()
                conn.execute(text("INSERT INTO companies (id, name, slug, settings, created_at) "
                                  "VALUES (:i, 'race', :s, '{}', now())"), {"i": cid, "s": f"p2-{cid.hex[:6]}"})
                for age, name in ((30, "Main"), (10, "Second")):
                    conn.execute(text("INSERT INTO locations (id, company_id, name, type, is_default, created_at) "
                                      "VALUES (:i, :c, :n, 'warehouse', true, now() - make_interval(days => :a))"),
                                 {"i": uuid.uuid4(), "c": cid, "n": name, "a": age})
            upgrade_to(sync_url, "head")
            waited, locks = asyncio.run(_race(async_url))
            with engine.connect() as conn:
                n = conn.execute(text("SELECT count(*) FROM notifications WHERE company_id = :c"), {"c": cid}).scalar()
                left = conn.execute(text("SELECT value FROM system_runtime_state WHERE id = 1")).scalar()
            assert waited and locks >= 1, (waited, locks)
            assert n == 1, n
            assert "location_default_cleared" not in (left or {}), left
        finally:
            engine.dispose()


async def _writers(async_url, first, second, seed=None, preload=False):
    """Run two writers of the runtime-state row in two sessions: *first* does its work and
    holds its transaction open while *second* starts and gets a second to run; then *first*
    commits, *second* finishes and commits, and the row's final value is returned.

    With *preload*, *first*'s session loads the row unlocked before a third session commits a
    change to ``draining``, the way an earlier read in the same request leaves a stale copy in
    the session for the writer's locked read to refresh."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool
    from celerp.db import create_tables
    from celerp.json_text import dump_json
    from celerp.models.auth import SystemRuntimeState

    engine = create_async_engine(async_url, poolclass=NullPool, json_serializer=dump_json)
    try:
        await create_tables(engine)
        mk = async_sessionmaker(engine, expire_on_commit=False)
        async with mk() as s:
            await s.execute(text("DELETE FROM system_runtime_state"))
            s.add(SystemRuntimeState(id=1, value=seed or {"draining": False}))
            await s.commit()
        async with mk() as a, mk() as b:
            held = await a.get(SystemRuntimeState, 1) if preload else None  # the map holds it weakly
            if preload:
                async with mk() as c:
                    await c.execute(text(
                        "UPDATE system_runtime_state SET value = CAST(:v AS json) WHERE id = 1"),
                        {"v": dump_json({**(seed or {}), "draining": True})})
                    await c.commit()
            real_commit = a.commit
            later = None

            async def _second_runs_before_first_commits():
                nonlocal later
                later = asyncio.create_task(_run_and_commit(second, b))
                await asyncio.sleep(1.0)
                await real_commit()

            a.commit = _second_runs_before_first_commits   # a writer that commits itself
            await first(a)
            if later is None:                             # a writer whose caller commits
                await a.commit()
            a.commit = real_commit
            await asyncio.wait_for(later, 20)
            del held
        async with mk() as s:
            return (await s.execute(text("SELECT value FROM system_runtime_state WHERE id = 1"))).scalar()
    finally:
        await engine.dispose()


async def _run_and_commit(writer, session):
    await writer(session)
    await session.commit()


_RECORD = "location_default_cleared"
_PENDING = {_RECORD: {str(uuid.uuid4()): {"kept": str(uuid.uuid4()), "cleared": [str(uuid.uuid4())]}}}


def _two_writers(first, second, **kw):
    with throwaway_db("rtstate") as (async_url, _sync_url):
        return asyncio.run(_writers(async_url, first, second, **kw))


async def _notice(session):
    from celerp.services.location_default_notice import run_location_default_notice
    await run_location_default_notice(session)


async def _nothing(session):
    pass


async def _drain(session):
    from celerp.services.runtime_state import set_draining
    await set_draining(session, True)


async def _star(session):
    from celerp.services.runtime_state import dismiss_star_prompt
    await dismiss_star_prompt(session)


def test_a_state_writer_holding_the_row_does_not_bring_back_a_processed_notice():
    """The drain switch reads the row, the notice processor clears the record it holds, and
    the drain switch commits: the record stays cleared and the drain flag is set."""
    value = _two_writers(_drain, _notice, seed={"draining": False, **_PENDING})
    assert value["draining"] is True and _RECORD not in value, value


def test_the_notice_processor_holding_the_row_does_not_lose_a_state_write():
    """The notice processor holds the row while the star dismissal runs: the dismissal waits,
    then writes over the cleared record, not over the copy it would have read before."""
    value = _two_writers(_notice, _star, seed={"draining": False, **_PENDING})
    assert value.get("star_prompt_dismissed") is True and _RECORD not in value, value


def test_a_writer_refreshes_a_copy_its_session_read_earlier():
    """A writer whose session read the row before another session changed it writes over the
    changed row (the locked read refreshes the session's copy), keeping that change."""
    value = _two_writers(_star, _nothing, seed={"draining": False}, preload=True)
    assert value["draining"] is True and value.get("star_prompt_dismissed") is True, value
