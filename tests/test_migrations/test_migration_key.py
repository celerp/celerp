# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Alembic changes a database only under the migration key.

Two runs at once would both apply the revision after the stamp they read. Every run holds
the exclusive migration key, so alembic refuses to run without it
(``celerp.db.migration_key``)."""

from __future__ import annotations

import asyncio
import os
import threading

import pytest
import sqlalchemy as sa
from alembic import command

from celerp.alembic_config import build_alembic_config
from celerp.db import _MIGRATION_LOCK_KEY, holds_migration_key, migration_key, migration_key_async

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL", "").startswith("postgresql"), reason="PostgreSQL only")


def _stamp(sync_url: str, async_url: str) -> None:
    os.environ["DATABASE_URL"] = async_url
    command.stamp(build_alembic_config(), "head")


def _stamped(sync_url: str) -> bool:
    engine = sa.create_engine(sync_url)
    try:
        with engine.connect() as conn:
            return sa.inspect(conn).has_table("alembic_version")
    finally:
        engine.dispose()


def _key_holders(sync_url: str) -> int:
    engine = sa.create_engine(sync_url)
    try:
        with engine.connect() as conn:
            return conn.execute(sa.text(
                "SELECT count(*) FROM pg_locks l JOIN pg_database d ON d.oid = l.database "
                "WHERE l.locktype = 'advisory' AND l.granted AND d.datname = current_database() "
                "AND l.objid::bigint = :k"), {"k": _MIGRATION_LOCK_KEY & 0xFFFFFFFF}).scalar_one()
    finally:
        engine.dispose()


def test_alembic_refuses_to_run_without_the_migration_key(fresh_db):
    async_url, sync_url = fresh_db
    with pytest.raises(RuntimeError, match="migration_key"):
        _stamp(sync_url, async_url)
    assert not _stamped(sync_url)


def test_alembic_runs_under_the_migration_key_and_the_key_is_released_after(fresh_db):
    async_url, sync_url = fresh_db
    engine = sa.create_engine(sync_url, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as lock_conn, migration_key(lock_conn):
            assert holds_migration_key(sync_url)
            assert _key_holders(sync_url) == 1
            _stamp(sync_url, async_url)
    finally:
        engine.dispose()
    assert _stamped(sync_url)
    assert not holds_migration_key(sync_url)
    assert _key_holders(sync_url) == 0


def test_the_key_held_on_one_database_does_not_open_another(fresh_db):
    async_url, sync_url = fresh_db
    other = sa.engine.make_url(sync_url).set(database="postgres").render_as_string(hide_password=False)
    engine = sa.create_engine(other, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as lock_conn, migration_key(lock_conn):
            assert not holds_migration_key(sync_url)
            with pytest.raises(RuntimeError, match="migration_key"):
                _stamp(sync_url, async_url)
    finally:
        engine.dispose()


def test_only_the_holding_flow_holds_the_key(fresh_db):
    """Another thread of the process runs outside the holder's serialization: it does not
    hold the key. Work the holder hands to a thread (asyncio.to_thread) still does."""
    async_url, sync_url = fresh_db
    seen: list[bool] = []

    async def main() -> None:
        from sqlalchemy.ext.asyncio import create_async_engine

        engine = create_async_engine(async_url)
        try:
            async with engine.connect() as conn:
                conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
                async with migration_key_async(conn):
                    seen.append(await asyncio.to_thread(holds_migration_key, sync_url))
                    thread = threading.Thread(target=lambda: seen.append(holds_migration_key(sync_url)))
                    thread.start()
                    thread.join()
            seen.append(holds_migration_key(sync_url))
        finally:
            await engine.dispose()

    asyncio.run(main())
    assert seen == [True, False, False]
