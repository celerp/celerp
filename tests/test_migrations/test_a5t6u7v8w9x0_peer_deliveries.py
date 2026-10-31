# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""The two-way sync revision adds the table of revisions sent to or declined from a
connected company, and reverses to the older shape."""

from __future__ import annotations

from alembic_support import alembic_run

from .conftest import schema_of, throwaway_db, upgrade_to

REVISION = "a5t6u7v8w9x0"


def _columns(sync_url: str, table: str) -> set[str]:
    return {column for name, column, *_ in schema_of(sync_url)["columns"] if name == table}


def test_peer_deliveries_revision_up_and_down():
    with throwaway_db("peerdeliveries") as (_, sync_url):
        upgrade_to(sync_url, "z4r5s6t7u8v9")
        assert not _columns(sync_url, "company_peer_deliveries")
        upgrade_to(sync_url, REVISION)
        assert {"company_id", "grant_id", "entity", "entity_id", "revision", "refused",
                "at"} <= _columns(sync_url, "company_peer_deliveries")
        alembic_run(sync_url, "downgrade", "z4r5s6t7u8v9")
        assert not _columns(sync_url, "company_peer_deliveries")
