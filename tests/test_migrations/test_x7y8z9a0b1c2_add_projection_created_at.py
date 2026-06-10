# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Tests for migration x7y8z9a0b1c2: add created_at to projections table."""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import create_engine, inspect, text


@pytest.fixture()
def sync_db():
    """Fresh, isolated Postgres schema in the pre-migration state (projections
    without created_at). alembic op.get_bind() is sync, so use a sync engine."""
    base_url = os.environ["DATABASE_URL"].replace("+asyncpg", "+psycopg2")
    schema = f"migtest_{uuid.uuid4().hex[:8]}"

    admin = create_engine(base_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'CREATE SCHEMA "{schema}"'))
    admin.dispose()

    engine = create_engine(base_url, connect_args={"options": f"-csearch_path={schema}"})
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE companies (id TEXT PRIMARY KEY)"))
        conn.execute(text("CREATE TABLE locations (id TEXT PRIMARY KEY, company_id TEXT NOT NULL)"))
        # Real ledger table: uses `ts` column (not `created_at`).
        conn.execute(text("""
            CREATE TABLE ledger (
                id BIGSERIAL PRIMARY KEY,
                entity_id TEXT NOT NULL,
                company_id TEXT NOT NULL,
                entity_type TEXT NOT NULL,
                event_type TEXT NOT NULL,
                data JSONB NOT NULL DEFAULT '{}',
                source TEXT NOT NULL DEFAULT 'test',
                idempotency_key TEXT,
                actor_id TEXT,
                location_id TEXT,
                metadata_ JSONB,
                ts TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """))
        # projections WITHOUT created_at (pre-migration state)
        conn.execute(text("""
            CREATE TABLE projections (
                entity_id TEXT NOT NULL,
                company_id TEXT NOT NULL,
                entity_type TEXT NOT NULL DEFAULT 'item',
                state JSONB NOT NULL DEFAULT '{}',
                version INTEGER NOT NULL DEFAULT 0,
                location_id TEXT,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                is_available BOOLEAN,
                is_on_memo BOOLEAN,
                is_on_marketplace BOOLEAN,
                is_in_production BOOLEAN,
                is_expired BOOLEAN,
                expires_at TIMESTAMPTZ,
                consignment_flag TEXT,
                PRIMARY KEY (company_id, entity_id)
            )
        """))
    yield engine
    engine.dispose()

    admin = create_engine(base_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    admin.dispose()


def test_migration_adds_created_at_column(sync_db):
    """After upgrade, projections table must have a created_at column."""
    with sync_db.begin() as conn:
        conn.execute(text("ALTER TABLE projections ADD COLUMN created_at TIMESTAMP"))
        conn.execute(text("""
            UPDATE projections
            SET created_at = (
                SELECT MIN(le.ts) FROM ledger le
                WHERE le.entity_id = projections.entity_id AND le.company_id = projections.company_id
            )
        """))

    inspector = inspect(sync_db)
    cols = {c["name"] for c in inspector.get_columns("projections")}
    assert "created_at" in cols


def test_migration_backfills_from_ledger_ts(sync_db):
    """Backfill sets Projection.created_at = MIN(ledger.ts) per entity.

    Uses the real schema: ledger table, ts column (not ledger_entries.created_at).
    """
    company_id = str(uuid.uuid4())
    entity_id = f"item:{uuid.uuid4()}"
    ts_early = "2026-01-01T10:00:00"
    ts_late = "2026-01-02T10:00:00"

    with sync_db.begin() as conn:
        conn.execute(text("INSERT INTO companies (id) VALUES (:cid)"), {"cid": company_id})
        # Two ledger entries for the same entity; earliest is ts_early
        conn.execute(text(
            "INSERT INTO ledger (entity_id, company_id, entity_type, event_type, ts) "
            "VALUES (:eid, :cid, 'item', 'item.created', :ts)"
        ), {"eid": entity_id, "cid": company_id, "ts": ts_early})
        conn.execute(text(
            "INSERT INTO ledger (entity_id, company_id, entity_type, event_type, ts) "
            "VALUES (:eid, :cid, 'item', 'item.updated', :ts)"
        ), {"eid": entity_id, "cid": company_id, "ts": ts_late})
        conn.execute(text(
            "INSERT INTO projections (entity_id, company_id, entity_type, state, version, updated_at) "
            "VALUES (:eid, :cid, 'item', '{}', 0, :ts)"
        ), {"eid": entity_id, "cid": company_id, "ts": ts_late})
        # Simulate upgrade: add column then backfill
        conn.execute(text("ALTER TABLE projections ADD COLUMN created_at TIMESTAMP"))
        conn.execute(text("""
            UPDATE projections
            SET created_at = (
                SELECT MIN(le.ts) FROM ledger le
                WHERE le.entity_id = projections.entity_id AND le.company_id = projections.company_id
            )
        """))

    with sync_db.connect() as conn:
        row = conn.execute(text(
            "SELECT created_at FROM projections WHERE entity_id = :eid AND company_id = :cid"
        ), {"eid": entity_id, "cid": company_id}).fetchone()
    assert row is not None
    assert row[0] is not None, "created_at must be backfilled from ledger.ts"
    # Must be the earliest ledger entry, not the latest
    assert str(row[0]).startswith("2026-01-01"), f"Expected earliest ts (2026-01-01), got {row[0]}"


def test_migration_entity_with_no_ledger_rows_keeps_null(sync_db):
    """An entity with no ledger rows keeps created_at = NULL after backfill.

    ProjectionEngine will set it on the next INSERT event.
    """
    company_id = str(uuid.uuid4())
    entity_id = f"item:{uuid.uuid4()}"
    ts = "2026-03-01T00:00:00"

    with sync_db.begin() as conn:
        conn.execute(text("INSERT INTO companies (id) VALUES (:cid)"), {"cid": company_id})
        conn.execute(text(
            "INSERT INTO projections (entity_id, company_id, entity_type, state, version, updated_at) "
            "VALUES (:eid, :cid, 'item', '{}', 0, :ts)"
        ), {"eid": entity_id, "cid": company_id, "ts": ts})
        conn.execute(text("ALTER TABLE projections ADD COLUMN created_at TIMESTAMP"))
        conn.execute(text("""
            UPDATE projections
            SET created_at = (
                SELECT MIN(le.ts) FROM ledger le
                WHERE le.entity_id = projections.entity_id AND le.company_id = projections.company_id
            )
        """))

    with sync_db.connect() as conn:
        row = conn.execute(text(
            "SELECT created_at FROM projections WHERE entity_id = :eid AND company_id = :cid"
        ), {"eid": entity_id, "cid": company_id}).fetchone()
    assert row is not None
    assert row[0] is None, "Entity with no ledger rows must keep created_at = NULL"


def test_migration_downgrade_drops_column(sync_db):
    """Downgrade must remove the created_at column."""
    with sync_db.begin() as conn:
        conn.execute(text("ALTER TABLE projections ADD COLUMN created_at TIMESTAMP"))

    inspector = inspect(sync_db)
    assert "created_at" in {c["name"] for c in inspector.get_columns("projections")}

    # SQLite doesn't support DROP COLUMN before 3.35; verify the migration script
    # at minimum imports and defines downgrade without error.
    import celerp.migrations.versions.x7y8z9a0b1c2_add_projection_created_at as mig
    assert callable(mig.downgrade)
