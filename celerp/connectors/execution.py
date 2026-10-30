# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""The one boundary connector work runs in.

`connector_execution(...)` admits the work, checks in one short transaction that the
connection is this company's and that the work may run now (`eligible`), and takes a
lease on the resources it writes. While it runs:

- every database transaction the execution's task uses is fenced at its first
  statement: it holds the lease rows in KEY SHARE and checks they still name this
  holder and generation, so a holder whose lease was taken over can never commit;
- a write outside any Session, or from a task the execution spawned, is refused;
- every call to the store first checks the lease is still alive by the database
  clock, and no store call is made while a fenced transaction is open.

A webhook, schedule tick or queue row that finds the resource held records a rerun on
the heartbeat row. Only an execution that runs the work clears it: the holder takes it
when it releases the resource and runs the resource's sync once more first; one with no
live holder (its holder crashed, or admission was full) is run by the sweeper within a
tick. An elapsed time never ends a lease: a holder that keeps renewing keeps it."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
import weakref
from collections.abc import AsyncIterator
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from sqlalchemy.pool import Pool

from celerp.capacity import REQUEST_DB_MAX_OVERFLOW

log = logging.getLogger(__name__)

TTL = timedelta(minutes=5)
RENEW_EVERY = 60.0
RENEW_RETRY = 10.0
# Longer than a connector HTTP call can take, so a call started just before this
# point ends before another holder can take the lease over.
MARGIN = timedelta(seconds=45)
# The request pool's overflow is kept for background work; one overflow connection
# stays free for the relay and sign-in.
EXECUTION_SLOTS = max(1, REQUEST_DB_MAX_OVERFLOW - 1)
# How often the sweeper looks for reruns no live execution holds.
SWEEP_EVERY = 60.0
# Reruns a holder runs before it releases; more recorded meanwhile go to the sweeper.
_RERUN_ROUNDS = 3
# A lifecycle transaction never waits longer than this for a lock or a statement.
_INTERNAL_LOCK_TIMEOUT_MS = 5000
_LOCK_NOT_AVAILABLE = "55P03"

PURPOSES = ("first_sync", "manual_sync", "schedule", "webhook", "outbound_queue",
            "item_publish", "undo", "resolve", "webhooks", "rerun")
WEBHOOKS = "webhooks"


class ExecutionError(RuntimeError):
    """Connector work could not run, or must stop."""


class ExecutionBusy(ExecutionError):
    """Another execution holds one of the resources."""


