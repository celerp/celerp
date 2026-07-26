# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""A brand-new install migrates to head.

This is the first thing that happens to a database on a machine that has never
run Celerp: `celerp migrate` against an empty database, before the API has ever
started. Nothing else in the migration tests covers it. The dev-to-release tests
next door all call `Base.metadata.create_all` first, so they start from a database
that already has every table, which is the upgrade path, not the install path.

The gap that leaves matters because the two paths disagree about which tables
exist. Core migrations run first, and a module's tables do not exist yet at that
point: they are created from the models when the API loads its modules
(celerp/main.py:189-192). So a core migration that touches a module-owned table
finds it on an upgrade and not on an install. The established answer is to check
for the table and return when it is absent (f6a7b8c9d0e1, t5u6v7w8x9y0), because
the module's own model already carries the change and create_all applies it.

A migration that forgets the check raises UndefinedTable, `celerp migrate` exits
non-zero, and the app never opens on a new machine. That is invisible to every
other test in the suite and to any developer whose database already exists, which
is everyone after their first run. Hence this test: it fails for any migration
that reaches for a table an install has not created yet, not just the one that
prompted it.
"""

from __future__ import annotations

from sqlalchemy import create_engine, text

from celerp.cli import _apply_migrations

from .conftest import head_rev


def test_empty_database_migrates_to_head(fresh_db):
    """`celerp migrate` on an untouched database reaches head without error.

    Asserts the stamp really landed on head rather than trusting the absence of
    an exception: the upgrade runner stamps past revisions whose DDL is already
    present, so "did not raise" alone would not prove every migration ran.
    """
    async_url, sync_url = fresh_db

    _apply_migrations(async_url)

    eng = create_engine(sync_url)
    try:
        with eng.connect() as conn:
            stamped = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
    finally:
        eng.dispose()

    assert stamped == head_rev()
