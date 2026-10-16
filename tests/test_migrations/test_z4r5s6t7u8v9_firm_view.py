# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""The firm view's revision adds when a key was last used and the table of records a
connected company sent that were not applied, and reverses to the older shape."""

from __future__ import annotations

from .conftest import schema_of, throwaway_db, upgrade_to

REVISION = "z4r5s6t7u8v9"


def _columns(sync_url: str, table: str) -> set[str]:
    return {column for name, column, *_ in schema_of(sync_url)["columns"] if name == table}


def test_firm_view_revision_up_and_down():
    with throwaway_db("firmview") as (_, sync_url):
        upgrade_to(sync_url, "y3q4r5s6t7u8")
        assert "last_used_at" not in _columns(sync_url, "company_peer_grants")
        assert not _columns(sync_url, "company_peer_attention")
        upgrade_to(sync_url, REVISION)
        assert "last_used_at" in _columns(sync_url, "company_peer_grants")
        assert {"company_id", "source_company_id", "kind", "entity_type", "source_entity_id",
                "label", "reason", "created_at"} <= _columns(sync_url, "company_peer_attention")
        from alembic import command

        from celerp.alembic_config import build_alembic_config
        command.downgrade(build_alembic_config(), "y3q4r5s6t7u8")
        assert "last_used_at" not in _columns(sync_url, "company_peer_grants")
        assert not _columns(sync_url, "company_peer_attention")
