# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Tests for migration n8o9p0q1r2s3: backfill payment_date on payment JEs."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from celerp.models.base import Base


# ---------------------------------------------------------------------------
# Minimal sync engine for migration testing (alembic op.get_bind() is sync)
# ---------------------------------------------------------------------------

@pytest.fixture()
def sync_db(tmp_path):
    """Create a fresh SQLite DB with only the tables we need."""
    from sqlalchemy import create_engine, text
    db_path = tmp_path / "migration_test.db"
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE companies (
                id TEXT PRIMARY KEY
            )
        """))
        conn.execute(text("""
            CREATE TABLE users (
                id TEXT PRIMARY KEY
            )
        """))
        conn.execute(text("""
            CREATE TABLE locations (
                id TEXT PRIMARY KEY
            )
        """))
        conn.execute(text("""
            CREATE TABLE ledger (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                company_id TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                entity_type TEXT NOT NULL,
                event_type TEXT NOT NULL,
                data TEXT NOT NULL,
                actor_id TEXT,
                location_id TEXT,
                source TEXT NOT NULL,
                idempotency_key TEXT UNIQUE NOT NULL,
                metadata TEXT,
                ts TEXT NOT NULL
            )
        """))
        conn.execute(text("""
            CREATE TABLE projections (
                entity_id TEXT NOT NULL,
                company_id TEXT NOT NULL,
                entity_type TEXT NOT NULL,
                state TEXT NOT NULL,
                version INTEGER NOT NULL,
                location_id TEXT,
                updated_at TEXT NOT NULL,
                is_available INTEGER,
                is_on_memo INTEGER,
                is_on_marketplace INTEGER,
                is_in_production INTEGER,
                is_expired INTEGER,
                expires_at TEXT,
                consignment_flag TEXT,
                PRIMARY KEY (company_id, entity_id)
            )
        """))
    yield engine
    engine.dispose()


def _insert_ledger(conn, *, company_id, entity_id, entity_type, event_type, data: dict, ts: str, idem: str):
    conn.execute(sa.text("""
        INSERT INTO ledger (company_id, entity_id, entity_type, event_type, data, source, idempotency_key, ts)
        VALUES (:cid, :eid, :et, :evt, :data, 'auto_je', :idem, :ts)
    """), {"cid": company_id, "eid": entity_id, "et": entity_type, "evt": event_type,
           "data": json.dumps(data), "idem": idem, "ts": ts})


def _insert_projection(conn, *, company_id, entity_id, entity_type, state: dict):
    conn.execute(sa.text("""
        INSERT INTO projections (company_id, entity_id, entity_type, state, version, updated_at)
        VALUES (:cid, :eid, :et, :state, 1, '2026-01-01 00:00:00')
    """), {"cid": company_id, "eid": entity_id, "et": entity_type, "state": json.dumps(state)})


def _run_upgrade(engine):
    """Run the migration upgrade() against a sync engine via alembic op mock."""
    from unittest.mock import patch, MagicMock
    with engine.connect() as conn:
        with conn.begin():
            mock_op = MagicMock()
            mock_op.get_bind.return_value = conn
            import importlib, sys
            # Ensure fresh import
            mod_name = "celerp.migrations.versions.n8o9p0q1r2s3_backfill_payment_date"
            if mod_name in sys.modules:
                del sys.modules[mod_name]
            with patch.dict("sys.modules", {"alembic.op": mock_op}):
                import celerp.migrations.versions.n8o9p0q1r2s3_backfill_payment_date as mig
                with patch.object(mig, "op", mock_op):
                    mig.upgrade()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_migration_backfills_payment_date_on_ledger(sync_db):
    """Ledger events with missing payment_date get DATE(ts) set."""
    cid = str(uuid.uuid4())
    doc_id = "doc:INV-2601-0001"
    ts_str = "2026-03-15 10:30:00"

    with sync_db.begin() as conn:
        conn.execute(sa.text("INSERT INTO companies (id) VALUES (:id)"), {"id": cid})
        _insert_ledger(conn, company_id=cid, entity_id=doc_id, entity_type="doc",
                       event_type="doc.payment.received", data={"amount": 100},
                       ts=ts_str, idem="idem-pay-1")

    _run_upgrade(sync_db)

    with sync_db.connect() as conn:
        row = conn.execute(sa.text("SELECT data FROM ledger WHERE entity_id = :eid"), {"eid": doc_id}).fetchone()
        data = json.loads(row[0])
        assert data["payment_date"] == "2026-03-15"


def test_migration_skips_ledger_rows_with_existing_payment_date(sync_db):
    """Ledger events that already have payment_date are not modified."""
    cid = str(uuid.uuid4())
    doc_id = "doc:INV-2601-0002"
    ts_str = "2026-03-15 10:30:00"

    with sync_db.begin() as conn:
        conn.execute(sa.text("INSERT INTO companies (id) VALUES (:id)"), {"id": cid})
        _insert_ledger(conn, company_id=cid, entity_id=doc_id, entity_type="doc",
                       event_type="doc.payment.received",
                       data={"amount": 100, "payment_date": "2026-03-10"},
                       ts=ts_str, idem="idem-pay-2")

    _run_upgrade(sync_db)

    with sync_db.connect() as conn:
        row = conn.execute(sa.text("SELECT data FROM ledger WHERE entity_id = :eid"), {"eid": doc_id}).fetchone()
        data = json.loads(row[0])
        # Must NOT be overwritten with the ledger ts date
        assert data["payment_date"] == "2026-03-10"


def test_migration_backfills_je_projection_ts(sync_db):
    """JE projections for payment JEs missing state.ts get DATE(ledger.ts) set."""
    cid = str(uuid.uuid4())
    doc_id = "doc:INV-2601-0003"
    je_id = f"je:auto:{doc_id}:pay:abc123"
    ts_str = "2026-04-20 08:15:00"

    with sync_db.begin() as conn:
        conn.execute(sa.text("INSERT INTO companies (id) VALUES (:id)"), {"id": cid})
        # JE creation ledger event with real wall-clock ts
        _insert_ledger(conn, company_id=cid, entity_id=je_id, entity_type="journal_entry",
                       event_type="acc.journal_entry.created",
                       data={"memo": "Payment", "entries": []},
                       ts=ts_str, idem="idem-je-1")
        # JE projection with missing ts
        _insert_projection(conn, company_id=cid, entity_id=je_id, entity_type="journal_entry",
                           state={"memo": "Payment", "entries": [], "status": "posted"})

    _run_upgrade(sync_db)

    with sync_db.connect() as conn:
        row = conn.execute(sa.text(
            "SELECT state FROM projections WHERE entity_id = :eid AND company_id = :cid"
        ), {"eid": je_id, "cid": cid}).fetchone()
        state = json.loads(row[0])
        assert state["ts"] == "2026-04-20"


def test_migration_skips_je_projection_with_existing_ts(sync_db):
    """JE projections that already have state.ts are not modified."""
    cid = str(uuid.uuid4())
    doc_id = "doc:INV-2601-0004"
    je_id = f"je:auto:{doc_id}:pay:def456"
    ts_str = "2026-04-20 08:15:00"

    with sync_db.begin() as conn:
        conn.execute(sa.text("INSERT INTO companies (id) VALUES (:id)"), {"id": cid})
        _insert_ledger(conn, company_id=cid, entity_id=je_id, entity_type="journal_entry",
                       event_type="acc.journal_entry.created",
                       data={"memo": "Payment", "entries": []},
                       ts=ts_str, idem="idem-je-2")
        # JE projection already has ts set
        _insert_projection(conn, company_id=cid, entity_id=je_id, entity_type="journal_entry",
                           state={"memo": "Payment", "entries": [], "status": "posted",
                                  "ts": "2026-03-01"})

    _run_upgrade(sync_db)

    with sync_db.connect() as conn:
        row = conn.execute(sa.text(
            "SELECT state FROM projections WHERE entity_id = :eid AND company_id = :cid"
        ), {"eid": je_id, "cid": cid}).fetchone()
        state = json.loads(row[0])
        # Must NOT be overwritten
        assert state["ts"] == "2026-03-01"


def test_migration_does_not_touch_non_payment_je_projections(sync_db):
    """Non-payment JE projections (e.g. fin, bill) are not touched."""
    cid = str(uuid.uuid4())
    doc_id = "doc:INV-2601-0005"
    je_id = f"je:auto:{doc_id}:fin"

    with sync_db.begin() as conn:
        conn.execute(sa.text("INSERT INTO companies (id) VALUES (:id)"), {"id": cid})
        _insert_ledger(conn, company_id=cid, entity_id=je_id, entity_type="journal_entry",
                       event_type="acc.journal_entry.created",
                       data={"memo": "Finalize", "entries": []},
                       ts="2026-04-01 09:00:00", idem="idem-je-fin")
        _insert_projection(conn, company_id=cid, entity_id=je_id, entity_type="journal_entry",
                           state={"memo": "Finalize", "entries": [], "status": "posted"})

    _run_upgrade(sync_db)

    with sync_db.connect() as conn:
        row = conn.execute(sa.text(
            "SELECT state FROM projections WHERE entity_id = :eid AND company_id = :cid"
        ), {"eid": je_id, "cid": cid}).fetchone()
        state = json.loads(row[0])
        # ts must NOT have been set by migration
        assert "ts" not in state
