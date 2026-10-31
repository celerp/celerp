# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Alembic, run by a test the way every production run is: under the migration key."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager, contextmanager

import sqlalchemy as sa
from alembic import command

from celerp.alembic_config import build_alembic_config
from celerp.db import migration_key, migration_key_async
from celerp.db_url import sync_url


def alembic_run(db_url: str, verb: str, revision: str) -> None:
    """``alembic <verb> <revision>`` on *db_url* (upgrade, downgrade or stamp), holding the
    migration key on a connection of its own (``celerp.db.migration_key``). Alembic's env
    reads the database from DATABASE_URL, so it is left pointing at *db_url*."""
    os.environ["DATABASE_URL"] = db_url
    engine = sa.create_engine(sync_url(db_url), isolation_level="AUTOCOMMIT", poolclass=sa.pool.NullPool)
    try:
        with engine.connect() as conn, migration_key(conn):
            getattr(command, verb)(build_alembic_config(), revision)
    finally:
        engine.dispose()


def apply_migrations(db_url: str) -> None:
    """``celerp.cli._apply_migrations`` as ``celerp migrate`` runs it: under ``_migration_lock``."""
    from celerp import cli

    with cli._migration_lock(db_url):
        cli._apply_migrations(db_url)


def run_migrations(db_url: str) -> None:
    """``celerp.cli._run_migrations`` (exit semantics) under ``_migration_lock``."""
    from celerp import cli

    with cli._migration_lock(db_url):
        cli._run_migrations(db_url)


@contextmanager
def history_by_hand(db_url: str):
    """A transaction on *db_url* for a test that writes alembic_version itself (a stamp another
    build left behind, or none), holding the migration key as every production writer of that
    table does."""
    engine = sa.create_engine(sync_url(db_url), poolclass=sa.pool.NullPool)
    try:
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as key_conn, \
                migration_key(key_conn), engine.begin() as conn:
            yield conn
    finally:
        engine.dispose()


@asynccontextmanager
async def history_by_hand_async(engine):
    """``history_by_hand`` on an async engine of the test's own."""
    async with engine.connect() as key_conn:
        key_conn = await key_conn.execution_options(isolation_level="AUTOCOMMIT")
        async with migration_key_async(key_conn), engine.begin() as conn:
            yield conn
