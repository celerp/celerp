# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Watch every statement the database runs while the app serves a request, and judge
each write of company data against the company lock.

A write is protected when, before it ran, lock_company() for the company it writes
completed in the same transaction while this request was served (holding the row lock and
judging the request's authority again) and PostgreSQL still holds that lock (the proofs
company_lock keeps), and no narrower lock (a chart, row or other advisory lock) was taken
before that company's lock; a write named in JUDGED_WITHOUT_LOCK is protected by the
request's authority judged again without the lock, one named in UNDER_ROW_LOCK by its
own row lock, and a write to a company this transaction created by that company being
invisible to every other transaction until it commits; a write on a connection no Session
uses while the request holds the migration lock is the schema migrator's. Each statement is judged on the Session that ran it. A write is
judged when its transaction commits: a write rolled back with its savepoint or transaction
never became durable and is dropped, as the lock proofs are. A durable write made while
serving a GET or HEAD request is a finding of its own however it is locked: those only read.

Statements are classified from the SQL PostgreSQL receives, parsed into its syntax tree
(pglast) once per distinct text and fingerprinted: INSERT, UPDATE, DELETE, MERGE and
TRUNCATE are writes, a data-changing CTE included; FOR UPDATE and FOR SHARE, LOCK TABLE
and the advisory lock functions are locks. A statement is a read only when every function
it calls is reviewed (_PURE); an unreviewed function, procedural SQL or any statement type
not named here is flagged as unknown, never taken as a read. A write is judged for the
companies its construct names, else for the request's company, else for the one company
whose lock its transaction holds, and the census says which.

