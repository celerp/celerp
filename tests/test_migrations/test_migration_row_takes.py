# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Every revision's data step takes its tables before their rows.

A migration can run inside a serving process (System Recovery reconciles a restored schema),
so a data step that held a company's row while it waited for its next statement could meet a
request holding that company's lock and waiting for the same row. The lock checker counts every
such take; this walk runs every revision up, and this release's down and up again, over a
database that carries data, and requires that none of them made one.
"""
from __future__ import annotations

import sys

from alembic_support import alembic_run, apply_migrations

from .conftest import throwaway_db, upgrade_to
from .test_alembic_graph import RELEASE_HEAD, _populate


def test_no_revision_takes_a_company_row_outside_a_table_lock():
    with throwaway_db("rowtakes") as (async_url, sync_url), \
            sys.modules["lock_rank"].ungated_takes(None, "celerp/migrations/") as found:
        upgrade_to(sync_url, RELEASE_HEAD)
        _populate(sync_url)
        apply_migrations(async_url)
        alembic_run(sync_url, "downgrade", RELEASE_HEAD)
        apply_migrations(async_url)
    assert sorted(found) == [], "\n".join(sorted(found))