class ExecutionRefused(ExecutionError):
    """The connection is not in a state where this work may run. ``reason`` names why."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class AdmissionBusy(ExecutionError):
    """Every execution slot is in use."""


class LeaseLost(ExecutionError):
    """The lease was taken over or expired; nothing more may be written."""


class NestedExecutionError(ExecutionError):
    """An execution asked for resources its own task does not hold."""


class SpawnedTaskWrite(ExecutionError):
    """A task spawned inside an execution wrote without opening its own."""


class UnfencedWrite(ExecutionError):
    """A write inside an execution ran on a transaction that was never fenced."""


class ExecutionPoolBudget(ExecutionError):
    """An execution checked out a second request connection while holding one."""


class RemoteCallInTransaction(ExecutionError):
    """A store call was made while a fenced transaction was open."""


def execution_resource(kind: str, name: str | None) -> str:
    """The lease resource one piece of connector work writes.

    ``kind`` is "entity" for a sync entity (products and products_out are both
    "products"), "outbound" for an outbound queue type, or "webhooks"."""
    if kind == "entity":
        return str(name).removesuffix("_out")
    if kind == "outbound":
        return {"inventory": "inventory", "invoice": "invoices"}[str(name)]
    if kind == WEBHOOKS:
        return WEBHOOKS
    raise ValueError(f"unknown execution resource kind {kind!r}")


def plan_resources(connector, directions: dict | None) -> list[str]:
    """Every resource the syncs these directions run write."""
    from celerp.connectors.sync_runner import sync_plan

    return sorted({execution_resource("entity", e) for e in sync_plan(connector, directions)})


def eligible(connector, config, first_sync, purpose: str, resource: str) -> str | None:
    """Why ``purpose`` may not write ``resource`` of this connection now, or None.

    The single answer manual syncs, schedules, webhooks, the outbound queue, item
    publishing, undo, conflict resolution and the first sync all take."""
    if purpose not in PURPOSES:
        raise ValueError(f"unknown execution purpose {purpose!r}")
    if config is None:
        return "not_connected"
    if first_sync is None:
        return "first_sync_missing"
    state = first_sync.state
    progress = (first_sync.progress or {}).get(resource) or {}
    if purpose == "first_sync":
        if not connector.reviewed_first_sync or state not in ("starting", "running"):
            return "first_sync_not_started"
        return None
    if purpose in ("undo", "resolve"):
        return None if state in ("completed", "failed") else "first_sync_not_finished"
    if resource == WEBHOOKS:
        return None if state != "pending" else "first_sync_not_started"
    if purpose == "item_publish":
        if progress.get("state") == "failed" or (state == "failed" and progress.get("state") != "completed"):
            return "products_need_attention"
        if state != "completed" and progress.get("state") != "completed":
            return "first_sync_not_finished"
    elif state != "completed":
        return "first_sync_not_finished"
    if resource not in plan_resources(connector, config.entity_directions):
        return "direction_off"
    return None


async def first_sync_state(session, company_id, connector: str) -> str | None:
    """The connection's first-sync state (FIRST_SYNC_STATES), None without a record."""
    from celerp.models.connector_first_sync import ConnectorFirstSync

    return await session.scalar(sa.select(ConnectorFirstSync.state).where(
        ConnectorFirstSync.company_id == str(company_id), ConnectorFirstSync.connector == connector))


@dataclass(eq=False)
class Execution:
    company_id: str
    connector: str
    purpose: str
    resources: list[str]
    holder: uuid.UUID = field(default_factory=uuid.uuid4)
    generations: dict[str, int] = field(default_factory=dict)
    confirmed_expiry: datetime | None = None
    # database clock minus local clock, measured at the last renewal
    clock_skew: timedelta = timedelta(0)
    lost: bool = False
    ctx: object | None = None
    task: asyncio.Task | None = None
    # connections its own task has checked out of a pool and not yet returned
    pool_connections: int = 0
    _open: "weakref.WeakSet" = field(default_factory=weakref.WeakSet)

    # -- the lease, as the store sees it ---------------------------------------

    def _db_now(self) -> datetime:
        return datetime.now(timezone.utc) + self.clock_skew

    def check_alive(self) -> None:
        """Refuse when the lease may no longer be ours by the time a store call ends."""
        if self.lost or self.confirmed_expiry is None or self._db_now() >= self.confirmed_expiry - MARGIN:
            self.lost = True
            raise LeaseLost(f"{self.connector} lease for {', '.join(self.resources)} is no longer held")

    def require(self, resource: str) -> None:
        if resource not in self.resources:
            raise NestedExecutionError(f"this execution does not hold {resource!r}")

    def has_open_transaction(self) -> bool:
        return any(tx.is_active for tx in list(self._open))

    # -- fence ------------------------------------------------------------------

    def fence(self, conn) -> None:
        """Fence a Core connection's transaction (sync Connection or its run_sync side)."""
        conn = getattr(conn, "sync_connection", conn)
        tx = conn.get_transaction()
        if tx is not None and tx in _FENCED:
            return
        if not self.resources:
            raise LeaseLost("this execution holds no resource any more")
        if self.lost:
            raise LeaseLost("this execution's lease is lost")
        if conn.dialect.name != "sqlite":
            rows = conn.execute(sa.text(
                "SELECT resource, generation, holder FROM connector_execution_leases "
                "WHERE company_id = :c AND connector = :k AND resource IN :r "
                "ORDER BY resource FOR KEY SHARE"
            ).bindparams(sa.bindparam("r", expanding=True)),
                {"c": self.company_id, "k": self.connector, "r": list(self.resources)}).all()
            held = {r.resource: (int(r.generation), uuid.UUID(str(r.holder)) if r.holder else None)
                    for r in rows}
            if any(held.get(res) != (self.generations[res], self.holder) for res in self.resources):
                self.lost = True
                raise LeaseLost(f"{self.connector} lease for {', '.join(self.resources)} was taken over")
        tx = conn.get_transaction()
        _FENCED[tx] = self
        self._open.add(tx)


