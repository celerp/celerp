# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Alembic, run by a test the way every production run is: under the migration key."""

from __future__ import annotations

import os

import sqlalchemy as sa
from alembic import command

from celerp.alembic_config import build_alembic_config
from celerp.db import migration_key
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
