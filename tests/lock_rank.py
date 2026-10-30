# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Lock rank registry: every lock a transaction takes, classified, ordered and recorded.

Every statement sent to Postgres passes through ``record_statement`` (asyncpg through the
adapter cursor, so raw ``conn.connection.cursor()`` calls are seen too; other drivers
through ``before_cursor_execute``). Each lock is reduced to a CLASS:

- an advisory lock: ``adv:<prefix>`` for a text key (the part before the first ``:``),
  ``adv:<name>`` for a numeric key named in ``NUMERIC_KEYS`` (any other numeric key is
  ``adv:?<function that took it>``, which no rank names);
- a row lock: ``row:<table>`` for ``SELECT ... FOR UPDATE / NO KEY UPDATE / SHARE / KEY
  SHARE`` and for the implicit row lock of ``UPDATE``, ``DELETE`` and ``ON CONFLICT DO
  UPDATE`` on the target table.

Locks are held per transaction, keyed by the database connection; a transaction ends at a
real COMMIT/ROLLBACK or when a Session's outermost transaction ends (the suite's sessions
commit into savepoints of one outer transaction, so the Session boundary is the production
one). Session-level advisory locks stay held until unlocked.

Each acquisition while other classes are held adds an edge held -> acquired. ``RANKS`` is
the one map: a class not in it, or an edge from a higher rank to a lower one, is a
violation. The rank table in the design is generated from ``RANKS`` (``python tests/lock_rank.py
table``); the observed graph is merged with ``python tests/lock_rank.py report <dir>``.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import contextvars
import functools
import itertools
import json
import os
import weakref
import re
import sys
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

# ── The one map ────────────────────────────────────────────────────────────────
# class -> rank. Lower ranks are taken first. Filled from the measured graph.
RANKS: dict[str, int] = {
    # Instance-wide: schema, version, a backup's restore claim (company_backup._claim: the
    # first lock a restore takes, before a bootstrap restore waits on bootstrap), bootstrap.
    "adv:migration": 0, "adv:version-admit": 1, "adv:version-fence": 2, "adv:restore-claim": 3,
    "adv:bootstrap": 4, "adv:migration-scan": 5, "adv:jti-cleanup": 6, "adv:migration-run": 9,
    "row:alembic_version": 7, "row:system_runtime_state": 8,
    # A restore's new company's attachment files (only ever tried).
    "adv:restore-landing": 12,
    # A company's gate for one module (registry.hold_module_gate: before the module state).
    "adv:module-gate": 13,
    # Module state, one restore per owner, then sign-in (auth.py hold_companyless_login:
    # before any company row).
    "adv:module-state": 14, "adv:restore-names": 15, "adv:direct-sign-in": 16, "row:users": 17,
    "row:password_resets": 18, "row:password_reset_notices": 19,
    # Connector guard (R0, R1), the run lock and the outbound identity lock (taken with no
    # transaction open in the task, on its own connection).
    "adv:connector-runtime": 20, "adv:connector-owner": 21, "adv:sync-run": 22, "adv:outbound": 23,
    # Execution lease (fence, KEY SHARE) and its heartbeat. Every FOR UPDATE on either table runs
    # in a transaction that holds nothing else, and acquire/takeover use NOWAIT.
    "row:connector_execution_leases": 25, "row:connector_execution_heartbeats": 26,
    # A company's module settings (registry.share_company_modules): judged before a write's
    # company row, after the fence; a turn-off takes it after the module state.
    "adv:module-use": 27,
    # The company (R3) and what is read under it.
    "row:companies": 30, "row:user_companies": 32,
    "row:company_peer_identities": 33, "row:locations": 34, "row:sync_runs": 35,
    # A connection's config, then its first-sync record (a disconnect forgets it, a first sync
    # locks it, before either touches the store's identities).
    "row:connector_configs": 38, "row:connector_first_syncs": 39, "row:connector_sources": 43,
    # Shop identity namespaces, after the company and before any item row.
    "adv:woocommerce-order": 40, "adv:external-link": 41, "adv:external-product": 42,
    # Peer links between companies, after the company's own identity rows.
    "row:company_peer_grants": 45, "row:company_peer_accounts": 46, "row:company_peer_deliveries": 47,
    "row:company_peer_attention": 48,
    # Chart and posting (R3a).
    "row:accounts": 51, "row:bank_accounts": 52, "row:reconciliation_sessions": 53,
    "row:bank_statement_lines": 54, "row:unmatched_payments": 55, "row:reconciliation_rules": 56,
    "row:unmatched_refunds": 57, "row:payment_closures": 58,
    # Items (R6), then import batches (R7), the items' ledger rows and the outbound queue (R8).
    "row:projections": 60, "row:import_batches": 61, "row:ledger": 62, "row:outbound_queue": 63,
    "row:payment_recoveries": 64,
    # Bookkeeping rows written last.
    "row:migration_runs": 70, "row:migration_cleanup_tasks": 71, "row:migration_templates": 72,
    # A fetch's start takes its key before its row, which may not exist yet.
    "adv:xero-attachments": 73, "row:migration_attachment_fetches": 74, "adv:xero-attachments-round": 75,
    "row:migration_batch_files": 76, "row:migration_entity_maps": 77,
    "row:ai_conversations": 78, "row:ai_messages": 79,
    "row:ai_batch_jobs": 80, "row:instance_meta": 81,
    # A login's session generation, then its sessions: revoking or issuing a session follows
    # whatever change it ends (a role, a reset, a restore), so both are taken late.
    "row:user_auth_state": 83, "row:session_registry": 84, "row:doc_share_tokens": 85, "row:label_templates": 86,
    "row:marketplace_configs": 87, "row:work_centers": 88, "row:supporter_badges": 89, "row:ai_usage": 90,
    "row:ai_job_steps": 91,
    # Notices, written as their transaction's last statements (celerp/notifications/service.py
    # _defer): after every other lock and parent key, their own key with their row.
    "adv:notices": 95, "row:notifications": 96, "row:notification_reads": 97,
    # A login is deleted only by a bootstrap discard (celerp/services/migrations.py discard), as
    # the transaction's last write, after its company's notices.
    "key:users": 98,
}


# LOCK TABLE runs only in the whole-company protocols (company_reset, db_catalog._hold), each
# locking every table it names in one statement, after the company row (30): one rank for
# every table lock, given to a table exactly when its rows are ranked.
TABLE_LOCK_RANK = 36


# A parent row's key (PostgreSQL row locks are two locks: the key, which KEY SHARE shares
# and FOR UPDATE, DELETE or a key change take exclusively, and the rest of the row). A
# foreign-key check shares the parent's key, so it can wait only on a transaction holding
# that key exclusively; that holder takes it last and waits on nothing after it, so a key
# share closes no cycle and is never ordered. A row ranked above it keeps its key at its own
# rank: the key is taken with the row.
KEY_RANK = 95


def rank(cls: str) -> int | None:
    if cls.startswith("tbl:"):
        return TABLE_LOCK_RANK if rank("row:" + cls[4:]) is not None else None
    if cls in RANKS:
        return RANKS[cls]
    if cls.startswith("key:"):
        row = rank("row:" + cls[4:])
        return None if row is None else max(KEY_RANK, row)
    return RANKS.get(cls)


# Numeric advisory keys with a fixed value (module constant) -> class.
NUMERIC_KEYS: dict[int, str] = {
    4207320001: "adv:migration",          # celerp/db.py _MIGRATION_LOCK_KEY
    0x63656C6572700002: "adv:version-admit",  # celerp/migrations/compatibility.py _ADMIT_LOCK_KEY
    0x63656C72: "adv:version-fence",      # compatibility.py _FENCE_NAMESPACE (two-key form)
    0x43454C4552500001: "adv:bootstrap",  # celerp/services/bootstrap.py
    0x43454C4552500002: "adv:module-state",  # celerp/modules/registry.py
    0x43454C5250: "adv:jti-cleanup",      # celerp/services/session_tracker.py
}

# R5: the classes of one fixed key, which a holder's exclusive session lock keeps every other
# taker out of (the version fence's class is a namespace of one key per version).
_SERIAL_KEYS = frozenset(NUMERIC_KEYS.values()) - {"adv:version-fence"}

# The classes of keys that are not text: a text key's class must never be one of them.
_FIXED_CLASSES = frozenset(NUMERIC_KEYS.values())

_ADVISORY = re.compile(
    r"pg_(try_)?advisory_(xact_)?(lock|unlock)(_shared)?\s*\(([^()]*(?:\([^()]*\)[^()]*)*)\)", re.I)
_PARAM = re.compile(r"\$(\d+)|%\((\w+)\)s|:(\w+)")
# Driver placeholders other than asyncpg's $n, renumbered to $n before parsing.
_NAMED = re.compile(r"%\((\w+)\)s|%s|(?<![:\w]):([A-Za-z_]\w*)")
# Only a statement holding one of these words can take a row or table lock.
_LOCKING = re.compile(r"\b(FOR|INSERT|UPDATE|DELETE|LOCK)\b", re.I)
_INT = re.compile(r"^-?(\d+|0x[0-9a-fA-F]+)$")

_CONTENDED_S = 0.05


@dataclass
class Lock:
    cls: str
    mode: str
    site: str
    origin: str = ""  # the test statement that made the call ("" outside a test)
    key: str = ""     # what is locked: the advisory key, or a row lock's bound parameters
    in_execution: bool = False  # sent from a connector execution's body (not its own lease work)
    company: str = ""  # the one company whose rows the statement names (R1, R2), "" when it names none
    gated: bool = False  # an insert made under its company's exclusive lock (R2)
    flow: tuple = ()  # a session lock's taker: its thread and task (R5)
    row: str = ""     # the one row a row lock names by its primary key ("" when it names none)


# The connector guard (rule (c)): an execution takes 20 and 21 only in its guard + acquire
# transaction, which commits before its body runs.
_GUARD = ("adv:connector-runtime", "adv:connector-owner")
# Lease rows (rule (d)): an exclusive take holds nothing but the guard's shared 20/21 (or,
# taken NOWAIT, anything under the guard held exclusively by a maintenance protocol), the
# acquire or takeover (the take under the guard) does not wait, and a lease row (its
# generation is the fence) is written only after its FOR UPDATE.
_LEASE_ROWS = ("row:connector_execution_leases", "row:connector_execution_heartbeats")


# Taken outside the task's other transactions by design (1.11): the outbound identity lock,
# session-level, on the execution's own connection.
def _own_connection(lock: Lock) -> bool:
    return lock.cls == "adv:outbound" and lock.mode.startswith("session")


# Keys every taker only tries (pg_try_advisory_lock): no one waits on them, so holding one
# closes no cycle, on its own connection or another. A waiting take of one fails ("try-only").
_TRY_ONLY = ("adv:migration-run", "adv:xero-attachments-round", "adv:restore-landing")
# Keys every taker takes shared: a version's fence (compatibility.py; versions are kept apart by
# polling under the admit lock, never by an exclusive take). No one waits on them, so holding one
# closes no cycle either. An exclusive take of one fails ("shared-only").
_SHARED_ONLY = ("adv:version-fence",)
_NEVER_WAITED = _TRY_ONLY + _SHARED_ONLY


def _copy_state(value):
    """One connection's transaction state, copied for a savepoint: its containers and their
    sets of strengths. A Lock is never changed once a transaction holds it, so it is shared."""
    if isinstance(value, dict):
        return {k: set(v) if isinstance(v, set) else v for k, v in value.items()}
    return type(value)(value)


_FLOW_IDS = itertools.count(1)
# The block sessions the running task has open on a shared connection, outermost first
# (Recorder.block).
_BLOCKS: contextvars.ContextVar[tuple[int, ...]] = contextvars.ContextVar("lock_rank_blocks", default=())


