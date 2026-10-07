# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""The import operation key revision follows the migration runs revisions directly,
and adds and removes its column and unique index cleanly."""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from .conftest import run_migration_ops

MODULE = "o2d3e4f5a6b7_add_import_batch_operation_key"


def test_revision_follows_migration_runs():
    from alembic.script import ScriptDirectory

    from celerp.alembic_config import build_alembic_config

    script = ScriptDirectory.from_config(build_alembic_config())
    assert script.get_revision("o2d3e4f5a6b7").down_revision == "n1c2d3e4f5a6"


@pytest.fixture()
def batches_db():
    base_url = os.environ["DATABASE_URL"].replace("+asyncpg", "+psycopg2")
    schema = f"migopkey_{uuid.uuid4().hex[:8]}"
    admin = create_engine(base_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'CREATE SCHEMA "{schema}"'))
    admin.dispose()
    engine = create_engine(base_url, connect_args={"options": f"-csearch_path={schema}"})
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE import_batches (id UUID PRIMARY KEY, company_id UUID NOT NULL)"))
    yield engine
    engine.dispose()
    admin = create_engine(base_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    admin.dispose()


def test_operation_key_upgrade_and_downgrade(batches_db):
    company = uuid.uuid4()
    with batches_db.begin() as conn:
        conn.execute(text("INSERT INTO import_batches (id, company_id) VALUES (:i, :c)"),
                     {"i": uuid.uuid4(), "c": company})

    run_migration_ops(batches_db, MODULE)
    with batches_db.begin() as conn:
        assert conn.execute(text("SELECT operation_key FROM import_batches")).scalar_one() is None
        conn.execute(text("INSERT INTO import_batches (id, company_id, operation_key) VALUES (:i, :c, 'k')"),
                     {"i": uuid.uuid4(), "c": company})
    with pytest.raises(IntegrityError):
        with batches_db.begin() as conn:
            conn.execute(text("INSERT INTO import_batches (id, company_id, operation_key) VALUES (:i, :c, 'k')"),
                         {"i": uuid.uuid4(), "c": company})

    run_migration_ops(batches_db, MODULE, "downgrade")
    with batches_db.connect() as conn:
        cols = set(conn.execute(text(
            "SELECT column_name FROM information_schema.columns WHERE table_schema = current_schema() "
            "AND table_name = 'import_batches'")).scalars())
    assert "operation_key" not in cols
