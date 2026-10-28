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
violation. The rank table in the design is generated from ``RANKS`` (``python lock_rank.py
table``); the observed graph is merged with ``python lock_rank.py report <dir>``.
"""

from __future__ import annotations

import json
import os
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
    # Whole-company backup and restore.
    "adv:backup-names": 10, "adv:backup": 11, "adv:backup-landing": 12,
    # Module state, then sign-in (auth.py hold_companyless_login: before any company row).
    "adv:module-state": 15, "adv:direct-sign-in": 16, "row:users": 17,
    # Connector guard (R0, R1) and the run lock.
    "adv:connector-runtime": 20, "adv:connector-owner": 21, "adv:sync-run": 22,
    # The company (R3) and what is read under it.
    "row:companies": 30, "row:user_auth_state": 31, "row:user_companies": 32,
    "row:company_peer_identities": 33, "row:locations": 34,
    # Shop identity namespaces, after the company and before any item row.
    "adv:woocommerce-order": 40, "adv:external-link": 41, "adv:external-product": 42,
    "row:connector_configs": 43, "row:connector_first_syncs": 44, "row:sync_runs": 45,
    # Chart and posting (R3a).
    "adv:chart": 50, "row:accounts": 51, "row:bank_accounts": 52, "row:reconciliation_sessions": 53,
    "row:bank_statement_lines": 54, "row:unmatched_payments": 55,
    # Items (R6), then import batches (R7) and the outbound queue (R8).
    "row:projections": 60, "row:import_batches": 61, "adv:outbound": 62, "row:outbound_queue": 63,
    # Bookkeeping rows written last.
    "row:migration_runs": 70, "row:migration_cleanup_tasks": 71, "row:migration_templates": 72,
    "row:migration_attachment_fetches": 73, "adv:xero-attachments": 74, "adv:xero-attachments-round": 75,
    "row:ai_messages": 80, "row:instance_meta": 81,
}

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
_ROW_LOCK = re.compile(
    r"\bFOR\s+(NO\s+KEY\s+UPDATE|UPDATE|KEY\s+SHARE|SHARE)(?:\s+OF\s+([\w\",. ]+?))?"
    r"(\s+NOWAIT|\s+SKIP\s+LOCKED)?\s*$", re.I)
_FROM = re.compile(r"\b(?:FROM|JOIN)\s+\"?(\w+)\"?", re.I)
_UPDATE = re.compile(r"^\s*UPDATE\s+\"?(\w+)\"?", re.I)
_DELETE = re.compile(r"^\s*DELETE\s+FROM\s+\"?(\w+)\"?", re.I)
_UPSERT = re.compile(r"^\s*INSERT\s+INTO\s+\"?(\w+)\"?.*\bON\s+CONFLICT\b.*\bDO\s+UPDATE\b", re.I | re.S)
_PARAM = re.compile(r"\$(\d+)|%\((\w+)\)s|:(\w+)")
_INT = re.compile(r"^-?(\d+|0x[0-9a-fA-F]+)$")

_CONTENDED_S = 0.05


@dataclass
class Lock:
    cls: str
    mode: str
    site: str
    origin: str = ""  # the test statement that made the call ("" outside a test)
    key: str = ""     # what is locked: the advisory key, or a row lock's bound parameters


@dataclass
class Recorder:
    held: dict[int, list[Lock]] = field(default_factory=lambda: defaultdict(list))
    session_held: dict[int, list[Lock]] = field(default_factory=lambda: defaultdict(list))
    edges: dict[tuple[str, str], dict] = field(default_factory=dict)
    classes: dict[str, dict] = field(default_factory=dict)
    contention: list[dict] = field(default_factory=list)
    violations: list[dict] = field(default_factory=list)
    test: str = ""

    def end(self, conn_key: int) -> None:
        self.held.pop(conn_key, None)

    def acquire(self, conn_key: int, lock: Lock, waited: float) -> None:
        info = self.classes.setdefault(lock.cls, {"count": 0, "modes": set(), "sites": set()})
        info["count"] += 1
        info["modes"].add(lock.mode)
        info["sites"].add(lock.site)
        if waited >= _CONTENDED_S:
            self.contention.append({"cls": lock.cls, "mode": lock.mode, "site": lock.site,
                                    "seconds": round(waited, 3), "test": self.test})
        held = self.session_held[conn_key] + self.held[conn_key]
        if any(h.cls == lock.cls and h.key == lock.key and _covers(h.mode, lock.mode) for h in held):
            # A re-take of what this transaction already holds, at no greater strength, is
            # granted at once: it waits for nothing, so it orders nothing.
            info["retakes"] = info.get("retakes", 0) + 1
            return
        waits = lock.mode.split(" ")[-1] not in ("nowait", "try", "skip")
        # A class already held is ordered where it was first taken (rows of one class are
        # taken in key order by their own helpers); a later take adds no edge.
        if any(h.cls == lock.cls for h in held):
            held = []
        for i, h in enumerate(held):
            if h.cls == lock.cls:
                continue
            # Classes this transaction held exclusively before h: a gate every other
            # transaction taking them waits on, so it cannot interleave with this edge.
            gate = {g.cls for g in held[:i] if _exclusive(g.mode)} - {h.cls, lock.cls}
            edge = self.edges.get((h.cls, lock.cls))
            if edge is None:
                edge = self.edges[(h.cls, lock.cls)] = {
                    "count": 0, "example": None, "waits": False, "gate": gate, "modes": set()}
            edge["gate"] = edge["gate"] & gate
            edge["count"] += 1
            edge["waits"] = edge["waits"] or waits
            if waits:
                combo = f"{_strength(h.mode)}>{_strength(lock.mode)}"
                edge["modes"].add(combo)
                # Both taken by one call: the order production runs. Taken by two test
                # statements sharing the suite's one outer transaction: an artifact.
                if h.origin == lock.origin:
                    edge.setdefault("call_modes", set()).add(combo)
                    if edge.get("call_example") is None:
                        edge["call_example"] = {"held": h.site, "acquired": lock.site, "test": self.test}
            if edge["example"] is None:
                edge["example"] = {"held": h.site, "acquired": lock.site, "test": self.test}
            if RANKS and waits and lock.cls in RANKS and h.cls in RANKS and RANKS[lock.cls] < RANKS[h.cls]:
                self._violate("order", lock, held_lock=h)
        if RANKS and lock.cls not in RANKS and lock.mode not in ("write", "delete"):
            self._violate("unknown", lock)
        target = self.session_held if lock.mode.startswith("session") else self.held
        target[conn_key].append(lock)

    def release_session(self, conn_key: int, cls: str) -> None:
        locks = self.session_held.get(conn_key, [])
        for i in range(len(locks) - 1, -1, -1):
            if locks[i].cls == cls:
                del locks[i]
                return

    def _violate(self, kind: str, lock: Lock, held_lock: Lock | None = None) -> None:
        self.violations.append({
            "kind": kind, "cls": lock.cls, "site": lock.site, "test": self.test,
            "held": held_lock.cls if held_lock else None, "held_site": held_lock.site if held_lock else None})

    def dump(self) -> dict:
        return {
            "edges": [{"from": a, "to": b, **v, "gate": sorted(v["gate"]), "modes": sorted(v["modes"]),
                       "call_modes": sorted(v.get("call_modes", ()))}
                      for (a, b), v in self.edges.items()],
            "classes": {k: {"count": v["count"], "modes": sorted(v["modes"]), "sites": sorted(v["sites"])}
                        for k, v in self.classes.items()},
            "contention": self.contention,
            "violations": self.violations,
        }


def _exclusive(mode: str) -> bool:
    return "exclusive" in mode or mode.startswith(("update", "no key update", "delete"))


_STRENGTH_ORDER = ("keyshare", "share", "nokey", "update")


def _covers(held: str, wanted: str) -> bool:
    """True when holding ``held`` already grants ``wanted`` on the same key."""
    h, w = _strength(held), _strength(wanted)
    if h in ("shared", "exclusive"):
        return h == "exclusive" or w == "shared"
    return _STRENGTH_ORDER.index(h) >= _STRENGTH_ORDER.index(w)


def _strength(mode: str) -> str:
    """The lock strength that decides conflicts: shared/exclusive for advisory locks,
    PostgreSQL's four row-lock strengths for rows (an UPDATE takes NO KEY UPDATE, a
    DELETE takes UPDATE)."""
    if mode.startswith(("xact", "session")):
        return "shared" if "shared" in mode else "exclusive"
    for prefix, name in (("no key update", "nokey"), ("update", "update"), ("delete", "update"),
                         ("write", "nokey"), ("key share", "keyshare"), ("share", "share")):
        if mode.startswith(prefix):
            return name
    return mode


# Row-lock conflict table (PostgreSQL 13.3.2) and advisory shared/exclusive.
_CONFLICTS = {
    ("shared", "exclusive"), ("exclusive", "shared"), ("exclusive", "exclusive"),
    ("keyshare", "update"), ("share", "nokey"), ("share", "update"),
    ("nokey", "share"), ("nokey", "nokey"), ("nokey", "update"),
    ("update", "keyshare"), ("update", "share"), ("update", "nokey"), ("update", "update"),
}


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
_ROOT = str(Path(__file__).resolve().parent)


def _site() -> str:
    """The innermost application frame (file:line function) that sent the statement."""
    frame = sys._getframe(2)
    while frame is not None:
        path = frame.f_code.co_filename
        if path.startswith(_ROOT) and "/.venv/" not in path and not path.endswith("lock_rank.py") \
                and "/tests/" not in path and "/test_" not in path.rsplit("/", 1)[-1][:5]:
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


def _key(cls: str, statement: str, parameters) -> str:
    """The locked thing, as far as the statement shows it: an advisory lock's resolved
    arguments, or a row lock's bound parameter values (the same values on the same table
    select the same rows; different values are treated as different rows)."""
    if cls.startswith("adv:"):
        m = _ADVISORY.search(statement)
        if m:
            return ",".join(str(_param(p, parameters or ())) for p in m.group(5).split(","))
    values = parameters.values() if isinstance(parameters, dict) else (parameters or ())
    return repr(sorted(map(str, values)))


def classify(statement: str, parameters) -> list[tuple[str, str]]:
    """(class, mode) for each lock the statement takes; ('-unlock', class) for an unlock."""
    out: list[tuple[str, str]] = []
    for m in _ADVISORY.finditer(statement):
        try_, xact, verb, shared, args = m.groups()
        cls = _advisory_class(args, parameters or ())
        if verb.lower() == "unlock":
            out.append(("-unlock", cls))
            continue
        mode = ("xact" if xact else "session") + (" shared" if shared else " exclusive")
        out.append((cls, mode + (" try" if try_ else " wait")))
    if out:
        return out
    m = _ROW_LOCK.search(statement)
    if m and re.match(r"\s*(\(|SELECT\b|WITH\b)", statement, re.I):
        strength, of, tail = m.group(1), m.group(2), (m.group(3) or "").strip().lower()
        tables = [t.strip().strip('"').split(".")[-1] for t in of.split(",")] if of else _FROM.findall(statement)
        mode = re.sub(r"\s+", " ", strength.lower()) + " " + (tail.replace("skip locked", "skip") or "wait")
        for t in dict.fromkeys(tables):
            out.append((f"row:{t}", mode))
        return out
    for pattern in (_UPDATE, _DELETE, _UPSERT):
        w = pattern.match(statement)
        if w:
            out.append((f"row:{w.group(1)}", "delete" if pattern is _DELETE else "write"))
            break
    return out


def record_statement(conn_key: int, statement: str, parameters, waited: float) -> None:
    head = statement.lstrip()[:20].upper()
    if head.startswith(("COMMIT", "ROLLBACK", "END")) and "SAVEPOINT" not in head and "TO " not in head:
        RECORDER.end(conn_key)
        return
    try:
        found = classify(statement, parameters)
    except Exception as exc:  # an unparseable lock statement is itself a finding
        found = [(f"?unparsed:{type(exc).__name__}", "wait")]
    if not found:
        return
    site, origin = _site(), _origin()
    for cls, mode in found:
        if cls == "-unlock":
            RECORDER.release_session(conn_key, mode)
            continue
        RECORDER.acquire(conn_key, Lock(cls, mode, site, origin, _key(cls, statement, parameters)), waited)


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

def _merge(directory: Path) -> dict:
    edges: dict[tuple[str, str], dict] = {}
    classes: dict[str, dict] = {}
    contention, violations = [], []
    for f in sorted(directory.glob("lock-graph-*.json")):
        d = json.loads(f.read_text())
        for e in d["edges"]:
            cur = edges.setdefault((e["from"], e["to"]),
                                   {"count": 0, "example": e["example"], "waits": False, "gate": set(e["gate"]),
                                    "modes": set(), "call_modes": set(), "call_example": None})
            cur["gate"] &= set(e["gate"])
            cur["modes"].update(e["modes"])
            cur["call_modes"].update(e.get("call_modes", ()))
            cur["call_example"] = cur["call_example"] or e.get("call_example")
            cur["count"] += e["count"]
            cur["waits"] = cur["waits"] or e["waits"]
        for k, v in d["classes"].items():
            cur = classes.setdefault(k, {"count": 0, "modes": set(), "sites": set()})
            cur["count"] += v["count"]
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


def check(directory: Path) -> str:
    """Judge a recorded graph against ``RANKS``: every class the suite locked must be ranked, and
    every waiting acquisition one call made after a lock of a higher rank is a violation."""
    g = _merge(directory)
    lines = [f"Unranked classes: {sorted(c for c, v in g['classes'].items()
                                          if c not in RANKS and set(v['modes']) - {'write', 'delete'}) or 'none'}", ""]
    bad = sorted((a, b) for (a, b), v in g["edges"].items()
                 if v["call_modes"] and RANKS.get(a, -1) > RANKS.get(b, 1 << 30))
    lines.append(f"Decreasing edges taken inside one call: {len(bad)}")
    for a, b in bad:
        ex = g["edges"][(a, b)]["call_example"]
        lines.append(f"- {a} ({RANKS[a]}) -> {b} ({RANKS[b]}): {ex['held']} -> {ex['acquired']} ({ex['test']})")
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
        print(check(Path(sys.argv[2])), end="")
    elif sys.argv[1:2] == ["table"]:
        print(table(), end="")
    else:
        raise SystemExit("usage: lock_rank.py report <dir> | check <dir> | table")