@dataclass
class Recorder:
    held: dict[int, list[Lock]] = field(default_factory=lambda: defaultdict(list))
    session_held: dict[int, list[Lock]] = field(default_factory=lambda: defaultdict(list))
    edges: dict[tuple[str, str], dict] = field(default_factory=dict)
    classes: dict[str, dict] = field(default_factory=dict)
    contention: list[dict] = field(default_factory=list)
    violations: list[dict] = field(default_factory=list)
    seen: dict[str, dict] = field(default_factory=dict)  # each distinct violation, its first example
    # R5, R6, R7: each distinct wait across connections, with the fixed keys its flow held and the classes its task held.
    cross: dict[str, dict] = field(default_factory=dict)
    test: str = ""
    # The connections each task has used, and the task that last used each connection.
    task_conns: weakref.WeakKeyDictionary = field(default_factory=weakref.WeakKeyDictionary)
    conn_task: dict[int, weakref.ref] = field(default_factory=dict)
    # Connections several flows use at once: the test session's one connection carries the
    # request, AI job and lifecycle sessions, which production runs on connections of their
    # own. On one of these each task is its own transaction, keyed (connection, flow).
    shared: set[int] = field(default_factory=set)
    flows: weakref.WeakKeyDictionary = field(default_factory=weakref.WeakKeyDictionary)

    # Per transaction (connection): what it holds by (class, key) -> strengths, the
    # strengths of its explicit locks per class, and the last explicit key per class.
    taken: dict[int, dict[tuple[str, str], set[str]]] = field(default_factory=lambda: defaultdict(dict))
    explicit: dict[int, dict[str, set[str]]] = field(default_factory=lambda: defaultdict(dict))
    last_key: dict[int, dict[str, str]] = field(default_factory=lambda: defaultdict(dict))
    # Per transaction: the companies it inserted, uncommitted (R1).
    own: dict[int, set[str]] = field(default_factory=lambda: defaultdict(set))
    # Per transaction: the strengths it holds on each company's row, a foreign key's share
    # included (R4).
    company_rows: dict[int, dict[str, set[str]]] = field(default_factory=lambda: defaultdict(dict))
    # Per transaction: the company of each row the ORM is about to write by its primary key,
    # by (table, primary key): the row's WHERE names only the key (R4's census).
    rows: dict[int, dict[tuple[str, str], str]] = field(default_factory=lambda: defaultdict(dict))
    # Per transaction: its open savepoints, oldest first, each with the transaction state as
    # it stood when the savepoint was made. PostgreSQL releases every lock taken after a
    # savepoint when the transaction rolls back to it; releasing a savepoint keeps them.
    savepoints: dict[int, list[tuple[str, tuple]]] = field(default_factory=lambda: defaultdict(list))
    # R4 leaf takes: per transaction, each take outside its company's lock (with the sites that
    # retook it) after which no lock that can wait has been taken (on any connection). Its
    # transaction never waits holding that row, so at its end the take is proof-neutral; one
    # still open when the graph is read counts as ungated.
    leaves: dict[int, list[tuple[dict, str, set[str]]]] = field(default_factory=lambda: defaultdict(list))

    def _transaction_state(self) -> tuple:
        return self.held, self.taken, self.explicit, self.last_key, self.own, self.company_rows, self.rows

    def flow(self, conn_key: int):
        """The transaction a statement on ``conn_key`` belongs to: the connection's, or on a
        shared connection the running task's."""
        if conn_key not in self.shared:
            return conn_key
        try:
            task = asyncio.current_task()
        except RuntimeError:
            task = None
        if task is None:
            return conn_key
        if task not in self.flows:
            self.flows[task] = next(_FLOW_IDS)
        return (conn_key, self.flows[task], *_BLOCKS.get())

    @contextlib.contextmanager
    def block(self):
        """A block session on a shared connection: a session the code opens for one block,
        on a connection of its own in production. While it is open the task's statements on
        a shared connection are its transaction, which ends alone; what the task took before
        stays held on the task's own, as on a second connection (_other_connections)."""
        token = _BLOCKS.set((*_BLOCKS.get(), next(_FLOW_IDS)))
        try:
            yield
        finally:
            _BLOCKS.reset(token)

    def end(self, conn_key: int) -> None:
        """The connection's transaction ended: so did every flow's on it."""
        states = (*self._transaction_state(), self.savepoints, self.leaves)
        for key in [k for k in {k for state in states for k in state}
                    if isinstance(k, tuple) and k[0] == conn_key]:
            self._end(key)
        self._end(conn_key)

    def end_flow(self, conn_key: int) -> None:
        """A session's transaction on ``conn_key`` ended: the running flow's."""
        self._end(self.flow(conn_key))

    def _end(self, key) -> None:
        for state in (*self._transaction_state(), self.savepoints):
            state.pop(key, None)
        for info, _, sites in self.leaves.pop(key, ()):
            info.setdefault("leaf_sites", set()).update(sites)

    def clear_transactions(self) -> None:
        self._holders_wait(list(self.taken))
        for state in (*self._transaction_state(), self.savepoints):
            state.clear()
        self._spoil_leaves()

    def _task_connections(self) -> set:
        try:
            task = asyncio.current_task()
        except RuntimeError:
            return set()
        return set(self.task_conns.get(task, ())) if task else set()

    def _holders_wait(self, keys) -> None:
        """R6: a transaction (or another of its task's connections) waits, or may yet wait,
        holding what it took: no class it holds is one whose holders never wait. Rows it only
        inserted are counted apart: until it commits, only an insert of the same key waits on them."""
        for key in keys:
            strengths = defaultdict(set)
            for (cls, _), taken in self.taken.get(key, {}).items():
                strengths[cls] |= taken
            for h in [*self.session_held.get(key, ()), *self.held.get(key, ())]:
                strengths[h.cls].add(_strength(h))
            for cls, taken in strengths.items():
                info = self.classes.setdefault(cls, {"count": 0, "modes": set(), "sites": set()})
                count = "insert_holder_waits" if taken == {"insert"} else "holder_waits"
                info[count] = info.get(count, 0) + 1

    def _spoil_leaves(self) -> None:
        """A lock that can wait was taken (or the transactions are no longer followed): every
        pending leaf take's transaction may wait holding its row, so each counts as ungated."""
        for info, take, sites in (leaf for leaves in self.leaves.values() for leaf in leaves):
            _ungated(info, take)
            info.setdefault("counted_sites", set()).update(sites)
        self.leaves.clear()

    def _savepoint(self, conn_key: int, statement: str) -> bool:
        """SAVEPOINT, RELEASE SAVEPOINT and ROLLBACK TO SAVEPOINT; False for any other statement."""
        words = statement.split()
        upper = [w.upper() for w in words]
        if upper[:1] == ["SAVEPOINT"] and len(words) == 2:
            snapshot = tuple(_copy_state(state[conn_key]) if conn_key in state else None
                             for state in self._transaction_state())
            self.savepoints[conn_key].append((words[1], snapshot))
            return True
        if upper[:2] == ["RELEASE", "SAVEPOINT"] and len(words) == 3:
            self._unwind(self._savepoint_owner(conn_key, words[2]), words[2], keep=False)
            return True
        if upper[:3] == ["ROLLBACK", "TO", "SAVEPOINT"] and len(words) == 4:
            conn_key = self._savepoint_owner(conn_key, words[3])
            snapshot = self._unwind(conn_key, words[3], keep=True)
            if snapshot is not None:
                for state, before in zip(self._transaction_state(), snapshot):
                    if before is None:
                        state.pop(conn_key, None)
                    else:
                        state[conn_key] = _copy_state(before)
            return True
        return False

    def _savepoint_owner(self, conn_key, name: str):
        """The transaction that made the savepoint ``name``: on a shared connection the flow
        whose savepoints hold it, which need not be the running task's (AsyncSession.close()
        runs shielded, in a task of its own). Savepoint names are unique on a connection."""
        if not isinstance(conn_key, tuple) or any(n == name for n, _ in self.savepoints.get(conn_key, ())):
            return conn_key
        return next((key for key, stack in self.savepoints.items()
                     if isinstance(key, tuple) and key[0] == conn_key[0] and any(n == name for n, _ in stack)),
                    conn_key)

    def _unwind(self, conn_key: int, name: str, *, keep: bool) -> tuple | None:
        """Drop the savepoints made after ``name`` (and ``name`` itself unless ``keep``);
        the state ``name`` was made with, or None when it is not open."""
        stack = self.savepoints.get(conn_key, [])
        for i in range(len(stack) - 1, -1, -1):
            if stack[i][0] == name:
                snapshot = stack[i][1]
                del stack[i + 1 if keep else i:]
                return snapshot
        return None

    def statement(self, conn_key: int, statement: str, parameters, waited: float) -> None:
        head = statement.lstrip()[:20].upper()
        if head.startswith(("COMMIT", "ROLLBACK", "END")) and "SAVEPOINT" not in head and "TO " not in head:
            self.end(conn_key)
            return
        conn_key = self.flow(conn_key)
        if self._savepoint(conn_key, statement.strip()):
            return
        try:
            found = _locks(statement, parameters)
        except Exception as exc:  # an unparseable lock statement is itself a finding
            found = [(f"?unparsed:{type(exc).__name__}", "wait", "", "", "", "")]
        if not found:
            return
        site, origin = _site(), _origin()
        execution = sys.modules.get("celerp.connectors.execution")
        in_execution = execution is not None and execution.current() is not None
        for cls, mode, key, company, row, parent in found:
            if cls == "-unlock":
                self.release_session(conn_key, mode)
                continue
            if row and company:
                # A statement binding both a row's key and its company names that row's company,
                # as an ORM write does (name_row): its children are judged under that company.
                self.rows[conn_key][(cls.split(":", 1)[1], row)] = company
            elif row:
                company = self.rows[conn_key].get((cls.split(":", 1)[1], row), "")
            if parent and not company:
                company = self.rows[conn_key].get((_company_link(cls.split(":", 1)[1])[1], parent), "")
            self.acquire(conn_key, Lock(cls, mode, site, origin, key, in_execution, company, row=row), waited)

    def name_row(self, conn_key: int, table: str, row: str, company: str) -> None:
        """The ORM is about to write ``table``'s row ``row`` (its primary key), which belongs
        to ``company``: the UPDATE or DELETE that names the row by its key alone is judged
        under that company."""
        self.rows[self.flow(conn_key)][(table, str(row))] = str(company)

    def _own(self, conn_key: int, lock: Lock) -> bool:
        """R1: a row of a company this transaction inserted and has not committed. Nobody else
        sees the company, so nobody holds, waits on or inserts a row naming it: its locks are
        granted at once and make nobody wait. An insert is own only on a table whose every
        unique key holds the company (``_row_locks`` names the company only then); the
        company's own row is not (its slug is unique across companies)."""
        if not lock.company or lock.company not in self.own[conn_key]:
            return False
        return lock.cls != "row:companies" or lock.mode != "insert"

    def _gated(self, conn_key: int, lock: Lock) -> bool:
        """R2: an insert made while this transaction holds its company's row exclusively. Two
        such inserters of one key (every unique key holds the company) run one at a time, so
        it waits on, and makes wait, only an inserter that does not take the company lock;
        the offline check fails a decreasing edge through it when its table has one."""
        return lock.mode == "insert" and self._company_held(conn_key, lock)

    def _company_held(self, conn_key: int, lock: Lock) -> bool:
        """Whether this transaction holds the row of ``lock``'s company exclusively (NO KEY
        UPDATE or stronger): every other transaction that takes it waits for this one."""
        if not lock.company:
            return False
        held = self.taken[conn_key].get(("row:companies", repr((lock.company,))), ())
        return bool(set(held) & {"nokey", "update"})

    def _keys_gated(self, conn_key: int, lock: Lock) -> bool:
        """R3: another key of ``lock``'s class taken while this transaction holds a lock every
        taker of the class's keys waits on: its company's row exclusively."""
        return self._company_held(conn_key, lock)

    def _company_gate(self, conn_key: int, lock: Lock) -> str:
        """R4: ``lock`` is a row of a company whose row this transaction holds exclusively. A
        cycle through an edge into ``lock`` needs another transaction holding that row while it
        waits; one that took it under the company's lock cannot, since this transaction holds
        that lock, so what this transaction already holds, and of which company, does not enter
        it. The offline check fails the edge when ``lock``'s class has a taker without the
        company lock, at a strength that conflicts, that holds nothing of the company's row the
        gate conflicts with. The gate's strength, or ""."""
        if not self._company_held(conn_key, lock):
            return ""
        return max(self.company_rows[conn_key].get(lock.company, set()) & {"nokey", "update"},
                   key=_STRENGTH_ORDER.index)

    def _census(self, conn_key: int, lock: Lock, info: dict, retake: bool) -> None:
        """R4: count each take of a company's row made without that company's exclusive lock,
        with what the transaction then holds of the company's row ("none" when nothing, "unknown"
        when the statement names no one company): a gate that conflicts with that still makes
        the take wait or keeps the gate's holder out. The company's own row is the gate. A take
        is counted once a lock that can wait follows it in the process before its transaction
        ends (a leaf otherwise); a retake adds nothing to what its covering take holds. Only a
        take production code sends is counted (``_production_site``)."""
        table = lock.cls.split(":", 1)[1]
        if table == "companies":
            company = _companies_row(lock)
            if company:
                self.company_rows[conn_key].setdefault(company, set()).add(_strength(lock))
            return
        if not lock.cls.startswith(("row:", "key:")) or not _company_owned(table):
            return
        if lock.company and self._company_held(conn_key, lock) or not _production_site(lock.site):
            # A take the test itself sends is no production transaction: it proves nothing.
            return
        if lock.company and lock.company == _instance_id():
            # R4c: a legacy row names the install's instance id, which no companies row holds.
            return
        if self._table_held_exclusively(conn_key, table):
            # R4t: no other transaction holds or takes a row of the table until this one ends.
            return
        held = self.company_rows[conn_key].get(lock.company, ()) if lock.company else ("unknown",)
        company = max(held, key=_STRENGTH_ORDER.index) if held and lock.company else (held or ("none",))[0]
        take, site = f"{_strength(lock)}|{company}", f"{lock.site} [{_strength(lock)}|{company}]"
        info.setdefault("ungated_sites", set()).add(site)
        if not retake:
            self.leaves[conn_key].append((info, take, {site}))
            return
        for leaf in self.leaves.get(conn_key, ()):
            if leaf[0] is info:
                leaf[2].add(site)

    def _table_held_exclusively(self, conn_key: int, table: str) -> bool:
        """R4t: whether this transaction holds ``table`` EXCLUSIVE or stronger (a migration's
        ``lock_tables``). That conflicts with the ROW SHARE every row lock takes on its table and
        the ROW EXCLUSIVE every write takes, so while it is held no other transaction holds a
        row of the table: a row taken under it never waits on a row, and a gated taker of the
        table waits on the table lock, ranked as every table lock is, before it holds any row."""
        exclusive = {f"t{TABLE_MODES.index(m)}" for m in ("exclusive", "access exclusive")}
        return any(cls == f"tbl:{table}" and strengths & exclusive
                   for (cls, _), strengths in self.taken[conn_key].items())

    def _retake(self, conn_key: int, lock: Lock) -> bool:
        """Whether this connection already holds what ``lock`` asks for: the same key at no
        less strength, or (for an implicit write) an explicit lock of the class that covers
        it. A statement does not show which rows an UPDATE touches, and the helpers lock
        the rows they then write. A row or key lock naming several values (an IN list, a
        multi-row insert's references) is a retake when each value was locked on its own at no
        less strength: every row it can select is then already held."""
        w = _strength(lock)
        held = set(self.taken[conn_key].get((lock.cls, lock.key), ()))
        held |= {_strength(h) for h in self.session_held.get(conn_key, ()) if h.cls == lock.cls and h.key == lock.key}
        if _key_waits_only(lock, held):
            return True
        if not _explicit(lock.mode):
            held = held | self.explicit[conn_key].get(lock.cls, set())
        if any(_covers(h, w) for h in held):
            return True
        values = _row_values(lock) if _explicit(lock.mode) else ()
        return len(values) > 1 and all(
            any(_covers(h, w) or _key_waits_only(lock, {h}) for h in self.taken[conn_key].get((lock.cls, repr((v,))), ()))
            for v in values)

    def _take(self, conn_key: int, lock: Lock) -> None:
        """Note what this transaction now holds. A row named by its primary key is also held by
        that key alone, as a foreign-key share of it binds it: the share is then granted at once.
        A lock naming several values holds each value's rows as a lock naming it alone would."""
        self.taken[conn_key].setdefault((lock.cls, lock.key), set()).add(_strength(lock))
        values = _row_values(lock) if _explicit(lock.mode) else ()
        for value in values if len(values) > 1 else ():
            self.taken[conn_key].setdefault((lock.cls, repr((value,))), set()).add(_strength(lock))
        if lock.row:
            parent = "key:" + lock.cls.split(":", 1)[1]
            self.taken[conn_key].setdefault((parent, repr((lock.row,))), set()).add(_strength(lock))

    def _serial_gates(self) -> set[str]:
        """R5: the fixed keys this flow of control holds exclusively at session level, on any
        connection. While held, no other run takes them, whatever connection it uses."""
        flow = _flow()
        return {h.cls for locks in self.session_held.values() for h in locks
                if h.cls in _SERIAL_KEYS and _exclusive(h.mode) and h.flow == flow}

    def acquire(self, conn_key: int, lock: Lock, waited: float) -> None:
        info = self.classes.setdefault(lock.cls, {"count": 0, "modes": set(), "sites": set()})
        info["count"] += 1
        retake = self._retake(conn_key, lock)
        if lock.mode.split(" ")[-1] not in ("nowait", "try", "skip") and not retake:
            self._spoil_leaves()
            self._holders_wait({conn_key, *self._task_connections()})
        serial = self._serial_gates()
        # R5: the keys every take of the class was made under.
        info["serial"] = info["serial"] & serial if "serial" in info else serial
        info["modes"].add(lock.mode)
        info["sites"].add(lock.site)
        if waited >= _CONTENDED_S:
            self.contention.append({"cls": lock.cls, "mode": lock.mode, "site": lock.site,
                                    "seconds": round(waited, 3), "test": self.test})
        if rank(lock.cls) is None:
            self._violate("unknown", lock)
        if lock.cls in _GUARD and lock.in_execution:
            self._violate("guard", lock)
        if lock.cls in _LEASE_ROWS and not retake:  # a retake of rows held FOR UPDATE waits on none
            self._lease(conn_key, lock)
        own = self._own(conn_key, lock)
        if not own:
            self._census(conn_key, lock, info, retake)
        if lock.cls.startswith("key:") and _strength(lock) == "keyshare":
            self._take(conn_key, lock)
            return
        if own:
            info["own"] = info.get("own", 0) + 1
            self._take(conn_key, lock)
            return
        if lock.mode == "insert":
            lock.gated = self._gated(conn_key, lock)
            kind = "gated" if lock.gated else "ungated"
            info[kind] = info.get(kind, 0) + 1
        if lock.cls == "row:companies" and lock.mode == "insert" and lock.company:
            self.own[conn_key].add(lock.company)
        if self._retake(conn_key, lock):
            # Granted at once: it waits for nothing, so it orders nothing.
            info["retakes"] = info.get("retakes", 0) + 1
            self._take(conn_key, lock)
            return
        waits = lock.mode.split(" ")[-1] not in ("nowait", "try", "skip")
        if lock.cls in _TRY_ONLY and waits:
            self._violate("try-only", lock)
        if lock.cls in _SHARED_ONLY and _exclusive(lock.mode):
            self._violate("shared-only", lock)
        self._other_connections(conn_key, lock, waits, serial)
        # One entry per run of a class, in the order taken: a class taken again with nothing
        # else between adds no order, so the list stays as short as the order is.
        held = self.session_held[conn_key] + self.held[conn_key]
        decreasing, seen = False, {lock.cls}
        for i, h in enumerate(held):
            if h.cls in seen:
                continue
            seen.add(h.cls)
            # Classes this transaction held exclusively before h: a gate every other
            # transaction taking them waits on, so it cannot interleave with this edge.
            gate = ({g.cls for g in held[:i] if _exclusive(g.mode)} | serial) - {h.cls, lock.cls}
            gated = {x.cls for x in (h, lock) if x.gated}
            company_gated = "" if gated else self._company_gate(conn_key, lock)
            inverts = waits and h.cls not in _NEVER_WAITED and rank(lock.cls) is not None \
                and rank(h.cls) is not None and rank(lock.cls) < rank(h.cls)
            # R5: judged offline against every take of the class outside those keys.
            serialized = inverts and bool(gate & serial)
            self._edge(h, lock, waits, gate, gated, company_gated, serialized)
            if gated or company_gated or serialized:
                continue
            if inverts:
                decreasing = True
                self._violate("order", lock, held_lock=h)
        last = self.last_key[conn_key].get(lock.cls)
        if last is not None and last != lock.key and waits and _explicit(lock.mode):
            # Another key of a class this transaction holds, with nothing higher taken since:
            # keys of one class are taken in sorted order (by their helper), or two
            # transactions taking two keys in opposite orders wait on each other.
            self._edge(lock, lock, waits, set())
            if self._keys_gated(conn_key, lock):
                # R3: two takers holding the company's lock never interleave, so the order of their keys orders nothing; the
                # offline check fails the class when it has a taker of more than one key
                # without that lock.
                if not decreasing and lock.key < last:
                    info["gated_unsorted"] = info.get("gated_unsorted", 0) + 1
                    info.setdefault("unsorted_strengths", set()).add(_strength(lock))
            else:
                info["ungated_multi"] = info.get("ungated_multi", 0) + 1
                info.setdefault("multi_sites", set()).add(f"{lock.site} [{_strength(lock)}]")
                info.setdefault("multi_strengths", set()).add(_strength(lock))
                if not decreasing and lock.key < last:
                    self._violate("unsorted", lock)
        if lock.mode.startswith("session"):
            lock.flow = _flow()
            self.session_held[conn_key].append(lock)
            return
        self._take(conn_key, lock)
        if _explicit(lock.mode):
            self.explicit[conn_key].setdefault(lock.cls, set()).add(_strength(lock))
            self.last_key[conn_key][lock.cls] = lock.key
        runs = self.held[conn_key]
        if not runs or runs[-1].cls != lock.cls or _exclusive(lock.mode) and not _exclusive(runs[-1].mode) \
                or runs[-1].gated and not lock.gated:
            runs.append(lock)

    def _lease(self, conn_key: int, lock: Lock) -> None:
        strength = _strength(lock)
        if lock.mode == "write":
            if lock.cls == _LEASE_ROWS[0] and "update" not in self.explicit[conn_key].get(lock.cls, ()):
                self._violate("lease-unlocked", lock)
            return
        if strength != "update":
            return
        held = self.session_held[conn_key] + self.held[conn_key]
        others = [h for h in held if h.cls not in _LEASE_ROWS
                  and not (h.cls in _GUARD and _strength(h) == "shared")]
        # A whole-company maintenance protocol (company_reset) holds the guard exclusively, so
        # no execution runs; it takes its company's lease rows NOWAIT and so waits on none.
        maintenance = lock.mode.endswith("nowait") and any(
            h.cls == _GUARD[0] and _exclusive(h.mode) for h in held)
        if others and not maintenance:
            self._violate("lease-held", lock, held_lock=others[0])
        if lock.cls == _LEASE_ROWS[0] and any(h.cls in _GUARD for h in held) \
                and not lock.mode.endswith("nowait"):
            self._violate("lease-wait", lock)

    def _edge(self, h: Lock, lock: Lock, waits: bool, gate: set[str], gated: set[str] = frozenset(),
              company_gated: str = "", serialized: bool = False) -> None:
        edge = self.edges.get((h.cls, lock.cls))
        if edge is None:
            edge = self.edges[(h.cls, lock.cls)] = {
                "count": 0, "example": None, "waits": False, "gate": gate, "modes": set(),
                "call_sites": {}, "gated": set(), "company_gated": set(), "company_gated_modes": set()}
        edge["gate"] = edge["gate"] & gate
        edge["count"] += 1
        if waits and gated:
            # R2: judged offline against the inserted tables' ungated inserters.
            edge["gated"] |= gated
            waits = False
        if waits and company_gated:
            # R4: judged offline against the taken class's takers without the company lock,
            # with the strength of the company lock this transaction holds.
            edge["company_gated"].add(lock.cls)
            edge["company_gated_modes"].add(f"{company_gated} {_strength(h)}>{_strength(lock)}")
            edge.setdefault("company_example", {"held": h.site, "acquired": lock.site, "test": self.test})
            waits = False
        if serialized:
            edge["serialized"] = True
            waits = False
        edge["waits"] = edge["waits"] or waits
        if waits:
            combo = f"{_strength(h)}>{_strength(lock)}"
            edge["modes"].add(combo)
            # Both taken by one call: the order production runs. Taken by two test
            # statements sharing the suite's one outer transaction: recorded apart.
            if h.origin == lock.origin:
                edge.setdefault("call_modes", set()).add(combo)
                # Every site pair that takes this edge in one call, with its mode pairs: the
                # triage judges each pair, not the first example.
                edge["call_sites"].setdefault(f"{h.site} -> {lock.site}", set()).add(combo)
                if edge.get("call_example") is None:
                    edge["call_example"] = {"held": h.site, "acquired": lock.site, "test": self.test}
        if edge["example"] is None:
            edge["example"] = {"held": h.site, "acquired": lock.site, "test": self.test}

    def _other_connections(self, conn_key: int, lock: Lock, waits: bool, serial: set[str]) -> None:
        """A task that waits on one connection while it holds a lock on another can wait on
        itself: the lock it holds is released only when this wait ends. Every take ties its
        connection to the task, a try included, so what that connection holds is seen. Such a
        wait is judged offline: it cannot wait on itself if every take of its class held one of
        the fixed keys the flow holds exclusively (``serial``, R5), if no holder of its class
        ever waits holding it (R6), or if its class ranks above every class the task holds (R7)."""
        try:
            task = asyncio.current_task()
        except RuntimeError:
            task = None
        if task is None:
            return
        conns = self.task_conns.setdefault(task, set())
        for other in sorted(conns - {conn_key}, key=str) if waits else ():
            ref = self.conn_task.get(other)
            if ref is None or ref() is not task:
                continue
            holding = [h for h in self.session_held.get(other, []) + self.held.get(other, [])
                       if not _own_connection(h) and h.cls not in _NEVER_WAITED]
            if any(h.cls == lock.cls and h.key == lock.key and _conflict(h, lock) for h in holding):
                self._violate("self-deadlock", lock, held_lock=holding[0])
                break
            if holding:
                # Judged offline (R5, R6, R7): the wait ends if the flow's keys serialize the
                # class, if no holder of the class ever waits holding it, or if it ranks above
                # every class the task holds.
                v = {"kind": "cross-connection", "cls": lock.cls, "site": lock.site, "test": self.test,
                     "held": holding[0].cls, "held_site": holding[0].site, "gate": sorted(serial),
                     "holding": sorted({h.cls for h in holding}), "insert": _strength(lock) == "insert"}
                self.cross.setdefault(f"{violation_key(v)} {v['gate']} {v['holding']}", v)
                break
        conns.add(conn_key)
        self.conn_task[conn_key] = weakref.ref(task)

    def release_session(self, conn_key: int, cls: str) -> None:
        locks = self.session_held.get(conn_key, [])
        for i in range(len(locks) - 1, -1, -1):
            if locks[i].cls == cls:
                del locks[i]
                return

    def take_violations(self) -> list[dict]:
        """The violations since the last call; the graph keeps each distinct one once."""
        out, self.violations = self.violations, []
        for v in out:
            self.seen.setdefault(violation_key(v), v)
        return out

    def _violate(self, kind: str, lock: Lock, held_lock: Lock | None = None) -> None:
        self.violations.append({
            "kind": kind, "cls": lock.cls, "site": lock.site, "test": self.test,
            "held": held_lock.cls if held_lock else None, "held_site": held_lock.site if held_lock else None})

    def ungated_sites(self, classes=None, path: str = "") -> set[str]:
        """The sites that took a row of ``classes`` (every class when None) outside its
        company's lock, as "<file>:<line> <function> [<strength>|<company>]", limited to
        sites in files whose path contains ``path``."""
        return {s for c, v in self.classes.items() if classes is None or c in classes
                for s in v.get("ungated_sites", ()) if path in s.split(":", 1)[0]}

    def dump(self) -> dict:
        self._spoil_leaves()
        self._holders_wait(list(self.taken))
        return {
            "edges": [{"from": a, "to": b, **v, "gate": sorted(v["gate"]), "modes": sorted(v["modes"]),
                       "call_modes": sorted(v.get("call_modes", ())), "gated": sorted(v["gated"]),
                       "company_gated": sorted(v["company_gated"]),
                       "company_gated_modes": sorted(v["company_gated_modes"]),
                       "call_sites": {k: sorted(m) for k, m in v["call_sites"].items()}}
                      for (a, b), v in self.edges.items()],
            "classes": {k: {"count": v["count"], "modes": sorted(v["modes"]), "sites": sorted(v["sites"]),
                            "serial": sorted(v.get("serial", ())),
                            **{c: v.get(c, 0) for c in _COUNTS}, **{c: sorted(v.get(c, ())) for c in _SETS}}
                        for k, v in self.classes.items()},
            "contention": self.contention,
            "cross": list(self.cross.values()),
            "violations": list({**self.seen, **{violation_key(v): v for v in self.violations}}.values()),
        }


