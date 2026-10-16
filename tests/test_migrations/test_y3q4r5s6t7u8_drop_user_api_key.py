# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Per-user API keys are gone: a company connects through an expiring, revocable peer key
instead. The column, its unique index and the route that minted it are removed, and the
revision reverses to the older shape."""

from __future__ import annotations

from .conftest import schema_of, throwaway_db, upgrade_to

REVISION = "y3q4r5s6t7u8"


def _user_columns(sync_url: str) -> set[str]:
    return {column for table, column, *_ in schema_of(sync_url)["columns"] if table == "users"}


def _user_indexes(sync_url: str) -> set[str]:
    return {name for table, name, _ in schema_of(sync_url)["indexes"] if table == "users"}


def test_user_api_key_removed():
    import celerp.main
    from celerp.models.company import User

    assert not hasattr(User, "api_key")
    assert not [r for r in celerp.main.app.routes if getattr(r, "path", "") == "/auth/api-key"]
    with throwaway_db("apikey") as (_, sync_url):
        upgrade_to(sync_url, "x2p3q4r5s6t7")
        assert "api_key" in _user_columns(sync_url) and "uq_user_api_key" in _user_indexes(sync_url)
        upgrade_to(sync_url, REVISION)
        assert "api_key" not in _user_columns(sync_url)
        assert "uq_user_api_key" not in _user_indexes(sync_url)
        from alembic import command

        from celerp.alembic_config import build_alembic_config
        command.downgrade(build_alembic_config(), "x2p3q4r5s6t7")
        assert "api_key" in _user_columns(sync_url) and "uq_user_api_key" in _user_indexes(sync_url)
