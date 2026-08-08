# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Shared harness for migration tests.

Runs against Postgres (the production engine) so each migration's real SQL is
exercised. alembic op.get_bind() is sync, so a sync psycopg2 engine is used,
with every test isolated in its own schema.

Three harnesses, for the two shapes of migration test. `mig_db` and `acc_db` each
build one table in their own schema, for exercising a single migration's SQL; a
migration writes to one or the other, projections for item state or accounts for
the chart. `fresh_db` provisions a whole throwaway database, for the tests that run
the real migration path end to end and so need alembic_version and every table
alembic creates.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import uuid
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url


def _run_migration(engine, module_name: str, fn: str) -> None:
    """Run versions/<module_name>.<fn>() against `engine` through an op mock.

    The module is dropped from sys.modules first: it binds `op` at import time,
    so a module left over from an earlier test would hold that test's
    connection.
    """
    full = f"celerp.migrations.versions.{module_name}"
    with engine.connect() as conn:
        with conn.begin():
            mock_op = MagicMock()
            mock_op.get_bind.return_value = conn
            if full in sys.modules:
                del sys.modules[full]
            with patch.dict("sys.modules", {"alembic.op": mock_op}):
                mig = importlib.import_module(full)
                with patch.object(mig, "op", mock_op):
                    getattr(mig, fn)()


def run_migration_ops(engine, module_name: str) -> None:
    """Run versions/<module_name>.upgrade() through a REAL alembic Operations
    context, in its own transaction.

    Unlike `_run_migration`'s MagicMock op (which only records calls and suits a
    raw-SQL migration reached via op.get_bind()), this binds a live Operations
    context, so real DDL ops (op.add_column, op.create_index, op.alter_column)
    actually execute against the connection. A raw-SQL migration works here too:
    its op.get_bind() returns the same connection.
    """
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext

    full = f"celerp.migrations.versions.{module_name}"
    with engine.connect() as conn:
        with conn.begin():
            ctx = MigrationContext.configure(conn)
            if full in sys.modules:
                del sys.modules[full]
            with Operations.context(ctx):
                mig = importlib.import_module(full)
                mig.upgrade()