@contextlib.contextmanager
def ungated_takes(classes=None, *paths: str):
    """Yields a set that, once the block ends, holds the sites in files whose path contains one
    of ``paths`` that took a row of ``classes`` (every class when None) inside the block without
    its company's lock, as the census counts them: empty when every take was company-gated or a
    leaf (its transaction waited on nothing after it, so it held the row while waiting on
    nothing). Sites recorded before the block are set aside for it and put back after, so one an
    earlier test recorded still counts there."""
    recorder = RECORDER
    sets = ("ungated_sites", "leaf_sites", "counted_sites")
    kept = {(c, k): info.pop(k, set()) for c, info in recorder.classes.items()
            if classes is None or c in classes for k in sets}
    found: set[str] = set()
    try:
        yield found
    finally:
        # A take whose transaction is still open when the block ends is not yet a leaf.
        open_sites = {s for leaves in recorder.leaves.values() for _, _, sites in leaves for s in sites}
        for c, info in recorder.classes.items():
            if classes is not None and c not in classes:
                continue
            leaves = set(info.get("leaf_sites", ())) - set(info.get("counted_sites", ())) - open_sites
            found.update(s for s in info.get("ungated_sites", ()) if s not in leaves
                         and any(path in s.split(":", 1)[0] for path in paths))
        for (c, k), sites in kept.items():
            recorder.classes[c].setdefault(k, set()).update(sites)


