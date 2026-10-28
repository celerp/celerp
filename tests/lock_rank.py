# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Lock rank registry: every lock a transaction takes, classified, ordered and recorded.

Every statement sent to Postgres passes through ``record_statement`` (asyncpg through the
adapter cursor, so raw ``conn.connection.cursor()`` calls are seen too; other drivers
through ``before_cursor_execute``). Each lock is reduced to a CLASS:

- an advisory lock: ``adv:<prefix>`` for a text key (the part before the first ``:``),
  ``adv:<name>`` for a numeric key named in ``NUMERIC_KEYS``, or the name ``CALLSITE_KEYS``
  gives the function that computed the key;
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

import asyncio
import functools
import json
import os
import weakref
import re
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

# ── The one map ────────────────────────────────────────────────────────────────
# class -> rank. Lower ranks are taken first. Filled from the measured graph.
RANKS: dict[str, int] = {
    # Instance-wide: schema, version, bootstrap.
    "adv:migration": 0, "adv:version-admit": 1, "adv:version-fence": 2, "adv:bootstrap": 3,
    "adv:migration-scan": 4, "adv:jti-cleanup": 5,
    "row:alembic_version": 6, "row:system_runtime_state": 7,
    # Whole-company backup and restore.
    "adv:backup-names": 10, "adv:backup": 11, "adv:backup-landing": 12,
    # Module state, then sign-in (auth.py hold_companyless_login: before any company row).
    "adv:module-state": 15, "adv:direct-sign-in": 16, "row:users": 17,
    "row:password_resets": 18, "row:password_reset_notices": 19,
    # Connector guard (R0, R1), the run lock and the outbound identity lock (taken with no
    # transaction open in the task, on its own connection).
    "adv:connector-runtime": 20, "adv:connector-owner": 21, "adv:sync-run": 22, "adv:outbound": 23,
    # Execution lease (fence, KEY SHARE) and its heartbeat. Every FOR UPDATE on either table runs
    # in a transaction that holds nothing else, and acquire/takeover use NOWAIT.
    "row:connector_execution_leases": 25, "row:connector_execution_heartbeats": 26,
    # The company (R3) and what is read under it.
    "row:companies": 30, "row:user_auth_state": 31, "row:user_companies": 32,
    "row:company_peer_identities": 33, "row:locations": 34, "row:sync_runs": 35,
    "row:connector_configs": 39, "row:connector_sources": 43,
    # Shop identity namespaces, after the company and before any item row.
    "adv:woocommerce-order": 40, "adv:external-link": 41, "adv:external-product": 42,
    "row:connector_first_syncs": 44,
    # Peer links between companies, after the company's own identity rows.
    "row:company_peer_grants": 45, "row:company_peer_accounts": 46, "row:company_peer_deliveries": 47,
    "row:company_peer_attention": 48,
    # Chart and posting (R3a).
    "adv:chart": 50, "row:accounts": 51, "row:bank_accounts": 52, "row:reconciliation_sessions": 53,
    "row:bank_statement_lines": 54, "row:unmatched_payments": 55, "row:reconciliation_rules": 56,
    "row:unmatched_refunds": 57, "row:payment_closures": 58,
    # Items (R6), then import batches (R7) and the outbound queue (R8).
    "row:ledger": 59, "row:projections": 60, "row:import_batches": 61, "row:outbound_queue": 63,
    "row:payment_recoveries": 64,
    # Bookkeeping rows written last.
    "row:migration_runs": 70, "row:migration_cleanup_tasks": 71, "row:migration_templates": 72,
    "row:migration_attachment_fetches": 73, "adv:xero-attachments": 74, "adv:xero-attachments-round": 75,
    "row:migration_batch_files": 76, "row:migration_entity_maps": 77,
    "row:ai_conversations": 78, "row:ai_batch_jobs": 79,
    "row:ai_messages": 80, "row:instance_meta": 81, "row:notifications": 82, "row:notification_reads": 83,
    "row:session_registry": 84, "row:doc_share_tokens": 85, "row:label_templates": 86,
    "row:marketplace_configs": 87, "row:work_centers": 88, "row:supporter_badges": 89, "row:ai_usage": 90,
    "row:ai_job_steps": 91,
}


# LOCK TABLE runs only in the whole-company protocols (company_reset, db_catalog._hold), each
# locking every table it names in one statement, after the company row (30): one rank for
# every table lock, given to a table exactly when its rows are ranked.
TABLE_LOCK_RANK = 36