_CURRENT: ContextVar[Execution | None] = ContextVar("connector_execution", default=None)
# Set while this module runs its own lease transactions, which are never fenced.
_INTERNAL: ContextVar[bool] = ContextVar("connector_execution_internal", default=False)
# Set while work inside an execution only reads before any write (unfenced_reads).
_UNFENCED: ContextVar[bool] = ContextVar("connector_execution_unfenced", default=False)
_FENCED: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()
_SLOTS: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore]" = weakref.WeakKeyDictionary()


def current() -> Execution | None:
    """The execution the current task runs, if any."""
    ex = _CURRENT.get()
    if ex is None or _INTERNAL.get():
        return None
    try:
        task = asyncio.current_task()
    except RuntimeError:
        task = None
    return ex if ex.task is task else None


@contextlib.contextmanager
def unfenced_reads():
    """Run the block's transactions without the lease fence, for reads that must take
    locks ranked before the lease rows (a module admission's instance-wide locks). Its
    writes still need the fence, so one is refused as unfenced."""
    token = _UNFENCED.set(True)
    try:
        yield
    finally:
        _UNFENCED.reset(token)


@contextlib.contextmanager
def _internal():
    token = _INTERNAL.set(True)
    try:
        yield
    finally:
        _INTERNAL.reset(token)


def _slots() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    sem = _SLOTS.get(loop)
    if sem is None:
        sem = _SLOTS[loop] = asyncio.Semaphore(EXECUTION_SLOTS)
    return sem


# -- guards on every session and engine -------------------------------------------

def _fence_session(session: Session) -> None:
    ex = current()
    if ex is None or _UNFENCED.get():
        return
    ex.fence(session.connection())


@event.listens_for(Session, "do_orm_execute")
def _on_orm_execute(state) -> None:
    _fence_session(state.session)


@event.listens_for(Session, "before_flush")
def _on_flush(session, _flush_context, _instances) -> None:
    _fence_session(session)


_READ_PREFIXES = ("SELECT", "WITH", "SHOW", "SET ", "SAVEPOINT", "RELEASE", "ROLLBACK", "BEGIN", "COMMIT")


@event.listens_for(Engine, "before_cursor_execute")
def _on_cursor_execute(conn, _cursor, statement, _params, _context, _executemany) -> None:
    ex = _CURRENT.get()
    if ex is None or _INTERNAL.get():
        return
    if statement.lstrip().upper().startswith(_READ_PREFIXES):
        return
    try:
        task = asyncio.current_task()
    except RuntimeError:
        task = None
    if ex.task is not task:
        raise SpawnedTaskWrite(
            f"a task spawned inside a {ex.connector} execution wrote without opening its own")
    if conn.get_transaction() not in _FENCED:
        raise UnfencedWrite(f"a write inside a {ex.connector} execution was not fenced")


@event.listens_for(Pool, "checkout")
def _on_checkout(_dbapi_connection, record, _proxy) -> None:
    """An execution's transactions are sequential, so it holds one connection at a
    time; a second at once is a nested session that could wait on the pool forever."""
    ex = current()
    if ex is None:
        return
    if ex.pool_connections >= 1:
        raise ExecutionPoolBudget(
            f"a {ex.connector} execution opened a second database connection while holding one")
    ex.pool_connections += 1
    record.info["connector_execution"] = ex


@event.listens_for(Pool, "checkin")
@event.listens_for(Pool, "detach")
def _on_checkin(_dbapi_connection, record) -> None:
    ex = record.info.pop("connector_execution", None)
    if ex is not None:
        ex.pool_connections -= 1


def before_remote_call(method: str) -> None:
    """Called by the connector HTTP client before every store request."""
    ex = current()
    if ex is None:
        return
    if ex.has_open_transaction():
        raise RemoteCallInTransaction(
            f"{method} to the store while a {ex.connector} transaction is open")
    ex.check_alive()