Writes are judged only inside a request (``observed`` wraps the app) and only for tables
that hold company data: companies itself, a table with a company_id column, or one whose
rows belong to company data through a foreign key or as a partition (for a table the tests
make while running, as its CREATE TABLE says). A write to any other table is either in
INFRASTRUCTURE or flagged as an unowned table."""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import weakref
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field, replace

import pglast
from fastapi.routing import _get_scope_effective_route_context
from greenlet import greenlet
from pglast.visitors import Visitor
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from sqlalchemy.sql import visitors
from sqlalchemy.sql.dml import Insert, UpdateBase
from sqlalchemy.sql.elements import BinaryExpression, BindParameter
from sqlalchemy.util import greenlet_spawn

import sqlalchemy.orm.session as _orm_session
from celerp.services.company_lock import _PROOFS, note, noted
from mutation_baseline import accepted
from mutation_read_notes import READ_ROUTE_WRITES

# Functions whose effects are reviewed: a statement calling only these reads (or writes
# what its statement type says). Any other function is unknown until reviewed, whatever
# its name suggests: PostgreSQL volatility is a catalog property, and a volatile function
# need not write.
_PURE = frozenset({
    "abs", "array_agg", "avg", "bool_and", "bool_or", "cast", "char_length", "coalesce", "concat",
    "count", "date", "date_trunc", "extract", "greatest", "json_agg", "json_build_object",
    "jsonb_agg", "jsonb_build_object", "jsonb_extract_path_text", "jsonb_array_elements",
    "jsonb_array_length", "jsonb_typeof", "least", "length", "lower", "max", "min", "now",
    "nullif", "round", "row_number", "string_agg", "substr", "substring", "sum", "trim",
    "upper", "current_date", "current_timestamp", "hashtext", "hashtextextended",
    # string, json and set functions
    "array_position", "convert_to", "encode", "format", "gen_random_uuid", "generate_series",
    "json_array_elements", "json_array_elements_text", "json_array_length", "json_typeof",
    "jsonb_populate_record", "like_escape",
    "octet_length", "quote_ident", "reverse", "sha256", "strpos", "to_jsonb", "unnest",
    # catalog and session information (pg_current_xact_id assigns the transaction an id,
    # which changes no data)
    "current_database", "current_schema", "current_schemas", "has_table_privilege",
    "pg_backend_pid", "pg_current_xact_id", "pg_get_expr", "pg_my_temp_schema",
    "pg_partition_root", "pg_relation_filenode", "pg_table_is_visible", "pg_xact_status",
    "row_security_active", "to_regclass", "to_regnamespace",
    # the catalog reads and formatting of the schema migrator
    "format_type", "generate_subscripts", "pg_collation_is_visible", "pg_get_indexdef",
    "pg_get_serial_sequence", "timezone", "to_char",
})
# Transaction-scoped advisory locks, and the session-scoped ones a guard holds across
# transactions and releases itself (_RELEASE).
_ADVISORY = frozenset({
    "pg_advisory_xact_lock", "pg_advisory_xact_lock_shared",
    "pg_try_advisory_xact_lock", "pg_try_advisory_xact_lock_shared",
    "pg_advisory_lock", "pg_advisory_lock_shared",
})
# Functions that change only this session's or transaction's settings or locks.
_UNLOCK = frozenset({"pg_advisory_unlock", "pg_advisory_unlock_shared"})
_CONTROL = frozenset({"set_config"}) | _UNLOCK
# A notice to listeners is an outbound effect, not a write of company data.
_EFFECT = frozenset({"pg_notify"})
# Advisory lock families taken before the company lock by the canonical order (module
# admission and the module-state boundary come first).
_BEFORE_COMPANY = ("module-use:", "module-gate:")
# The connector fences (celerp.connectors.ownership.lock_connector_key) rank before the
# company lock too: the install-wide runtime fence, then one connector's ownership fence.
_CONNECTOR_RUNTIME, _CONNECTOR_OWNER = "connector-runtime", "connector-owner:"
_DDL = ("Create", "Alter", "Drop", "Rename", "Comment", "Grant", "Index", "View")
_ROW_LOCK = {1: "row share key", 2: "row share", 3: "row update key", 4: "row update"}
# Tables that hold no company's data, each with the infrastructure that writes it.
INFRASTRUCTURE: dict[str, str] = {
    "system_runtime_state": "the install's runtime state (drain flag, star prompt): services/runtime_state.py",
    "users": "user accounts: services/provisioning.py, routers/companies.py create_user, services/session_tracker.py",
    "user_auth_state": "a user's session nonce, under its own row lock: services/session_tracker.py",
    "password_resets": "a user's password resets: services/password_reset.py",
    "password_reset_notices": "who is told of a password reset: services/password_reset.py, routers/auth.py",
    "migration_templates": "a user's remembered migration answers: services/migration_templates.py",
    "migration_batch_files": "a user's migration files staged before any company exists: services/migration_batches.py",
    "supporter_badges": "a user's supporter badge: services/supporter_badge.py",
    "payment_closures": "a request to Celerp Cloud to close a company's payments, kept past the company: services/payments.py",
    "payment_recoveries": "a System Recovery restore of the install, for Celerp Cloud: services/payments.py",
    "unmatched_payments": "payments received for no company the install holds: services/payments.py",
    "unmatched_refunds": "refunds received for no company the install holds: services/payments.py",
}

# The writes a request may make after judging its authority again without the company
# lock (company_lock.judge_without_company_lock), each the one operation that cannot wait
# for that lock, by table and statement type. Each names the only functions that may make
# it; the same write from anywhere else is judged as usual.
@dataclass(frozen=True)
class Judged:
    sites: tuple[str, ...]  # "<file> <function>" that make the write
    reason: str


JUDGED_WITHOUT_LOCK: dict[tuple[str, str], Judged] = {
    ("migration_runs", "UPDATE"): Judged(
        ("celerp/services/migrations.py request_cancel",),
        "a migration cancel, saved while the runner holds the company lock through a batch"),
}
# The writes protected by a row lock taken earlier in the transaction instead of a company
# lock, by table and statement type: rows that outlive their company or are one login's,
# so no company lock can hold them. Each names the row lock it needs and the only
# functions that may make it; the same write from anywhere else is judged as usual.
@dataclass(frozen=True)
class RowLocked:
    table: str  # the table whose row is locked FOR UPDATE earlier
    sites: tuple[str, ...]  # "<file> <function>" that make the write
    reason: str


_TOKEN_ISSUE = RowLocked(
    "user_auth_state", ("celerp/credentials.py issue_token_pair",),
    "a session token recorded for the company it is issued for, under the login's session state: the single "
    "issuance point checks the login's membership under the company's row lock first")
_TOKEN_REVOKE = RowLocked(
    "user_auth_state",
    ("celerp/services/session_tracker.py invalidate_sessions", "celerp/services/session_tracker.py _rotate_every_nonce"),
    "a login's session tokens ended with its session state rotated: every company the login worked in")
UNDER_ROW_LOCK: dict[tuple[str, str], RowLocked] = {
    ("migration_cleanup_tasks", "DELETE"): RowLocked(
        "migration_cleanup_tasks", ("celerp/services/migrations.py _run_task",),
        "a cleanup task deleted once its files are, after its company was discarded, reset or replaced"),
    ("session_registry", "INSERT"): _TOKEN_ISSUE,
    ("session_registry", "UPDATE"): _TOKEN_ISSUE,
    ("session_registry", "DELETE"): _TOKEN_REVOKE,
    ("ai_batch_jobs", "UPDATE"): RowLocked(
        "ai_batch_jobs", ("celerp/ai/jobs.py _heartbeat",),
        "a runner's lease renewed, and nothing else: only while its runner id still holds the job open, so it "
        "never takes a job, and never waits on the company lock, as a renewal held past the lease would hand a "
        "healthy runner's job to another"),
}

# A request in one of these methods only reads, and so does every request made for it in
# turn (a page load that calls the API): a durable write made for it, to any table, is a
# finding however it is locked, as a link, a prefetch or a page load can send it.
READ_METHODS = ("GET", "HEAD")

# Functions that write on any request, at a time no test controls: the middleware's sliding
# token refresh, once a token is past half its life. Their writes are judged like any other,
# and do not show that the request they ride on, or one it is made for, writes.
ANY_REQUEST_WRITERS = ("celerp/middleware.py _refresh_bearer_validated",)
# The header a request from the UI to the API names the UI request it is made for by, when
# the two talk over HTTP (the browser suite's servers): outgoing_headers.
OUTER_HEADER = "x-observed-request"


_ROOT = os.path.dirname(os.path.abspath(__file__))
_REQUEST: ContextVar[dict | None] = ContextVar("mutation_observer_request", default=None)
_SESSIONS: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()
_LIVE: "weakref.WeakSet" = weakref.WeakSet()
_BEGUN_IN: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()  # session: the task its transaction began in
_SERVING: dict[int, dict] = {}  # the requests being served now, by id
_WRITE, _LOCK, _FENCE, _ROW = "observed-write", "observed-lock", "observed-fence", "observed-row-lock"
_READ = "observed-read-route-write"  # writes made for a read request, reported when they commit
_CREATED = "observed-created"  # companies this transaction inserted: no other can see them before it commits
_CALLS = "observed-write-calls"  # session.info: the app lines that added, deleted or merged in a request


@dataclass
class Op:
    kind: str  # read, write, lock, ddl, control (transaction, settings), effect (outbound), unknown
    table: str | None = None
    companies: tuple[str, ...] = ()
    detail: str = ""
    name: object = None  # an advisory lock's key or name
    fingerprint: str = ""
    columns: tuple[str, ...] = ()  # the columns an INSERT or UPDATE sets


@dataclass
class Finding:
    kind: str  # unprotected, lock-order, cross-company, unknown-sql, unowned-table, no-session, read-route-write
    route: str
    company: str | None
    table: str | None
    sql: str
    detail: str
    earlier_locks: list[str] = field(default_factory=list)
    transaction: str = ""
    where: list[str] = field(default_factory=list)
    test: str = ""
    company_from: str = "statement"  # statement, or request when the SQL names no company
    fingerprint: str = ""  # the statement's shape, without its values (pglast)
    sites: list[str] = field(default_factory=list)  # every app frame of a write, for site coverage
    through: list[str] = field(default_factory=list)  # the requests the write's request is made for
    own: bool = True  # the request's own write, not one ANY_REQUEST_WRITERS makes on any request


@dataclass
class Census:
    """What one run saw: per route, protected and unprotected writes, and findings."""
    findings: list[Finding] = field(default_factory=list)
    routes: dict[str, dict[str, int]] = field(default_factory=dict)
    sites: dict[str, int] = field(default_factory=dict)  # "file:line function@first" -> durable writes made there
    test: str = ""

    def count(self, route: str, what: str) -> None:
        bucket = self.routes.setdefault(route, {})
        bucket[what] = bucket.get(what, 0) + 1


CENSUS = Census()


def reset(test: str = "") -> Census:
    """Start a fresh record (one per test), returning the previous one."""
    global CENSUS
    previous, CENSUS = CENSUS, Census(test=test)
    return previous


# -- request scope -----------------------------------------------------------------

def observed(app, label: str):
    """The app, with each HTTP request it serves marked as request scope; its routes are
    recorded under *label*, as the API and the UI serve some of the same paths."""
    async def _app(scope, receive, send):
        if scope.get("type") != "http":
            return await app(scope, receive, send)
        await _settle()
        request = {"scope": scope, "company": None, "app": label, "inherited": _held(), "sessions": weakref.WeakSet(),
                   "outer": _outer(scope)}
        token = _REQUEST.set(request)
        _SERVING[id(request)] = request
        try:
            return await app(scope, receive, send)
        finally:
            del _SERVING[id(request)]
            _REQUEST.reset(token)
            CENSUS.count(_route(request), "requests")
    return _app


def _outer(scope) -> dict | None:
    """The request a request is made for: the one served in the same task (a test's
    in-process transport), or the one the UI names when it calls the API over HTTP."""
    enclosing = _REQUEST.get()
    if enclosing is not None:
        return enclosing
    named = dict(scope.get("headers") or ()).get(OUTER_HEADER.encode())
    return _SERVING.get(int(named)) if named and named.isdigit() else None


def outgoing_headers() -> dict[str, str]:
    """The header a request to the API sends to name the request it is made for."""
    request = _REQUEST.get()
    return {OUTER_HEADER: str(id(request))} if request is not None and id(request) in _SERVING else {}


class serving:
    """Request scope without an app, for the observer's own tests; nested, the inner
    request is made for the outer one."""

    def __init__(self, method: str, path: str) -> None:
        self.scope = {"type": "http", "method": method, "path": path}

    def __enter__(self):
        self._token = _REQUEST.set({"scope": self.scope, "company": None, "inherited": _held(),
                                    "sessions": weakref.WeakSet(), "outer": _REQUEST.get()})
        return self

    def __exit__(self, *exc) -> None:
        _REQUEST.reset(self._token)


@contextmanager
def another_client():
    """Work a test stages inside a request as another client's (a concurrent edit a patched
    step makes on its own connection): outside request scope, so not the request's."""
    token = _REQUEST.set(None)
    try:
        yield
    finally:
        _REQUEST.reset(token)


