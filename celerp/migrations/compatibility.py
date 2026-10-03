# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Decide, read-only, whether this copy of Celerp may open a database.

Every path that opens a database (desktop start, ``celerp start``, the API on its
own, ``celerp migrate``, the update's migrate step, restore and recovery) asks this
module first, before anything can write. A database is refused when:

- it records that a newer Celerp last opened it (``instance_meta.projection_version``
  is newer than this copy), or
- it is stamped with an alembic revision this copy's migration scripts do not
  contain, which only a newer Celerp can have written, or
- the version it records cannot be read as a version at all.

An older database, or one that has never recorded a version, is compatible: the
normal upgrade path moves it forward. Nothing here creates, stamps, repairs or
writes anything; on Postgres the check runs in a read-only transaction.
"""
from __future__ import annotations

import dataclasses
import functools
import json

import sqlalchemy as sa
from packaging.version import InvalidVersion, Version

# instance_meta and its key are owned by _data_reconcile; read here without its
# get_meta helper, which creates the table.
from celerp.migrations._data_reconcile import _META_TABLE, PROJECTION_VERSION_KEY

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


def _recorded_version(conn: sa.Connection, tables: set[str]) -> str | None:
    if _META_TABLE not in tables:
        return None
    return conn.execute(
        sa.text(f"SELECT value FROM {_META_TABLE} WHERE key = :k"), {"k": PROJECTION_VERSION_KEY},
    ).scalar()


def check(conn: sa.Connection) -> Compatibility:
    """Classify *conn*'s database for this copy. Reads only.

    On Postgres the current transaction is made read-only, so *conn* must be a
    connection the caller rolls back and does not reuse for writes.
    """
    if conn.dialect.name == "postgresql":
        conn.execute(sa.text("SET TRANSACTION READ ONLY"))
    tables = set(sa.inspect(conn).get_table_names())
    running = running_version()
    revisions = _unknown_revisions(conn, tables)
    recorded = _recorded_version(conn, tables)
    if recorded is not None:
        try:
            newer = Version(recorded) > Version(running)
        except InvalidVersion:
            return Compatibility(INVALID_VERSION_RECORD, running, recorded, revisions)
        if newer:
            return Compatibility(NEWER_APP, running, recorded, revisions)
    if revisions:
        return Compatibility(UNKNOWN_SCHEMA, running, recorded, revisions)
    return Compatibility(COMPATIBLE, running, recorded)


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


def refuse_incompatible(sync_url: str) -> None:
    """Raise IncompatibleDatabase unless this copy may open *sync_url*'s database."""
    result = check_url(sync_url)
    if not result.ok:
        raise IncompatibleDatabase(result)
