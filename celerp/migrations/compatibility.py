# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Decide whether this copy of Celerp may open a database, and record that it has.

Every path that opens a database (desktop start, ``celerp start``, the API on its
own, ``celerp migrate``, the update's migrate and verify steps, restore and
recovery) is admitted here first, before it changes anything. A database is
refused when:

- a newer Celerp has begun opening it (``instance_meta.newest_celerp_version``,
  or, for a database from before that record existed, ``projection_version``, is
  newer than this copy), or
- it is stamped with an alembic revision this copy's migration scripts do not
  contain, which only a newer Celerp can have written, or
- a version it records cannot be read as a version at all.

An older database, or one that has never recorded a version, is compatible: the
normal upgrade path moves it forward.

``check`` only reads; on Postgres it runs in a read-only transaction. ``admit``
makes the same decision under a lock, writes nothing when it refuses, and
otherwise raises ``newest_celerp_version`` to this copy's version, which the
caller commits on its own before its first change. The record is never lowered,
and it stands whatever the caller does next: a startup that fails, stops early (the update's
verify start) or never finishes the projection reconcile has still begun to
change the database, so an older copy refuses it from then on.
"""
from __future__ import annotations

import dataclasses
import functools
import json

import sqlalchemy as sa
from packaging.version import InvalidVersion, Version

# instance_meta is owned by _data_reconcile; read here without its get_meta helper,
# which creates the table.
from celerp.migrations._data_reconcile import _META_TABLE, PROJECTION_VERSION_KEY, set_meta

# The newest Celerp version that has begun opening this database. Only admit()
# writes it. projection_version belongs to the projection reconcile; it is read
# here only as the floor for a database from before this record existed (and,
# should both exist, the newer of the two decides).
NEWEST_CELERP_KEY = "newest_celerp_version"
_RECORDED_KEYS = (NEWEST_CELERP_KEY, PROJECTION_VERSION_KEY)
# Serializes admissions, so two copies opening at once cannot lower the record.
_ADMIT_LOCK_KEY = 0x63656C6572700002

COMPATIBLE = "compatible"
NEWER_APP = "newer_app"
UNKNOWN_SCHEMA = "unknown_schema"
INVALID_VERSION_RECORD = "invalid_version_record"


@dataclasses.dataclass(frozen=True)
class Compatibility:
    status: str
    running: str
    recorded: str | None = None
    revisions: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.status == COMPATIBLE

    @property
    def message(self) -> str:
        if self.status == NEWER_APP:
            return (
                f"This data was last opened with Celerp {self.recorded}, which is newer "
                f"than this copy ({self.running}). Nothing was changed. Install the "
                f"latest version of Celerp to open it."
            )
        if self.status == UNKNOWN_SCHEMA:
            return (
                f"This data cannot safely be opened by this copy of Celerp ({self.running}). "
                f"Nothing was changed. Install the latest version of Celerp to open it. "
                f"(Database revision: {', '.join(self.revisions)})"
            )
        if self.status == INVALID_VERSION_RECORD:
            return (
                f"This data cannot safely be opened by this copy of Celerp ({self.running}): "
                f"the version it records ({self.recorded!r}) is not readable. Nothing was changed."
            )
        return "compatible"

    def to_json(self) -> str:
        return json.dumps({
            "status": self.status,
            "running": self.running,
            "recorded": self.recorded,
            "revisions": list(self.revisions),
            "message": self.message,
        })


class IncompatibleDatabase(RuntimeError):
    """This copy must not open the database; nothing was changed."""

    def __init__(self, result: Compatibility):
        super().__init__(result.message)
        self.result = result


def running_version() -> str:
    from celerp import __version__
    return __version__


def is_newer_than_running(version: str) -> bool:
    """Whether *version* is newer than this copy. Raises InvalidVersion if unreadable."""
    return Version(version) > Version(running_version())


@functools.lru_cache(maxsize=1)
def _known_revisions() -> frozenset[str]:
    from alembic.script import ScriptDirectory
    from celerp.alembic_config import build_alembic_config
    script = ScriptDirectory.from_config(build_alembic_config())
    return frozenset(rev.revision for rev in script.walk_revisions())


def _unknown_revisions(conn: sa.Connection, tables: set[str]) -> tuple[str, ...]:
    if "alembic_version" not in tables:
        return ()
    stamped = conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalars()
    known = _known_revisions()
    return tuple(sorted(rev for rev in stamped if rev not in known))


def _recorded_versions(conn: sa.Connection, tables: set[str]) -> dict[str, str]:
    if _META_TABLE not in tables:
        return {}
    rows = conn.execute(sa.text(f"SELECT key, value FROM {_META_TABLE}"))
    return {key: value for key, value in rows if key in _RECORDED_KEYS}


def _classify(conn: sa.Connection) -> tuple[Compatibility, dict[str, str]]:
    tables = set(sa.inspect(conn).get_table_names())
    running = running_version()
    revisions = _unknown_revisions(conn, tables)
    recorded = _recorded_versions(conn, tables)
    newest: str | None = None
    for value in recorded.values():
        try:
            parsed = Version(value)
        except InvalidVersion:
            return Compatibility(INVALID_VERSION_RECORD, running, value, revisions), recorded
        if newest is None or parsed > Version(newest):
            newest = value
    if newest is not None and Version(newest) > Version(running):
        return Compatibility(NEWER_APP, running, newest, revisions), recorded
    if revisions:
        return Compatibility(UNKNOWN_SCHEMA, running, newest, revisions), recorded
    return Compatibility(COMPATIBLE, running, newest), recorded


def check(conn: sa.Connection) -> Compatibility:
    """Classify *conn*'s database for this copy. Reads only.

    On Postgres the current transaction is made read-only, so *conn* must be a
    connection the caller rolls back and does not reuse for writes.
    """
    if conn.dialect.name == "postgresql":
        conn.execute(sa.text("SET TRANSACTION READ ONLY"))
    return _classify(conn)[0]


def admit(conn: sa.Connection) -> Compatibility:
    """Decide again and, when this copy may open the database, record it. Raises
    IncompatibleDatabase, having written nothing, when it may not.

    *conn* must be in a transaction of its own that the caller commits before it
    changes anything else, so the record stands however the caller's work ends.
    """
    if conn.dialect.name == "postgresql":
        conn.execute(sa.text("SELECT pg_advisory_xact_lock(:k)"), {"k": _ADMIT_LOCK_KEY})
    result, recorded = _classify(conn)
    if not result.ok:
        raise IncompatibleDatabase(result)
    mine = recorded.get(NEWEST_CELERP_KEY)
    if mine is None or Version(result.running) > Version(mine):
        set_meta(conn, NEWEST_CELERP_KEY, result.running)
    return result


def check_url(sync_url: str) -> Compatibility:
    """Open *sync_url*, classify it, and roll back. Reads only."""
    engine = sa.create_engine(sync_url, poolclass=sa.pool.NullPool)
    try:
        with engine.connect() as conn:
            try:
                return check(conn)
            finally:
                conn.rollback()
    finally:
        engine.dispose()


def admit_url(sync_url: str) -> Compatibility:
    """Admit this copy to *sync_url*'s database and commit the record (see admit)."""
    engine = sa.create_engine(sync_url, poolclass=sa.pool.NullPool)
    try:
        with engine.begin() as conn:
            return admit(conn)
    finally:
        engine.dispose()