def _open_sessions() -> list:
    """Every session that has begun work and still exists, in or between transactions."""
    return list(_LIVE)


async def _settle() -> None:
    """A request starts where the work before it ended: a session the tests reuse is
    handed to the request with their edits still unflushed, and whichever statement of
    the request flushes them, they are the tests' writes, so they are flushed first. A
    session another request is serving with at the same time is that request's, and one
    whose transaction began in other work still running (a migration runner, outside
    request scope) is that work's."""
    busy = {session for request in list(_SERVING.values()) for session in request["sessions"]}
    for session in _open_sessions():
        if session not in busy and not _elsewhere(session) and (session.new or session.dirty or session.deleted):
            await greenlet_spawn(session.flush)


def _elsewhere(session) -> bool:
    task = _BEGUN_IN.get(session)
    task = task() if task is not None else None
    return task is not None and not task.done() and task is not _current_task()


def _current_task():
    try:
        return asyncio.current_task()
    except RuntimeError:
        return None


def _held() -> "weakref.WeakKeyDictionary":
    """The lock proofs each open session holds as a request starts: a session the tests
    reuse can carry an open transaction, and the proofs in it, from earlier work; those
    are not the request's evidence. Holding them keeps their ids from being reused while
    the request lasts. A session that begins later holds none (proofs end with the
    outer transaction)."""
    return weakref.WeakKeyDictionary(
        {session: {id(p): p for p in session.info.get(_PROOFS, ())} for session in _open_sessions()})


def _reads(request: dict) -> bool:
    return request["scope"].get("method") in READ_METHODS


def _read_route(request: dict) -> str:
    """The read request a write is made for, as the routes from it to the request that
    writes ("ui GET /a -> api POST /b"); empty when no request on the way reads."""
    chain = []
    while request is not None:
        chain.insert(0, request)
        request = request.get("outer")
    for i, outer in enumerate(chain):
        if _reads(outer):
            return " -> ".join(_route(r) for r in chain[i:])
    return ""


