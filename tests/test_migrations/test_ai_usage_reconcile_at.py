# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The usage record's reconcile time. Every row gets one, NOT NULL: a row from before
the upgrade belongs to a run the restart already ended, so it is due at once
(reconcile_at = created_at). The downgrade drops the column."""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from .conftest import run_migration_ops

VERSIONS = Path(__file__).resolve().parents[2] / "celerp" / "migrations" / "versions"


def _module() -> str:
    found = [p.stem for p in VERSIONS.glob("*.py") if "reconcile_at" in p.read_text()]
    assert len(found) == 1, f"one migration adds ai_usage.reconcile_at, found {found}"
    return found[0]


def test_revision_follows_ai_jobs_on_the_one_line_of_revisions():
    from alembic.script import ScriptDirectory

    from celerp.alembic_config import build_alembic_config

    script = ScriptDirectory.from_config(build_alembic_config())
    (head,) = script.get_heads()
    revision = script.get_revision(_module().split("_", 1)[0])
    assert revision.down_revision == "h4c5d6e7f8a9"
    assert revision.revision in {r.revision for r in script.iterate_revisions(head, "base")}


@pytest.fixture()
def usage_db():
    base_url = os.environ["DATABASE_URL"].replace("+asyncpg", "+psycopg2")
    schema = f"migusage_{uuid.uuid4().hex[:8]}"
    admin = create_engine(base_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'CREATE SCHEMA "{schema}"'))
    admin.dispose()
    engine = create_engine(base_url, connect_args={"options": f"-csearch_path={schema}"})
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE ai_usage (id UUID PRIMARY KEY, company_id UUID NOT NULL, "
                          "user_id UUID NOT NULL, operation_id UUID NOT NULL, credits INTEGER NOT NULL, "
                          "pending BOOLEAN NOT NULL, created_at TIMESTAMPTZ NOT NULL)"))
    yield engine
    engine.dispose()
    admin = create_engine(base_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    admin.dispose()


def _nullable(conn) -> str | None:
    return conn.execute(text("SELECT is_nullable FROM information_schema.columns WHERE table_schema = "
                             "current_schema() AND table_name = 'ai_usage' AND column_name = 'reconcile_at'")
                        ).scalar()


def test_upgrade_backfills_reconcile_at_and_downgrade_drops_it(usage_db):
    created = datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)
    with usage_db.begin() as conn:
        conn.execute(text("INSERT INTO ai_usage VALUES (:id, :c, :u, :op, 0, true, :at)"),
                     {"id": uuid.uuid4(), "c": uuid.uuid4(), "u": uuid.uuid4(), "op": uuid.uuid4(), "at": created})
    run_migration_ops(usage_db, _module())
    with usage_db.connect() as conn:
        assert conn.execute(text("SELECT reconcile_at FROM ai_usage")).scalar() == created
        assert _nullable(conn) == "NO"
    run_migration_ops(usage_db, _module(), "downgrade")
    with usage_db.connect() as conn:
        assert _nullable(conn) is None