# A parent row's key (PostgreSQL row locks are two locks: the key, which KEY SHARE shares
# and FOR UPDATE, DELETE or a key change take exclusively, and the rest of the row). A
# foreign-key check shares the parent's key, so it can wait only on a transaction holding
# that key exclusively; that holder takes it last and waits on nothing after it, so a key
# share closes no cycle and is never ordered.
KEY_RANK = 95


def rank(cls: str) -> int | None:
    if cls.startswith("tbl:"):
        return TABLE_LOCK_RANK if rank("row:" + cls[4:]) is not None else None
    if cls.startswith("key:"):
        return KEY_RANK if rank("row:" + cls[4:]) is not None else None
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

# Numeric advisory keys computed per call (a hash) -> class, by the function that takes them.
CALLSITE_KEYS: dict[str, str] = {
    "celerp.services.company_lock.lock_chart": "adv:chart",
    "celerp.services.company_backup._land": "adv:backup-landing",
    "celerp.services.company_backup.reconcile_landings": "adv:backup-landing",
    "celerp.services.company_backup.<lambda>": "adv:backup",
    "celerp.services.company_backup._wait_for": "adv:backup",
    "celerp.services.company_backup._claim": "adv:backup",
    "celerp.services.company_backup.restore_company": "adv:backup-names",
}

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


# The connector guard (rule (c)): an execution takes 20 and 21 only in its guard + acquire
# transaction, which commits before its body runs.
_GUARD = ("adv:connector-runtime", "adv:connector-owner")
# Lease rows (rule (d)): an exclusive take holds nothing but the guard's shared 20/21, the
# acquire or takeover (the take under the guard) does not wait, and a lease row (its
# generation is the fence) is written only after its FOR UPDATE.
_LEASE_ROWS = ("row:connector_execution_leases", "row:connector_execution_heartbeats")


# Taken outside the task's other transactions by design (1.11): the outbound identity lock,
# session-level, on the execution's own connection.
def _own_connection(lock: Lock) -> bool:
    return lock.cls == "adv:outbound" and lock.mode.startswith("session")


