# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""The GL Impact cost-scope lookup index. An INVALID index of its name, what a cancelled
concurrent build leaves, is rebuilt before the revision is stamped; and a database stamped at
the revision whose index is missing (a build that failed at an earlier start and was dropped)
is restamped below it at the next start, which builds it."""

from __future__ import annotations

from sqlalchemy import create_engine, text

from alembic_support import apply_migrations
from .conftest import throwaway_db, upgrade_to

REVISION = "t3u4v5w6x7y8"
DOWN_REVISION = "r1s2t3u4v5w6"
INDEX = "ix_ledger_je_cost_scope"
_VALID = "SELECT indisvalid FROM pg_index WHERE indexrelid = to_regclass(:name)"


def _query(sync_url: str, sql: str):
    engine = create_engine(sync_url, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as conn:
            result = conn.execute(text(sql), {"name": INDEX})
            return result.scalar() if result.returns_rows else None
    finally:
        engine.dispose()


def test_an_invalid_cost_scope_index_is_rebuilt():
    with throwaway_db("costscopeidx") as (_, sync_url):
        upgrade_to(sync_url, DOWN_REVISION)
        _query(sync_url, "CREATE INDEX ix_ledger_je_cost_scope ON ledger (company_id, entity_id text_pattern_ops) "
                         "WHERE event_type = 'acc.journal_entry.created'")
        _query(sync_url, "UPDATE pg_index SET indisvalid = false WHERE indexrelid = to_regclass(:name)")
        assert _query(sync_url, _VALID) is False

        upgrade_to(sync_url, REVISION)

        assert _query(sync_url, _VALID) is True
        assert _query(sync_url, "SELECT count(*) FROM pg_class WHERE relname = :name") == 1
        assert _query(sync_url, "SELECT version_num FROM alembic_version") == REVISION


def test_a_stamped_database_missing_the_cost_scope_index_rebuilds_it_next_start():
    with throwaway_db("costscoperetry") as (async_url, sync_url):
        upgrade_to(sync_url, REVISION)
        # A build that failed at an earlier start: the revision is stamped, the index dropped.
        _query(sync_url, "DROP INDEX ix_ledger_je_cost_scope")
        assert _query(sync_url, _VALID) is None

        apply_migrations(async_url)

        assert _query(sync_url, _VALID) is True
        assert _query(sync_url, "SELECT version_num FROM alembic_version") == REVISION
