# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""The import reversibility revision follows the unmatched refunds revision directly, records every
import that existed before it as not reversible, and removes its column cleanly."""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import create_engine, text

from .conftest import run_migration_ops

MODULE = "t7i8d9e0f1a2_import_batch_reversible"


def test_revision_follows_its_parent():
    from alembic.script import ScriptDirectory

    from celerp.alembic_config import build_alembic_config

    script = ScriptDirectory.from_config(build_alembic_config())
    assert script.get_revision("t7i8d9e0f1a2").down_revision == "s6h7c8d9e0f1"


@pytest.fixture()
def batches_db():
    base_url = os.environ["DATABASE_URL"].replace("+asyncpg", "+psycopg2")
    schema = f"migrev_{uuid.uuid4().hex[:8]}"
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


def _columns(engine) -> set[str]:
    with engine.connect() as conn:
        return set(conn.execute(text(
            "SELECT column_name FROM information_schema.columns WHERE table_schema = current_schema() "
            "AND table_name = 'import_batches'")).scalars())


def test_existing_imports_become_not_reversible_and_downgrade_drops_the_column(batches_db):
    company = uuid.uuid4()
    with batches_db.begin() as conn:
        conn.execute(text("INSERT INTO import_batches (id, company_id) VALUES (:i, :c)"),
                     {"i": uuid.uuid4(), "c": company})

    run_migration_ops(batches_db, MODULE)
    with batches_db.begin() as conn:
        assert conn.execute(text("SELECT reversible FROM import_batches")).scalars().all() == [False]
        # A batch written without the field is not reversible either.
        conn.execute(text("INSERT INTO import_batches (id, company_id) VALUES (:i, :c)"),
                     {"i": uuid.uuid4(), "c": company})
        assert conn.execute(text("SELECT bool_or(reversible) FROM import_batches")).scalar_one() is False

    run_migration_ops(batches_db, MODULE, "downgrade")
    assert "reversible" not in _columns(batches_db)