def _outer_routes(request: dict) -> list[str]:
    """The routes of the requests *request* is made for, outermost last."""
    routes = []
    while (request := request.get("outer")) is not None:
        routes.append(_route(request))
    return routes


def _route(request: dict) -> str:
    key = _route_key(request)
    return f"{request['app']} {key}" if request.get("app") else key


def _route_key(request: dict) -> str:
    scope = request["scope"]
    # A route of a router the app includes keeps its own path; FastAPI records the
    # prefixed template, the one the route inventory lists, in the effective context.
    template = getattr(_get_scope_effective_route_context(scope), "path", None) \
        or getattr(scope.get("route"), "path", None) or scope.get("path", "?")
    return f"{scope.get('method', '?')} {template}"


# -- classification ----------------------------------------------------------------

def _bound(context, param: BindParameter):
    """The value bound to *param* in this execution. A cached statement keeps the construct
    (and its parameters' values) of the execution that compiled it first; this execution's
    values are under the names the compiler rendered."""
    name = context.compiled.bind_names.get(param, param.key)
    values = [params[name] for params in context.compiled_parameters or () if name in params]
    return values[0] if values else None


def _where_companies(context, stmt, column: str) -> set[str]:
    found = set()
    where = getattr(stmt, "whereclause", None)
    if where is None:
        return found
    for el in visitors.iterate(where):
        if isinstance(el, BinaryExpression) and getattr(el.left, "name", None) == column \
                and isinstance(el.right, BindParameter):
            value = _bound(context, el.right)
            if value is not None:
                found.add(str(value))
    return found


def _named_companies(context, table: str) -> tuple[str, ...]:
    """The companies the construct of a write to *table* names: an insert's company_id,
    an update's or delete's WHERE on it (id for companies itself). Raw SQL names none."""
    stmt = getattr(getattr(context, "compiled", None), "statement", None)
    if stmt is None:
        return ()
    dml = [el for el in visitors.iterate(stmt) if isinstance(el, UpdateBase)
           and getattr(el.table, "name", None) == table]
    if not dml:
        return ()
    key = "id" if table == "companies" else "company_id"
    companies: set[str] = set()
    if isinstance(dml[0], Insert):
        for params in context.compiled_parameters or ():
            if params.get(key) is not None:
                companies.add(str(params[key]))
    else:
        companies = _where_companies(context, dml[0], key)
        for params in context.compiled_parameters or ():
            if params.get("company_id") is not None and table != "companies":
                companies.add(str(params["company_id"]))
    return tuple(sorted(companies))


def _advisory(context, parameters, function: str) -> Op:
    """An advisory lock, named by its key: the one string it hashes, else its one number."""
    bound = getattr(context, "compiled_parameters", None)
    keys = [v for p in bound for v in p.values()] if bound else \
        list(parameters.values() if isinstance(parameters, dict) else parameters or ())
    names = [k for k in keys if isinstance(k, str)] or keys
    return Op("lock", detail=f"{function}({', '.join(map(str, keys))})",
              name=names[0] if len(names) == 1 else None)


class _Parts(Visitor):
    """What a parsed statement does: the functions it calls, the tables its INSERT, UPDATE,
    DELETE and MERGE parts write (a data-changing CTE included), the row locks its SELECTs
    take, and any statement inside it the classifier does not know."""

    def __init__(self) -> None:
        super().__init__()
        self.functions: set[str] = set()
        self.writes: list[tuple[str, str]] = []
        self.columns: list[str] = []
        self.locks: list[tuple[str, str]] = []

    def visit_FuncCall(self, ancestors, node) -> None:
        self.functions.add(node.funcname[-1].sval.lower())

    def _write(self, node, verb: str) -> None:
        self.writes.append((node.relation.relname, verb))

    def visit_InsertStmt(self, ancestors, node) -> None:
        self._write(node, "INSERT")
        self.columns += [target.name for target in node.cols or ()]

    def visit_OnConflictClause(self, ancestors, node) -> None:
        self.columns += [target.name for target in node.targetList or ()]

    def visit_UpdateStmt(self, ancestors, node) -> None:
        self._write(node, "UPDATE")
        self.columns += [target.name for target in node.targetList]

    def visit_DeleteStmt(self, ancestors, node) -> None:
        self._write(node, "DELETE")

    def visit_MergeStmt(self, ancestors, node) -> None:
        self._write(node, "MERGE")

    def visit_SelectStmt(self, ancestors, node) -> None:
        for clause in node.lockingClause or ():
            rels = clause.lockedRels or [r for f in node.fromClause or () for r in _relations(f)]
            mode = _ROW_LOCK[int(clause.strength)]
            self.locks += [(r.relname, mode) for r in rels]


def _relations(node) -> list:
    found = []

    class _Rels(Visitor):
        def visit_RangeVar(self, ancestors, rel) -> None:
            found.append(rel)
    _Rels()(node)
    return found


_SYNTAX: dict[str, Op] = {}


# A placeholder of the format and pyformat parameter styles (psycopg2), or its escaped
# literal percent sign.
_PERCENT = re.compile(r"%(?:\(\w+\))?s|%%")


def _postgres_text(statement: str, paramstyle: str | None) -> str:
    """*statement* as PostgreSQL syntax: a driver's percent placeholders become $n
    parameters and %% the percent sign it stands for."""
    if paramstyle not in ("format", "pyformat"):
        return statement
    numbers = iter(range(1, statement.count("%") + 1))
    return _PERCENT.sub(lambda m: "%" if m.group() == "%%" else f"${next(numbers)}", statement)


