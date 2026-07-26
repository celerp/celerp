# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Shared harness for migration tests.

Runs against Postgres (the production engine) so each migration's real SQL is
exercised. alembic op.get_bind() is sync, so a sync psycopg2 engine is used,
with every test isolated in its own schema. Two tables get a harness each,
because a migration writes to one or the other: projections for item state,
accounts for the chart.
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
