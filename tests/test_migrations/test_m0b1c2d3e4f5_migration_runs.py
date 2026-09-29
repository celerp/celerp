# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""The migration_runs revision creates the run and entity-map tables with their integrity rules."""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from .conftest import run_migration_ops

MODULE = "m0b1c2d3e4f5_migration_runs"


@pytest.fixture()
def base_db():
    """An isolated schema holding the two tables the revision references."""
    base_url = os.environ["DATABASE_URL"].replace("+asyncpg", "+psycopg2")
    schema = f"migruns_{uuid.uuid4().hex[:8]}"
    admin = create_engine(base_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'CREATE SCHEMA "{schema}"'))
    admin.dispose()

    engine = create_engine(base_url, connect_args={"options": f"-csearch_path={schema}"})
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE companies (id UUID PRIMARY KEY, name TEXT NOT NULL)"))
        conn.execute(text("CREATE TABLE users (id UUID PRIMARY KEY, email TEXT NOT NULL)"))
    yield engine
    engine.dispose()

    admin = create_engine(base_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    admin.dispose()


def _insert_run(conn, company_id, user_id, claim: str | None = None) -> uuid.UUID:
    run_id = uuid.uuid4()
    conn.execute(text(
        "INSERT INTO migration_runs (id, company_id, created_by_user_id, scan_claim_sha256, source_system,"
        " source_artifact_sha256, adapter_version, cif_version, mode, status, created_at)"
        " VALUES (:id, :cid, :uid, :claim, 'manager_io', :sha, '1', '2', 'full_history', 'preparing', NOW())"
    ), {"id": run_id, "cid": company_id, "uid": user_id, "claim": claim or uuid.uuid4().hex * 2, "sha": "a" * 64})
    return run_id


def _insert_map(conn, run_id, external_id: str) -> None:
    conn.execute(text(
        "INSERT INTO migration_entity_maps (id, migration_run_id, source_type, source_external_id,"
        " target_entity_type, target_entity_id, status)"
        " VALUES (gen_random_uuid(), :rid, 'Customer', :ext, 'contact', 'contact:1', 'created')"
    ), {"rid": run_id, "ext": external_id})


def test_migration_runs_schema_upgrade_integrity(base_db):
    run_migration_ops(base_db, MODULE)

    company_id, user_id = uuid.uuid4(), uuid.uuid4()
    with base_db.begin() as conn:
        conn.execute(text("INSERT INTO companies (id, name) VALUES (:id, 'Staged')"), {"id": company_id})
        conn.execute(text("INSERT INTO users (id, email) VALUES (:id, 'owner@example.com')"), {"id": user_id})
        run_id = _insert_run(conn, company_id, user_id, claim="c" * 64)
        _insert_map(conn, run_id, "c-1")

    with base_db.connect() as conn:
        row = conn.execute(text(
            "SELECT phase_state::text, reconciliation::text, prepared_by FROM migration_runs WHERE id = :id"
        ), {"id": run_id}).one()
        assert row == ("{}", "{}", None)
        meta = conn.execute(text("SELECT metadata::text FROM migration_entity_maps")).scalar_one()
        assert meta == "{}"

    with pytest.raises(IntegrityError) as dup:
        with base_db.begin() as conn:
            _insert_map(conn, run_id, "c-1")
    assert "uq_migration_entity_map_source" in str(dup.value)

    # One scan claim starts at most one run.
    with pytest.raises(IntegrityError) as dup:
        with base_db.begin() as conn:
            _insert_run(conn, company_id, user_id, claim="c" * 64)
    assert "migration_runs_scan_claim_sha256_key" in str(dup.value)

    with pytest.raises(IntegrityError):
        with base_db.begin() as conn:
            _insert_run(conn, uuid.uuid4(), user_id)
    with pytest.raises(IntegrityError):
        with base_db.begin() as conn:
            _insert_run(conn, company_id, uuid.uuid4())

    with base_db.begin() as conn:
        conn.execute(text("DELETE FROM migration_runs WHERE id = :id"), {"id": run_id})
    with base_db.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM migration_entity_maps")).scalar_one() == 0


def test_migration_runs_schema_stages_companies_and_downgrades_cleanly(base_db):
    existing = uuid.uuid4()
    with base_db.begin() as conn:
        conn.execute(text("INSERT INTO companies (id, name) VALUES (:id, 'Live')"), {"id": existing})
    run_migration_ops(base_db, MODULE)

    staged = uuid.uuid4()
    with base_db.begin() as conn:
        conn.execute(text("INSERT INTO companies (id, name, is_migration_staged) VALUES (:id, 'Staged', true)"),
                     {"id": staged})
    with base_db.connect() as conn:
        flags = dict(conn.execute(text("SELECT id, is_migration_staged FROM companies")).all())
    assert flags == {existing: False, staged: True}

    run_migration_ops(base_db, MODULE, "downgrade")
    with base_db.connect() as conn:
        columns = set(conn.execute(text(
            "SELECT column_name FROM information_schema.columns WHERE table_schema = current_schema()"
            " AND table_name = 'companies'")).scalars())
        tables = set(conn.execute(text(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()")).scalars())
    assert columns == {"id", "name"}
    assert tables == {"companies", "users"}