@dataclass
class Recorder:
    held: dict[int, list[Lock]] = field(default_factory=lambda: defaultdict(list))
    session_held: dict[int, list[Lock]] = field(default_factory=lambda: defaultdict(list))
    edges: dict[tuple[str, str], dict] = field(default_factory=dict)
    classes: dict[str, dict] = field(default_factory=dict)
    contention: list[dict] = field(default_factory=list)
    violations: list[dict] = field(default_factory=list)
    seen: dict[str, dict] = field(default_factory=dict)  # each distinct violation, its first example
    test: str = ""
    # The connections each task has used, and the task that last used each connection.
    task_conns: weakref.WeakKeyDictionary = field(default_factory=weakref.WeakKeyDictionary)
    conn_task: dict[int, weakref.ref] = field(default_factory=dict)

    # Per transaction (connection): what it holds by (class, key) -> strengths, the
    # strengths of its explicit locks per class, and the last explicit key per class.
    taken: dict[int, dict[tuple[str, str], set[str]]] = field(default_factory=lambda: defaultdict(dict))
    explicit: dict[int, dict[str, set[str]]] = field(default_factory=lambda: defaultdict(dict))
    last_key: dict[int, dict[str, str]] = field(default_factory=lambda: defaultdict(dict))
    # Per transaction: the companies it inserted, uncommitted (R1).
    own: dict[int, set[str]] = field(default_factory=lambda: defaultdict(set))

    def _transaction_state(self) -> tuple:
        return self.held, self.taken, self.explicit, self.last_key, self.own

    def end(self, conn_key: int) -> None:
        for state in self._transaction_state():
            state.pop(conn_key, None)

    def clear_transactions(self) -> None:
        for state in self._transaction_state():
            state.clear()

    def statement(self, conn_key: int, statement: str, parameters, waited: float) -> None:
        head = statement.lstrip()[:20].upper()
        if head.startswith(("COMMIT", "ROLLBACK", "END")) and "SAVEPOINT" not in head and "TO " not in head:
            self.end(conn_key)
            return
        try:
            found = _locks(statement, parameters)
        except Exception as exc:  # an unparseable lock statement is itself a finding
            found = [(f"?unparsed:{type(exc).__name__}", "wait", "", "")]
        if not found:
            return
        site, origin = _site(), _origin()
        execution = sys.modules.get("celerp.connectors.execution")
        in_execution = execution is not None and execution.current() is not None
        for cls, mode, key, company in found:
            if cls == "-unlock":
                self.release_session(conn_key, mode)
                continue
            self.acquire(conn_key, Lock(cls, mode, site, origin, key, in_execution, company), waited)

    def _own(self, conn_key: int, lock: Lock) -> bool:
        """R1: a row of a company this transaction inserted and has not committed. Nobody else
        sees the company, so nobody holds, waits on or inserts a row naming it: its locks are
        granted at once and make nobody wait. An insert is own only on a table whose every
        unique key holds the company (``_row_locks`` names the company only then); the
        company's own row is not (its slug is unique across companies). The chart lock of such
        a company is its own too: its key is derived from the company id."""
        if lock.cls == "adv:chart":
            from celerp.services.company_lock import chart_key

            return any(lock.key == str(chart_key(c)) for c in self.own[conn_key])
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

    def _retake(self, conn_key: int, lock: Lock) -> bool:
        """Whether this connection already holds what ``lock`` asks for: the same key at no
        less strength, or (for an implicit write) an explicit lock of the class that covers
        it. A statement does not show which rows an UPDATE touches, and the helpers lock
        the rows they then write."""
        w = _strength(lock)
        held = set(self.taken[conn_key].get((lock.cls, lock.key), ()))
        held |= {_strength(h) for h in self.session_held.get(conn_key, ()) if h.cls == lock.cls and h.key == lock.key}
        if not _explicit(lock.mode):
            held = held | self.explicit[conn_key].get(lock.cls, set())
        return any(_covers(h, w) for h in held)

    def acquire(self, conn_key: int, lock: Lock, waited: float) -> None:
        info = self.classes.setdefault(lock.cls, {"count": 0, "modes": set(), "sites": set()})
        info["count"] += 1
        info["modes"].add(lock.mode)
        info["sites"].add(lock.site)
        if waited >= _CONTENDED_S:
            self.contention.append({"cls": lock.cls, "mode": lock.mode, "site": lock.site,
                                    "seconds": round(waited, 3), "test": self.test})
        if rank(lock.cls) is None:
            self._violate("unknown", lock)
        if lock.cls in _GUARD and lock.in_execution:
            self._violate("guard", lock)
        if lock.cls in _LEASE_ROWS:
            self._lease(conn_key, lock)
        if lock.cls.startswith("key:") and _strength(lock) == "keyshare":
            return
        if self._own(conn_key, lock):
            info["own"] = info.get("own", 0) + 1
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
            return
        waits = lock.mode.split(" ")[-1] not in ("nowait", "try", "skip")
        if waits:
            self._other_connections(conn_key, lock)
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
            gate = {g.cls for g in held[:i] if _exclusive(g.mode)} - {h.cls, lock.cls}
            gated = {x.cls for x in (h, lock) if x.gated}
            self._edge(h, lock, waits, gate, gated)
            if gated:
                continue
            if waits and rank(lock.cls) is not None and rank(h.cls) is not None and rank(lock.cls) < rank(h.cls):
                decreasing = True
                self._violate("order", lock, held_lock=h)
        last = self.last_key[conn_key].get(lock.cls)
        if last is not None and last != lock.key and waits and _explicit(lock.mode):
            # Another key of a class this transaction holds, with nothing higher taken since:
            # keys of one class are taken in sorted order (by their helper), or two
            # transactions taking two keys in opposite orders wait on each other.
            self._edge(lock, lock, waits, set())
            if self._company_held(conn_key, lock):
                # R3: two takers holding the company's lock never interleave, so the order of
                # their keys orders nothing; the offline check fails the class when it has a
                # taker of more than one key without that lock.
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
            self.session_held[conn_key].append(lock)
            return
        self.taken[conn_key].setdefault((lock.cls, lock.key), set()).add(_strength(lock))
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
        if others:
            self._violate("lease-held", lock, held_lock=others[0])
        if lock.cls == _LEASE_ROWS[0] and any(h.cls in _GUARD for h in held) \
                and not lock.mode.endswith("nowait"):
            self._violate("lease-wait", lock)

    def _edge(self, h: Lock, lock: Lock, waits: bool, gate: set[str], gated: set[str] = frozenset()) -> None:
        edge = self.edges.get((h.cls, lock.cls))
        if edge is None:
            edge = self.edges[(h.cls, lock.cls)] = {
                "count": 0, "example": None, "waits": False, "gate": gate, "modes": set(),
                "call_sites": {}, "gated": set()}
        edge["gate"] = edge["gate"] & gate
        edge["count"] += 1
        if waits and gated:
            # R2: judged offline against the inserted tables' ungated inserters.
            edge["gated"] |= gated
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

    def _other_connections(self, conn_key: int, lock: Lock) -> None:
        """A task that waits on one connection while it holds a lock on another can wait on
        itself: the lock it holds is released only when this wait ends."""
        try:
            task = asyncio.current_task()
        except RuntimeError:
            task = None
        if task is None:
            return
        conns = self.task_conns.setdefault(task, set())
        for other in sorted(conns - {conn_key}):
            ref = self.conn_task.get(other)
            if ref is None or ref() is not task:
                continue
            holding = [h for h in self.session_held.get(other, []) + self.held.get(other, []) if not _own_connection(h)]
            if any(h.cls == lock.cls and h.key == lock.key and _conflict(h, lock) for h in holding):
                self._violate("self-deadlock", lock, held_lock=holding[0])
                break
            if holding:
                self._violate("cross-connection", lock, held_lock=holding[0])
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

    def dump(self) -> dict:
        return {
            "edges": [{"from": a, "to": b, **v, "gate": sorted(v["gate"]), "modes": sorted(v["modes"]),
                       "call_modes": sorted(v.get("call_modes", ())), "gated": sorted(v["gated"]),
                       "call_sites": {k: sorted(m) for k, m in v["call_sites"].items()}}
                      for (a, b), v in self.edges.items()],
            "classes": {k: {"count": v["count"], "modes": sorted(v["modes"]), "sites": sorted(v["sites"]),
                            **{c: v.get(c, 0) for c in _COUNTS}, **{c: sorted(v.get(c, ())) for c in _SETS}}
                        for k, v in self.classes.items()},
            "contention": self.contention,
            "violations": list({**self.seen, **{violation_key(v): v for v in self.violations}}.values()),
        }


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


