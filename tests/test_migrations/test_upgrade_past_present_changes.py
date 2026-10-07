# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""An update whose later change is already in the database still applies every
earlier one.

`celerp migrate` steps past a revision whose change a database already holds (a
develop build, or a branch build that ran it first). A database stamped several
revisions back can hold the newest revision's column while missing the ones before
it. Every pending revision has to run except the one that is already there; skipping
any other leaves the database short of tables the code reads, which surfaces only
later as a missing-table error.
"""

from __future__ import annotations

from sqlalchemy import create_engine, text

from celerp.cli import _apply_migrations

from .conftest import schema_of, throwaway_db, upgrade_to


def _upgrade_matches_fresh(stamped_at: str, *present: str) -> None:
    """Stamp a database at ``stamped_at``, apply the ``present`` statements of a later
    revision by hand, then upgrade it and a fresh database alike."""
    with throwaway_db("present") as (old_async, old_sync), throwaway_db("fresh") as (new_async, new_sync):
        upgrade_to(old_sync, stamped_at)
        eng = create_engine(old_sync)
        try:
            with eng.begin() as conn:
                for statement in present:
                    conn.execute(text(statement))
        finally:
            eng.dispose()

        _apply_migrations(old_async)
        _apply_migrations(new_async)

        assert schema_of(old_sync) == schema_of(new_sync)


def test_only_the_change_already_present_is_skipped():
    # Stamped three revisions before the import operation key, already holding that
    # revision's column and index, and none of the revisions in between.
    _upgrade_matches_fresh(
        "k8f9a0b1c2d3",
        "ALTER TABLE import_batches ADD COLUMN operation_key TEXT",
        "CREATE UNIQUE INDEX uq_import_batch_company_operation ON import_batches (company_id, operation_key)")


def test_a_later_release_change_already_present_is_skipped():
    # Stamped at an earlier release, already holding the import reversibility column a
    # later revision adds, and none of the revisions in between.
    _upgrade_matches_fresh(
        "o2d3e4f5a6b7", "ALTER TABLE import_batches ADD COLUMN reversible BOOLEAN DEFAULT false NOT NULL")