def _syntax(statement: str) -> Op:
    """What the SQL text does, from its PostgreSQL syntax tree, once per distinct text."""
    op = _SYNTAX.get(statement)
    if op is None:
        op = _SYNTAX[statement] = _parse(statement)
    return op


def _parse(statement: str) -> Op:
    try:
        fingerprint = pglast.fingerprint(statement)
        raws = pglast.parse_sql(statement)
    except pglast.parser.ParseError as exc:
        return Op("unknown", detail=f"unparsed: {exc}")
    if len(raws) != 1:
        return Op("unknown", detail=f"{len(raws)} statements in one", fingerprint=fingerprint)
    stmt = raws[0].stmt
    kind = type(stmt).__name__.removesuffix("Stmt")
    op = lambda *a, **k: Op(*a, **k, fingerprint=fingerprint)
    if kind in ("Transaction", "VariableSet"):
        return op("control")
    if kind == "VariableShow":
        return op("read")
    if kind == "Notify":
        return op("effect")
    if kind.startswith(_DDL):
        return op("ddl", detail=kind)
    if kind == "Lock":
        return op("lock", ",".join(sorted(r.relname for r in stmt.relations)), detail="table lock")
    if kind == "Explain":
        analyzed = any(o.defname == "analyze" for o in stmt.options or ())
        return op("unknown", detail="EXPLAIN ANALYZE runs its statement") if analyzed else op("read")
    if kind == "Truncate":
        tables = sorted(r.relname for r in stmt.relations)
        return op("write", tables[0], detail="TRUNCATE") if len(tables) == 1 else \
            op("unknown", detail=f"TRUNCATE of {', '.join(tables)}")
    if kind not in ("Select", "Insert", "Update", "Delete", "Merge"):
        return op("unknown", detail=f"statement {kind}")
    parts = _Parts()
    parts(stmt)
    unknown = parts.functions - _PURE - _ADVISORY - _EFFECT - _CONTROL
    if unknown:
        return op("unknown", detail=f"function {', '.join(sorted(unknown))}")
    tables = sorted({t for t, _ in parts.writes})
    if len(tables) > 1:
        return op("unknown", detail=f"writes {', '.join(tables)} in one statement")
    if tables:
        verb = parts.writes[0][1] if kind != "Select" else f"{parts.writes[0][1]} in WITH"
        return op("write", tables[0], detail=verb, columns=tuple(sorted(set(parts.columns))))
    if parts.functions & _ADVISORY:
        return op("lock", detail=min(parts.functions & _ADVISORY))
    if parts.locks:
        modes = sorted({m for _, m in parts.locks})
        return op("lock", ",".join(sorted({t for t, _ in parts.locks})), detail=" + ".join(modes))
    if parts.functions & _EFFECT:
        return op("effect")
    if parts.functions & _UNLOCK:
        return op("control", detail=min(parts.functions & _UNLOCK))
    if parts.functions & _CONTROL:
        return op("control")
    return op("read")


def classify(context, statement: str, parameters=None) -> Op:
    """The statement's syntax says what it does; a write's construct, when SQLAlchemy built
    one, says which companies it names; an advisory lock's parameters name the lock."""
    op = _syntax(_postgres_text(statement, getattr(getattr(context, "dialect", None), "paramstyle", None)))
    if op.kind == "write":
        return replace(op, companies=_named_companies(context, op.table))
    if op.detail in _ADVISORY or op.detail in _UNLOCK:
        return replace(_advisory(context, parameters, op.detail), kind=op.kind, fingerprint=op.fingerprint)
    return op


# Tables made while the tests run (extension tables a test creates to prove backup and
# reset carry them): name -> (has a company_id column, the tables it references or extends).
_MADE: dict[str, tuple[bool, frozenset[str]]] = {}


def _learn(statement: str) -> None:
    """Remember what each CREATE TABLE in *statement* says about company data."""
    try:
        raws = pglast.parse_sql(statement)
    except pglast.parser.ParseError:
        return
    for raw in raws:
        stmt = raw.stmt
        if type(stmt).__name__ != "CreateStmt":
            continue
        columns = {getattr(e, "colname", None) for e in stmt.tableElts or ()}
        refs = {r.relname for r in stmt.inhRelations or ()}

        class _Refs(Visitor):
            def visit_Constraint(self, ancestors, node) -> None:
                if node.pktable is not None:
                    refs.add(node.pktable.relname)
        _Refs()(stmt)
        _MADE[stmt.relation.relname] = ("company_id" in columns, frozenset(refs))


def _owned(table: str, seen: frozenset = frozenset()) -> bool:
    """Company data: companies itself, a table with a company_id column, or a table whose
    rows belong to company data through a foreign key or as its partition."""
    from celerp.models.base import Base
    if table == "companies":
        return True
    if table in seen:
        return False
    model = Base.metadata.tables.get(table)
    if model is not None:
        direct, refs = "company_id" in model.c, {fk.column.table.name for fk in model.foreign_keys}
    elif table in _MADE:
        direct, refs = _MADE[table]
    else:
        return False
    return direct or any(_owned(ref, seen | {table}) for ref in refs)


def _before_company(op: Op) -> bool:
    """The company lock itself (its row, the membership judged under it) and the locks
    the canonical order takes before it (module admission, the module-state boundary, the
    schema key a backup or restore shares, a restore's claim of its backup)."""
    from celerp.db import _MIGRATION_LOCK_KEY
    from celerp.modules.registry import _MODULE_STATE_LOCK_KEY
    from celerp.services.company_backup import CLAIM
    if op.table in ("companies", "user_companies"):
        return True
    return op.name in (_MODULE_STATE_LOCK_KEY, _MIGRATION_LOCK_KEY) or _fence(op) or (
        isinstance(op.name, str) and op.name.startswith((*_BEFORE_COMPANY, CLAIM)))