def _ungated(info: dict, take: str) -> None:
    """R4: count a take of a company's row outside its company's lock that its transaction
    may hold while it waits."""
    info["company_ungated"] = info.get("company_ungated", 0) + 1
    info.setdefault("ungated_takes", set()).add(take)


def _flow() -> tuple:
    """This flow of control: its thread, and its task when one runs."""
    try:
        task = asyncio.current_task()
    except RuntimeError:
        task = None
    return threading.get_ident(), id(task) if task is not None else None


def _exclusive(mode: str) -> bool:
    return "exclusive" in mode or mode.startswith(("update", "no key update", "delete"))


def _explicit(mode: str) -> bool:
    """A lock the statement asks for by name (advisory, FOR ..., LOCK TABLE), whose key the
    statement shows; an implicit write, delete or insert lock does not."""
    return mode not in ("write", "delete", "insert")


_STRENGTH_ORDER = ("keyshare", "share", "nokey", "update")

# Table-lock conflicts (PostgreSQL 13.3.1), by mode number 1 (ACCESS SHARE) .. 8 (ACCESS EXCLUSIVE).
_TABLE_CONFLICTS = {1: {8}, 2: {7, 8}, 3: {5, 6, 7, 8}, 4: {4, 5, 6, 7, 8}, 5: {3, 4, 6, 7, 8},
                    6: {3, 4, 5, 6, 7, 8}, 7: {2, 3, 4, 5, 6, 7, 8}, 8: {1, 2, 3, 4, 5, 6, 7, 8}}


def _covers(h: str, w: str) -> bool:
    """True when holding strength ``h`` already grants ``w`` on the same thing: every lock
    another transaction can hold beside ``h`` leaves ``w`` free too."""
    if h == w:
        return True
    if h.startswith("t") and w.startswith("t"):
        return all(m in _TABLE_CONFLICTS[int(h[1:])] for m in _TABLE_CONFLICTS[int(w[1:])])
    if h in ("shared", "exclusive"):
        return h == "exclusive" and w == "shared"
    if h in _STRENGTH_ORDER and w in _STRENGTH_ORDER:
        return _STRENGTH_ORDER.index(h) >= _STRENGTH_ORDER.index(w)
    return False


def _key_waits_only(lock: Lock, held) -> bool:
    """A row taken FOR UPDATE (or deleted) that this transaction holds FOR NO KEY UPDATE:
    other transactions can hold only its key share, so the take waits only as the row's key
    does, and the statement takes that key as its own lock."""
    return lock.cls.startswith("row:") and _strength(lock) == "update" and "nokey" in held


def _row_values(lock: Lock) -> tuple:
    """The values a row or key lock's key binds (``_row_key``); () for any other lock."""
    if not lock.cls.startswith(("row:", "key:")) or not lock.key.startswith("("):
        return ()
    try:
        return tuple(ast.literal_eval(lock.key))
    except (ValueError, SyntaxError):
        return ()


def _strength(lock: Lock) -> str:
    """The lock strength that decides conflicts: shared/exclusive for advisory locks,
    PostgreSQL's four row-lock strengths for rows (an UPDATE takes NO KEY UPDATE, a
    DELETE takes UPDATE), ``insert`` for a unique-key wait, t1..t8 for table modes."""
    mode = lock.mode
    if lock.cls.startswith("tbl:"):
        return "t" + str(TABLE_MODES.index(mode.rsplit(" ", 1)[0]))
    if mode.startswith(("xact", "session")):
        return "shared" if "shared" in mode else "exclusive"
    for prefix, name in (("no key update", "nokey"), ("update", "update"), ("delete", "update"),
                         ("write", "nokey"), ("key share", "keyshare"), ("share", "share")):
        if mode.startswith(prefix):
            return name
    return mode


# Row-lock conflict table (PostgreSQL 13.3.2), advisory shared/exclusive, a unique-key wait
# between two inserts, and table modes.
_CONFLICTS = {
    ("shared", "exclusive"), ("exclusive", "shared"), ("exclusive", "exclusive"),
    ("keyshare", "update"), ("share", "nokey"), ("share", "update"),
    ("nokey", "share"), ("nokey", "nokey"), ("nokey", "update"),
    ("update", "keyshare"), ("update", "share"), ("update", "nokey"), ("update", "update"),
    ("insert", "insert"),
} | {(f"t{a}", f"t{b}") for a, bs in _TABLE_CONFLICTS.items() for b in bs}


def _conflict(a: Lock, b: Lock) -> bool:
    return (_strength(a), _strength(b)) in _CONFLICTS


def _deadlocks(forward: set[str], backward: set[str]) -> bool:
    """Whether T1 (holds a, wants b) and T2 (holds b, wants a) can wait on each other."""
    for f in forward:
        a1, b1 = f.split(">")
        for r in backward:
            b2, a2 = r.split(">")
            if (a1, a2) in _CONFLICTS and (b2, b1) in _CONFLICTS:
                return True
    return False


RECORDER = Recorder()
_ROOT = str(Path(__file__).resolve().parent.parent)


def _frames(frame):
    """``frame`` and its callers, innermost first, entering each SQLAlchemy greenlet_spawn
    at the frame its greenlet is suspended in (sqlalchemy.util.concurrency)."""
    while frame is not None:
        context = frame.f_locals.get("context") if frame.f_code.co_name == "greenlet_spawn" else None
        inner = getattr(context, "gr_frame", None)
        while inner is not None and inner is not frame:
            yield inner
            inner = inner.f_back
        yield frame
        frame = frame.f_back


def _site() -> str:
    """The innermost application frame (file:line function) that sent the statement, else
    the innermost test frame; a conftest.py wrapper (the shared session's turn taking) is
    the site only when nothing else in the project sent it. A statement sent from
    synchronous code SQLAlchemy runs in a greenlet (an ORM event hook, run_sync) is sent
    from that greenlet's frames, which the awaiting coroutine's stack does not hold."""
    frames = []
    for frame in _frames(sys._getframe(2)):
        path = frame.f_code.co_filename
        if path.startswith(_ROOT) and "/.venv/" not in path and not path.endswith("lock_rank.py"):
            frames.append((path, path.rsplit("/", 1)[-1], frame))
    for chosen in ((f for p, n, f in frames if "/tests/" not in p and not n.startswith("test_") and n != "conftest.py"),
                   (f for p, n, f in frames if n != "conftest.py"),
                   (f for p, n, f in frames)):
        for frame in chosen:
            return f"{os.path.relpath(frame.f_code.co_filename, _ROOT)}:{frame.f_lineno} {frame.f_code.co_name}"
    return "?"


def _origin() -> str:
    frame = sys._getframe(2)
    while frame is not None:
        path = frame.f_code.co_filename
        name = path.rsplit("/", 1)[-1]
        if path.startswith(_ROOT) and (name.startswith("test_") or "/tests/" in path) and "/.venv/" not in path:
            return f"{name}:{frame.f_lineno}"
        frame = frame.f_back
    return ""


def _callsite_function() -> str:
    frame = sys._getframe(2)
    while frame is not None:
        mod = frame.f_globals.get("__name__", "")
        if (mod.startswith("celerp") or mod.startswith("ui")) and mod != __name__:
            return f"{mod}.{frame.f_code.co_name}"
        frame = frame.f_back
    return "?"


def _param(token: str, parameters):
    m = _PARAM.fullmatch(token.strip())
    if m is None:
        return token.strip()
    if m.group(1):
        return parameters[int(m.group(1)) - 1]
    return parameters[m.group(2) or m.group(3)]


def _advisory_class(args: str, parameters) -> str:
    if "hashtext" in args:
        inner = re.search(r"hashtext(?:extended)?\s*\(\s*([^,()]+)", args).group(1)
        key = str(_param(inner, parameters)).strip("'")
        cls = "adv:" + key.split(":", 1)[0]
        # A text key named like a fixed key's class would be judged as that other lock:
        # left unranked, so the take fails as unknown until the key is renamed.
        return f"adv:?{cls} (named like a fixed key)" if cls in _FIXED_CLASSES else cls
    parts = [str(_param(p, parameters)) for p in args.split(",")]
    first = int(parts[0], 0) if _INT.match(parts[0]) else None
    if first is not None and first in NUMERIC_KEYS:
        return NUMERIC_KEYS[first]
    return f"adv:?{_callsite_function()}"


def _advisory_key(statement: str, parameters) -> str:
    m = _ADVISORY.search(statement)
    return ",".join(str(_param(p, parameters or ())) for p in m.group(5).split(",")) if m else ""


def _bound(n: int, names: tuple, parameters):
    ref = names[n - 1] if names else n
    try:
        return parameters[ref] if isinstance(ref, str) else parameters[ref - 1]
    except (KeyError, IndexError, TypeError):
        return None


