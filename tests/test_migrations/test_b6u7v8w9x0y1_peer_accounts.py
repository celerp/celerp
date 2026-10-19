# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""The account links revision adds the table linking a connected company's accounts to
this company's and the fix a held record needs, and reverses to the older shape."""

from __future__ import annotations

from .conftest import schema_of, throwaway_db, upgrade_to

REVISION = "b6u7v8w9x0y1"


def _columns(sync_url: str, table: str) -> set[str]:
    return {column for name, column, *_ in schema_of(sync_url)["columns"] if name == table}


def test_peer_accounts_revision_up_and_down():
    with throwaway_db("peeraccounts") as (_, sync_url):
        upgrade_to(sync_url, "a5t6u7v8w9x0")
        assert not _columns(sync_url, "company_peer_accounts")
        assert "fix" not in _columns(sync_url, "company_peer_attention")
        upgrade_to(sync_url, REVISION)
        assert {"company_id", "source_company_id", "other_account_id", "other_code", "other_name",
                "account_id", "code", "how", "added_from"} <= _columns(sync_url, "company_peer_accounts")
        assert "fix" in _columns(sync_url, "company_peer_attention")
        from alembic import command

        from celerp.alembic_config import build_alembic_config
        command.downgrade(build_alembic_config(), "a5t6u7v8w9x0")
        assert not _columns(sync_url, "company_peer_accounts")
        assert "fix" not in _columns(sync_url, "company_peer_attention")
