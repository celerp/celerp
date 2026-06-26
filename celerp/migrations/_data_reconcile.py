# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Replay data-backfill migrations on a develop-origin (create_all) database.

The auto-stamp walker (`_auto_stamp.find_safe_stamp`) deliberately stamps a
`create_all`-built database to near-head and skips every migration with no
verifiable DDL signature — which is exactly the set of data backfills, seeds,
and no-ops. That is the right call for the normal `celerp migrate` path (a
develop database is usually throwaway scratch data), but the develop→release
in-place upgrade must *preserve* real data, so those backfills have to run.

A `create_all` database has the head schema but never ran any migration's
`upgrade()`, so its data backfills never happened. Every such migration is
written idempotently (guarded by `WHERE ... IS NULL` / `ON CONFLICT`) or is a
no-op, so replaying them on an already-at-head schema is safe and converges.

We re-run each one's `upgrade()` against a live connection through a real
alembic Operations context — the same `op` proxy the migration was written
against — so no SQL is duplicated and nothing is monkeypatched.
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from celerp.migrations._auto_stamp import extract_signatures

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection

# ── Instance metadata marker ────────────────────────────────────────────────
# A tiny key/value table recording which celerp version last applied each
# upgrade phase. The develop build never persists a version (it has no marker),
# so the first release boot always reconciles; afterwards each phase is gated to
# run once per version. Managed here (CREATE TABLE IF NOT EXISTS) rather than as
# a model/migration so it works on any database, including a create_all one.
_META_TABLE = "instance_meta"

# Marker keys: the backfill replay runs at `celerp migrate` time (no handlers
# needed); the projection rebuild runs in the API lifespan (handlers loaded), so
# each is gated by its own key.
BACKFILL_VERSION_KEY = "backfill_version"
PROJECTION_VERSION_KEY = "projection_version"


def _ensure_meta_table(conn: "Connection") -> None:
    from sqlalchemy import text

    conn.execute(text(f"CREATE TABLE IF NOT EXISTS {_META_TABLE} (key TEXT PRIMARY KEY, value TEXT NOT NULL)"))


def get_meta(conn: "Connection", key: str) -> str | None:
    """Read an instance_meta value, or None if unset (table auto-created)."""
    from sqlalchemy import text

    _ensure_meta_table(conn)
    return conn.execute(text(f"SELECT value FROM {_META_TABLE} WHERE key = :k"), {"k": key}).scalar()


def set_meta(conn: "Connection", key: str, value: str) -> None:
    """Upsert an instance_meta value (table auto-created)."""
    from sqlalchemy import text

    _ensure_meta_table(conn)
    conn.execute(
        text(
            f"INSERT INTO {_META_TABLE} (key, value) VALUES (:k, :v) "
            f"ON CONFLICT (key) DO UPDATE SET value = :v"
        ),
        {"k": key, "v": value},
    )


def data_backfill_scripts():
    """Return alembic Script objects the walker advances past, oldest→newest.

    "Advances past" == `extract_signatures` finds no verifiable DDL, which is
    precisely the data-backfill / seed / no-op migrations. Oldest→newest order
    matches the order alembic would have applied them.
    """
    from alembic.script import ScriptDirectory

    from celerp.alembic_config import build_alembic_config

    cfg = build_alembic_config()
    script = ScriptDirectory.from_config(cfg)
    # walk_revisions() yields newest→oldest; reverse to replay in apply order.
    out = []
    for sc in reversed(list(script.walk_revisions())):
        if not extract_signatures(Path(sc.path)):
            out.append(sc)
    return out


def replay_data_backfills(connection: "Connection") -> list[str]:
    """Re-run every data-backfill migration's `upgrade()` against `connection`.

    `connection` is a sync SQLAlchemy Connection; the caller owns the
    transaction. Returns the revision ids replayed, in apply order.
    """
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext

    scripts = data_backfill_scripts()
    ctx = MigrationContext.configure(connection)
    replayed: list[str] = []
    # Operations.context binds the global `alembic.op` proxy these migrations
    # import, so their `op.get_bind()` returns this connection.
    with Operations.context(ctx):
        for sc in scripts:
            sc.module.upgrade()
            replayed.append(sc.revision)
    return replayed
