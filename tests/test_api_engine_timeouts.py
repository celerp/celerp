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