def _site() -> str:
    """The innermost application frame (file:line function) that sent the statement; test
    files and conftest.py wrappers (the shared session's turn taking) are never the site."""
    frame = sys._getframe(2)
    while frame is not None:
        path = frame.f_code.co_filename
        name = path.rsplit("/", 1)[-1]
        if path.startswith(_ROOT) and "/.venv/" not in path and not path.endswith("lock_rank.py") \
                and "/tests/" not in path and not name.startswith("test_") and name != "conftest.py":
            return f"{os.path.relpath(path, _ROOT)}:{frame.f_lineno} {frame.f_code.co_name}"
        frame = frame.f_back
    frame = sys._getframe(2)
    while frame is not None:
        path = frame.f_code.co_filename
        if path.startswith(_ROOT) and "/.venv/" not in path and not path.endswith("lock_rank.py"):
            return f"{os.path.relpath(path, _ROOT)}:{frame.f_lineno} {frame.f_code.co_name}"
        frame = frame.f_back
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
        return "adv:" + key.split(":", 1)[0]
    parts = [str(_param(p, parameters)) for p in args.split(",")]
    first = int(parts[0], 0) if _INT.match(parts[0]) else None
    if first is not None and first in NUMERIC_KEYS:
        return NUMERIC_KEYS[first]
    caller = _callsite_function()
    return CALLSITE_KEYS.get(caller, f"adv:?{caller}")


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


def _company_column(table: str) -> str:
    return "id" if table == "companies" else "company_id"


@functools.lru_cache(maxsize=None)
def _company_keyed(table: str) -> bool:
    """Whether every unique key of the table, its primary key aside, holds the company: two
    inserts of one such key name one company. A primary key is a generated id."""
    from sqlalchemy import UniqueConstraint

    mapped = _app_tables().get(table)
    col = _company_column(table)
    if mapped is None or col not in mapped.c:
        return False
    keys = [c.columns for c in mapped.constraints if isinstance(c, UniqueConstraint)]
    keys += [i.columns for i in mapped.indexes if i.unique]
    keys += [[c] for c in mapped.columns if c.unique]
    return all(col in [c.name for c in k] for k in keys)


def _company_param(table: str, where) -> tuple[int, ...] | None:
    """The parameter a WHERE clause binds the table's company column to, when that equality
    is the whole clause or one of its top-level AND terms: every row it selects names that
    company. None when no such term exists."""
    if where is None:
        return None
    (kind, body), = where.items()
    terms = body["args"] if kind == "BoolExpr" and body["boolop"] == "AND_EXPR" else [where]
    for term in terms:
        (kind, body), = term.items()
        if kind != "A_Expr" or body.get("kind") != "AEXPR_OP" or body["name"][0]["String"]["sval"] != "=":
            continue
        for col, val in ((body.get("lexpr"), body.get("rexpr")), (body.get("rexpr"), body.get("lexpr"))):
            fields = (col or {}).get("ColumnRef", {}).get("fields", [])
            if fields and fields[-1].get("String", {}).get("sval") == _company_column(table):
                numbers = _params(val)
                if numbers and len(numbers) == 1:
                    return numbers
    return None


