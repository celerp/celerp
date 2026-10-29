# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Password resets are recorded, and the reset-link columns go.

A real upgrade on Postgres from the previous head: the reset record and notice
tables exist afterwards, the two reset-link columns on users are gone, and every
user keeps their row and password. The tables come from the models at start too, so
each case also runs on a database this version started on before the upgrade.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import create_engine, text

from celerp.cli import _apply_migrations

from alembic_support import alembic_run

from .conftest import throwaway_db, upgrade_to

REVISION = "y3p4q5r6s7t8"
PARENT = "x2o3p4q5r6s7"
RESET_COLUMNS = {"reset_token", "reset_token_expires"}


def test_revision_follows_its_parent():
    from alembic.script import ScriptDirectory

    from celerp.alembic_config import build_alembic_config

    assert ScriptDirectory.from_config(build_alembic_config()).get_revision(REVISION).down_revision == PARENT


def _at_parent(sync_url: str, started_first: bool) -> dict[str, str]:
    """A database at the previous head with two users, one holding a reset link.
    started_first: this version started once before the upgrade ran, so its models
    already created the reset tables next to the old columns."""
    upgrade_to(sync_url, PARENT)
    users = {"alice": str(uuid.uuid4()), "bob": str(uuid.uuid4())}
    eng = create_engine(sync_url)
    try:
        with eng.begin() as conn:
            for key, user_id in users.items():
                conn.execute(text(
                    "INSERT INTO users (id, email, name, auth_hash, is_active, created_at, reset_token, "
                    "reset_token_expires) VALUES (:u, :e, 'User', :h, true, now(), :t, :x)"),
                    {"u": user_id, "e": f"{key}@example.com", "h": f"hash-{key}",
                     "t": "link-token" if key == "alice" else None,
                     "x": None})
            if started_first:
                from celerp.models.auth import PasswordReset, PasswordResetNotice
                PasswordReset.__table__.create(conn)
                PasswordResetNotice.__table__.create(conn)
    finally:
        eng.dispose()
    return users


def _state(sync_url: str) -> tuple[set, set, set]:
    eng = create_engine(sync_url)
    try:
        with eng.connect() as conn:
            users = {tuple(map(str, r)) for r in conn.execute(text("SELECT id, email, auth_hash FROM users"))}
            columns = set(conn.execute(text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'users'")).scalars())
            tables = {t for t in ("password_resets", "password_reset_notices")
                      if conn.execute(text("SELECT to_regclass(:t)"), {"t": t}).scalar() is not None}
    finally:
        eng.dispose()
    return users, columns, tables


def _expected_users(users: dict[str, str]) -> set:
    return {(user_id, f"{key}@example.com", f"hash-{key}") for key, user_id in users.items()}


@pytest.mark.parametrize("path", ["alembic", "celerp migrate"])
@pytest.mark.parametrize("started_first", [False, True], ids=["upgrade", "started-before-upgrade"])
def test_migration_drops_reset_columns_and_keeps_users(started_first, path):
    with throwaway_db("pwresets") as (async_url, sync_url):
        users = _at_parent(sync_url, started_first)
        if path == "alembic":
            upgrade_to(sync_url, REVISION)
        else:
            _apply_migrations(async_url)
        rows, columns, tables = _state(sync_url)

        # Running it again finds nothing to do.
        if path == "celerp migrate":
            _apply_migrations(async_url)
            assert _state(sync_url) == (rows, columns, tables)

    assert rows == _expected_users(users)
    assert not RESET_COLUMNS & columns
    assert tables == {"password_resets", "password_reset_notices"}


def test_downgrade_and_upgrade_keep_users():
    """Back on the previous head the columns are there again, empty, and the users and
    their passwords are untouched; upgrading again removes them once more."""


    with throwaway_db("pwresets") as (_, sync_url):
        users = _at_parent(sync_url, started_first=False)
        upgrade_to(sync_url, REVISION)
        alembic_run(sync_url, "downgrade", PARENT)
        rows, columns, tables = _state(sync_url)
        assert rows == _expected_users(users)
        assert RESET_COLUMNS <= columns
        assert tables == set()
        upgrade_to(sync_url, REVISION)
        rows, columns, tables = _state(sync_url)

    assert rows == _expected_users(users)
    assert not RESET_COLUMNS & columns
    assert tables == {"password_resets", "password_reset_notices"}