class MigDB:
    """An isolated Postgres schema holding a projections table (item state)."""

    def __init__(self, engine):
        self.engine = engine

    def insert_item(self, company_id: str, entity_id: str, state: dict) -> None:
        with self.engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO projections (entity_id, company_id, entity_type, state)
                VALUES (:eid, :cid, 'item', CAST(:state AS jsonb))
            """), {"eid": entity_id, "cid": company_id, "state": json.dumps(state)})

    def get_state(self, company_id: str, entity_id: str) -> dict:
        with self.engine.connect() as conn:
            row = conn.execute(text("""
                SELECT state FROM projections WHERE company_id = :cid AND entity_id = :eid
            """), {"cid": company_id, "eid": entity_id}).fetchone()
        assert row is not None
        # psycopg2 returns JSONB as a dict; tolerate a str just in case.
        return row[0] if isinstance(row[0], dict) else json.loads(row[0])

    def run_upgrade(self, module_name: str) -> None:
        """Run versions/<module_name>.upgrade() against this schema via an op mock."""
        _run_migration(self.engine, module_name, "upgrade")


class AccountsDB:
    """An isolated Postgres schema holding an accounts table (the chart)."""

    def __init__(self, engine):
        self.engine = engine

    def add_account(self, company_id: str, code: str, name: str = "x",
                    account_type: str = "expense", parent_code: str | None = None) -> None:
        with self.engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO accounts (id, company_id, code, name, account_type, parent_code,"
                " is_active, created_at) VALUES (gen_random_uuid(), :cid, :code, :name,"
                " :t, :p, TRUE, NOW())"
            ), {"cid": company_id, "code": code, "name": name, "t": account_type, "p": parent_code})

    def get(self, company_id: str, code: str):
        with self.engine.connect() as conn:
            return conn.execute(text(
                "SELECT name, account_type, parent_code FROM accounts"
                " WHERE company_id = :cid AND code = :code"
            ), {"cid": company_id, "code": code}).fetchone()

    def run_upgrade(self, module_name: str) -> None:
        _run_migration(self.engine, module_name, "upgrade")

    def run_downgrade(self, module_name: str) -> None:
        _run_migration(self.engine, module_name, "downgrade")


@pytest.fixture()
def mig_db():
    base_url = os.environ["DATABASE_URL"].replace("+asyncpg", "+psycopg2")
    schema = f"migtest_{uuid.uuid4().hex[:8]}"

    admin = create_engine(base_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'CREATE SCHEMA "{schema}"'))
    admin.dispose()

    engine = create_engine(base_url, connect_args={"options": f"-csearch_path={schema}"})
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE projections (
                entity_id TEXT NOT NULL,
                company_id TEXT NOT NULL,
                entity_type TEXT NOT NULL,
                state JSONB NOT NULL,
                version INTEGER NOT NULL DEFAULT 1,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT '2026-01-01',
                PRIMARY KEY (company_id, entity_id)
            )
        """))
    yield MigDB(engine)
    engine.dispose()

    admin = create_engine(base_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    admin.dispose()


@pytest.fixture()
def acc_db():
    base_url = os.environ["DATABASE_URL"].replace("+asyncpg", "+psycopg2")
    schema = f"accmig_{uuid.uuid4().hex[:8]}"

    admin = create_engine(base_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'CREATE SCHEMA "{schema}"'))
    admin.dispose()

    engine = create_engine(base_url, connect_args={"options": f"-csearch_path={schema}"})
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE accounts (
                id UUID PRIMARY KEY,
                company_id TEXT NOT NULL,
                code TEXT NOT NULL,
                name TEXT NOT NULL,
                account_type TEXT NOT NULL,
                parent_code TEXT,
                is_active BOOLEAN NOT NULL DEFAULT TRUE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """))
    yield AccountsDB(engine)
    engine.dispose()

    admin = create_engine(base_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    admin.dispose()


def head_rev() -> str:
    """The current alembic head revision id."""
    from alembic.script import ScriptDirectory

    from celerp.alembic_config import build_alembic_config

    return ScriptDirectory.from_config(build_alembic_config()).get_current_head()


def swap_db(url: str, dbname: str) -> str:
    """Replace the database name in a SQLAlchemy URL.

    Parsed rather than string-split: a socket DSN carries its directory in a
    query parameter (?host=/var/run/postgresql), so splitting on the last "/"
    rewrites the socket path instead of the database.
    """
    return make_url(url).set(database=dbname).render_as_string(hide_password=False)


@pytest.fixture()
def fresh_db():
    """Provision a throwaway database, yield (async_url, sync_url), then drop it.

    Restores os.environ['DATABASE_URL'] on teardown - `_run_migrations` mutates
    it, which would otherwise leak the throwaway DB into later tests.
    """
    base_async = os.environ["DATABASE_URL"]
    base_sync = base_async.replace("+asyncpg", "+psycopg2")
    saved_env = os.environ.get("DATABASE_URL")
    dbname = f"advtest_{uuid.uuid4().hex[:8]}"

    admin = create_engine(base_sync, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'CREATE DATABASE "{dbname}"'))
    admin.dispose()

    try:
        yield swap_db(base_async, dbname), swap_db(base_sync, dbname)
    finally:
        if saved_env is not None:
            os.environ["DATABASE_URL"] = saved_env
        admin = create_engine(base_sync, isolation_level="AUTOCOMMIT")
        with admin.connect() as c:
            # Only client backends: this sweep exists to kill leaked test
            # connection pools. Autovacuum workers run as the superuser cluster
            # owner, which a non-superuser test role cannot signal; DROP
            # DATABASE evicts them itself.
            c.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :d AND pid <> pg_backend_pid() "
                    "AND backend_type = 'client backend'"
                ),
                {"d": dbname},
            )
            c.execute(text(f'DROP DATABASE IF EXISTS "{dbname}"'))
        admin.dispose()


def wc_mkcompany(conn, settings: dict) -> str:
    """Insert one company with the given settings; return its id."""
    cid = str(uuid.uuid4())
    conn.execute(text("INSERT INTO companies (id, name, settings) VALUES (:id, :n, CAST(:s AS json))"),
                 {"id": cid, "n": "Co", "s": json.dumps(settings)})
    return cid


def wc_add_work_center(conn, company_id: str, name: str) -> None:
    """Insert a plain (non-default) work center for a company."""
    conn.execute(text(
        "INSERT INTO work_centers (id, company_id, name, created_at) "
        "VALUES (gen_random_uuid(), :cid, :n, NOW())"),
        {"cid": company_id, "n": name})


@pytest.fixture()
def wc_db():
    """Isolated Postgres schema with companies, a 7-column work_centers (the
    pre-migration shape: no is_default/hours_per_day), and notifications.

    The schema migration adds the two columns and the partial index; the data
    migration seeds/notifies/strips. Two migrations run against one schema, so a
    test can compose them exactly as alembic would.
    """
    base_url = os.environ["DATABASE_URL"].replace("+asyncpg", "+psycopg2")
    schema = f"wcmig_{uuid.uuid4().hex[:8]}"

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
