# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Notices move from one shared read flag to a read receipt per user.

A real alembic upgrade on Postgres from the previous head: a personal notice its
user had read keeps that state as a receipt, an unread one stays unread, a
company-wide notice already read stays read for every current member of its company,
and the shared column is gone. The notices table comes from the models at start, so
each test builds it as the previous release did.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import create_engine, text

from celerp.cli import _apply_migrations

from .conftest import throwaway_db, upgrade_to

REVISION = "w1n2o3p4q5r6"
PARENT = "v0m1n2o3p4q5"


def test_revision_follows_its_parent():
    from alembic.script import ScriptDirectory

    from celerp.alembic_config import build_alembic_config

    assert ScriptDirectory.from_config(build_alembic_config()).get_revision(REVISION).down_revision == PARENT


# The notices table as the previous release's models created it at start.
_OLD_NOTIFICATIONS = """
    CREATE TABLE notifications (
        id UUID PRIMARY KEY,
        company_id UUID NOT NULL REFERENCES companies (id),
        user_id UUID REFERENCES users (id),
        category VARCHAR(32) NOT NULL,
        title TEXT NOT NULL,
        body TEXT NOT NULL,
        action_url TEXT,
        priority VARCHAR(16) NOT NULL,
        read BOOLEAN NOT NULL,
        created_at TIMESTAMP WITH TIME ZONE NOT NULL
    )
"""


def _seed(conn) -> dict[str, str]:
    ids = {k: str(uuid.uuid4()) for k in ("company", "alice", "bob", "carol", "read", "unread", "wide")}
    conn.execute(text("INSERT INTO companies (id, name, slug, settings, is_active, created_at) "
                      "VALUES (:c, 'Kept', 'kept', '{}', true, now())"), {"c": ids["company"]})
    for key in ("alice", "bob", "carol"):
        conn.execute(text("INSERT INTO users (id, email, name, auth_hash, is_active, created_at) "
                          "VALUES (:u, :e, 'User', 'x', true, now())"),
                     {"u": ids[key], "e": f"{key}@example.test"})
    for key, user, read in (("read", ids["alice"], True), ("unread", ids["alice"], False), ("wide", None, True)):
        conn.execute(text(
            "INSERT INTO notifications (id, company_id, user_id, category, title, body, priority, read, created_at) "
            "VALUES (:id, :c, :u, 'system', :t, 'B', 'medium', :r, now())"),
            {"id": ids[key], "c": ids["company"], "u": user, "t": key, "r": read})
    return ids


def _add_members(conn, ids: dict[str, str]) -> None:
    """Alice and Bob are current members of the company, Carol a former one."""
    for key, active in (("alice", True), ("bob", True), ("carol", False)):
        conn.execute(text("INSERT INTO user_companies (id, user_id, company_id, role, is_active) "
                          "VALUES (:id, :u, :c, 'operator', :a)"),
                     {"id": str(uuid.uuid4()), "u": ids[key], "c": ids["company"], "a": active})


def _expected(ids: dict[str, str]) -> set[tuple[str, str]]:
    return {(ids["read"], ids["alice"]), (ids["wide"], ids["alice"]), (ids["wide"], ids["bob"])}


def _converted(sync_url: str) -> tuple[set, set, int]:
    eng = create_engine(sync_url)
    try:
        with eng.connect() as conn:
            receipts = {tuple(map(str, r)) for r in conn.execute(text(
                "SELECT notification_id, user_id FROM notification_reads"))}
            columns = set(conn.execute(text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'notifications'")).scalars())
            notices = conn.execute(text("SELECT count(*) FROM notifications")).scalar_one()
    finally:
        eng.dispose()
    return receipts, columns, notices


def _at_parent(sync_url: str, started_first: bool) -> dict[str, str]:
    """A database at the previous head holding notices with the shared flag.
    started_first: this version started once before the upgrade ran, so its
    models already created the receipts table next to the old flag."""
    from celerp.models.notification import NotificationRead

    upgrade_to(sync_url, PARENT)
    eng = create_engine(sync_url)
    try:
        with eng.begin() as conn:
            conn.execute(text(_OLD_NOTIFICATIONS))
            ids = _seed(conn)
            _add_members(conn, ids)
            if started_first:
                NotificationRead.__table__.create(conn)
    finally:
        eng.dispose()
    return ids


@pytest.mark.parametrize("started_first", [False, True], ids=["upgrade", "started-before-upgrade"])
def test_migration_keeps_read_state(started_first):
    with throwaway_db("notifreads") as (_, sync_url):
        ids = _at_parent(sync_url, started_first)
        upgrade_to(sync_url, REVISION)
        receipts, columns, notices = _converted(sync_url)

    assert receipts == _expected(ids)
    assert "read" not in columns
    assert notices == 3


@pytest.mark.parametrize("started_first", [False, True], ids=["upgrade", "started-before-upgrade"])
def test_migrate_converts_notices_on_a_database_at_the_previous_head(started_first):
    """`celerp migrate` repairs the stamp before upgrading: the receipts table a
    start created is not taken as proof the conversion ran."""
    with throwaway_db("notifreads") as (async_url, sync_url):
        ids = _at_parent(sync_url, started_first)
        _apply_migrations(async_url)
        receipts, columns, _ = _converted(sync_url)

    assert receipts == _expected(ids)
    assert "read" not in columns


def test_new_installation_has_nothing_to_convert():
    """With no notices table yet the revision changes nothing; start creates both."""
    with throwaway_db("notifreads") as (_, sync_url):
        upgrade_to(sync_url, REVISION)
        eng = create_engine(sync_url)
        try:
            with eng.connect() as conn:
                assert conn.execute(text("SELECT to_regclass('notification_reads')")).scalar() is None
        finally:
            eng.dispose()
