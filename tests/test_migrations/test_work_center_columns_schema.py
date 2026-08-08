# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Schema half of the work-center default split (revision e7c9a1b3d5f2).

The rewritten e7c9a1b3d5f2 carries only verifiable DDL: it adds hours_per_day
and is_default and the uq_work_center_one_default partial index. Runs against
Postgres through a real alembic Operations context so op.add_column /
op.create_index / op.alter_column emit real DDL.
"""

from __future__ import annotations

import sqlalchemy as sa

from .conftest import run_migration_ops, wc_add_work_center, wc_mkcompany

_SCHEMA_MIGRATION = "e7c9a1b3d5f2_work_center_default_columns"


def _columns(conn) -> dict:
    rows = conn.execute(sa.text(
        "SELECT column_name, is_nullable, column_default FROM information_schema.columns "
        "WHERE table_name = 'work_centers' AND table_schema = current_schema()")).fetchall()
    return {r[0]: (r[1], r[2]) for r in rows}


def test_columns_and_index_added(wc_db):
    """The schema migration adds hours_per_day and is_default (NOT NULL,
    existing rows backfilled false, no residual server default) plus the
    partial unique index uq_work_center_one_default."""
    with wc_db.begin() as conn:
        cid = wc_mkcompany(conn, {"manufacturing": {"hours_per_day": 10}})
        wc_add_work_center(conn, cid, "Bench")  # a pre-existing row to backfill

    run_migration_ops(wc_db, _SCHEMA_MIGRATION)

    with wc_db.connect() as conn:
        cols = _columns(conn)
        # both columns exist
        assert "hours_per_day" in cols
        assert "is_default" in cols
        # is_default is NOT NULL and carries no residual server default
        assert cols["is_default"][0] == "NO"
        assert cols["is_default"][1] is None
        # hours_per_day is nullable
        assert cols["hours_per_day"][0] == "YES"
        # the pre-existing row was backfilled to false (not left NULL)
        val = conn.execute(sa.text("SELECT is_default FROM work_centers WHERE name = 'Bench'")).scalar()
        assert val is False
        # the partial unique index exists
        idx = conn.execute(sa.text(
            "SELECT indexdef FROM pg_indexes "
            "WHERE indexname = 'uq_work_center_one_default' AND schemaname = current_schema()")).scalar()
        assert idx is not None
        assert "is_default" in idx  # partial predicate WHERE is_default
