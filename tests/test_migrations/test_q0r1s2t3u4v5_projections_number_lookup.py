# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""The numbering lookup revision keeps a valid index a start of this version already built from
the models, and replaces an INVALID index of its name, the state a cancelled CREATE INDEX
CONCURRENTLY leaves behind, with a valid one before it is stamped. An invalid index is never used
by a query, so keeping it would leave numbering reading every record of the company."""

from __future__ import annotations

from sqlalchemy import create_engine, text

from .conftest import throwaway_db, upgrade_to

REVISION = "q0r1s2t3u4v5"
DOWN_REVISION = "p9q0r1s2t3u4"
INDEXES = ("ix_projections_number_shown", "ix_projections_number_kept", "ix_projections_number_key")


def _query(sync_url: str, sql: str, name: str | None = None):
    engine = create_engine(sync_url, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as conn:
            result = conn.execute(text(sql), {"name": name})
            return result.scalar() if result.returns_rows else None
    finally:
        engine.dispose()


def _valid(sync_url: str, name: str):
    return _query(sync_url, "SELECT indisvalid FROM pg_index WHERE indexrelid = to_regclass(:name)", name)


def test_upgrade_rebuilds_an_invalid_index_keeps_a_valid_one_and_stamps():
    with throwaway_db("numberidx") as (_, sync_url):
        upgrade_to(sync_url, DOWN_REVISION)
        # A start before the upgrade built the first index from the models; a cancelled
        # concurrent build left the second one invalid; the third is missing.
        _query(sync_url, "CREATE INDEX ix_projections_number_shown ON projections "
                         "(company_id, (lower(state ->> 'ref_id')))")
        kept = _query(sync_url, "SELECT to_regclass('ix_projections_number_shown')::oid")
        _query(sync_url, "CREATE INDEX ix_projections_number_kept ON projections "
                         "(company_id, (lower(state ->> 'doc_number')))")
        _query(sync_url, "UPDATE pg_index SET indisvalid = false WHERE indexrelid = to_regclass(:name)",
               "ix_projections_number_kept")
        assert _valid(sync_url, "ix_projections_number_kept") is False

        upgrade_to(sync_url, REVISION)

        assert [_valid(sync_url, name) for name in INDEXES] == [True, True, True]
        assert _query(sync_url, "SELECT to_regclass('ix_projections_number_shown')::oid") == kept
        assert _query(sync_url, "SELECT version_num FROM alembic_version") == REVISION