@functools.lru_cache(maxsize=8192)
def _row_locks(statement: str) -> tuple[tuple[tuple[str, str, tuple[int, ...] | None, tuple[int, ...] | None], ...],
                                        tuple]:
    """(class, mode, parameter numbers of the key, parameter numbers of the company) for each
    row or table lock the statement takes, and the placeholder map. A statement the parser
    rejects raises."""
    from pglast.parser import parse_sql_json

    text, names = _numbered(statement)
    tree = json.loads(parse_sql_json(text))
    ctes = {body["ctename"] for kind, body in _nodes(tree) if kind == "CommonTableExpr"}
    out: list[tuple[str, str, tuple[int, ...] | None, tuple[int, ...] | None]] = []
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
                    company = _company_param(t, where) if len(tables) == 1 else None
                    out.append((f"row:{t}", mode, key, company))
                    if mode.startswith("update ") and t in _parents():
                        out.append((f"key:{t}", mode, key, company))
        elif kind == "LockStmt":
            mode = f"{TABLE_MODES[body['mode']]} {'nowait' if body.get('nowait') else 'wait'}"
            out += [(f"tbl:{r['RangeVar']['relname']}", mode, None, None) for r in body["relations"]]
        elif kind == "UpdateStmt":
            table, where = body["relation"]["relname"], body.get("whereClause")
            company = _company_param(table, where) if not body.get("fromClause") else None
            out.append((f"row:{table}", "write", _params(where), company))
            fks = _foreign_keys(table)
            for target in body.get("targetList", ()):
                t = target["ResTarget"]
                if t.get("name") in fks:
                    out.append((f"key:{fks[t['name']]}", "key share wait", _params(t.get("val")), None))
        elif kind == "DeleteStmt":
            table, where = body["relation"]["relname"], body.get("whereClause")
            key = _params(where)
            company = _company_param(table, where) if not body.get("usingClause") else None
            out.append((f"row:{table}", "delete", key, company))
            if table in _parents():
                out.append((f"key:{table}", "delete", key, company))
        elif kind == "InsertStmt":
            table = body["relation"]["relname"]
            fks = _foreign_keys(table)
            cols = [c["ResTarget"]["name"] for c in body.get("cols", ())]
            rows = body.get("selectStmt", {}).get("SelectStmt", {}).get("valuesLists") or []
            company = None
            if _company_column(table) in cols and (table == "companies" or _company_keyed(table)):
                values = [r["List"]["items"][cols.index(_company_column(table))] for r in rows]
                numbers = [_params(v) for v in values]
                if numbers and all(n and len(n) == 1 for n in numbers):
                    company = tuple(n[0] for n in numbers)
            out.append((f"row:{table}", "insert", None, company))
            for col, ref in fks.items():
                if cols and col not in cols:
                    continue
                values = [r["List"]["items"][cols.index(col)] for r in rows if cols] or None
                out.append((f"key:{ref}", "key share wait", _params(values) if values else None, None))
            if body.get("onConflictClause", {}).get("action") == "ONCONFLICT_UPDATE":
                out.append((f"row:{table}", "write", None, company if table != "companies" else None))
    return tuple(dict.fromkeys(out)), names


def _company(numbers: tuple[int, ...] | None, names: tuple, parameters) -> str:
    """The one company the bound parameters name; "" when they name none or several."""
    values = {str(_bound(n, names, parameters)) for n in numbers or ()}
    return values.pop() if len(values) == 1 and "None" not in values else ""


def _locks(statement: str, parameters) -> list[tuple[str, str, str, str]]:
    """(class, mode, key, company) for each lock the statement takes; ('-unlock', class, '', '')
    for an unlock."""
    out: list[tuple[str, str, str, str]] = []
    for m in _ADVISORY.finditer(statement):
        try_, xact, verb, shared, args = m.groups()
        cls = _advisory_class(args, parameters or ())
        if verb.lower() == "unlock":
            out.append(("-unlock", cls, "", ""))
            continue
        mode = ("xact" if xact else "session") + (" shared" if shared else " exclusive")
        out.append((cls, mode + (" try" if try_ else " wait"), _advisory_key(statement, parameters), ""))
    if out or not _LOCKING.search(statement):
        return out
    found, names = _row_locks(statement)
    return [(cls, mode, _row_key(numbers, names, parameters), _company(company, names, parameters))
            for cls, mode, numbers, company in found if cls.split(":", 1)[1] in _app_tables()]