@contextlib.asynccontextmanager
async def identity_lock(key: str) -> AsyncIterator[None]:
    """Serialize the sends of one remote identity across workers, with no transaction
    open in the task: a session-level advisory lock on the boundary's own
    autocommit connection, held across the fresh read, the remote write and the
    result, and released after them."""
    ex = current()
    if ex is not None and ex.has_open_transaction():
        raise RemoteCallInTransaction("an identity lock is taken with no transaction open")
    import celerp.db as db

    with _internal():
        conn = await db.lifecycle_engine.connect()
    try:
        postgres = conn.dialect.name != "sqlite"
        if postgres:
            with _internal():
                conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
                await conn.execute(sa.text("SELECT pg_advisory_lock(hashtextextended(:k, 0))"), {"k": key})
        try:
            yield
        finally:
            if postgres:
                with _internal():
                    await conn.execute(sa.text("SELECT pg_advisory_unlock(hashtextextended(:k, 0))"), {"k": key})
    finally:
        with _internal():
            await conn.close()


# -- lease transactions ---------------------------------------------------------------

def _sessions():
    import celerp.db as db
    return db.SessionLocal


def _lifecycle_sessions():
    import celerp.db as db
    return db.LifecycleSessionLocal


async def _bound(session) -> bool:
    """Bound this lifecycle transaction's waits; False on SQLite, which has none."""
    if session.get_bind().dialect.name == "sqlite":
        return False
    await session.execute(sa.text(f"SET LOCAL lock_timeout = '{_INTERNAL_LOCK_TIMEOUT_MS}ms'"))
    await session.execute(sa.text(f"SET LOCAL statement_timeout = '{2 * _INTERNAL_LOCK_TIMEOUT_MS}ms'"))
    return True


def _key(ex_or_company, connector=None, resources=None) -> dict:
    if isinstance(ex_or_company, Execution):
        ex = ex_or_company
        return {"c": ex.company_id, "k": ex.connector, "r": list(resources or ex.resources)}
    return {"c": str(ex_or_company), "k": connector, "r": list(resources or [])}


_IN = sa.bindparam("r", expanding=True)


async def _ensure_rows(session, key: dict) -> None:
    for table in ("connector_execution_leases", "connector_execution_heartbeats"):
        for resource in key["r"]:
            await session.execute(sa.text(
                f"INSERT INTO {table} (company_id, connector, resource) VALUES (:c, :k, :res) "
                "ON CONFLICT DO NOTHING"), {"c": key["c"], "k": key["k"], "res": resource})


def _is_lock_timeout(exc: DBAPIError) -> bool:
    from celerp.db import sqlstate
    return sqlstate(exc) == _LOCK_NOT_AVAILABLE


