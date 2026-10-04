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

The record alone cannot stop two versions overlapping: an older process that was
admitted first keeps writing after a newer one is admitted. So every process that
writes also holds a ``Fence`` for as long as it may write. Processes of the same
version share the database (the API, the UI, ``celerp migrate`` and
``celerp reset-password`` run side by side); a process of another version waits
until the last holder of the current version has gone, and is refused if it never
goes. On Postgres, embedded or external, the fence is a session advisory lock per
version, so a process that dies releases it with its connection. Other dialects
(SQLite in tests) have no fence; only the record applies there.

A process can also lose its fence while it lives: its fence session ends (a
Postgres restart, a dropped connection, a terminated backend) and the pool simply
reconnects. So a long-running process ``guard``s the engines it writes through:
every transaction on them first takes this version's fence lock for its own
lifetime and confirms the process's fence session still holds it. If the fence
is gone it is taken again, after the database is classified again, before the
transaction may go on; when the database now records a newer version, the
process ends instead.
"""
from __future__ import annotations

import contextlib
import dataclasses
import functools
import json
import logging
import os
import sys
import threading
import time
import zlib

import sqlalchemy as sa
from packaging.version import InvalidVersion, Version

# instance_meta is owned by _data_reconcile; read here without its get_meta helper,
# which creates the table.
from celerp.migrations._data_reconcile import _META_TABLE, PROJECTION_VERSION_KEY, set_meta

log = logging.getLogger(__name__)

# The newest Celerp version that has begun opening this database. Only admit()
# writes it. projection_version belongs to the projection reconcile; it is read
# here only as the floor for a database from before this record existed (and,
# should both exist, the newer of the two decides).
NEWEST_CELERP_KEY = "newest_celerp_version"
_RECORDED_KEYS = (NEWEST_CELERP_KEY, PROJECTION_VERSION_KEY)
# Serializes admissions and fence entries: one copy at a time decides, records,
# and takes its fence, so the record never goes down and two versions never both
# pass the "no other version holds the fence" check.
_ADMIT_LOCK_KEY = 0x63656C6572700002
# Fence locks are (namespace, crc32(version)) shared session advisory locks.
_FENCE_NAMESPACE = 0x63656C72
# How long a copy waits for another version's processes to stop before refusing.
FENCE_WAIT_SECONDS = 30.0
_FENCE_POLL_SECONDS = 0.25

COMPATIBLE = "compatible"
NEWER_APP = "newer_app"
UNKNOWN_SCHEMA = "unknown_schema"
INVALID_VERSION_RECORD = "invalid_version_record"
IN_USE = "in_use"


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
        if self.status == IN_USE:
            return (
                f"Another version of Celerp is still using this data, so this copy "
                f"({self.running}) cannot open it. Nothing was changed. Close the other "
                f"version, then try again."
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


def _stamped_revisions(conn: sa.Connection, tables: set[str]) -> list[str]:
    if "alembic_version" not in tables:
        return []
    return list(conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalars())


def _recorded_versions(conn: sa.Connection, tables: set[str]) -> dict[str, str]:
    if _META_TABLE not in tables:
        return {}
    return dict(conn.execute(sa.text(f"SELECT key, value FROM {_META_TABLE}")).all())


def decide(meta: dict[str, str], stamped: list[str]) -> Compatibility:
    """Classify a database from its instance_meta rows and alembic stamps.

    Pure, so a caller that can only read the database some other way (the
    provisioning path reads it through psql as the superuser) decides the same way.
    """
    running = running_version()
    known = _known_revisions()
    revisions = tuple(sorted(rev for rev in stamped if rev not in known))
    newest: str | None = None
    for key in _RECORDED_KEYS:
        value = meta.get(key)
        if value is None:
            continue
        try:
            parsed = Version(value)
        except InvalidVersion:
            return Compatibility(INVALID_VERSION_RECORD, running, value, revisions)
        if newest is None or parsed > Version(newest):
            newest = value
    if newest is not None and Version(newest) > Version(running):
        return Compatibility(NEWER_APP, running, newest, revisions)
    if revisions:
        return Compatibility(UNKNOWN_SCHEMA, running, newest, revisions)
    return Compatibility(COMPATIBLE, running, newest)


def _classify(conn: sa.Connection) -> tuple[Compatibility, dict[str, str]]:
    tables = set(sa.inspect(conn).get_table_names())
    meta = _recorded_versions(conn, tables)
    return decide(meta, _stamped_revisions(conn, tables)), meta


def check(conn: sa.Connection) -> Compatibility:
    """Classify *conn*'s database for this copy. Reads only.

    On Postgres the current transaction is made read-only, so *conn* must be a
    connection the caller rolls back and does not reuse for writes.
    """
    if conn.dialect.name == "postgresql":
        conn.execute(sa.text("SET TRANSACTION READ ONLY"))
    return _classify(conn)[0]


def _accepted(result: Compatibility, accept: str | None) -> bool:
    """Whether *result* lets this copy in. *accept* is a newer version whose
    record this copy may write under: the update's rollback restores the
    database the target version had begun to change."""
    if result.ok:
        return True
    return (accept is not None and result.status == NEWER_APP
            and Version(result.recorded) <= Version(accept))


def admit(conn: sa.Connection, accept: str | None = None) -> Compatibility:
    """Decide again and, when this copy may open the database, record it. Raises
    IncompatibleDatabase, having written nothing, when it may not.

    *conn* must be in a transaction of its own that the caller commits before it
    changes anything else, so the record stands however the caller's work ends.
    """
    if conn.dialect.name == "postgresql":
        conn.execute(sa.text("SELECT pg_advisory_xact_lock(:k)"), {"k": _ADMIT_LOCK_KEY})
    result, recorded = _classify(conn)
    if not _accepted(result, accept):
        raise IncompatibleDatabase(result)
    mine = recorded.get(NEWEST_CELERP_KEY)
    if result.ok and (mine is None or Version(result.running) > Version(mine)):
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


def _cohort_key(version: str) -> int:
    return zlib.crc32(version.encode()) & 0x7FFFFFFF


# Other versions' fence holders in this database. A two-int advisory key shows in
# pg_locks as classid=key1, objid=key2, objsubid=2.
_OTHER_HOLDERS = sa.text(
    "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND granted "
    "AND objsubid = 2 AND classid::bigint = :ns AND objid::bigint <> :mine "
    "AND database = (SELECT oid FROM pg_database WHERE datname = current_database())"
)


# Fences this process holds, newest last (see current_fence).
_HELD: list["Fence"] = []


def current_fence() -> "Fence | None":
    """The fence this process most recently joined and still holds, for a writer
    that opens its own engine inside it (migrations, restore) to ``guard``."""
    return _HELD[-1] if _HELD else None


class Fence:
    """This process's hold on a database for its version, from ``join`` to
    ``release`` (see the module docstring).

    The fence connection is used only for the lock and for admission; it holds
    no transaction open between them.
    """

    def __init__(self, sync_url: str, accept: str | None = None):
        self._engine = sa.create_engine(sync_url, poolclass=sa.pool.NullPool)
        self._conn: sa.Connection | None = None
        self._accept = accept
        self._key = _cohort_key(running_version())
        # The fence session, as (pid, backend_start): a pid alone can be reused.
        self._session: tuple[int, str] | None = None
        self._guarded: list[sa.Engine] = []
        self._retaking = threading.Lock()

    @classmethod
    def join(cls, sync_url: str, accept: str | None = None) -> "Fence":
        """Take this version's fence, waiting while another version holds one.

        Raises IncompatibleDatabase, having changed nothing, when the database
        records a version this copy may not open (see ``accept`` in admit), so
        an older copy never holds the fence once a newer one has opened the
        database, or when the other version is still there after
        FENCE_WAIT_SECONDS.
        """
        fence = cls(sync_url, accept)
        try:
            fence._conn = fence._engine.connect()
            if fence._conn.dialect.name == "postgresql":
                deadline = time.monotonic() + FENCE_WAIT_SECONDS
                while not fence._try_enter():
                    if time.monotonic() >= deadline:
                        raise IncompatibleDatabase(Compatibility(IN_USE, running_version()))
                    time.sleep(_FENCE_POLL_SECONDS)
        except BaseException:
            fence.release()
            raise
        _HELD.append(fence)
        return fence

    def _try_enter(self) -> bool:
        """Take the fence lock unless another version holds one. Raises
        IncompatibleDatabase when this copy may not open the database."""
        conn = self._conn
        params = {"ns": _FENCE_NAMESPACE, "mine": self._key}
        with conn.begin():
            conn.execute(sa.text("SELECT pg_advisory_xact_lock(:k)"), {"k": _ADMIT_LOCK_KEY})
            result = _classify(conn)[0]
            if not _accepted(result, self._accept):
                raise IncompatibleDatabase(result)
            if conn.execute(_OTHER_HOLDERS, params).scalar():
                return False
            # Taken under the admit lock, so no other version can pass the checks
            # above between them and this lock.
            conn.execute(sa.text("SELECT pg_advisory_lock_shared(:ns, :mine)"), params)
            pid, started = conn.execute(sa.text(
                "SELECT pid, backend_start::text FROM pg_stat_activity "
                "WHERE pid = pg_backend_pid()")).one()
            self._session = (pid, started)
            return True

    def guard(self, *engines) -> None:
        """Gate every transaction on *engines* (sync or async) on this fence until
        ``release`` (see the module docstring)."""
        for engine in engines:
            target = getattr(engine, "sync_engine", engine)
            if self._session is None or target.dialect.name != "postgresql":
                continue
            sa.event.listen(target, "begin", self._gate)
            self._guarded.append(target)

    def unguard(self, *engines) -> None:
        """Stop gating *engines* (a writer's own engine, before it is disposed)."""
        for engine in engines:
            target = getattr(engine, "sync_engine", engine)
            if target in self._guarded:
                sa.event.remove(target, "begin", self._gate)
                self._guarded.remove(target)

    def _gate(self, conn: sa.Connection) -> None:
        # Runs before the transaction's first statement, on its own connection, so
        # the fence lock below lasts exactly as long as the transaction: a newer
        # version cannot be admitted while it is open.
        cursor = conn.connection.cursor()
        try:
            cursor.execute(f"SELECT pg_advisory_xact_lock_shared({_FENCE_NAMESPACE}, {self._key})")
            cursor.fetchall()
            session = self._session
            if not self._holds(cursor, session):
                self._retake(session)
        finally:
            cursor.close()

    def _holds(self, cursor, session: tuple[int, str] | None) -> bool:
        if session is None:
            return False
        pid, started = session
        cursor.execute(
            "SELECT EXISTS (SELECT 1 FROM pg_locks l JOIN pg_stat_activity a ON a.pid = l.pid "
            f"WHERE l.pid = {int(pid)} AND a.backend_start::text = '{started.replace(chr(39), '')}' "
            f"AND l.locktype = 'advisory' AND l.granted AND l.mode = 'ShareLock' "
            f"AND l.objsubid = 2 AND l.classid::bigint = {_FENCE_NAMESPACE} "
            f"AND l.objid::bigint = {self._key})")
        return bool(cursor.fetchone()[0])

    def _retake(self, lost: tuple[int, str] | None) -> None:
        """Take the fence again on a new session, classifying the database first.
        Ends the process when it now records a version this copy may not open;
        raises IncompatibleDatabase (IN_USE) while another version holds it."""
        with self._retaking:
            if self._session != lost:
                return  # another transaction already took it again
            log.warning("The database version fence was lost; taking it again before writing")
            if self._conn is not None:
                with contextlib.suppress(Exception):
                    self._conn.close()
            self._session = None
            self._conn = self._engine.connect()
            try:
                if not self._try_enter():
                    raise IncompatibleDatabase(Compatibility(IN_USE, running_version()))
            except IncompatibleDatabase as exc:
                if exc.result.status != IN_USE:
                    _end_process(exc)
                raise

    def check(self) -> Compatibility:
        """Classify the database for this copy (see check). Reads only."""
        try:
            return check(self._conn)
        finally:
            self._conn.rollback()

    def admit(self, accept: str | None = None) -> Compatibility:
        """Admit this copy and commit the record (see admit)."""
        with self._conn.begin():
            return admit(self._conn, accept)

    def release(self) -> None:
        """Give up the fence. Closing the connection releases the lock; safe to repeat."""
        if self in _HELD:
            _HELD.remove(self)
        self.unguard(*self._guarded)
        if self._conn is not None:
            with contextlib.suppress(Exception):
                self._conn.close()
            self._conn = None
        self._engine.dispose()


def _end_process(exc: IncompatibleDatabase) -> None:
    """A newer version has opened the database since this process lost its fence:
    stop at once, before anything else of this process can write."""
    log.critical("Stopping: %s", exc)
    print(f"\n{exc}\n", file=sys.stderr, flush=True)
    os._exit(1)


@contextlib.contextmanager
def fence(sync_url: str, accept: str | None = None):
    """Join the fence and admit this copy for the wrapped block."""
    held = Fence.join(sync_url, accept)
    try:
        held.admit(accept)
        yield held
    finally:
        held.release()


def admit_url(sync_url: str) -> Compatibility:
    """Admit this copy to *sync_url*'s database and commit the record (see admit).

    Takes the fence only for the admission: callers that go on writing hold a
    fence of their own around this.
    """
    held = Fence.join(sync_url)
    try:
        return held.admit()
    finally:
        held.release()
