# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""The session company revision is the one head. It clears sessions that cannot be
attributed to a company, ties every new session to its company so removing the company
ends them, and reverses cleanly."""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from .conftest import run_migration_ops

MODULE = "p3e4f5a6b7c8_session_registry_company"


def test_revision_is_the_single_head_after_import_operation_key():
    from alembic.script import ScriptDirectory

    from celerp.alembic_config import build_alembic_config

    script = ScriptDirectory.from_config(build_alembic_config())
    assert script.get_heads() == ["p3e4f5a6b7c8"]
    assert script.get_revision("p3e4f5a6b7c8").down_revision == "o2d3e4f5a6b7"
    ids = [r.revision for r in script.walk_revisions()]
    assert len(ids) == len(set(ids)), "a revision id is declared twice"


@pytest.fixture()
def sessions_db():
    base_url = os.environ["DATABASE_URL"].replace("+asyncpg", "+psycopg2")
    schema = f"migsess_{uuid.uuid4().hex[:8]}"
    admin = create_engine(base_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'CREATE SCHEMA "{schema}"'))
    admin.dispose()
    engine = create_engine(base_url, connect_args={"options": f"-csearch_path={schema}"})
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE companies (id UUID PRIMARY KEY)"))
        conn.execute(text("CREATE TABLE session_registry (jti VARCHAR(64) PRIMARY KEY, user_id UUID NOT NULL, "
                          "expiry TIMESTAMPTZ NOT NULL)"))
    yield engine
    engine.dispose()
    admin = create_engine(base_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    admin.dispose()


def _later():
    return datetime.now(timezone.utc) + timedelta(hours=1)


def test_session_company_upgrade_and_downgrade(sessions_db):
    a, b = uuid.uuid4(), uuid.uuid4()
    with sessions_db.begin() as conn:
        conn.execute(text("INSERT INTO companies (id) VALUES (:a), (:b)"), {"a": a, "b": b})
        conn.execute(text("INSERT INTO session_registry (jti, user_id, expiry) VALUES ('old', :u, :e)"),
                     {"u": uuid.uuid4(), "e": _later()})

    run_migration_ops(sessions_db, MODULE)
    with sessions_db.begin() as conn:
        # A session from before cannot be attributed to a company and is cleared.
        assert conn.execute(text("SELECT count(*) FROM session_registry")).scalar_one() == 0
        for jti, cid in (("on-a", a), ("on-b", b)):
            conn.execute(text("INSERT INTO session_registry (jti, user_id, company_id, expiry) "
                              "VALUES (:j, :u, :c, :e)"), {"j": jti, "u": uuid.uuid4(), "c": cid, "e": _later()})
        conn.execute(text("DELETE FROM companies WHERE id = :a"), {"a": a})
        assert list(conn.execute(text("SELECT jti FROM session_registry")).scalars()) == ["on-b"]
    with pytest.raises(IntegrityError):
        with sessions_db.begin() as conn:
            conn.execute(text("INSERT INTO session_registry (jti, user_id, expiry) VALUES ('none', :u, :e)"),
                         {"u": uuid.uuid4(), "e": _later()})

    run_migration_ops(sessions_db, MODULE, "downgrade")
    with sessions_db.connect() as conn:
        cols = set(conn.execute(text(
            "SELECT column_name FROM information_schema.columns WHERE table_schema = current_schema() "
            "AND table_name = 'session_registry'")).scalars())
    assert "company_id" not in cols
