# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""The 6960 backfill for companies that predate foreign-currency entries.

The shared mig_db fixture builds a projections table; this migration writes to
accounts, so the fixture here builds that table instead, in the same style:
an isolated schema and a MagicMock standing in for alembic.op.
"""

from __future__ import annotations

import importlib
import os
import sys
import uuid
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine, text

MODULE = "celerp.migrations.versions.f6a7b8c9d0e1_seed_fx_rounding_account"


class AccountsDB:
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

    def _run(self, fn: str) -> None:
        with self.engine.connect() as conn:
            with conn.begin():
                mock_op = MagicMock()
                mock_op.get_bind.return_value = conn
                if MODULE in sys.modules:
                    del sys.modules[MODULE]
                with patch.dict("sys.modules", {"alembic.op": mock_op}):
                    mig = importlib.import_module(MODULE)
                    with patch.object(mig, "op", mock_op):
                        getattr(mig, fn)()

    def upgrade(self) -> None:
        self._run("upgrade")

    def downgrade(self) -> None:
        self._run("downgrade")


@pytest.fixture()
def acc_db():
    base_url = os.environ["DATABASE_URL"].replace("+asyncpg", "+psycopg2")
    schema = f"fxmig_{uuid.uuid4().hex[:8]}"

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


def test_backfills_account_for_company_with_operating_expenses_parent(acc_db):
    cid = str(uuid.uuid4())
    acc_db.add_account(cid, "6000", "Operating Expenses")
    acc_db.upgrade()

    row = acc_db.get(cid, "6960")
    assert row is not None
    assert row.name == "Foreign Exchange Rounding"
    assert row.account_type == "expense"
    assert row.parent_code == "6000"


def test_skips_company_that_already_has_the_account(acc_db):
    """A company that already has 6960 keeps whatever it named it."""
    cid = str(uuid.uuid4())
    acc_db.add_account(cid, "6000", "Operating Expenses")
    acc_db.add_account(cid, "6960", "FX Rounding (renamed)", parent_code="6000")
    acc_db.upgrade()

    assert acc_db.get(cid, "6960").name == "FX Rounding (renamed)"


def test_skips_company_without_the_parent_account(acc_db):
    """No 6000 means a chart this migration does not understand; leave it be."""
    cid = str(uuid.uuid4())
    acc_db.add_account(cid, "1111", "Bank", account_type="asset")
    acc_db.upgrade()

    assert acc_db.get(cid, "6960") is None


def test_downgrade_removes_the_seeded_row(acc_db):
    cid = str(uuid.uuid4())
    acc_db.add_account(cid, "6000", "Operating Expenses")
    acc_db.upgrade()
    acc_db.downgrade()

    assert acc_db.get(cid, "6960") is None