def _fence(op: Op) -> bool:
    return isinstance(op.name, str) and (
        op.name == _CONNECTOR_RUNTIME or op.name.startswith(_CONNECTOR_OWNER))


def _unassigned_connector(request: dict, session, target) -> bool:
    """A connector set up before companies had their own keeps its rows under the install
    id, which is no company; its exclusive ownership fence protects them
    (ownership.lock_unassigned_connector)."""
    from celerp.config import ensure_instance_id
    return any(exclusive and name.startswith(_CONNECTOR_OWNER)
               for name, exclusive in _here(request, session, _FENCE)) and str(target) == ensure_instance_id()


def _under_row_lock(request: dict, session, op: Op) -> bool:
    """The write is one UNDER_ROW_LOCK names, made by one of its functions after its row lock."""
    rule = UNDER_ROW_LOCK.get((op.table, op.detail))
    if rule is None or not any(rule.table in tables.split(",") and "row update" in mode.split(" + ")
                               for tables, mode in (held.split(" ", 1) for held in _here(request, session, _ROW))):
        return False
    return _made_by(rule.sites)


def _read_note(request: dict, op: Op) -> bool:
    """The write is one READ_ROUTE_WRITES lets a read request make: its columns only,
    made by one of its functions, for its route when it names one."""
    rule = READ_ROUTE_WRITES.get((op.table, op.detail))
    return rule is not None and bool(op.columns) and set(op.columns) <= set(rule.columns) \
        and rule.route in (None, _route_key(request)) and _made_by(rule.sites)


def _made_by(sites: tuple[str, ...]) -> bool:
    """The statement runs inside one of *sites* ("<file> <function>")."""
    made_in = {" ".join((frame.split(" ")[0].rsplit(":", 1)[0], frame.split(" ")[1])) for frame in _app_frames()}
    return any(site in made_in for site in sites)


def _exclusive(op: Op) -> bool:
    return "_shared(" not in op.detail


def _fence_order(request: dict, session, op: Op) -> str | None:
    """How a connector fence breaks the canonical order: taken under a company lock, or an
    ownership fence taken without the runtime fence. Taking a fence again in a mode this
    transaction already holds (or a stronger one) never waits, so it is re-entry."""
    held: dict[str, bool] = {}  # fence -> held exclusive
    for name, exclusive in _here(request, session, _FENCE):
        held[name] = held.get(name, False) or exclusive
    if op.name in held and (held[op.name] or not _exclusive(op)):
        return None
    if _here(request, session, "locked"):
        return f"{op.name} after the company lock"
    if op.name != _CONNECTOR_RUNTIME and _CONNECTOR_RUNTIME not in held:
        return f"{op.name} before {_CONNECTOR_RUNTIME}"
    return None


# -- session lookup ----------------------------------------------------------------

def _session_for(conn) -> Session | None:
    """The Session running the statement: the one whose execute or flush is on the stack
    (sessions can share a connection), else the only session in a transaction on the
    connection; None when that is ambiguous."""
    frame = sys._getframe(2)
    while frame is not None:
        if frame.f_code.co_filename == _orm_session.__file__:
            candidate = frame.f_locals.get("self")
            if isinstance(candidate, Session):
                return candidate
        frame = frame.f_back
    sessions = _SESSIONS.get(conn)
    return sessions[0] if sessions and len(sessions) == 1 else None


def _proofs(request: dict, session) -> list:
    """The lock proofs the session holds that were taken while this request was served."""
    inherited = request["inherited"].get(session, {})
    return [p for p in session.info.get(_PROOFS, ()) if id(p) not in inherited]


def _here(request: dict, session, kind: str) -> list:
    return [key for k, key, _ in _proofs(request, session) if k == kind]


def _holds(request: dict, session, company) -> bool:
    return str(company) in {str(c) for c in _here(request, session, "locked")}


@event.listens_for(Session, "after_begin")
def _begun(session, transaction, connection) -> None:
    _LIVE.add(session)
    task = _current_task()
    _BEGUN_IN[session] = weakref.ref(task) if task is not None else None
    request = _REQUEST.get()
    if request is not None:
        request["sessions"].add(session)
    stack = _SESSIONS.setdefault(connection, [])
    if session not in stack:
        stack.append(session)


@event.listens_for(Session, "after_transaction_end")
def _ended(session, transaction) -> None:
    if transaction.parent is not None:
        return
    session.info.pop(_CALLS, None)
    for conn, stack in list(_SESSIONS.items()):
        if session in stack:
            stack.remove(session)


# -- observation -------------------------------------------------------------------

_RELATIVE: dict[str, str | None] = {}


def _relative(filename: str) -> str | None:
    """The app source file a frame runs in, relative to the repository; None for tests,
    this observer, and anything outside the repository."""
    if filename not in _RELATIVE:
        mine = filename.startswith(_ROOT) and "/tests/" not in filename \
            and not filename.endswith("mutation_observer.py")
        _RELATIVE[filename] = os.path.relpath(filename, _ROOT) if mine else None
    return _RELATIVE[filename]


def _app_frames(first_line: bool = False) -> list[str]:
    """The app's own frames that led to the statement, outermost first, through the
    greenlets the async session runs its database work in. With *first_line*, each
    names the line its function starts on too ("file:line name@first"), so the census
    tells apart two functions of one name in a file."""
    chains, frame, child = [], sys._getframe(1), greenlet.getcurrent()
    while frame is not None:
        chain = []
        while frame is not None:
            path = _relative(frame.f_code.co_filename)
            if path:
                chain.append(f"{path}:{frame.f_lineno} {frame.f_code.co_name}"
                             + (f"@{frame.f_code.co_firstlineno}" if first_line else ""))
            frame = frame.f_back
        chains.append(chain)
        child = child.parent
        frame = child.gr_frame if child is not None else None
    return [f for chain in reversed(chains) for f in reversed(chain)]


