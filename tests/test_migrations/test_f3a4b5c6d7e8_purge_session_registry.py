# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""The one-time registry cleanup that follows the token v2 cutover.

The session_reg_db fixture in conftest builds the session_registry table in an
isolated schema; run_migration_ops runs the migration's raw DELETE through a real
alembic Operations context so op.execute actually executes.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from .conftest import run_migration_ops

MODULE = "f3a4b5c6d7e8_purge_session_registry"


def _seed_jti(engine, expiry: datetime) -> str:
    jti = uuid.uuid4().hex
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO session_registry (jti, user_id, expiry) "
                "VALUES (:jti, :uid, :exp)"
            ),
            {"jti": jti, "uid": str(uuid.uuid4()), "exp": expiry},
        )
    return jti


def _count(engine) -> int:
    with engine.connect() as conn:
        return conn.execute(text("SELECT COUNT(*) FROM session_registry")).scalar_one()


def test_upgrade_clears_every_registered_jti(session_reg_db):
    """Every pre-cutover JTI row is deleted, so no dead session holds connection-
    limit occupancy after the version bump."""
    now = datetime.now(timezone.utc)
    _seed_jti(session_reg_db, now + timedelta(minutes=10))
    _seed_jti(session_reg_db, now + timedelta(minutes=10))
    _seed_jti(session_reg_db, now - timedelta(minutes=1))
    assert _count(session_reg_db) == 3

    run_migration_ops(session_reg_db, MODULE, "upgrade")

    assert _count(session_reg_db) == 0


def test_downgrade_is_a_noop(session_reg_db):
    """Downgrade restores nothing: the deleted rows named already-invalid
    sessions, so a reversal leaves the emptied registry untouched rather than
    fabricating rows."""
    _seed_jti(session_reg_db, datetime.now(timezone.utc) + timedelta(minutes=10))
    run_migration_ops(session_reg_db, MODULE, "upgrade")
    assert _count(session_reg_db) == 0

    run_migration_ops(session_reg_db, MODULE, "downgrade")

    assert _count(session_reg_db) == 0
