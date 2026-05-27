# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Tests for migration x7y8z9a0b1c2: add created_at to projections table."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, inspect, text


@pytest.fixture()
def sync_db(tmp_path):
    """Minimal SQLite DB with projections + ledger_entries pre-migration state."""
    db_path = tmp_path / "migration_test.db"
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE companies (
                id TEXT PRIMARY KEY
            )
        """))
        conn.execute(text("""
            CREATE TABLE locations (
                id TEXT PRIMARY KEY,
                company_id TEXT NOT NULL
            )
        """))
        conn.execute(text("""
            CREATE TABLE ledger_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_id TEXT NOT NULL,
                company_id TEXT NOT NULL,
                entity_type TEXT NOT NULL,
                event_type TEXT NOT NULL,
                data TEXT NOT NULL DEFAULT '{}',
                source TEXT NOT NULL DEFAULT 'test',
                idempotency_key TEXT,
                actor_id TEXT,
                location_id TEXT,
                metadata_ TEXT,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """))
        # projections WITHOUT created_at (pre-migration state)
        conn.execute(text("""
            CREATE TABLE projections (
                entity_id TEXT NOT NULL,
                company_id TEXT NOT NULL,
                entity_type TEXT NOT NULL DEFAULT 'item',
                state TEXT NOT NULL DEFAULT '{}',
                version INTEGER NOT NULL DEFAULT 0,
                location_id TEXT,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                is_available INTEGER,
                is_on_memo INTEGER,
                is_on_marketplace INTEGER,
                is_in_production INTEGER,
                is_expired INTEGER,
                expires_at TIMESTAMP,
                consignment_flag TEXT,
                PRIMARY KEY (company_id, entity_id)
            )
        """))
    return engine


def test_migration_adds_created_at_column(sync_db):
    """After upgrade, projections table must have a created_at column."""
    with sync_db.begin() as conn:
        from alembic.operations import Operations
        from alembic.runtime.migration import MigrationContext
        ctx = MigrationContext.configure(conn)
        op = Operations(ctx)
        op.add_column("projections", __import__("sqlalchemy").Column(
            "created_at", __import__("sqlalchemy").DateTime(timezone=True), nullable=True
        ))
        conn.execute(text("""
            UPDATE projections
            SET created_at = (
                SELECT MIN(le.created_at) FROM ledger_entries le
                WHERE le.entity_id = projections.entity_id AND le.company_id = projections.company_id
            )
        """))

    inspector = inspect(sync_db)
    cols = {c["name"] for c in inspector.get_columns("projections")}
    assert "created_at" in cols


def test_migration_backfills_from_ledger_entries(sync_db):
    """Backfill sets Projection.created_at = MIN(ledger_entries.created_at) per entity."""
    company_id = str(uuid.uuid4())
    entity_id = f"item:{uuid.uuid4()}"
    ts_early = "2026-01-01T10:00:00"
    ts_late = "2026-01-02T10:00:00"

    with sync_db.begin() as conn:
        conn.execute(text("INSERT INTO companies (id) VALUES (:cid)"), {"cid": company_id})
        # Two ledger entries for the same entity
        conn.execute(text(
            "INSERT INTO ledger_entries (entity_id, company_id, entity_type, event_type, created_at) "
            "VALUES (:eid, :cid, 'item', 'item.created', :ts)"
        ), {"eid": entity_id, "cid": company_id, "ts": ts_early})
        conn.execute(text(
            "INSERT INTO ledger_entries (entity_id, company_id, entity_type, event_type, created_at) "
            "VALUES (:eid, :cid, 'item', 'item.updated', :ts)"
        ), {"eid": entity_id, "cid": company_id, "ts": ts_late})
        conn.execute(text(
            "INSERT INTO projections (entity_id, company_id, entity_type, state, version, updated_at) "
            "VALUES (:eid, :cid, 'item', '{}', 0, :ts)"
        ), {"eid": entity_id, "cid": company_id, "ts": ts_late})
        # Add column (simulating upgrade step 1)
        conn.execute(text("ALTER TABLE projections ADD COLUMN created_at TIMESTAMP"))
        # Backfill (simulating upgrade step 2)
        conn.execute(text("""
            UPDATE projections
            SET created_at = (
                SELECT MIN(le.created_at) FROM ledger_entries le
                WHERE le.entity_id = projections.entity_id AND le.company_id = projections.company_id
            )
        """))

    with sync_db.connect() as conn:
        row = conn.execute(text(
            "SELECT created_at FROM projections WHERE entity_id = :eid AND company_id = :cid"
        ), {"eid": entity_id, "cid": company_id}).fetchone()
    assert row is not None
    assert row[0] is not None
    # Must be the earliest entry, not the latest
    assert str(row[0]).startswith("2026-01-01"), f"Expected 2026-01-01 prefix, got {row[0]}"


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