def _row_key(numbers: tuple[int, ...] | None, names: tuple, parameters) -> str:
    """What a row lock locks, as far as the statement shows it: the bound values of the
    parameters its WHERE clause (or the referencing column) uses, in order; every bound value
    when it uses none. The same values on the same table select the same rows."""
    if numbers:
        values = [_bound(n, names, parameters) for n in numbers]
    else:
        values = list(parameters.values()) if isinstance(parameters, dict) else list(parameters or ())
    # An array (``= ANY(:ids)``) names its members, as an IN list does.
    values = [v for value in values for v in (value if isinstance(value, (list, tuple)) else (value,))]
    return repr(tuple(map(str, values)))


# ── Row and table locks, from the parse tree ───────────────────────────────────
_ROW_STRENGTH = {"LCS_FORKEYSHARE": "key share", "LCS_FORSHARE": "share",
                 "LCS_FORNOKEYUPDATE": "no key update", "LCS_FORUPDATE": "update"}
_WAIT_POLICY = {"LockWaitBlock": "wait", "LockWaitSkip": "skip", "LockWaitError": "nowait"}
TABLE_MODES = ("", "access share", "row share", "row exclusive", "share update exclusive", "share",
               "share row exclusive", "exclusive", "access exclusive")


def _numbered(statement: str) -> tuple[str, tuple]:
    """The statement with every driver placeholder as $n, and what each $n binds: the
    parameter's name (pyformat, named) or its position (format); () when it is already $n."""
    names: list = []
    if re.search(r"\$\d", statement):
        return statement, ()

    def number(m: re.Match) -> str:
        names.append(m.group(1) or m.group(2) or len(names) + 1)
        return f"${len(names)}"
    return _NAMED.sub(number, statement), tuple(names)


def _nodes(tree):
    """Every parse-tree node, pre-order, as (node type, body)."""
    stack = [tree]
    while stack:
        item = stack.pop()
        if isinstance(item, list):
            stack.extend(reversed(item))
        elif isinstance(item, dict):
            if len(item) == 1:
                (kind, body), = item.items()
                if kind[:1].isupper() and isinstance(body, dict):
                    yield kind, body
                    stack.extend(reversed(list(body.values())))
                    continue
            stack.extend(reversed(list(item.values())))


def _params(tree) -> tuple[int, ...] | None:
    numbers = tuple(body["number"] for kind, body in _nodes(tree) if kind == "ParamRef" and "number" in body)
    return numbers or None


def _from_tables(from_clause, ctes: set[str]) -> dict[str, str]:
    """alias or name -> table for every relation of one FROM list, through joins but not
    into subqueries (a subquery's own locking clause is its own node)."""
    found: dict[str, str] = {}
    stack = list(from_clause or ())
    while stack:
        item = stack.pop()
        (kind, body), = item.items()
        if kind == "RangeVar" and body["relname"] not in ctes:
            found[body.get("alias", {}).get("aliasname", body["relname"])] = body["relname"]
        elif kind == "JoinExpr":
            stack += [body["larg"], body["rarg"]]
    return found


@functools.lru_cache(maxsize=1)
def _app_tables() -> dict:
    """Every table the app declares: the core models and each default module's models, plus
    Alembic's own. A lock on any other table is on one a test created, which production never
    takes."""
    import importlib
    import pkgutil
    import sys

    import celerp.models
    from celerp.models.base import Base

    for info in pkgutil.iter_modules(celerp.models.__path__):
        importlib.import_module(f"celerp.models.{info.name}")
    for path in sorted((Path(_ROOT) / "default_modules").glob("*/celerp_*/models*.py")):
        if str(path.parent.parent) not in sys.path:
            sys.path.insert(0, str(path.parent.parent))
        importlib.import_module(f"{path.parent.name}.{path.stem}")
    return {**Base.metadata.tables, "alembic_version": None}


def _foreign_keys(table: str) -> dict[str, str]:
    """column -> referenced table, for one app table (empty for a table no model maps)."""
    mapped = _app_tables().get(table)
    if mapped is None:
        return {}
    return {fk.parent.name: fk.column.table.name for fk in mapped.foreign_keys}


@functools.lru_cache(maxsize=1)
def _parents() -> frozenset[str]:
    """Every table a foreign key of the app references."""
    return frozenset(fk.column.table.name for t in _app_tables().values() if t is not None
                     for fk in t.foreign_keys)


def _companies_row(lock: Lock) -> str:
    """The company whose row a lock on ``companies`` takes: the statement's company, or the
    one id of a foreign key's share."""
    if lock.company:
        return lock.company
    try:
        key = ast.literal_eval(lock.key)
    except (ValueError, SyntaxError):
        return ""
    return str(key[0]) if isinstance(key, tuple) and len(key) == 1 and key[0] is not None else ""


def _company_column(table: str) -> str:
    return "id" if table == "companies" else "company_id"


@functools.lru_cache(maxsize=None)
def _has_company(table: str) -> bool:
    """Whether the table's rows each belong to one company (a company column)."""
    mapped = _app_tables().get(table)
    return mapped is not None and _company_column(table) in mapped.c


# Tables whose rows belong to no one company: the installation's users, their sign-in state,
# password resets and badges, a user's own import files and templates, the runtime state, and
# the payments and refunds the installation holds for whichever company it matches them to.
# The census judges no take of them under a company's lock.
INSTALLATION_TABLES = frozenset({
    "migration_batch_files", "migration_templates", "password_reset_notices", "password_resets",
    "payment_closures", "payment_recoveries", "supporter_badges", "system_runtime_state",
    "unmatched_payments", "unmatched_refunds", "user_auth_state", "users"})


@functools.lru_cache(maxsize=None)
def _company_link(table: str) -> tuple[str, str] | None:
    """(column, parent) for a table without a company column whose every row names, by its
    one required foreign key to a table with one, the row of a company's table it belongs to:
    a job's steps, a conversation's messages, a run's entity maps. None otherwise."""
    mapped = _app_tables().get(table)
    if mapped is None or _has_company(table):
        return None
    links = {(fk.parent.name, fk.column.table.name) for fk in mapped.foreign_keys
             if fk.column.table.name != table and not fk.parent.nullable and _has_company(fk.column.table.name)}
    return links.pop() if len(links) == 1 else None


def _company_owned(table: str) -> bool:
    """Whether each row of the table belongs to one company: by its company column or its
    parent's."""
    return _has_company(table) or _company_link(table) is not None


@functools.lru_cache(maxsize=None)
def _company_keyed(table: str) -> bool:
    """Whether every unique key of the table, its primary key and its SECRET_KEYED column
    aside, holds the company: two inserts of one such key name one company. A primary key is
    a generated id."""
    from sqlalchemy import UniqueConstraint

    mapped = _app_tables().get(table)
    col = _company_column(table)
    if mapped is None or col not in mapped.c:
        return False
    keys = [c.columns for c in mapped.constraints if isinstance(c, UniqueConstraint)]
    keys += [i.columns for i in mapped.indexes if i.unique]
    keys += [[c] for c in mapped.columns if c.unique]
    secret = [SECRET_KEYED.get(table)]
    return all(col in names or names == secret for names in ([c.name for c in k] for k in keys))


@functools.lru_cache(maxsize=None)
def _serial_keyed(table: str) -> str | None:
    """The serial id column when the table's only unique key is that id: an insert that
    leaves it to the database carries no key another insert can carry, so it waits on no
    row of its own table. None for any other table."""
    mapped = _app_tables().get(table)
    if mapped is None:
        return None
    from sqlalchemy import Integer, UniqueConstraint

    pk = list(mapped.primary_key.columns)
    serial = len(pk) == 1 and isinstance(pk[0].type, Integer) and pk[0].autoincrement in (True, "auto")
    others = [c for c in mapped.constraints if isinstance(c, UniqueConstraint)]
    others += [i for i in mapped.indexes if i.unique]
    others += [c for c in mapped.columns if c.unique]
    return pk[0].name if serial and not others else None


# Tables whose only unique key is a uuid primary key every insert draws fresh (uuid.uuid4,
# by the model's default or the writer): no two inserts carry one key, so an insert waits on
# no row of its own table. tests/test_lock_rank_checker.py proves each table's writers do.
FRESH_KEYED = frozenset({"migration_cleanup_tasks", "notifications", "password_resets"})

# Tables whose every unique key beside a fresh uuid primary key holds a column naming a row of
# a FRESH_KEYED table the same function has just built: that row is this transaction's own, so
# no other insert carries the key and the insert waits on no row of its table.
# tests/test_lock_rank_checker.py proves each table's writers do.
FRESH_PARENTED = {"password_reset_notices": ("reset_id", "password_resets")}


def _insert_waits_on_no_key(table: str) -> bool:
    """Whether an insert into *table* can wait on no other transaction's row of it."""
    return table in FRESH_KEYED or table in FRESH_PARENTED

# Tables whose unique keys, those holding the company aside, are one column the writer draws
# from ``secrets`` (stored as is or as its hash): no two inserts carry one value, so that key
# never makes an insert of one company wait on another's. tests/test_lock_rank_checker.py
# proves each table's writers do.
SECRET_KEYED = {"doc_share_tokens": "token", "company_peer_grants": "token_hash"}


def _company_param(table: str, where, qualifier: str | None = None) -> tuple[int, ...] | None:
    """The parameter a WHERE clause binds the table's company column to, when that equality
    is the whole clause or one of its top-level AND terms: every row it selects names that
    company. For a table whose rows are their parent's company, the term is its parent key
    IN (SELECT the parent's key FROM the parent WHERE the parent's company term). None when no
    such term exists. With ``qualifier`` (a write joined to other tables) a column counts only
    when it is qualified by it, the written table's alias or name."""
    link = _company_link(table)
    if link is None:
        return _column_param(_company_column(table), where, qualifier)
    column, parent = link
    if parent == "companies":
        return _column_param(column, where, qualifier)
    target = next(fk.column.name for fk in _app_tables()[table].foreign_keys if fk.parent.name == column)
    for term in _and_terms(where):
        body = term.get("SubLink", {})
        select_ = body.get("subselect", {}).get("SelectStmt", {})
        picked = [t["ResTarget"]["val"] for t in select_.get("targetList", ())]
        if (body.get("subLinkType") == "ANY_SUBLINK" and _column_name(body.get("testexpr"), qualifier) == column
                and [_column_name(v) for v in picked] == [target]
                and [f.get("RangeVar", {}).get("relname") for f in select_.get("fromClause", ())] == [parent]):
            return _company_param(parent, select_.get("whereClause"))
    return None


def _parent_param(table: str, where, qualifier: str | None = None) -> tuple[int, ...] | None:
    """For a table whose rows are their parent's company, the one parameter a WHERE clause
    binds the parent key to by equality: every row it selects is that parent's. None otherwise."""
    link = _company_link(table)
    return _column_param(link[0], where, qualifier) if link and link[1] != "companies" else None


def _and_terms(where) -> list:
    """The top-level AND terms of a WHERE clause (the clause itself when it is no AND)."""
    if where is None:
        return []
    (kind, body), = where.items()
    return body["args"] if kind == "BoolExpr" and body["boolop"] == "AND_EXPR" else [where]


def _column_name(node, qualifier: str | None = None) -> str | None:
    """The column a column reference names (its last field); None for any other node, and,
    with ``qualifier``, for a reference not qualified by it."""
    fields = [f.get("String", {}).get("sval") for f in (node or {}).get("ColumnRef", {}).get("fields", [])]
    if qualifier is not None and fields[:-1] != [qualifier]:
        return None
    return fields[-1] if fields else None


@functools.lru_cache(maxsize=None)
def _row_key_column(table: str) -> str:
    """The one-column primary key of a table whose rows each belong to one company (the
    companies table aside), else ""."""
    mapped = _app_tables().get(table)
    pk = list(mapped.primary_key.columns) if mapped is not None else []
    return pk[0].name if len(pk) == 1 and _has_company(table) and table != "companies" else ""


def _row_param(table: str, where, qualifier: str | None = None) -> tuple[int, ...] | None:
    """The parameter a WHERE clause binds such a table's primary key to (as
    ``_company_param``): the statement writes at most that one row. None otherwise."""
    column = _row_key_column(table)
    return _column_param(column, where, qualifier) if column else None


def _column_param(column: str, where, qualifier: str | None = None) -> tuple[int, ...] | None:
    """The one parameter a WHERE clause binds ``column`` (qualified by ``qualifier``, when
    given) to by equality, as the whole clause or one of its top-level AND terms; None when no
    such term exists."""
    for term in _and_terms(where):
        (kind, body), = term.items()
        if kind != "A_Expr" or body.get("kind") != "AEXPR_OP" or body["name"][0]["String"]["sval"] != "=":
            continue
        for col, val in ((body.get("lexpr"), body.get("rexpr")), (body.get("rexpr"), body.get("lexpr"))):
            if _column_name(col, qualifier) == column:
                numbers = _params(val)
                if numbers and len(numbers) == 1:
                    return numbers
    return None