async def _acquire(ex: Execution, connector) -> None:
    """The guard and the lease, in one short transaction."""
    from celerp.connectors.ownership import ConnectorBusyError, lock_connector_operation
    from celerp.models.connector_first_sync import ConnectorFirstSync

    async with _sessions()() as s:
        postgres = await _bound(s)
        try:
            config = await lock_connector_operation(s, ex.company_id, ex.connector, require_owner=True)
            first_sync = await s.get(ConnectorFirstSync, (ex.company_id, ex.connector))
            for resource in ex.resources:
                reason = eligible(connector, config, first_sync, ex.purpose, resource)
                if reason is not None:
                    raise ExecutionRefused(reason)
            key = _key(ex)
            await _ensure_rows(s, key)
            nowait = " NOWAIT" if postgres else ""
            lock = " FOR UPDATE" if postgres else ""
            leases = (await s.execute(sa.text(
                "SELECT resource, generation, holder FROM connector_execution_leases "
                "WHERE company_id = :c AND connector = :k AND resource IN :r ORDER BY resource"
                + lock + nowait).bindparams(_IN), key)).all()
            beats = (await s.execute(sa.text(
                "SELECT resource, holder, expires_at, now() AS db_now FROM connector_execution_heartbeats "
                "WHERE company_id = :c AND connector = :k AND resource IN :r ORDER BY resource"
                + lock).bindparams(_IN), key)).all()
        except DBAPIError as exc:
            await s.rollback()
            if _is_lock_timeout(exc):
                raise ExecutionBusy(f"{ex.connector} is busy") from exc
            raise
        except ConnectorBusyError as exc:
            await s.rollback()
            raise ExecutionBusy(str(exc)) from exc
        for beat in beats:
            expires = _aware(beat.expires_at)
            if beat.holder is not None and expires is not None and expires > _aware(beat.db_now):
                await s.rollback()
                raise ExecutionBusy(f"{ex.connector} {beat.resource} is being synced")
        ex.generations = {row.resource: int(row.generation) + 1 for row in leases}
        await s.execute(sa.text(
            "UPDATE connector_execution_leases SET generation = generation + 1, holder = :h "
            "WHERE company_id = :c AND connector = :k AND resource IN :r").bindparams(_IN),
            {**key, "h": ex.holder})
        row = (await s.execute(sa.text(
            "UPDATE connector_execution_heartbeats SET holder = :h, expires_at = now() + :ttl "
            "WHERE company_id = :c AND connector = :k AND resource IN :r "
            "RETURNING expires_at, now() AS db_now").bindparams(_IN),
            {**key, "h": ex.holder, "ttl": TTL})).first()
        await s.commit()
    _confirm(ex, row.expires_at, row.db_now)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _confirm(ex: Execution, expires_at, db_now) -> None:
    ex.confirmed_expiry = _aware(expires_at)
    ex.clock_skew = _aware(db_now) - datetime.now(timezone.utc)


async def _renew_once(ex: Execution) -> bool:
    async with _lifecycle_sessions()() as s:
        await _bound(s)
        row = (await s.execute(sa.text(
            "UPDATE connector_execution_heartbeats SET expires_at = now() + :ttl "
            "WHERE company_id = :c AND connector = :k AND resource IN :r "
            "AND holder = :h AND expires_at > now() RETURNING expires_at, now() AS db_now"
        ).bindparams(_IN), {**_key(ex), "h": ex.holder, "ttl": TTL})).all()
        await s.commit()
    if len(row) != len(ex.resources):
        ex.lost = True
        return False
    _confirm(ex, min(r.expires_at for r in row), row[0].db_now)
    return True


async def _renewal(ex: Execution) -> None:
    """Keep the heartbeat ahead of the TTL; mark the lease lost when it cannot be."""
    with _internal():
        loop = asyncio.get_running_loop()
        while not ex.lost and ex.resources:
            deadline = (ex.confirmed_expiry - MARGIN - ex._db_now()).total_seconds()
            timer = loop.call_later(max(deadline, 0), setattr, ex, "lost", True)
            try:
                await asyncio.sleep(RENEW_EVERY)
                while not ex.lost and ex.resources:
                    try:
                        if await _renew_once(ex):
                            break
                        return
                    except Exception as exc:  # retried until the deadline marks it lost
                        log.warning("renewing the %s lease failed: %s", ex.connector, exc)
                        await asyncio.sleep(RENEW_RETRY)
            finally:
                timer.cancel()


async def release(ex: Execution, resource: str) -> None:
    """Release one resource before the execution ends (a first sync releases each
    resource as its phase completes). A rerun recorded meanwhile stays on the
    heartbeat row, and the sweeper runs it."""
    ex.require(resource)
    await _release(ex, [resource])


