# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""The ledger document-lookup revision replaces an INVALID index of its name, the state a
cancelled CREATE INDEX CONCURRENTLY leaves behind, with a valid one before it is stamped. An
invalid index is never used by a query, so keeping it would leave GL lookups unindexed."""

from __future__ import annotations

from sqlalchemy import create_engine, text

from .conftest import throwaway_db, upgrade_to

REVISION = "p9q0r1s2t3u4"
DOWN_REVISION = "m8a9b0c1d2e3"
INDEX = "ix_ledger_je_doc_refs_gin"


def _query(sync_url: str, sql: str):
    engine = create_engine(sync_url, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as conn:
            result = conn.execute(text(sql), {"name": INDEX})
            return result.scalar() if result.returns_rows else None
    finally:
        engine.dispose()


def test_upgrade_rebuilds_an_invalid_index_and_stamps():
    with throwaway_db("ledgerdocidx") as (_, sync_url):
        upgrade_to(sync_url, DOWN_REVISION)
        # What a cancelled concurrent build leaves: the index exists, marked invalid.
        _query(sync_url, "CREATE INDEX ix_ledger_je_doc_refs_gin ON ledger USING gin "
                         "((metadata::jsonb) jsonb_path_ops) WHERE event_type = 'acc.journal_entry.created'")
        _query(sync_url, "UPDATE pg_index SET indisvalid = false WHERE indexrelid = to_regclass(:name)")
        assert _query(sync_url, "SELECT indisvalid FROM pg_index WHERE indexrelid = to_regclass(:name)") is False

        upgrade_to(sync_url, REVISION)

        assert _query(sync_url, "SELECT indisvalid FROM pg_index WHERE indexrelid = to_regclass(:name)") is True
        assert _query(sync_url, "SELECT count(*) FROM pg_class WHERE relname = :name") == 1
        assert _query(sync_url, "SELECT version_num FROM alembic_version") == REVISION
