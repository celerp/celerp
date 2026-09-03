# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""The production async engine bounds lock and statement waits.

Without a statement/lock timeout, a query that blocks on a lock or runs away
holds one of the few pooled API connections for the full request lifetime; under
a request storm those waits are what exhaust the pool. The production engine must
be built with connect_args carrying a bounded lock_timeout and statement_timeout,
so asyncpg cancels a stuck query instead of pinning the connection indefinitely.

The test suite forces NullPool on celerp.db.engine (CELERP_TEST_NULLPOOL) for
event-loop isolation, so the imported engine is not the production one. This test
rebuilds celerp.db with that flag cleared to exercise the real production branch
and inspect its connect_args.
"""

from __future__ import annotations

import importlib
import os

import pytest


@pytest.fixture
def production_engine():
    """celerp.db.engine as built by the production (pooled) branch.

    Clears CELERP_TEST_NULLPOOL, reloads the module so the pooled engine is
    constructed, disposes it, and restores the test-mode module so no other test
    inherits a pooled (loop-bound) engine.
    """
    saved = os.environ.get("CELERP_TEST_NULLPOOL")
    os.environ.pop("CELERP_TEST_NULLPOOL", None)
    import celerp.db as dbmod
    try:
        importlib.reload(dbmod)
        yield dbmod.engine
    finally:
        if saved is not None:
            os.environ["CELERP_TEST_NULLPOOL"] = saved
        else:
            os.environ.pop("CELERP_TEST_NULLPOOL", None)
        importlib.reload(dbmod)


def test_api_engine_bounded_timeouts(production_engine):
    """The production async engine is built with lock_timeout and
    statement_timeout server_settings in connect_args."""
    server_settings = _server_settings(production_engine)

    assert server_settings is not None, "engine has no connect_args server_settings"
    assert "lock_timeout" in server_settings, "lock_timeout missing from connect_args"
    assert "statement_timeout" in server_settings, "statement_timeout missing from connect_args"
    assert int(str(server_settings["lock_timeout"]).rstrip("ms") or 0) > 0
    assert int(str(server_settings["statement_timeout"]).rstrip("ms") or 0) > 0


@pytest.mark.asyncio
async def test_migration_advisory_lock_wait_survives_statement_timeout():
    """A second worker's migration advisory-lock wait must not be cancelled by the
    request statement_timeout.

    The migration phase acquires a shared advisory lock so two boots never migrate
    one database at once. While the first worker migrates, the second worker's
    SELECT pg_advisory_lock() blocks on that lock; a statement_timeout applied to
    that connection cancels the wait and aborts the boot once the first worker's
    phase runs longer than the timeout. run_migration_phase must clear the timeout
    on the lock connection so the wait blocks until the lock is free.

    Reproduced here with a 1s statement_timeout and a holder that keeps the lock
    for 2s: on the unfixed path the wait is cancelled at 1s and the phase raises;
    fixed, it waits the full 2s and returns cleanly.
    """
    import asyncio

    import sqlalchemy as sa
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    from celerp.config import settings
    from celerp.db import _MIGRATION_LOCK_KEY
    from celerp.modules.migrations_runner import run_migration_phase

    if not settings.database_url.startswith("postgresql"):
        pytest.skip("advisory-lock timeout behavior is postgres-only")

    engine = create_async_engine(
        settings.database_url, future=True, poolclass=NullPool,
        connect_args={"server_settings": {"statement_timeout": "1000", "lock_timeout": "1000"}},
    )
    holder = create_async_engine(settings.database_url, future=True, poolclass=NullPool)
    hconn = await holder.connect()
    hconn = await hconn.execution_options(isolation_level="AUTOCOMMIT")
    try:
        await hconn.execute(sa.text("SELECT pg_advisory_lock(:k)"), {"k": _MIGRATION_LOCK_KEY})

        async def _release_after(delay: float):
            await asyncio.sleep(delay)
            await hconn.execute(sa.text("SELECT pg_advisory_unlock(:k)"), {"k": _MIGRATION_LOCK_KEY})

        releaser = asyncio.create_task(_release_after(2.0))
        # No modules enabled: the phase just contends for the lock (blocking ~2s),
        # then releases it. It must not raise a cancelled-statement error.
        surviving, errors = await run_migration_phase(engine, set())
        await releaser
        assert surviving == set()
        assert errors == {}
    finally:
        await hconn.close()
        await engine.dispose()
        await holder.dispose()


def _server_settings(engine):
    """Extract the asyncpg server_settings passed to create_async_engine's
    connect_args.

    SQLAlchemy merges the explicit connect_args into the connection kwargs
    captured in the pool's connection-creator closure. Scan the closure cells for
    the merged mapping that carries server_settings.
    """
    sync_engine = getattr(engine, "sync_engine", engine)
    creator = sync_engine.pool._creator
    for cell in getattr(creator, "__closure__", None) or []:
        val = cell.cell_contents
        try:
            if isinstance(val, dict) or hasattr(val, "get"):
                ss = val.get("server_settings")
                if ss:
                    return ss
        except (TypeError, AttributeError):
            continue
    return None