def _where() -> list[str]:
    return _app_frames()[-6:]


def _request_company(request: dict, session) -> str | None:
    authority = session.info.get("celerp_request_authority") if session is not None else None
    if authority is not None:
        request["company"] = str(authority.company_id)
    return request["company"]


def _migrating(request: dict, conn, op: Op) -> None:
    """Keep the connections on which this request holds the migration lock (the session
    lock celerp.cli._migration_lock takes): while one does, a write made on a connection no
    Session uses is the schema migrator's, which System Recovery runs inside its request
    on a database it has just replaced, with writes paused."""
    from celerp.db import _MIGRATION_LOCK_KEY
    if op.name != _MIGRATION_LOCK_KEY:
        return
    held = request.setdefault("migrating", set())
    if op.detail.startswith("pg_advisory_lock("):
        held.add(id(conn.connection.dbapi_connection))
    elif op.detail.startswith("pg_advisory_unlock("):
        held.discard(id(conn.connection.dbapi_connection))


@event.listens_for(Engine, "after_cursor_execute")
def _executed(conn, cursor, statement, parameters, context, executemany) -> None:
    if context is None:
        return
    if statement.lstrip()[:6].upper() == "CREATE":
        _learn(statement)
    request = _REQUEST.get()
    if request is None:
        return
    session = _session_for(conn)
    if session is not None:
        request["sessions"].add(session)
    op = classify(context, statement, parameters)
    _migrating(request, conn, op)
    if op.kind in ("read", "ddl", "control", "effect"):
        return
    company = _request_company(request, session)
    route = _route(request)

    def found(kind: str, detail: str = op.detail, target=company) -> None:
        CENSUS.findings.append(Finding(kind, route, target, op.table, statement[:300], detail, where=_where(),
                                       test=CENSUS.test, fingerprint=op.fingerprint))

    if op.kind == "unknown":
        return found("unknown-sql")
    if session is None:
        return found("no-session") if op.kind == "write" and not request.get("migrating") else None
    if op.kind == "lock":
        if _fence(op):
            if inversion := _fence_order(request, session, op):
                found("lock-order", inversion)
            note(session, _FENCE, (op.name, _exclusive(op)))
        if op.detail.startswith("row "):
            note(session, _ROW, f"{op.table} {op.detail}")
        locked = company is not None and _holds(request, session, company)
        if locked and "users" in (op.table or "").split(",") and "row update" in op.detail.split(" + "):
            # A sign-in writes rows naming the login after it holds the login's session
            # state; a company request that then rotates that state must not hold the
            # login FOR UPDATE, which those rows' foreign keys wait for.
            found("lock-order", "a login locked FOR UPDATE under the company lock")
        if not _before_company(op):
            # With the companies whose lock, or deletion lock, it was taken under: it is
            # out of order for a write to any other company.
            held = frozenset(str(c) for kind in ("locked", "deleting") for c in _here(request, session, kind))
            note(session, _LOCK, (f"{op.table or ''} {op.detail}".strip(), held))
        return
    if op.detail in ("UPDATE", "DELETE", "MERGE") and getattr(cursor, "rowcount", -1) == 0:
        return  # matched nothing: no mutation
    read_route = _read_route(request)
    if read_route and not _read_note(request, op):
        note(session, _READ, Finding("read-route-write", read_route, company, op.table, statement[:300], op.detail,
                                     where=_where(), test=CENSUS.test, fingerprint=op.fingerprint))
    if not _owned(op.table):
        return None if op.table in INFRASTRUCTURE else found("unowned-table")
    # The statement names the companies it writes (an insert's company_id, a WHERE on it),
    # or the write is taken as the request's company and says so: an update by primary
    # key or raw SQL names no company, and the company lock never proves which company
    # owns a row. Work with no request authority to name its company (a job's runner, a
    # background pass) is taken, the same way, as the one company whose lock its
    # transaction holds, and says so; holding none, or several, it names no company.
    targets, source = op.companies, "statement"
    if not targets and company:
        targets, source = (company,), "request"
    if not targets and len(held := {str(c) for c in _here(request, session, "locked")}) == 1:
        targets, source = tuple(held), "lock"
    if (op.table, op.detail) == ("companies", "INSERT"):
        for created in op.companies:
            note(session, _CREATED, created)
    row_locked = _under_row_lock(request, session, op)
    if not targets and not row_locked:
        return found("unprotected", "no company for the write", None)
    for target in targets or (None,):
        before = [lock for lock, held in _here(request, session, _LOCK) if str(target) not in held]
        if row_locked or target in _here(request, session, _CREATED) \
                or _unassigned_connector(request, session, target):
            kind = None
        elif company is not None and target != company and not _holds(request, session, target):
            kind = "cross-company"
        elif (op.table, op.detail) in JUDGED_WITHOUT_LOCK and target in _here(request, session, "judged") \
                and not _holds(request, session, target) and _made_by(JUDGED_WITHOUT_LOCK[(op.table, op.detail)].sites):
            kind = None
        elif not _holds(request, session, target):
            kind = "unprotected"
        elif before:
            kind = "lock-order"
        else:
            kind = None
        root = session.get_transaction()
        frames = _app_frames(first_line=True)
        note(session, _WRITE, Finding(
            kind or "protected", route, target, op.table, statement[:300], op.detail,
            earlier_locks=before, transaction=f"{id(root):x}",
            where=_where() if kind else [], test=CENSUS.test, company_from=source,
            sites=frames, fingerprint=op.fingerprint, through=_outer_routes(request),
            own=not _made_by(ANY_REQUEST_WRITERS)))