async def _release(ex: Execution, resources: list[str], connector=None) -> list[str]:
    """Release ``resources`` and return the ones kept. With ``connector``, a resource
    whose recorded rerun may run now (for the webhooks, also a dirty mark on the
    config) is kept, its rerun taken (cleared in the same
    transaction), for the holder to run before it releases it; a rerun the
    directions no longer call for is dropped. Released resources leave
    ``ex.resources`` before any later transaction begins."""
    from celerp.models.connector_config import ConnectorConfig
    from celerp.models.connector_first_sync import ConnectorFirstSync

    taken: list[str] = []
    with _internal():
        async with _sessions()() as s:
            # Lease rows before heartbeat rows, each in resource order, as _acquire takes them.
            lock = " ORDER BY resource FOR UPDATE" if await _bound(s) else ""
            key = _key(ex, resources=sorted(resources))
            await s.execute(sa.text(
                "SELECT resource FROM connector_execution_leases WHERE company_id = :c "
                "AND connector = :k AND resource IN :r" + lock).bindparams(_IN), key)
            beats = (await s.execute(sa.text(
                "SELECT resource, holder, rerun_requested FROM connector_execution_heartbeats "
                "WHERE company_id = :c AND connector = :k AND resource IN :r" + lock
            ).bindparams(_IN).columns(sa.column("resource"), sa.column("holder"),
                                      sa.column("rerun_requested", sa.JSON)), key)).all()
            pending = [b.resource for b in beats if b.rerun_requested and b.holder is not None
                       and uuid.UUID(str(b.holder)) == ex.holder]
            config = None
            if connector is not None and not ex.lost and (pending or WEBHOOKS in resources):
                config = await s.scalar(sa.select(ConnectorConfig).where(
                    ConnectorConfig.company_id == ex.company_id, ConnectorConfig.connector == ex.connector))
                if WEBHOOKS in resources and WEBHOOKS not in pending and config is not None \
                        and config.webhooks_dirty:
                    pending.append(WEBHOOKS)
            if pending and config is not None:
                first_sync = await s.get(ConnectorFirstSync, (ex.company_id, ex.connector))
                reasons = {r: eligible(connector, config, first_sync, "rerun", r) for r in pending}
                taken = [r for r in pending if reasons[r] is None]
                cleared = [r for r in pending if reasons[r] in (None, "direction_off")]
                if cleared:
                    await s.execute(sa.text(
                        "UPDATE connector_execution_heartbeats SET rerun_requested = CAST('[]' AS json) "
                        "WHERE company_id = :c AND connector = :k AND resource IN :r AND holder = :h"
                    ).bindparams(_IN), {**_key(ex, resources=cleared), "h": ex.holder})
            for resource in resources:
                if resource in taken:
                    continue
                await s.execute(sa.text(
                    "UPDATE connector_execution_leases SET holder = NULL WHERE company_id = :c "
                    "AND connector = :k AND resource = :res AND holder = :h AND generation = :g"),
                    {"c": ex.company_id, "k": ex.connector, "res": resource, "h": ex.holder,
                     "g": ex.generations[resource]})
                await s.execute(sa.text(
                    "UPDATE connector_execution_heartbeats SET holder = NULL, expires_at = NULL "
                    "WHERE company_id = :c AND connector = :k AND resource = :res AND holder = :h"),
                    {"c": ex.company_id, "k": ex.connector, "res": resource, "h": ex.holder})
            await s.commit()
    for resource in resources:
        if resource not in taken:
            ex.resources.remove(resource)
    return taken


async def _run_reruns(ex: Execution, connector) -> None:
    """Before the holder releases: run, once, each rerun recorded on its resources
    while it held them. A first sync leaves them to the sweeper, which runs them
    once the first sync has completed."""
    if ex.purpose == "first_sync":
        return
    for _ in range(_RERUN_ROUNDS):
        if ex.lost or not ex.resources:
            return
        for resource in await _release(ex, list(ex.resources), connector):
            try:
                await _rerun(connector, ex, resource)
            except Exception as exc:  # recorded again, for the next holder or the sweeper
                log.warning("the %s %s rerun failed: %s", ex.connector, resource, exc)
                with _internal():
                    await record_rerun(ex.company_id, ex.connector, resource)
                return


async def _rerun(connector, ex: Execution, resource: str) -> None:
    """The work a rerun asks for: the resource's syncs with the reconciliation pass,
    or the webhook reconcile."""
    from celerp.connectors.sync_runner import run_connector_sync
    from celerp.connectors.webhook_reconcile import reconcile_webhooks

    if resource == WEBHOOKS:
        await reconcile_webhooks(ex)
        return
    results = await run_connector_sync(connector, ex.company_id, purpose="rerun", reconcile=True,
                                       resource=resource)
    if busy := next((r for r in results if r.busy), None):
        # Refused while modules change: asked again, never dropped.
        raise ExecutionBusy(busy.errors[0])


