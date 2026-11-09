# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""System Recovery and the setup restore hold connector_maintenance_guard across the schema
upgrade of the restored database. A migration that builds an index CONCURRENTLY waits for
every open transaction to end, so the guard must hold its lock with no transaction open, or
the restore waits on itself forever."""

from __future__ import annotations

import pytest
import sqlalchemy as sa

import celerp.db as db
from celerp.connectors.ownership import connector_maintenance_guard

# The state of each backend holding the exclusive advisory lock on :key in this database.
_HOLDER_STATE = sa.text(
    "SELECT a.state FROM pg_locks l JOIN pg_stat_activity a ON a.pid = l.pid "
    "WHERE l.locktype = 'advisory' AND l.granted AND l.mode = 'ExclusiveLock' "
    "AND l.database = (SELECT oid FROM pg_database WHERE datname = current_database()) "
    "AND ((l.classid::bigint << 32) | l.objid::bigint) = :key")
_CONNECTOR_KEY = sa.text("SELECT hashtextextended('connector-runtime', 0)")


async def _holder_states() -> dict[str, list[str]]:
    from celerp.services.bootstrap import BOOTSTRAP_LOCK_KEY

    async with db.lifecycle_engine.connect() as conn:
        keys = {"connector": (await conn.execute(_CONNECTOR_KEY)).scalar_one(), "bootstrap": BOOTSTRAP_LOCK_KEY}
        return {name: (await conn.execute(_HOLDER_STATE, {"key": key})).scalars().all() for name, key in keys.items()}


@pytest.mark.asyncio
async def test_the_restore_locks_are_held_with_no_transaction_open():
    """Every lock the setup restore and System Recovery hold across the restored database's
    upgrade (bootstrap_restore_lock, then the recovery locks) sits on an idle connection."""
    from celerp.services.backup_import import _recovery_locks
    from celerp.services.bootstrap import bootstrap_restore_lock

    async with bootstrap_restore_lock(), _recovery_locks():
        held = await _holder_states()
    assert held == {"connector": ["idle"], "bootstrap": ["idle"]}
    assert await _holder_states() == {"connector": [], "bootstrap": []}


@pytest.mark.asyncio
async def test_a_concurrent_index_build_completes_while_the_maintenance_guard_is_held():
    # lock_timeout turns the wait into a failure in seconds, never a hung run.
    async with db.lifecycle_engine.connect() as conn:
        await conn.execution_options(isolation_level="AUTOCOMMIT")
        await conn.execute(sa.text("CREATE TABLE IF NOT EXISTS guard_cic_probe (x int)"))
        try:
            async with connector_maintenance_guard():
                await conn.execute(sa.text("SET lock_timeout = '3s'"))
                await conn.execute(sa.text("CREATE INDEX CONCURRENTLY guard_cic_probe_ix ON guard_cic_probe (x)"))
        finally:
            await conn.execute(sa.text("RESET lock_timeout"))
            await conn.execute(sa.text("DROP TABLE IF EXISTS guard_cic_probe"))