@functools.lru_cache(maxsize=8192)
def _row_locks(statement: str) -> tuple[tuple[tuple[str, str, tuple[int, ...] | None, tuple[int, ...] | None,
                                                    tuple[int, ...] | None, tuple[int, ...] | None], ...], tuple]:
    """(class, mode, parameter numbers of the key, of the company, of the one row's primary
    key, of the one parent whose company the rows are) for each row or table lock the statement
    takes, and the placeholder map. A statement the parser rejects raises."""
    from pglast.parser import parse_sql_json

    text, names = _numbered(statement)
    tree = json.loads(parse_sql_json(text))
    ctes = {body["ctename"] for kind, body in _nodes(tree) if kind == "CommonTableExpr"}
    out: list[tuple[str, str, tuple[int, ...] | None, tuple[int, ...] | None, tuple[int, ...] | None,
                    tuple[int, ...] | None]] = []
    for kind, body in _nodes(tree):
        if kind == "SelectStmt" and body.get("lockingClause"):
            tables = _from_tables(body.get("fromClause"), ctes)
            where = body.get("whereClause")
            key = _params(where)
            for clause in body["lockingClause"]:
                c = clause["LockingClause"]
                mode = f"{_ROW_STRENGTH[c['strength']]} {_WAIT_POLICY[c.get('waitPolicy', 'LockWaitBlock')]}"
                named = [r["RangeVar"]["relname"] for r in c.get("lockedRels", ())]
                for t in ([tables.get(n, n) for n in named] if named else list(tables.values())):
                    company, row, parent = _one_table(t, where) if len(tables) == 1 else (None, None, None)
                    shares_key = _foreign_key_share(t, _ROW_STRENGTH[c["strength"]])
                    out.append((f"{'key' if shares_key else 'row'}:{t}", mode, key, company, row, parent))
                    if mode.startswith("update ") and t in _parents():
                        out.append((f"key:{t}", mode, key, company, row, parent))
        elif kind == "LockStmt":
            mode = f"{TABLE_MODES[body['mode']]} {'nowait' if body.get('nowait') else 'wait'}"
            out += [(f"tbl:{r['RangeVar']['relname']}", mode, None, None, None, None) for r in body["relations"]]
        elif kind == "UpdateStmt":
            table, where = body["relation"]["relname"], body.get("whereClause")
            company, row, parent = _one_table(table, where, _qualifier(body, "fromClause"))
            out.append((f"row:{table}", "write", _params(where), company, row, parent))
            fks = _foreign_keys(table)
            for target in body.get("targetList", ()):
                t = target["ResTarget"]
                if t.get("name") in fks:
                    out.append((f"key:{fks[t['name']]}", "key share wait", _params(t.get("val")), None, None, None))
        elif kind == "DeleteStmt":
            table, where = body["relation"]["relname"], body.get("whereClause")
            key = _params(where)
            company, row, parent = _one_table(table, where, _qualifier(body, "usingClause"))
            out.append((f"row:{table}", "delete", key, company, row, parent))
            if table in _parents():
                out.append((f"key:{table}", "delete", key, company, row, parent))
        elif kind == "InsertStmt":
            table = body["relation"]["relname"]
            fks = _foreign_keys(table)
            cols = [c["ResTarget"]["name"] for c in body.get("cols", ())]
            select_ = body.get("selectStmt", {}).get("SelectStmt", {})
            rows = select_.get("valuesLists") or []
            # INSERT ... SELECT: its one target list is every row's values.
            picked = [t["ResTarget"]["val"] for t in select_.get("targetList", ())]
            company = None
            if _company_column(table) in cols and (table == "companies" or _company_keyed(table)):
                values = [r["List"]["items"][cols.index(_company_column(table))] for r in rows]
                if not rows and len(picked) == len(cols):
                    values = [picked[cols.index(_company_column(table))]]
                numbers = [_params(v) for v in values]
                if numbers and all(n and len(n) == 1 for n in numbers):
                    company = tuple(n[0] for n in numbers)
            # A one-row insert into a table whose rows are their parent's company names the parent
            # by its link column (as _parent_param does for a WHERE clause).
            link = _company_link(table)
            parent = _params(rows[0]["List"]["items"][cols.index(link[0])]) \
                if link and link[1] != "companies" and len(rows) == 1 and link[0] in cols else None
            if not _insert_waits_on_no_key(table) and (_serial_keyed(table) is None or _serial_keyed(table) in cols):
                out.append((f"row:{table}", "insert", None, company, None, parent))
            for col, ref in fks.items():
                if cols and col not in cols:
                    continue
                values = [r["List"]["items"][cols.index(col)] for r in rows if cols] or None
                out.append((f"key:{ref}", "key share wait", _params(values) if values else None, None, None, None))
            if body.get("onConflictClause", {}).get("action") == "ONCONFLICT_UPDATE":
                out.append((f"row:{table}", "write", None, company if table != "companies" else None, None, None))
    return tuple(dict.fromkeys(out)), names


def _one_table(table: str, where, qualifier: str | None = None) -> tuple[tuple[int, ...] | None, ...]:
    """(company, row, parent) parameters of a statement on one table (see ``_row_locks``)."""
    return (_company_param(table, where, qualifier), _row_param(table, where, qualifier),
            _parent_param(table, where, qualifier))


def _qualifier(body: dict, joined: str) -> str | None:
    """For an UPDATE or DELETE that reads other tables (its ``joined`` clause), the alias (or
    name) a column must be qualified by to be the written table's; None when it reads none."""
    if not body.get(joined):
        return None
    relation = body["relation"]
    return relation.get("alias", {}).get("aliasname") or relation["relname"]


def _company(numbers: tuple[int, ...] | None, names: tuple, parameters) -> str:
    """The one company the bound parameters name; "" when they name none or several."""
    values = {str(_bound(n, names, parameters)) for n in numbers or ()}
    return values.pop() if len(values) == 1 and "None" not in values else ""


def _locks(statement: str, parameters) -> list[tuple[str, str, str, str, str, str]]:
    """(class, mode, key, company, row, parent) for each lock the statement takes, ``row`` the
    primary key of the one company row a row lock, UPDATE or DELETE names by it, ``parent`` the
    key of the one parent row whose company the rows are ("" otherwise);
    ('-unlock', class, '', '', '', '') for an unlock."""
    out: list[tuple[str, str, str, str, str, str]] = []
    for m in _ADVISORY.finditer(statement):
        try_, xact, verb, shared, args = m.groups()
        cls = _advisory_class(args, parameters or ())
        if verb.lower() == "unlock":
            out.append(("-unlock", cls, "", "", "", ""))
            continue
        mode = ("xact" if xact else "session") + (" shared" if shared else " exclusive")
        out.append((cls, mode + (" try" if try_ else " wait"), _advisory_key(statement, parameters), "", "", ""))
    if out or not _LOCKING.search(statement):
        return out
    found, names = _row_locks(statement)
    out = []
    for cls, mode, numbers, company, row, parent in found:
        if cls.split(":", 1)[1] not in _app_tables():
            continue
        key = _row_key(numbers, names, parameters)
        row_id, parent_id = _company(row, names, parameters), _company(parent, names, parameters)
        # A multi-row insert is one insert per row: each company it names is inserted (R1, R2).
        companies = [company] if mode != "insert" else [(n,) for n in company or ()] or [company]
        out += [(cls, mode, key, c, row_id, parent_id)
                for c in dict.fromkeys(_company(n, names, parameters) for n in companies)]
    return out


def classify(statement: str, parameters) -> list[tuple[str, str]]:
    """(class, mode) for each lock the statement takes; ('-unlock', class) for an unlock."""
    return [(cls, mode) for cls, mode, *_ in _locks(statement, parameters)]


def record_statement(conn_key: int, statement: str, parameters, waited: float) -> None:
    RECORDER.statement(conn_key, statement, parameters, waited)


_INSTALLED = False


def install() -> None:
    """Route every Postgres statement and transaction end through the recorder."""
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True
    from sqlalchemy import event, inspect
    from sqlalchemy.dialects.postgresql import asyncpg as pg_asyncpg
    from sqlalchemy.engine import Engine
    from sqlalchemy.orm import Mapper, Session
    from sqlalchemy.pool import Pool

    cursor_cls = pg_asyncpg.AsyncAdapt_asyncpg_cursor
    conn_cls = pg_asyncpg.AsyncAdapt_asyncpg_connection
    real_execute = cursor_cls._prepare_and_execute
    real_executemany = cursor_cls._executemany

    async def _prepare_and_execute(self, operation, parameters):
        started = time.monotonic()
        try:
            return await real_execute(self, operation, parameters)
        finally:
            record_statement(id(self._adapt_connection), operation, parameters, time.monotonic() - started)

    async def _executemany(self, operation, seq_of_parameters):
        result = await real_executemany(self, operation, seq_of_parameters)
        for params in seq_of_parameters[:1]:
            record_statement(id(self._adapt_connection), operation, params, 0.0)
        return result

    cursor_cls._prepare_and_execute = _prepare_and_execute
    cursor_cls._executemany = _executemany
    for name in ("commit", "rollback", "close"):
        real = getattr(conn_cls, name)

        def _ended(self, _real=real):
            try:
                return _real(self)
            finally:
                RECORDER.end(id(self))
        setattr(conn_cls, name, _ended)

    @event.listens_for(Engine, "before_cursor_execute")
    def _other_drivers(conn, cursor, statement, parameters, context, executemany):
        if conn.dialect.name == "postgresql" and conn.dialect.driver != "asyncpg":
            record_statement(id(conn.connection.dbapi_connection), statement, parameters, 0.0)

    @event.listens_for(Pool, "close")
    @event.listens_for(Pool, "close_detached")
    def _closed(dbapi_connection, *_):
        # Postgres drops a closed connection's session locks; a later connection may reuse its id.
        RECORDER.end(id(dbapi_connection))
        RECORDER.shared.discard(id(dbapi_connection))
        for key in [k for k in RECORDER.session_held if k == id(dbapi_connection)
                    or isinstance(k, tuple) and k[0] == id(dbapi_connection)]:
            del RECORDER.session_held[key]

    @event.listens_for(Engine, "commit")
    @event.listens_for(Engine, "rollback")
    def _engine_end(conn):
        if conn.dialect.name == "postgresql" and conn.dialect.driver != "asyncpg":
            RECORDER.end(id(conn.connection.dbapi_connection))

    @event.listens_for(Mapper, "before_update")
    @event.listens_for(Mapper, "before_delete")
    def _name_row(mapper, connection, target):
        table = mapper.local_table.name
        if connection.dialect.name != "postgresql" or not _row_key_column(table):
            return
        state = inspect(target)
        if not state.identity or len(state.identity) != 1:
            return
        column = _company_column(table)
        company = state.committed_state.get(column, getattr(target, column, None))
        if company is not None:
            RECORDER.name_row(id(connection.connection.dbapi_connection), table, state.identity[0], company)

    @event.listens_for(Session, "after_transaction_end")
    def _session_end(session, transaction):
        if transaction.parent is not None or transaction.nested:
            return
        for conn in list(getattr(transaction, "_lock_rank_conns", ())):
            RECORDER.end_flow(conn)

    @event.listens_for(Session, "after_begin")
    def _session_begin(session, transaction, connection):
        root = transaction
        while root.parent is not None:
            root = root.parent
        try:
            key = id(connection.connection.dbapi_connection)
        except Exception:
            return
        conns = getattr(root, "_lock_rank_conns", None)
        if conns is None:
            conns = set()
            root._lock_rank_conns = conns
        conns.add(key)


def dump(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({**RECORDER.dump(), "writers": static_writers()}, indent=1, default=sorted))


# ── Census from code: every production function that sends a statement locking a company's rows ──

_WRITE_CALLS = {"insert": "insert", "pg_insert": "insert", "update": "write", "delete": "delete"}
_SA_MODULES = {"sa", "_sa", "sqlalchemy", "postgresql"}  # aliases the code imports SQLAlchemy as
_TEXT_DML = re.compile(r"\b(?:(INSERT)\s+INTO|UPDATE|DELETE\s+FROM)\s+\"?(\w+)", re.I)
_TEXT_FOR = re.compile(r"\bFROM\s+\"?(\w+)[^;]*?\bFOR\s+(NO\s+KEY\s+UPDATE|UPDATE|KEY\s+SHARE|SHARE)\b", re.I | re.S)


def _foreign_key_share(table: str, strength: str) -> bool:
    """FOR KEY SHARE of a parent row is the lock a foreign-key check takes: its key, shared
    (key:<t>, never a row:<t> take). The recorder and the census both ask this."""
    return strength == "key share" and table in _parents()