async def sweep_reruns() -> None:
    """Start an execution for each recorded rerun, and each connection whose webhooks
    are marked dirty, that no live execution holds and whose connection may run it; the execution runs it as it releases. A busy or
    refused one waits for the next tick; one the directions no longer call for is
    dropped. Runs in a task of its own, outside any execution."""
    with _internal():
        async with _sessions()() as s:
            rows = (await s.execute(sa.text(
                "SELECT h.company_id, h.connector, h.resource FROM connector_execution_heartbeats h "
                "JOIN connector_first_syncs f ON f.company_id = h.company_id AND f.connector = h.connector "
                "WHERE CAST(h.rerun_requested AS text) <> '[]' "
                "AND (h.holder IS NULL OR h.expires_at <= now()) "
                "AND (f.state IN (:done) OR (h.resource = :w AND f.state NOT IN (:new))) "
                "UNION SELECT c.company_id, c.connector, CAST(:w AS VARCHAR) FROM connector_configs c "
                "JOIN connector_first_syncs f ON f.company_id = c.company_id AND f.connector = c.connector "
                "LEFT JOIN connector_execution_heartbeats h ON h.company_id = c.company_id "
                "AND h.connector = c.connector AND h.resource = :w "
                "WHERE c.webhooks_dirty = :dirty AND f.state NOT IN (:new) "
                "AND (h.holder IS NULL OR h.expires_at <= now()) "
                "ORDER BY 1, 2, 3"), {"w": WEBHOOKS, "done": "completed", "new": "pending",
                                      "dirty": True})).all()
            await s.commit()
    for company_id, connector, resource in rows:
        try:
            async with connector_execution(company_id, connector, "rerun", [resource], admission_wait=0):
                pass
        except (ExecutionBusy, AdmissionBusy):
            continue
        except ExecutionRefused as exc:
            if exc.reason == "direction_off":
                with _internal():
                    async with _sessions()() as s:
                        await s.execute(sa.text(
                            "UPDATE connector_execution_heartbeats SET rerun_requested = CAST('[]' AS json) "
                            "WHERE company_id = :c AND connector = :k AND resource = :res "
                            "AND (holder IS NULL OR expires_at <= now())"),
                            {"c": company_id, "k": connector, "res": resource})
                        await s.commit()
            log.info("rerun of %s %s not run: %s", connector, resource, exc.reason)
        except Exception as exc:
            log.warning("rerun of %s %s failed: %s", connector, resource, exc)


async def rerun_sweeper_loop() -> None:
    """Every SWEEP_EVERY seconds, run the reruns no live execution holds. Started
    from the API lifespan beside the daily scheduler, which stays the backstop."""
    while True:
        try:
            await sweep_reruns()
        except Exception as exc:
            log.error("rerun sweeper: %s", exc)
        await asyncio.sleep(SWEEP_EVERY)


async def record_rerun(company_id, connector: str, resource: str, *, session=None) -> None:
    """Ask the next execution of ``resource`` (or the sweeper) to run its work once
    more. In ``session`` when given, else in its own short transaction; raises when it
    cannot be saved."""
    key = {"c": str(company_id), "k": connector, "res": resource, "list": f'["{resource}"]'}
    statement = sa.text(
        "INSERT INTO connector_execution_heartbeats (company_id, connector, resource, rerun_requested) "
        "VALUES (:c, :k, :res, CAST(:list AS json)) ON CONFLICT (company_id, connector, resource) "
        "DO UPDATE SET rerun_requested = CAST(:list AS json)")
    if session is not None:
        await session.execute(statement, key)
        return
    with _internal():
        async with _sessions()() as s:
            await _bound(s)
            await s.execute(statement, key)
            await s.commit()