def classify(statement: str, parameters) -> list[tuple[str, str]]:
    """(class, mode) for each lock the statement takes; ('-unlock', class) for an unlock."""
    return [(cls, mode) for cls, mode, _, _ in _locks(statement, parameters)]


def record_statement(conn_key: int, statement: str, parameters, waited: float) -> None:
    RECORDER.statement(conn_key, statement, parameters, waited)


_INSTALLED = False


def install() -> None:
    """Route every Postgres statement and transaction end through the recorder."""
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True
    from sqlalchemy import event
    from sqlalchemy.dialects.postgresql import asyncpg as pg_asyncpg
    from sqlalchemy.engine import Engine
    from sqlalchemy.orm import Session

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
                if _real.__name__ == "close":
                    RECORDER.session_held.pop(id(self), None)
        setattr(conn_cls, name, _ended)

    @event.listens_for(Engine, "before_cursor_execute")
    def _other_drivers(conn, cursor, statement, parameters, context, executemany):
        if conn.dialect.name == "postgresql" and conn.dialect.driver != "asyncpg":
            record_statement(id(conn.connection.dbapi_connection), statement, parameters, 0.0)

    @event.listens_for(Engine, "commit")
    @event.listens_for(Engine, "rollback")
    def _engine_end(conn):
        if conn.dialect.name == "postgresql" and conn.dialect.driver != "asyncpg":
            RECORDER.end(id(conn.connection.dbapi_connection))

    @event.listens_for(Session, "after_transaction_end")
    def _session_end(session, transaction):
        if transaction.parent is not None or transaction.nested:
            return
        for conn in list(getattr(transaction, "_lock_rank_conns", ())):
            RECORDER.end(conn)

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
    path.write_text(json.dumps(RECORDER.dump(), indent=1, default=sorted))


# ── Report: merge worker dumps into one graph ─────────────────────────────────

# Per class counts the offline check judges: inserts without their company's lock (R2), keys
# taken out of order under it, and second keys taken without it (R3).
_COUNTS = ("ungated", "gated_unsorted", "ungated_multi")
# The strengths of those R3 takes, and where each second key without the lock was taken.
_SETS = ("unsorted_strengths", "multi_strengths", "multi_sites")


def _merge(directory: Path) -> dict:
    edges: dict[tuple[str, str], dict] = {}
    classes: dict[str, dict] = {}
    contention, violations = [], []
    for f in sorted(directory.glob("lock-graph-*.json")):
        d = json.loads(f.read_text())
        for e in d["edges"]:
            cur = edges.setdefault((e["from"], e["to"]),
                                   {"count": 0, "example": e["example"], "waits": False, "gate": set(e["gate"]),
                                    "modes": set(), "call_modes": set(), "call_example": None,
                                    "call_sites": {}, "gated": set()})
            cur["gate"] &= set(e["gate"])
            cur["gated"].update(e.get("gated", ()))
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
        contention += d["contention"]
        violations += d["violations"]
    return {"edges": edges, "classes": classes, "contention": contention, "violations": violations}


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


def check(directory: Path) -> str:
    """Judge a recorded graph against ``RANKS``: every class the suite locked, wrote or
    referenced must be ranked, and every waiting acquisition after a lock of a higher rank
    is a violation, whether one call or two test statements took the two. An edge through an
    insert made under its company's lock (R2) waits only when its table also has an insert
    made without that lock; keys of a class taken out of order under the company lock (R3)
    fail only when the class has a taker of more than one key without that lock, at a
    strength that conflicts."""
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
    # Rule (e): the debt list only shrinks, so an entry the run no longer hits must go.
    hit = {violation_key(v) for v in g["violations"]}
    stale = sorted(set(debt()) - hit)
    lines += ["", f"Stale debt entries: {len(stale)}", *(f"- {e}" for e in stale)]
    return "\n".join(lines) + "\n"


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
        if "\nStale debt entries: 0\n" not in out:
            raise SystemExit(1)
    elif sys.argv[1:2] == ["table"]:
        print(table(), end="")
    else:
        raise SystemExit("usage: tests/lock_rank.py report <dir> | check <dir> | table")