def _recording_the_call(name: str):
    """Session.<name>, noting the app line that called it in a request. An added or deleted
    row is sent at a later flush, from the line that flushed it; the census credits the
    line that made the change, and only when its transaction commits a durable write."""
    original = getattr(Session, name)

    def recorded(self, *args, **kwargs):
        if _REQUEST.get() is not None:
            frames = _app_frames(first_line=True)
            if frames:
                self.info.setdefault(_CALLS, set()).add(frames[-1])
        return original(self, *args, **kwargs)

    recorded.__wrapped__ = original
    return recorded


for _name in ("add", "add_all", "delete", "merge"):
    setattr(Session, _name, _recording_the_call(_name))


@event.listens_for(Session, "after_commit")
def _committed(session) -> None:
    if session.in_nested_transaction():
        return
    for finding in noted(session, _WRITE):
        CENSUS.count(finding.route, finding.kind if finding.own else "made for any request")
        CENSUS.count(finding.route, f"company from {finding.company_from}")
        for outer in finding.through if finding.own else ():
            CENSUS.count(outer, "writes through")
        for site in set(finding.sites):
            CENSUS.sites[site] = CENSUS.sites.get(site, 0) + 1
        if finding.kind != "protected":
            CENSUS.findings.append(finding)
    if noted(session, _WRITE):
        for site in session.info.pop(_CALLS, ()):
            CENSUS.sites[site] = CENSUS.sites.get(site, 0) + 1
    CENSUS.findings.extend(noted(session, _READ))


# -- census files ------------------------------------------------------------------

def explain(findings: list[Finding]) -> str:
    """One paragraph per finding: what was written where, and why it was judged so."""
    out = [f"{len(findings)} write(s) of company data the write observer refused:"]
    for f in findings:
        out.append(
            f"- {f.kind}: {f.route} {f.table or ''} company {f.company} ({f.company_from}); {f.detail}\n"
            f"  sql: {f.sql[:300]}\n  earlier locks: {f.earlier_locks or 'none'}\n"
            f"  at: {' <- '.join(reversed(f.where[-4:])) or 'no app frame'}")
    return "\n".join(out)


class Totals:
    """Everything one test process saw, merged across its tests: request and write counts
    per route, durable writes per app frame, and each distinct finding once, with how
    often it was seen, every test that saw it, and the baseline issue that accepts it."""

    def __init__(self) -> None:
        self.routes: dict[str, dict[str, int]] = {}
        self.sites: dict[str, int] = {}
        self.findings: dict[str, dict] = {}

    def add(self, census: Census) -> None:
        for route, counts in census.routes.items():
            bucket = self.routes.setdefault(route, {})
            for what, n in counts.items():
                bucket[what] = bucket.get(what, 0) + n
        for site, n in census.sites.items():
            self.sites[site] = self.sites.get(site, 0) + n
        for f in census.findings:
            # One finding per statement shape (its fingerprint), wherever its values differ.
            key = "|".join(map(str, (f.kind, f.route, f.table, f.detail, f.fingerprint or f.sql, *f.where)))
            seen = self.findings.get(key)
            if seen is None:
                entry = accepted(f)
                self.findings[key] = {**asdict(f), "key": key, "sites": [], "count": 1, "tests": [census.test],
                                      "accepted": entry["issue"] if entry else None}
            else:
                seen["count"] += 1
                if census.test not in seen["tests"]:
                    seen["tests"].append(census.test)

    def write(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as out:
            json.dump({"routes": self.routes, "sites": self.sites,
                       "findings": list(self.findings.values())}, out, default=str)


def _handler(fn):
    """The function a FastHTML route was declared with: FastHTML serves it through a
    wrapper that holds it in its closure as `f`."""
    code = getattr(fn, "__code__", None)
    if code is not None and fn.__closure__ and "f" in code.co_freevars:
        inner = fn.__closure__[code.co_freevars.index("f")].cell_contents
        if callable(inner):
            return inner
    return fn


def write_routes(path: str, apps) -> None:
    """The routes the test apps serve: method, path template and the endpoint's source."""
    import inspect
    def walk(routes, prefix=""):
        for route in routes:
            included = getattr(route, "original_router", None)  # a router FastAPI includes lazily
            if included is not None:
                yield from walk(included.routes, prefix + route.include_context.prefix)
            else:
                yield prefix, route

    rows = []
    for app in apps:
        for prefix, route in walk(getattr(app, "routes", ())):
            endpoint = getattr(route, "endpoint", None)
            methods = sorted(getattr(route, "methods", None) or ())
            if endpoint is None or not methods:
                continue
            fn = _handler(inspect.unwrap(endpoint))
            try:
                source = f"{_relative(inspect.getsourcefile(fn)) or inspect.getsourcefile(fn)}:" \
                         f"{inspect.getsourcelines(fn)[1]}"
            except (OSError, TypeError):
                source = "?"
            rows.append({"app": type(app).__name__, "methods": methods, "path": prefix + route.path,
                         "endpoint": f"{fn.__module__}.{fn.__qualname__}", "source": source})
    with open(path, "w", encoding="utf-8") as out:
        json.dump(rows, out)


def census_path() -> str | None:
    folder = os.environ.get("CELERP_MUTATION_CENSUS")
    if not folder:
        return None
    os.makedirs(folder, exist_ok=True)
    worker = os.environ.get("PYTEST_XDIST_WORKER", "main")
    return os.path.join(folder, f"census-{os.getpid()}-{worker}")