def _orm_strength(keywords) -> str:
    """The row-lock strength of ``with_for_update(read=, key_share=)`` keywords."""
    flag = {k.arg: isinstance(k.value, ast.Constant) and k.value.value is True for k in keywords}
    read, key = flag.get("read", False), flag.get("key_share", False)
    return {(True, True): "key share", (True, False): "share",
            (False, True): "no key update", (False, False): "update"}[(read, key)]



def _get_strength(value) -> str:
    """The row-lock strength of ``session.get(..., with_for_update=<value>)``: True, or a
    dict of ``with_for_update`` keywords."""
    if isinstance(value, ast.Dict):
        return _orm_strength(ast.keyword(arg=k.value, value=v) for k, v in zip(value.keys, value.values)
                             if isinstance(k, ast.Constant))
    return "update"


_PRODUCTION_PATH = re.compile(r"(celerp|default_modules/[^/]+/celerp_[^/]*)/")


def _instance_id() -> str:
    """The install's instance id as the connector code reads it (tests patch that name), or ""
    before any connector code has minted it. Never mints one itself."""
    ownership = sys.modules.get("celerp.connectors.ownership")
    if ownership is None:
        return ""
    from celerp import config

    read = ownership.ensure_instance_id
    return read() if read is not config.ensure_instance_id else (config.settings.gateway_instance_id or "")


def _production_site(site: str) -> bool:
    """Whether a recorded site ("path:line function") is in a file the census scans: the
    application and the first-party modules' packages, never a test or test helper."""
    return bool(_PRODUCTION_PATH.match(site))


def _production_files():
    root = Path(_ROOT)
    yield from sorted(p for p in [*root.joinpath("celerp").rglob("*.py"), *root.joinpath("default_modules").rglob("*.py")]
                      if _production_site(os.path.relpath(p, _ROOT)))


def _model_tables(trees) -> dict[str, str]:
    """Model class or Table variable name -> table name, from ``__tablename__`` and ``Table("t", ...)``."""
    out = {}
    for tree in trees:
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                for stmt in node.body:
                    if (isinstance(stmt, ast.Assign) and any(getattr(t, "id", "") == "__tablename__" for t in stmt.targets)
                            and isinstance(stmt.value, ast.Constant) and isinstance(stmt.value.value, str)):
                        out[node.name] = stmt.value.value
            elif (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)
                  and getattr(node.value.func, "id", getattr(node.value.func, "attr", "")) == "Table"
                  and node.value.args and isinstance(node.value.args[0], ast.Constant)):
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        out[t.id] = node.value.args[0].value
    return out


def _named_table(node, tables: dict[str, str]) -> str | None:
    """The table a statement's first argument names: a model, a Table variable, or a model's column."""
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in tables:
        return tables[node.value.id]
    return tables.get(node.attr if isinstance(node, ast.Attribute) else getattr(node, "id", None))


def _chain_root(node):
    """The first call of a method chain (``select(M)`` of ``select(M).where(...).with_for_update()``)."""
    while isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Call):
        node = node.func.value
    return node


def _text(node) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(v.value if isinstance(v, ast.Constant) else " x " for v in node.values)
    return ""


def _writes(call: ast.Call, tables: dict[str, str]) -> list[tuple[str, str]]:
    """(table, kind) for each row lock one call's statement takes."""
    name = call.func.id if isinstance(call.func, ast.Name) else getattr(call.func, "attr", "")
    module_call = isinstance(call.func, ast.Name) or getattr(getattr(call.func, "value", None), "id", "") in _SA_MODULES
    if name in _WRITE_CALLS and module_call and call.args and (t := _named_table(call.args[0], tables)):
        return [(t, _WRITE_CALLS[name])]
    if name == "with_for_update" and isinstance(call.func, ast.Attribute):
        root = _chain_root(call.func.value)
        if isinstance(root, ast.Call) and root.args and (t := _named_table(root.args[0], tables)):
            return _locking_read(t, _orm_strength(call.keywords))
    if name == "get" and call.args and (t := _named_table(call.args[0], tables)):
        for k in call.keywords:
            if k.arg == "with_for_update" and not (isinstance(k.value, ast.Constant) and k.value.value is not True):
                return _locking_read(t, _get_strength(k.value))
    if name == "text" and call.args:
        sql = _text(call.args[0])
        return ([(t, "insert" if ins else "write") for ins, t in _TEXT_DML.findall(sql)]
                + [w for t, strength in _TEXT_FOR.findall(sql)
                   for w in _locking_read(t, " ".join(strength.lower().split()))])
    return []


def _locking_read(table: str, strength: str) -> list[tuple[str, str]]:
    """A locking read's census entry: none for a parent's key share (the foreign-key check's
    lock), "key lock" for FOR UPDATE (it also holds the row's key: a new row referring to it
    waits), else "lock"."""
    if _foreign_key_share(table, strength):
        return []
    return [(table, "key lock" if strength == "update" else "lock")]


def _scan_writers(files) -> dict[str, set[tuple[str, str, str]]]:
    """table -> {(path, function, kind)}: every production function that sends a statement
    locking the table's rows, named as the recorder names a site (the innermost function)."""
    parsed = [(os.path.relpath(f, _ROOT), ast.parse(f.read_text())) for f in files]
    tables = _model_tables(tree for _, tree in parsed)
    out: dict[str, set[tuple[str, str, str]]] = defaultdict(set)

    def visit(path: str, node, function: str) -> None:
        for child in ast.iter_child_nodes(node):
            inner = child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else (
                "<lambda>" if isinstance(child, ast.Lambda) else function)
            if isinstance(child, ast.Call) and function:
                for table, kind in _writes(child, tables):
                    out[table].add((path, function, kind))
            visit(path, child, inner)
    for path, tree in parsed:
        visit(path, tree, "")
    return out


@functools.lru_cache(maxsize=1)
def static_writers() -> dict[str, list[str]]:
    """class -> sorted "path function" of every production function that takes a row lock of a
    company's table the recorder counts: updates, deletes and locking reads, and inserts into a
    table whose insert waits on a key of its own (not FRESH_KEYED or FRESH_PARENTED, not serial-keyed)."""
    out: dict[str, set[str]] = defaultdict(set)
    for table, found in _scan_writers(_production_files()).items():
        if table not in _app_tables() or not _company_owned(table):
            continue
        for path, function, kind in found:
            if kind == "insert" and (_insert_waits_on_no_key(table) or _serial_keyed(table) is not None):
                continue
            out[f"row:{table}"].add(f"{path} {function}")
    return {k: sorted(v) for k, v in out.items()}



def key_locks() -> list[str]:
    """Sorted "path function table" of every production function that locks a parent row
    FOR UPDATE: it holds the row's key too, so a new row referring to it waits until the
    transaction ends, and the recorder ranks the take as key:<t>. Every other lock that
    changes neither the key nor removes the row is FOR NO KEY UPDATE."""
    return sorted(f"{path} {function} {table}" for table, found in _scan_writers(_production_files()).items()
                  if table in _parents() for path, function, kind in found if kind == "key lock")

def _unexercised(g: dict) -> dict[str, list[str]]:
    """class -> the static writers of the class no recorded take was made from: their takes
    are unproven, so a company-gated edge of the class stays a violation."""
    out = {}
    for cls, writers in g["writers"].items():
        seen = {" ".join((s.split(" ", 1)[0].rsplit(":", 1)[0], s.split(" ", 1)[1]))
                for s in g["classes"].get(cls, {}).get("sites", ()) if " " in s}
        if missing := sorted(set(writers) - seen):
            out[cls] = missing
    return out


# ── Report: merge worker dumps into one graph ─────────────────────────────────

# Per class counts the offline check judges: inserts without their company's lock (R2), keys
# taken out of order under it, and second keys taken without it (R3), and every take of a
# company's row without its company's lock (R4).
_COUNTS = ("ungated", "gated_unsorted", "ungated_multi", "company_ungated", "holder_waits",
           "insert_holder_waits")
# The strengths of those R3 and R4 takes, where each was taken, and the R4 leaf takes' sites.
_SETS = ("unsorted_strengths", "multi_strengths", "multi_sites", "ungated_takes", "ungated_sites", "leaf_sites", "counted_sites")


def _merge(directory: Path) -> dict:
    edges: dict[tuple[str, str], dict] = {}
    classes: dict[str, dict] = {}
    contention, violations, cross, writers = [], [], [], defaultdict(set)
    for f in sorted(directory.glob("lock-graph-*.json")):
        d = json.loads(f.read_text())
        for k, v in d.get("writers", {}).items():
            writers[k].update(v)
        for e in d["edges"]:
            cur = edges.setdefault((e["from"], e["to"]),
                                   {"count": 0, "example": e["example"], "waits": False, "gate": set(e["gate"]),
                                    "modes": set(), "call_modes": set(), "call_example": None,
                                    "call_sites": {}, "gated": set(), "company_gated": set(),
                                    "company_gated_modes": set(), "company_example": None})
            cur["gate"] &= set(e["gate"])
            cur["gated"].update(e.get("gated", ()))
            cur["company_gated"].update(e.get("company_gated", ()))
            cur["company_gated_modes"].update(e.get("company_gated_modes", ()))
            cur["company_example"] = cur["company_example"] or e.get("company_example")
            cur["serialized"] = cur.get("serialized", False) or e.get("serialized", False)
            for pair, combos in e.get("call_sites", {}).items():
                cur["call_sites"].setdefault(pair, set()).update(combos)
            cur["modes"].update(e["modes"])
            cur["call_modes"].update(e.get("call_modes", ()))
            cur["call_example"] = cur["call_example"] or e.get("call_example")
            cur["count"] += e["count"]
            cur["waits"] = cur["waits"] or e["waits"]
        for k, v in d["classes"].items():
            cur = classes.setdefault(k, {"count": 0, "modes": set(), "sites": set(), **dict.fromkeys(_COUNTS, 0)})
            cur["count"] += v["count"]
            for c in _COUNTS:
                cur[c] += v.get(c, 0)
            for c in _SETS:
                cur.setdefault(c, set()).update(v.get(c, ()))
            cur["modes"].update(v["modes"])
            cur["sites"].update(v["sites"])
            serial = set(v.get("serial", ()))
            cur["serial"] = cur["serial"] & serial if "serial" in cur else serial
        contention += d["contention"]
        violations += d["violations"]
        cross += d.get("cross", [])
    return {"edges": edges, "classes": classes, "contention": contention, "violations": violations,
            "cross": cross, "writers": dict(writers)}


def _sccs(nodes, adj) -> list[list[str]]:
    index, low, stack, on, out, n = {}, {}, [], set(), [], [0]

    def visit(v):
        index[v] = low[v] = n[0]
        n[0] += 1
        stack.append(v)
        on.add(v)
        for w in adj.get(v, ()):
            if w not in index:
                visit(w)
                low[v] = min(low[v], low[w])
            elif w in on:
                low[v] = min(low[v], index[w])
        if low[v] == index[v]:
            comp = []
            while True:
                w = stack.pop()
                on.discard(w)
                comp.append(w)
                if w == v:
                    break
            out.append(comp)

    sys.setrecursionlimit(10000)
    for v in nodes:
        if v not in index:
            visit(v)
    return out


