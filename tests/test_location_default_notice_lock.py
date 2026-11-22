# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The default-location notice locks the runtime-state row with a statement that names it
(review r4 probe P2), and a second concurrent start does not write back a stale copy of a
sibling key on that row (M2)."""
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


async def _sibling_key_race(async_url, cid, entry):
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool
    from celerp.db import create_tables
    from celerp.json_text import dump_json
    from celerp.models.auth import SystemRuntimeState
    from celerp.services.location_default_notice import RECORD, run_location_default_notice
    from celerp.services.runtime_state import _SINGLETON_ID

    engine = create_async_engine(async_url, poolclass=NullPool, json_serializer=dump_json)
    try:
        await create_tables(engine)
        mk = async_sessionmaker(engine, expire_on_commit=False)
        async with mk() as seed:
            row = await seed.get(SystemRuntimeState, _SINGLETON_ID)
            if row is None:
                seed.add(SystemRuntimeState(id=_SINGLETON_ID, value={"draining": False, RECORD: {str(cid): entry}}))
            else:
                row.value = {**row.value, "draining": False, RECORD: {str(cid): entry}}
            await seed.commit()

        async with mk() as t, mk() as sibling:
            real_get = t.get

            async def _get_then_sibling_flips_draining(*args, **kwargs):
                """Right after the notice's own first (unlocked) read, a second start's own
                process-wide flag flip lands on the same row and commits, before the notice
                takes the row's lock."""
                result = await real_get(*args, **kwargs)
                row = await sibling.get(SystemRuntimeState, _SINGLETON_ID)
                row.value = {**row.value, "draining": True}
                await sibling.commit()
                return result

            t.get = _get_then_sibling_flips_draining
            await run_location_default_notice(t)
            await t.commit()
            final = (await t.execute(
                select(SystemRuntimeState).where(SystemRuntimeState.id == _SINGLETON_ID))).scalar_one()
            return final.value
    finally:
        await engine.dispose()


def test_a_concurrent_second_start_does_not_write_back_a_stale_sibling_key():
    """M2: the lock statement's populate_existing refreshes the session's cached row before the
    notice rewrites it, so a sibling key (``draining``) another start committed while this one
    was between its first read and taking the lock survives, instead of being overwritten with
    the value the first read saw."""
    with throwaway_db("p2m2test") as (async_url, sync_url):
        upgrade_to(sync_url, "y8d9e0f1a2b3")
        engine = create_engine(sync_url)
        try:
            with engine.begin() as conn:
                cid = uuid.uuid4()
                conn.execute(text("INSERT INTO companies (id, name, slug, settings, created_at) "
                                  "VALUES (:i, 'race2', :s, '{}', now())"), {"i": cid, "s": f"p2m2-{cid.hex[:6]}"})
                main = uuid.uuid4()
                conn.execute(text("INSERT INTO locations (id, company_id, name, type, is_default, created_at) "
                                  "VALUES (:i, :c, 'Main', 'warehouse', true, now())"), {"i": main, "c": cid})
            upgrade_to(sync_url, "head")
            entry = {"kept": str(main), "cleared": []}
            value = asyncio.run(_sibling_key_race(async_url, cid, entry))
        finally:
            engine.dispose()
        assert value["draining"] is True, value
