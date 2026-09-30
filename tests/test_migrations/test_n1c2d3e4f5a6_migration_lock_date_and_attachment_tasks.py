# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""The lock date and attachment task revision adds both nullable columns to existing rows
and removes them again on downgrade."""

from __future__ import annotations

import os
import uuid
from datetime import date

import pytest
from sqlalchemy import create_engine, text

from .conftest import run_migration_ops

PARENT = "m0b1c2d3e4f5_migration_runs"
MODULE = "n1c2d3e4f5a6_migration_lock_date_and_attachment_tasks"


@pytest.fixture()
def parent_db():
    """An isolated schema at the parent revision's migration tables."""
    base_url = os.environ["DATABASE_URL"].replace("+asyncpg", "+psycopg2")
    schema = f"miglock_{uuid.uuid4().hex[:8]}"
    admin = create_engine(base_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'CREATE SCHEMA "{schema}"'))
    admin.dispose()

    engine = create_engine(base_url, connect_args={"options": f"-csearch_path={schema}"})
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE companies (id UUID PRIMARY KEY, name TEXT NOT NULL)"))
        conn.execute(text("CREATE TABLE users (id UUID PRIMARY KEY, email TEXT NOT NULL)"))
    run_migration_ops(engine, PARENT)
    yield engine
    engine.dispose()

    admin = create_engine(base_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    admin.dispose()


def _columns(conn, table: str) -> set[str]:
    return set(conn.execute(text(
        "SELECT column_name FROM information_schema.columns WHERE table_schema = current_schema() "
        "AND table_name = :t"), {"t": table}).scalars())


def test_lock_date_and_attachment_columns_upgrade_and_downgrade(parent_db):
    company_id, user_id, run_id, task_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    with parent_db.begin() as conn:
        conn.execute(text("INSERT INTO companies (id, name) VALUES (:id, 'Staged')"), {"id": company_id})
        conn.execute(text("INSERT INTO users (id, email) VALUES (:id, 'owner@example.com')"), {"id": user_id})
        conn.execute(text(
            "INSERT INTO migration_runs (id, company_id, created_by_user_id, scan_claim_sha256, source_system,"
            " source_artifact_sha256, adapter_version, cif_version, mode, status, created_at)"
            " VALUES (:id, :cid, :uid, :claim, 'manager_io', :sha, '1', '2', 'full_history', 'running', NOW())"
        ), {"id": run_id, "cid": company_id, "uid": user_id, "claim": "c" * 64, "sha": "a" * 64})
        conn.execute(text("INSERT INTO migration_cleanup_tasks (id, company_id, run_ids, created_at)"
                          " VALUES (:id, :cid, '[]', NOW())"), {"id": task_id, "cid": company_id})

    run_migration_ops(parent_db, MODULE)
    with parent_db.begin() as conn:
        assert conn.execute(text("SELECT source_lock_date FROM migration_runs")).scalar_one() is None
        assert conn.execute(text("SELECT attachment FROM migration_cleanup_tasks")).scalar_one() is None
        conn.execute(text("UPDATE migration_runs SET source_lock_date = :d"), {"d": date(2026, 2, 28)})
        conn.execute(text("UPDATE migration_cleanup_tasks SET attachment = :a"),
                     {"a": '{"file_id": "f", "mime": "image/png", "idempotency_key": "k"}'})
        assert conn.execute(text("SELECT source_lock_date FROM migration_runs")).scalar_one() == date(2026, 2, 28)
        assert conn.execute(text("SELECT attachment FROM migration_cleanup_tasks")).scalar_one()["file_id"] == "f"

    run_migration_ops(parent_db, MODULE, "downgrade")
    with parent_db.connect() as conn:
        assert "source_lock_date" not in _columns(conn, "migration_runs")
        assert "attachment" not in _columns(conn, "migration_cleanup_tasks")
