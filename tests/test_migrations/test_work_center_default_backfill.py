# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Data half of the work-center default split (child of e7c9a1b3d5f2).

The signature-less data migration seeds one Default work center per
manufacturing-active company (mapping absent/blank/0/non-numeric hours to 8.0),
posts one company-wide notice, and strips the migrated settings key. It is
idempotent, because the develop-to-release reconcile replays it on every
version change, and on a restore boot it can be BOTH alembic-applied and
reconcile-replayed in one pass.

Runs against Postgres through a real alembic Operations context (schema
migration first, then the data migration), so the jsonb casts and the x-or-8.0
CASE are genuinely exercised.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa

from celerp.migrations.versions.f8a9b0c1d2e3_work_center_default_backfill import (
    _NOTICE_BODY,
    _NOTICE_TITLE,
)

from .conftest import run_migration_ops, wc_add_work_center, wc_mkcompany

_SCHEMA_MIGRATION = "e7c9a1b3d5f2_work_center_default_columns"
_DATA_MIGRATION = "f8a9b0c1d2e3_work_center_default_backfill"


def _default_hours(conn, cid: str):
    rows = conn.execute(sa.text(
        "SELECT hours_per_day FROM work_centers WHERE company_id = :cid AND is_default"),
        {"cid": cid}).fetchall()
    assert len(rows) <= 1
    return rows[0][0] if rows else None


def _notice_count(conn, cid: str) -> int:
    return conn.execute(sa.text(
        "SELECT count(*) FROM notifications WHERE company_id = :cid AND title = :t"),
        {"cid": cid, "t": _NOTICE_TITLE}).scalar()


def test_seed_notify_strip(wc_db):
    """One Default center per mfg-active company with the x-or-8.0 mapping
    (10->10, block-without-hours->8, 0->8, non-numeric->8), a non-mfg company
    with none, a non-mfg company that already has a work center seeded at 8,
    exactly one company-wide notice, and the migrated key stripped."""
    with wc_db.begin() as conn:
        c1 = wc_mkcompany(conn, {"manufacturing": {"hours_per_day": 10}})
        c2 = wc_mkcompany(conn, {})  # no manufacturing at all
        c3 = wc_mkcompany(conn, {"manufacturing": {"require_issued_before_complete": True}})  # no hours
        c4 = wc_mkcompany(conn, {"manufacturing": {"hours_per_day": 0}})  # stored 0
        c5 = wc_mkcompany(conn, {"manufacturing": {"hours_per_day": "abc"}})  # non-numeric (F5)
        c6 = wc_mkcompany(conn, {})  # no mfg block, but has a work center
        wc_add_work_center(conn, c6, "Bench")

    run_migration_ops(wc_db, _SCHEMA_MIGRATION)
    run_migration_ops(wc_db, _DATA_MIGRATION)

    with wc_db.connect() as conn:
        assert _default_hours(conn, c1) == 10.0
        assert _default_hours(conn, c2) is None  # non-mfg, no WC -> not seeded
        assert _default_hours(conn, c3) == 8.0
        assert _default_hours(conn, c4) == 8.0  # x-or-8.0, not a plain COALESCE
        assert _default_hours(conn, c5) == 8.0  # non-numeric degrades to 8, insert not aborted
        assert _default_hours(conn, c6) == 8.0  # existing WC -> Default seeded too
        # c6 keeps its Bench center alongside the new Default
        total_c6 = conn.execute(sa.text("SELECT count(*) FROM work_centers WHERE company_id = :cid"),
                                {"cid": c6}).scalar()
        assert total_c6 == 2

        assert _notice_count(conn, c1) == 1
        assert _notice_count(conn, c2) == 0
        # the notice is company-wide (user_id NULL) with the moved-setting copy
        row = conn.execute(sa.text(
            "SELECT user_id, category, priority, action_url, body FROM notifications "
            "WHERE company_id = :cid AND title = :t"), {"cid": c1, "t": _NOTICE_TITLE}).fetchone()
        assert row[0] is None
        assert row[1] == "manufacturing"
        assert row[2] == "high"
        assert row[3] == "/settings/manufacturing"
        assert row[4] == _NOTICE_BODY

        # migrated key stripped where present; unrelated settings untouched
        s1 = conn.execute(sa.text("SELECT settings FROM companies WHERE id = :id"), {"id": c1}).scalar()
        assert "hours_per_day" not in (s1.get("manufacturing") or {})
        s3 = conn.execute(sa.text("SELECT settings FROM companies WHERE id = :id"), {"id": c3}).scalar()
        assert s3.get("manufacturing", {}).get("require_issued_before_complete") is True


def test_data_migration_atomic(wc_db):
    """The DATA migration is its own transaction: forcing the notice step to
    fail rolls back this migration's seed and strip, but the columns added by
    the separate schema migration survive (they committed in their own txn)."""
    with wc_db.begin() as conn:
        c1 = wc_mkcompany(conn, {"manufacturing": {"hours_per_day": 10}})

    run_migration_ops(wc_db, _SCHEMA_MIGRATION)  # columns land, own txn

    # Force the notice INSERT to fail mid data-migration (drop the local table;
    # the public.notifications guard still sees the global one and proceeds).
    with wc_db.begin() as conn:
        conn.execute(sa.text("DROP TABLE notifications"))

    with pytest.raises(Exception):
        run_migration_ops(wc_db, _DATA_MIGRATION)

    with wc_db.connect() as conn:
        # Columns from the schema migration are still present.
        cols = conn.execute(sa.text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'work_centers' AND table_schema = current_schema() "
            "AND column_name IN ('is_default', 'hours_per_day')")).fetchall()
        assert {c[0] for c in cols} == {"is_default", "hours_per_day"}
        # The seed rolled back with the failed data transaction.
        assert conn.execute(sa.text("SELECT count(*) FROM work_centers WHERE name = 'Default'")).scalar() == 0
        # The settings key survives un-stripped.
        s1 = conn.execute(sa.text("SELECT settings FROM companies WHERE id = :id"), {"id": c1}).scalar()
        assert s1["manufacturing"]["hours_per_day"] == 10


def test_double_application_idempotent(wc_db):
    """Applying the data migration twice (alembic apply then reconcile replay,
    as on a restore boot) yields exactly one Default center and one notice per
    company."""
    with wc_db.begin() as conn:
        c1 = wc_mkcompany(conn, {"manufacturing": {"hours_per_day": 10}})

    run_migration_ops(wc_db, _SCHEMA_MIGRATION)
    run_migration_ops(wc_db, _DATA_MIGRATION)
    run_migration_ops(wc_db, _DATA_MIGRATION)  # replay

    with wc_db.connect() as conn:
        assert conn.execute(sa.text(
            "SELECT count(*) FROM work_centers WHERE company_id = :cid AND is_default"),
            {"cid": c1}).scalar() == 1
        assert _notice_count(conn, c1) == 1
        assert _default_hours(conn, c1) == 10.0