def report(directory: Path) -> str:
    g = _merge(directory)
    explicit = {c for c, v in g["classes"].items() if v["modes"] - {"write", "delete"}}
    keep = lambda c: c in explicit  # noqa: E731 - writes count only on explicitly locked tables
    adj: dict[str, set[str]] = defaultdict(set)
    for (a, b), v in g["edges"].items():
        if keep(a) and keep(b) and v["modes"]:
            adj[a].add(b)
    comps = _sccs(sorted(explicit), adj)
    cycles = [sorted(c) for c in comps if len(c) > 1]
    pairs = []  # two classes taken in both orders, with no gate common to both orders
    for (a, b), v in sorted(g["edges"].items()):
        back = g["edges"].get((b, a))
        if a < b and back and keep(a) and keep(b) and _deadlocks(v["modes"], back["modes"]):
            pairs.append((a, b, v, back, v["gate"] & back["gate"]))
    lines = ["# Observed lock graph", "", f"Classes (explicitly locked): {len(explicit)}", ""]
    lines += ["| Class | Acquisitions | Modes | Sites |", "|---|---|---|---|"]
    for c in sorted(explicit):
        v = g["classes"][c]
        lines.append(f"| {c} | {v['count']} | {', '.join(sorted(v['modes']))} | "
                     + "<br>".join(sorted(v["sites"])[:12]) + (" ..." if len(v["sites"]) > 12 else "") + " |")
    lines += ["", f"## Cycles (strongly connected sets of waiting edges): {len(cycles)}", ""]
    for comp in cycles:
        lines.append("- " + " , ".join(comp))
        for (a, b), v in sorted(g["edges"].items()):
            if a in comp and b in comp and v["waits"]:
                ex = v["example"]
                lines.append(f"  - {a} -> {b} x{v['count']}: held at {ex['held']}; then {ex['acquired']} ({ex['test']})")
    lines += ["", f"## Opposite-order pairs whose modes conflict: {len(pairs)} "
              f"({sum(1 for p in pairs if not p[4])} with no common gate)", ""]
    call = [p for p in pairs if not p[4] and _deadlocks(p[2]["call_modes"], p[3]["call_modes"])]
    lines.append(f"### Both orders taken inside one call, no common gate: {len(call)}")
    lines.append("")
    for a, b, v, back, gate in call:
        lines.append(f"- {a} <-> {b}: modes {sorted(v['call_modes'])} / {sorted(back['call_modes'])}")
        for x, y, e in ((a, b, v), (b, a, back)):
            ex = e["call_example"]
            lines.append(f"  - {x} -> {y}: {ex['held']} -> {ex['acquired']} ({ex['test']})")
    lines += ["", "### The rest (gated, or one order seen only across test statements)", ""]
    for a, b, v, back, gate in sorted(pairs, key=lambda p: (bool(p[4]), p[0], p[1])):
        if any(p[0] == a and p[1] == b for p in call):
            continue
        lines.append(f"- {a} <-> {b}: gate {sorted(gate) or 'NONE'}; modes {sorted(v['modes'])} / {sorted(back['modes'])}")
    lines += ["", "## Edges (held -> acquired, waiting acquisitions)", "", "| From | To | Count | Gate | Example |", "|---|---|---|---|---|"]
    for (a, b), v in sorted(g["edges"].items()):
        if keep(a) and keep(b) and v["waits"]:
            ex = v["example"]
            lines.append(f"| {a} | {b} | {v['count']} | {', '.join(sorted(v['gate']))} | {ex['held']} -> {ex['acquired']} |")
    worst = sorted(g["contention"], key=lambda c: -c["seconds"])[:40]
    lines += ["", f"## Contention (waits >= {_CONTENDED_S}s): {len(g['contention'])}", ""]
    lines += [f"- {c['seconds']}s {c['cls']} ({c['mode']}) at {c['site']} in {c['test']}" for c in worst]
    lines += ["", f"## Violations against RANKS: {len(g['violations'])}", ""]
    for v in g["violations"][:200]:
        lines.append(f"- {v['kind']} {v['cls']} at {v['site']} holding {v['held']} ({v['held_site']}) in {v['test']}")
    return "\n".join(lines) + "\n"


DEBT_FILE = Path(__file__).resolve().parent / "lock_order_debt.txt"


def violation_key(v: dict) -> str:
    """One debt entry's text: the kind, the classes and the acquiring function (no line, so
    an unrelated edit above it does not move the entry)."""
    site = v["site"].split(" ", 1)
    return f"{v['kind']} {v['held'] or '-'} -> {v['cls']} {site[0].rsplit(':', 1)[0]} {site[-1]}"


def debt() -> dict[str, str]:
    """entry -> its owner (the #402 group or the maintenance protocol), from the debt file."""
    out = {}
    for line in DEBT_FILE.read_text().splitlines() if DEBT_FILE.exists() else ():
        entry, _, owner = line.partition("#")
        if entry.strip():
            out[entry.strip()] = owner.strip()
    return out


def new_violations() -> list[dict]:
    """The violations recorded since the last call that no debt entry names."""
    known = debt()
    return [v for v in RECORDER.take_violations() if violation_key(v) not in known]


def _gated_keys(g: dict) -> set[str]:
    """R4, offline: "gated <held> -> <taken>" for each decreasing edge taken under the taken
    row's company lock where the taken class has a take outside that lock, at a strength that
    conflicts with the edge's own, made holding nothing of the company's row that conflicts
    with the gate, or a production writer the run never took a lock from (``_unexercised``)."""
    unexercised = _unexercised(g)

    def unproven(cls: str, strength: str, gate: str) -> bool:
        if cls in unexercised:
            return True
        for take in g["classes"].get(cls, {}).get("ungated_takes", ()):
            u, company = take.split("|")
            if ((strength, u) in _CONFLICTS or (u, strength) in _CONFLICTS) \
                    and (company not in _STRENGTH_ORDER or (company, gate) not in _CONFLICTS):
                return True
        return False
    out = set()
    for (a, b), e in g["edges"].items():
        if rank(a) is None or rank(b) is None or rank(a) <= rank(b):
            continue
        for mode in e["company_gated_modes"]:
            gate, pair = mode.split(" ")
            if unproven(b, pair.split(">")[1], gate):
                out.add(f"gated {a} -> {b}")
    return out


def _serialized_keys(g: dict) -> set[str]:
    """R5, offline: "serialized <held> -> <taken>" for each decreasing edge taken under a fixed
    key its flow held exclusively (a migration run's table, then its stamp), unless every
    recorded take of the taken class held one of the edge's keys too, and the run exercised
    every production writer of that class. No order ranks such a class; the key serializes it."""
    unexercised = _unexercised(g)

    def unproven(cls: str, gate: set[str]) -> bool:
        return cls in unexercised or not gate & g["classes"].get(cls, {}).get("serial", set())
    out = set()
    for (a, b), e in g["edges"].items():
        if not e.get("serialized") or rank(a) is None or rank(b) is None or rank(a) <= rank(b):
            continue
        if unproven(b, e["gate"]):
            out.add(f"serialized {a} -> {b}")
    return out


def _cross_keys(g: dict) -> set[str]:
    """R5 and R6, offline: a task that waits on one connection while it holds a lock on another
    waits on itself only if the row it waits on can be held by a transaction that waits on
    what the task holds. The wait is proven when every take of the class held one of the
    flow's serializing keys (R5), or when no transaction ever waited, on any of its task's
    connections, while it held the class (R6: its holder always commits; one holding only rows
    it inserted counts against an inserting wait alone), or when the class ranks above every
    class the task holds (R7: a holder of the class waits only on higher ranks, so no chain of
    waits from it comes back down to what the task holds; a debt edge is the one decreasing
    wait allowed to stand, so R7 holds only above the lowest class a debt edge waits on). Each
    needs every production writer of the class exercised."""
    unexercised = _unexercised(g)
    debt_floor = min((rank(entry.split()[3]) for entry in debt() if entry.split()[0] == "order"
                      and rank(entry.split()[3]) is not None), default=None)

    def ranked(v: dict) -> bool:
        ranks = [rank(c) for c in (v.get("holding") or [v["held"]])]
        top, taken = (None if None in ranks else max(ranks)), rank(v["cls"])
        return (top is not None and taken is not None and taken > top
                and (debt_floor is None or top < debt_floor))

    def proven(v: dict) -> bool:
        info = g["classes"].get(v["cls"], {})
        waits = info.get("holder_waits", 0) + (info.get("insert_holder_waits", 0) if v.get("insert", True) else 0)
        return v["cls"] not in unexercised and (
            bool(set(v["gate"]) & info.get("serial", set())) or not waits or ranked(v))
    return {violation_key(v) for v in g["cross"] if not proven(v)}


def _offline_keys(g: dict) -> set[str]:
    return _gated_keys(g) | _serialized_keys(g) | _cross_keys(g)


def _recorded_keys(directory: Path) -> set[str]:
    return {violation_key(v) for v in _merge(directory)["violations"]}


def _entry(key: str, owner: str) -> str:
    return f"{key}  # {owner}" if owner else key


def debt_listing(directory: Path) -> str:
    """Every violation key a recorded run hit, sorted, each with its owner in the debt file
    ("untriaged" when it has none): for review, never written to the file."""
    known = debt()
    keys = _recorded_keys(directory) | _offline_keys(_merge(directory))
    return "".join(_entry(k, known.get(k, "untriaged")) + "\n" for k in sorted(keys))


def debt_diff(directory: Path) -> str:
    """The keys a recorded run hit that the debt file lacks, and the entries it no longer hits."""
    known, hit = debt(), _recorded_keys(directory) | _offline_keys(_merge(directory))
    added, removed = sorted(hit - set(known)), sorted(set(known) - hit)
    return "".join([f"Added: {len(added)}\n", *(f"+ {k}\n" for k in added),
                    f"Removed: {len(removed)}\n", *(f"- {_entry(k, known[k])}\n" for k in removed)])


def check(directory: Path) -> str:
    """Judge a recorded graph against ``RANKS``: every class the suite locked, wrote or
    referenced must be ranked, and every waiting acquisition after a lock of a higher rank
    is a violation, whether one call or two test statements took the two. An edge through an
    insert made under its company's lock (R2) waits only when its table also has an insert
    made without that lock; keys of a class taken out of order under the company lock (R3)
    fail only when the class has a taker of more than one key without that lock, at a
    strength that conflicts. An inversion into a row whose company's lock the transaction holds
    (R4) is listed as a "gated" key unless the debt file names it, when the taken class has
    a take outside that lock that could make it wait (``_gated_keys``). An inversion taken under
    a fixed key its flow held exclusively (R5, a migration run) is listed as a "serialized" key
    unless every take of the taken class held that key too (``_serialized_keys``)."""
    g = _merge(directory)
    lines = [f"Unranked classes: {sorted(c for c in g['classes'] if rank(c) is None) or 'none'}", ""]

    def waits(v: dict) -> bool:
        return v["waits"] or any(g["classes"].get(c, {}).get("ungated") for c in v["gated"])
    bad = sorted((a, b) for (a, b), v in g["edges"].items()
                 if waits(v) and rank(a) is not None and rank(b) is not None and rank(a) > rank(b))
    lines.append(f"Decreasing waiting edges: {len(bad)}")
    for a, b in bad:
        e = g["edges"][(a, b)]
        ex = e["call_example"] or e["example"]
        lines.append(f"- {a} ({rank(a)}) -> {b} ({rank(b)}): {ex['held']} -> {ex['acquired']} ({ex['test']})")
    # R3: a class whose keys a transaction took out of order under the company lock, and that
    # another transaction takes more than one key of without it: each can hold the key the
    # other waits on, when the key one asks for conflicts with what the other holds of it.
    mixed = sorted(c for c, v in g["classes"].items()
                   if any((a, b) in _CONFLICTS for a in v["unsorted_strengths"] for b in v["multi_strengths"]))
    lines += ["", f"Unsorted under the company lock, with a multi-key taker without it: {len(mixed)}"]
    for c in mixed:
        v = g["classes"][c]
        lines.append(f"- {c}: {v['gated_unsorted']} unsorted, {v['ungated_multi']} ungated: "
                     + ", ".join(sorted(v["multi_sites"])))
    # R4: an inversion under the taken row's company lock, with a take of the taken class
    # outside it, or a writer of that class the run never exercised.
    gated_classes = {b for (a, b), e in g["edges"].items() if e["company_gated_modes"]}
    idle = {c: w for c, w in _unexercised(g).items() if c in gated_classes}
    lines += ["", f"Company-gated classes with an unexercised writer: {len(idle)}",
              *(f"- {c}: {', '.join(w)}" for c, w in sorted(idle.items()))]
    # R5: an inversion under a fixed key its flow held, with a take of the taken class outside it.
    serial = sorted({b for (a, b), e in g["edges"].items() if e.get("serialized")})
    lines += ["", f"Serialized classes: {len(serial)}",
              *(f"- {c} by {', '.join(sorted(g['classes'][c]['serial'])) or 'NONE'}" for c in serial)]
    new = sorted(k for k in _offline_keys(g) if k not in debt())
    lines += ["", f"Gated or serialized inversions not proven, not in the debt file: {len(new)}",
              *(f"- {k}" for k in new)]
    # Rule (e): the debt list only shrinks, so an entry the run no longer hits must go.
    hit = _recorded_keys(directory) | _offline_keys(g)
    stale = sorted(set(debt()) - hit)
    lines += ["", f"Stale debt entries: {len(stale)}", *(f"- {e}" for e in stale)]
    return "\n".join(lines) + "\n"


def check_failed(out: str) -> bool:
    """Whether a ``check`` report fails the lock-order job: a stale debt entry, or an unproven
    company-gated or serialized inversion the debt file lacks."""
    return "\nStale debt entries: 0\n" not in out or "not in the debt file: 0\n" not in out


def table() -> str:
    lines = ["| Rank | Lock class |", "|---|---|"]
    for cls, rank in sorted(RANKS.items(), key=lambda kv: (kv[1], kv[0])):
        lines.append(f"| {rank} | {cls} |")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    if sys.argv[1:2] == ["report"]:
        print(report(Path(sys.argv[2])), end="")
    elif sys.argv[1:2] == ["check"]:
        out = check(Path(sys.argv[2]))
        print(out, end="")
        if check_failed(out):
            raise SystemExit(1)
    elif sys.argv[1:2] == ["debt"]:
        print(debt_listing(Path(sys.argv[2])), end="")
    elif sys.argv[1:2] == ["debt-diff"]:
        print(debt_diff(Path(sys.argv[2])), end="")
    elif sys.argv[1:2] == ["table"]:
        print(table(), end="")
    else:
        raise SystemExit("usage: tests/lock_rank.py report <dir> | check <dir> | debt <dir> | debt-diff <dir> | table")
