# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Replay data-backfill migrations a stamp skipped.

The auto-stamp walker (`_auto_stamp.find_safe_stamp`) deliberately stamps a
`create_all`-built database to near-head and skips every migration with no
verifiable DDL signature, which is exactly the set of data backfills, seeds,
and no-ops. That is the right call for the schema, but the develop->release
in-place upgrade must *preserve* real data, so those backfills have to run.

Each data backfill runs once per database: by alembic upgrade, or here when a
stamp moved past it without running it. Never twice: a backfill converts data
as it stood when it was written, and run again it would convert data a user set
since (the category-unit backfill turns a stone sold by the carat into one sold
by the gram). So `_apply_migrations` records the backfills a stamp skips
(`track_restamp`) and the reconcile replays exactly those, removing each in
the transaction that applies it; one a stamp moves back below is stamped past.

We re-run each one's `upgrade()` against a live connection through a real
alembic Operations context (the same `op` proxy the migration was written
against), so no SQL is duplicated and nothing is monkeypatched.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from celerp.migrations._auto_stamp import extract_signatures

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection, Engine

_log = logging.getLogger(__name__)

# ── Instance metadata marker ────────────────────────────────────────────────
# A tiny key/value table of installation markers. Managed here (CREATE TABLE IF
# NOT EXISTS) rather than as a model/migration so it works on any database,
# including a create_all one.
_META_TABLE = "instance_meta"

# PENDING_BACKFILLS_KEY lists the data backfills a stamp moved past without
# running them (a comma list of revision ids, oldest first). Every other backfill
# in the stamp's history ran under alembic. The projection rebuild runs in the API
# lifespan (handlers loaded), gated by its own key.
PENDING_BACKFILLS_KEY = "backfills_pending"
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


def pending_backfills(conn: "Connection") -> list[str]:
    """The data backfills a stamp skipped and no replay has applied yet."""
    raw = get_meta(conn, PENDING_BACKFILLS_KEY)
    return [rev for rev in raw.split(",") if rev] if raw else []


def _lineage(script, revision: str | None) -> set[str]:
    """Every revision in `revision`'s history, itself included. Empty for no
    stamp, and for a stamp this build does not know (alembic refuses it)."""
    from alembic.script.revision import RevisionError

    if revision is None:
        return set()
    try:
        return {sc.revision for sc in script.iterate_revisions(revision, "base")}
    except RevisionError:
        return set()


def track_restamp(conn: "Connection", script, *, ran_to: str | None, stamped_to: str | None) -> frozenset:
    """Keep each data backfill to one run across a stamp moving from `ran_to` to
    `stamped_to`. `ran_to` is the stamp the database had: alembic ran every
    backfill in its history; None, an unstamped database (create_all built it),
    ran none.

    A backfill the stamp moves past without running it is added to the pending
    list, for the reconcile; one it moves back below is the upgrade's: if it is
    pending, the upgrade runs it and it leaves the list; if it already ran, it is
    returned, and the upgrade stamps past it instead of running it again.
    """
    before, after = _lineage(script, ran_to), _lineage(script, stamped_to)
    listed = pending_backfills(conn)
    was_pending = set(listed)
    backfills = [sc.revision for sc in data_backfill_scripts()]
    pending = [rev for rev in backfills if rev in after and (rev in was_pending or rev not in before)]
    if pending != listed:
        set_meta(conn, PENDING_BACKFILLS_KEY, ",".join(pending))
    return frozenset(rev for rev in backfills if rev in before - after and rev not in was_pending)


def replay_data_backfills(engine: "Engine") -> tuple[list[str], list[str]]:
    """Run each pending data backfill's `upgrade()`, in apply order, each in its
    OWN transaction against `engine`, which also removes it from the pending list.

    The removal shares the backfill's transaction, so a backfill is applied and
    struck off together or neither: it runs once. A per-backfill top-level
    transaction (not a savepoint inside one outer transaction) makes the replay
    durable: each backfill that succeeds commits on its own, so a later failure
    cannot undo work that already converged. One failing backfill is logged
    loudly and stays pending, so the next start retries it; the rest still run.

    Returns `(replayed, failures)`: the revision ids that applied cleanly and
    the ids that raised, both in apply order.
    """
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext

    with engine.begin() as conn:
        pending = set(pending_backfills(conn))
    replayed: list[str] = []
    failures: list[str] = []
    for sc in data_backfill_scripts():
        if sc.revision not in pending:
            continue
        try:
            with engine.begin() as conn:
                ctx = MigrationContext.configure(conn)
                # Operations.context binds the global `alembic.op` proxy these
                # migrations import, so their `op.get_bind()` returns this
                # connection; the with-block commits on clean exit, rolls back
                # on any exception.
                with Operations.context(ctx):
                    sc.module.upgrade()
                set_meta(conn, PENDING_BACKFILLS_KEY,
                         ",".join(rev for rev in pending_backfills(conn) if rev != sc.revision))
            replayed.append(sc.revision)
        except Exception:
            _log.exception("data backfill %s failed to replay; it stays pending", sc.revision)
            failures.append(sc.revision)
    return replayed, failures