async def live_executions(session, company_id=None, connector: str | None = None) -> list[tuple[str, str, str]]:
    """(company, connector, resource) of every live lease, optionally of one company
    and connector; read without a row lock."""
    query = ("SELECT company_id, connector, resource FROM connector_execution_heartbeats "
             "WHERE holder IS NOT NULL AND expires_at > now()")
    params: dict = {}
    if company_id is not None:
        query += " AND company_id = :c"
        params["c"] = str(company_id)
    if connector is not None:
        query += " AND connector = :k"
        params["k"] = connector
    with _internal():
        rows = (await session.execute(sa.text(query + " ORDER BY 1, 2, 3"), params)).all()
    return [tuple(r) for r in rows]


async def refuse_live_execution(session, company_id, connector: str | None = None) -> None:
    """For a person's exclusive operation on a company (disconnect, reset, restore):
    refuse while one of the company's executions is live."""
    from celerp.connectors.ownership import ConnectorBusyError
    from celerp.connectors.registry import service_name
    from ui.i18n import t

    live = await live_executions(session, company_id, connector)
    if live:
        raise ConnectorBusyError(t("error.connector_sync_running_company", service=service_name(live[0][1])))


async def forget_first_sync(session, company_ids, connector: str) -> None:
    """With a connection's config: its first-sync record. Its lease and heartbeat rows
    stay: the caller refused while one was live, a lease row is changed only by an
    acquire, takeover or release (1.4), so no fence ever meets a delete, and a later
    connection's execution takes them over. One statement per owner, in id order, so each
    names one company (a legacy record names the instance id alone)."""
    for company_id in sorted(set(map(str, company_ids))):
        await session.execute(sa.text(
            "DELETE FROM connector_first_syncs WHERE connector = :k AND company_id = :c"
        ), {"k": connector, "c": company_id})


async def _credentials(ex: Execution):
    """The store context, fetched with no transaction open."""
    from celerp.connectors import relay_token

    with _internal():
        ctx = await relay_token.fetch_context(ex.company_id, ex.connector)
    if ctx is None:  # no relay session: nothing can reach the store
        raise ExecutionRefused("not_connected")
    return ctx


@contextlib.asynccontextmanager
async def connector_execution(
    company_id, connector, purpose: str, resources: list[str], *,
    admission_wait: float | None = None,
) -> AsyncIterator[Execution]:
    """Run connector work for ``resources`` of one connection (``connector`` is the
    connector or its registered name).

    ``admission_wait`` bounds the wait for a free slot (None waits; 0 only tries);
    AdmissionBusy when none frees. ExecutionBusy when a resource is held,
    ExecutionRefused when `eligible` refuses, LeaseLost when the lease ends early."""
    from celerp import connectors

    if isinstance(connector, str):
        connector = connectors.get(connector)
    connector_name = connector.name
    company_id = str(company_id)
    resources = sorted(set(resources))
    mine = current()
    if mine is not None:
        if mine.company_id == company_id and mine.connector == connector_name and set(resources) <= set(mine.resources):
            yield mine
            return
        raise NestedExecutionError(
            f"{connector_name} {resources} requested inside an execution holding {mine.resources}")
    slots = _slots()
    try:
        if admission_wait is None:
            await slots.acquire()
        else:
            await asyncio.wait_for(slots.acquire(), timeout=max(admission_wait, 0.001))
    except asyncio.TimeoutError as exc:
        raise AdmissionBusy("every connector execution slot is in use") from exc
    ex = Execution(company_id=company_id, connector=connector_name, purpose=purpose,
                   resources=list(resources), task=asyncio.current_task())
    renewal = None
    try:
        with _internal():
            await _acquire(ex, connector)
        renewal = asyncio.create_task(_renewal(ex))
        try:
            ex.ctx = await _credentials(ex)
            token = _CURRENT.set(ex)
            try:
                yield ex
                await _run_reruns(ex, connector)
            finally:
                _CURRENT.reset(token)
        finally:
            renewal.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await renewal
            try:
                await _release_all(ex)
            except Exception as exc:  # the lease expires on its own after TTL
                log.warning("releasing the %s lease failed: %s", connector_name, exc)
    finally:
        slots.release()


async def _release_all(ex: Execution) -> None:
    if ex.resources:
        await _release(ex, list(ex.resources))
