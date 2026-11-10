# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""The trimmed numbering lookup revision keeps a valid index a start of this version already
built from the models, rebuilds an INVALID index of its name, builds a missing one, and drops the
indexes on the untrimmed expressions no query reads any more. A downgrade puts them back."""

from __future__ import annotations

from alembic_support import alembic_run

from .conftest import throwaway_db, upgrade_to
from .test_q0r1s2t3u4v5_projections_number_lookup import INDEXES as UNTRIMMED, _query, _valid

REVISION = "r1s2t3u4v5w6"
DOWN_REVISION = "o0c1d2e3f4a5"
INDEXES = ("ix_projections_canonical_number_shown", "ix_projections_canonical_number_kept",
           "ix_projections_canonical_number_key")


def test_upgrade_rebuilds_an_invalid_index_keeps_a_valid_one_drops_the_untrimmed_and_stamps():
    with throwaway_db("trimidx") as (_, sync_url):
        upgrade_to(sync_url, DOWN_REVISION)
        assert [_valid(sync_url, name) for name in UNTRIMMED] == [True, True, True]
        # A start before the upgrade built the first index from the models; a cancelled
        # concurrent build left the second one invalid; the third is missing.
        _query(sync_url, "CREATE INDEX ix_projections_canonical_number_shown ON projections "
                         "(company_id, (trim(lower(state ->> 'ref_id'))))")
        kept = _query(sync_url, "SELECT to_regclass('ix_projections_canonical_number_shown')::oid")
        _query(sync_url, "CREATE INDEX ix_projections_canonical_number_kept ON projections "
                         "(company_id, (trim(lower(state ->> 'doc_number'))))")
        _query(sync_url, "UPDATE pg_index SET indisvalid = false WHERE indexrelid = to_regclass(:name)",
               "ix_projections_canonical_number_kept")

        upgrade_to(sync_url, REVISION)

        assert [_valid(sync_url, name) for name in INDEXES] == [True, True, True]
        assert _query(sync_url, "SELECT to_regclass('ix_projections_canonical_number_shown')::oid") == kept
        assert [_valid(sync_url, name) for name in UNTRIMMED] == [None, None, None]
        assert _query(sync_url, "SELECT version_num FROM alembic_version") == REVISION

        alembic_run(sync_url, "downgrade", DOWN_REVISION)

        assert [_valid(sync_url, name) for name in UNTRIMMED] == [True, True, True]
        assert [_valid(sync_url, name) for name in INDEXES] == [None, None, None]
