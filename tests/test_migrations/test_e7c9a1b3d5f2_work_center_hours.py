# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Tests for migration e7c9a1b3d5f2: move hours_per_day onto work centers.

Runs against Postgres (the production engine) so the migration's real SQL path
- the jsonb membership/strip casts and the x-or-8.0 CASE - is exercised. The
companies.settings column is created as JSON (matching sa.JSON in the model), so
the jsonb casts the migration relies on are genuinely tested.
"""

from __future__ import annotations

import json
import os
import uuid

import pytest
import sqlalchemy as sa

_NOTICE_TITLE = "Hours per day moved to work centers"
_NOTICE_BODY = (
    "Your company Hours per day setting now lives on the new Default work center under "
    "Settings > Manufacturing > Work centers. Nothing changed in your To-Make estimates."
)


@pytest.fixture()
def sync_db():
    """Fresh, isolated Postgres schema with the tables the migration touches."""
    from sqlalchemy import create_engine, text

    base_url = os.environ["DATABASE_URL"].replace("+asyncpg", "+psycopg2")
    schema = f"migtest_{uuid.uuid4().hex[:8]}"

    admin = create_engine(base_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'CREATE SCHEMA "{schema}"'))
    admin.dispose()

    engine = create_engine(base_url, connect_args={"options": f"-csearch_path={schema}"})
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE companies (id UUID PRIMARY KEY, name TEXT, settings JSON NOT NULL)"))
        conn.execute(text("""
            CREATE TABLE work_centers (
                id UUID PRIMARY KEY,
                company_id UUID NOT NULL REFERENCES companies(id),
                name TEXT NOT NULL,
                wip_location_id UUID,
                labor_rate DOUBLE PRECISION,
                capacity DOUBLE PRECISION,
                created_at TIMESTAMPTZ NOT NULL,
                CONSTRAINT uq_work_center_company_name UNIQUE (company_id, name)
            )
        """))
        conn.execute(text("""
            CREATE TABLE notifications (
                id UUID PRIMARY KEY,
                company_id UUID NOT NULL,
                user_id UUID,
                category VARCHAR(32) NOT NULL,
                title TEXT NOT NULL,
                body TEXT NOT NULL,
                action_url TEXT,
                priority VARCHAR(16) NOT NULL DEFAULT 'medium',
                read BOOLEAN NOT NULL DEFAULT false,
                created_at TIMESTAMPTZ NOT NULL
            )
        """))
    yield engine
    engine.dispose()

    admin = create_engine(base_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    admin.dispose()


def _mkcompany(conn, settings: dict) -> str:
    cid = str(uuid.uuid4())
    conn.execute(sa.text("INSERT INTO companies (id, name, settings) VALUES (:id, :n, CAST(:s AS json))"),
                 {"id": cid, "n": "Co", "s": json.dumps(settings)})
    return cid


def _add_work_center(conn, company_id: str, name: str) -> None:
    conn.execute(sa.text(
        "INSERT INTO work_centers (id, company_id, name, created_at) VALUES (gen_random_uuid(), :cid, :n, NOW())"),
        {"cid": company_id, "n": name})


def _run_upgrade(engine):
    """Run the migration upgrade() against a sync engine via an alembic op mock."""
    from unittest.mock import patch, MagicMock
    with engine.connect() as conn:
        with conn.begin():
            mock_op = MagicMock()
            mock_op.get_bind.return_value = conn
            import sys
            mod_name = "celerp.migrations.versions.e7c9a1b3d5f2_work_center_hours_and_default"
            if mod_name in sys.modules:
                del sys.modules[mod_name]
            with patch.dict("sys.modules", {"alembic.op": mock_op}):
                import celerp.migrations.versions.e7c9a1b3d5f2_work_center_hours_and_default as mig
                with patch.object(mig, "op", mock_op):
                    mig.upgrade()


def _default_wc(conn, cid: str):
    return conn.execute(sa.text(
        "SELECT name, hours_per_day FROM work_centers WHERE company_id = :cid AND is_default"),
        {"cid": cid}).fetchall()


# ---------------------------------------------------------------------------

def test_wc_hours_column_migration(sync_db):
    """Every manufacturing-active company gets exactly one is_default WC whose
    hours match its old setting (10->10, absent->8, stored 0->8); a non-mfg
    company gets none; the migrated key is stripped."""
    with sync_db.begin() as conn:
        c1 = _mkcompany(conn, {"manufacturing": {"hours_per_day": 10}})
        c2 = _mkcompany(conn, {})  # no manufacturing at all
        c3 = _mkcompany(conn, {"manufacturing": {"require_issued_before_complete": True}})  # block, no hours
        c4 = _mkcompany(conn, {"manufacturing": {"hours_per_day": 0}})  # stored 0
        c5 = _mkcompany(conn, {})  # no mfg block, but has a work center
        _add_work_center(conn, c5, "Bench")

    _run_upgrade(sync_db)

    with sync_db.connect() as conn:
        # c1: hours 10
        rows = _default_wc(conn, c1)
        assert len(rows) == 1 and rows[0][0] == "Default" and rows[0][1] == 10.0
        # c2: none
        assert _default_wc(conn, c2) == []
        # c3: mfg block without hours -> 8
        rows = _default_wc(conn, c3)
        assert len(rows) == 1 and rows[0][1] == 8.0
        # c4: stored 0 -> 8 (x-or-8.0, not a plain COALESCE)
        rows = _default_wc(conn, c4)
        assert len(rows) == 1 and rows[0][1] == 8.0
        # c5: existing work center -> also seeded a default at 8, and Bench survives non-default
        rows = _default_wc(conn, c5)
        assert len(rows) == 1 and rows[0][0] == "Default" and rows[0][1] == 8.0
        total_c5 = conn.execute(sa.text("SELECT count(*) FROM work_centers WHERE company_id = :cid"),
                                {"cid": c5}).scalar()
        assert total_c5 == 2

        # migrated key stripped where it existed; other settings untouched
        s1 = conn.execute(sa.text("SELECT settings FROM companies WHERE id = :id"), {"id": c1}).scalar()
        assert "hours_per_day" not in (s1.get("manufacturing") or {})
        s3 = conn.execute(sa.text("SELECT settings FROM companies WHERE id = :id"), {"id": c3}).scalar()
        assert s3.get("manufacturing", {}).get("require_issued_before_complete") is True


def test_migration_notifies_hours_moved(sync_db):
    """Each company that got a Default center gets exactly one company-wide
    manufacturing notice (user_id NULL); a non-mfg company gets none."""
    with sync_db.begin() as conn:
        c1 = _mkcompany(conn, {"manufacturing": {"hours_per_day": 10}})
        c2 = _mkcompany(conn, {})

    _run_upgrade(sync_db)

    with sync_db.connect() as conn:
        rows = conn.execute(sa.text(
            "SELECT user_id, category, priority, action_url, body FROM notifications "
            "WHERE company_id = :cid AND title = :t"),
            {"cid": c1, "t": _NOTICE_TITLE}).fetchall()
        assert len(rows) == 1
        user_id, category, priority, action_url, body = rows[0]
        assert user_id is None
        assert category == "manufacturing"
        assert priority == "high"
        assert action_url == "/settings/manufacturing"
        assert body == _NOTICE_BODY
        # c2 (no manufacturing) is not notified
        n2 = conn.execute(sa.text("SELECT count(*) FROM notifications WHERE company_id = :cid"),
                          {"cid": c2}).scalar()
        assert n2 == 0


def test_migration_atomic_all_or_nothing(sync_db):
    """The whole revision is one transaction: a forced failure at the notification
    step leaves NO partial state - no new columns, no Default WC, and the
    settings key un-stripped."""
    with sync_db.begin() as conn:
        c1 = _mkcompany(conn, {"manufacturing": {"hours_per_day": 10}})
    # Force step 3 (the notification insert) to fail mid-transaction.
    with sync_db.begin() as conn:
        conn.execute(sa.text("DROP TABLE notifications"))

    with pytest.raises(Exception) as exc:
        _run_upgrade(sync_db)
    # The failure is the forced one (missing notifications table), which only the
    # real single-transaction revision can reach - not a missing-migration error.
    assert "notifications" in str(exc.value).lower()

    with sync_db.connect() as conn:
        # Columns rolled back with the transaction (DDL is transactional in PG).
        col = conn.execute(sa.text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'work_centers' AND column_name = 'hours_per_day' "
            "AND table_schema = current_schema()")).fetchone()
        assert col is None
        # No Default center was left behind.
        assert conn.execute(sa.text("SELECT count(*) FROM work_centers WHERE name = 'Default'")).scalar() == 0
        # The settings key survives un-stripped.
        s1 = conn.execute(sa.text("SELECT settings FROM companies WHERE id = :id"), {"id": c1}).scalar()
        assert s1["manufacturing"]["hours_per_day"] == 10
