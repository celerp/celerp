# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Watch every statement the database runs while the app serves a request, and judge
each write of company data against the company lock.

A write is protected when, before it ran, lock_company() for the company it writes
completed in the same transaction (holding the row lock and judging the request's
authority again) and PostgreSQL still holds that lock (company_lock.holds_company_lock),
and no narrower lock (a chart, row or other advisory lock) was taken before the company
lock. A write is judged when its transaction commits: a write rolled back with its
savepoint or transaction never became durable and is dropped, as the lock proofs are.

Statements are classified from the construct SQLAlchemy compiled, never from text
patterns: INSERT, UPDATE and DELETE constructs are writes; a SELECT is a read only when
every function it calls is in _PURE, and a lock when it carries FOR UPDATE or calls an
advisory lock function. Raw SQL text is known only when the whole statement is in
_REVIEWED_SQL; any other text, a writable CTE, an unknown function or a statement with no
construct is flagged for classification, never taken as a read. A write is judged for the
companies its statement names, else for the request's company, and the census says which.

Writes are judged only inside a request (``observed`` wraps the app) and only for tables
that hold company data: a table with a company_id column, or companies itself. A write to
any other table is either in INFRASTRUCTURE or flagged as an unowned table."""

from __future__ import annotations

import json
import os
import sys
import weakref
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field

from greenlet import greenlet
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from sqlalchemy.sql import visitors
from sqlalchemy.sql.ddl import ExecutableDDLElement
from sqlalchemy.sql.dml import Delete, Insert, Update, UpdateBase
from sqlalchemy.sql.elements import (BinaryExpression, BindParameter, ReleaseSavepointClause,
                                     RollbackToSavepointClause, SavepointClause, TextClause)
from sqlalchemy.sql.functions import FunctionElement
from sqlalchemy.sql.selectable import CompoundSelect, Select

from celerp.services.company_lock import holds_company_lock, note, noted

# Functions a SELECT may call and still be a read.
_PURE = frozenset({
    "abs", "array_agg", "avg", "bool_and", "bool_or", "cast", "char_length", "coalesce", "concat",
    "count", "date", "date_trunc", "extract", "greatest", "json_agg", "json_build_object",
    "jsonb_agg", "jsonb_build_object", "jsonb_extract_path_text", "jsonb_array_elements",
    "jsonb_array_length", "jsonb_typeof", "least", "length", "lower", "max", "min", "now",
    "nullif", "round", "row_number", "string_agg", "substr", "substring", "sum", "trim",
    "upper", "current_date", "current_timestamp", "hashtext", "hashtextextended",
})
_ADVISORY = frozenset({
    "pg_advisory_xact_lock", "pg_advisory_xact_lock_shared",
    "pg_try_advisory_xact_lock", "pg_try_advisory_xact_lock_shared",
})
# Advisory lock families taken before the company lock by the canonical order (module
# admission and the module-state boundary come first).
_BEFORE_COMPANY = ("module-use:", "module-gate:")
# Raw SQL the app runs, each statement reviewed whole: what it does. Any other text, or
# any edit to one of these, is flagged. A notice to listeners is an outbound effect, not a
# write of company data; a SET LOCAL changes only this transaction's settings; an advisory
# lock is named by its parameters.
_REVIEWED_SQL = {
    "SELECT id FROM ledger WHERE company_id = CAST(:cid AS uuid) AND idempotency_key=:k": "read",
    "SELECT pg_notify('events', :payload)": "effect",
    "SET LOCAL lock_timeout = 0": "control",
    "SET LOCAL statement_timeout = 0": "control",
    "SET LOCAL lock_timeout TO DEFAULT": "control",
    "SET LOCAL statement_timeout TO DEFAULT": "control",
    "SELECT pg_advisory_xact_lock(:key)": "lock",
    "SELECT pg_advisory_xact_lock(:k)": "lock",
    "SELECT pg_advisory_xact_lock_shared(:k)": "lock",
    "SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))": "lock",
    "SELECT pg_advisory_xact_lock_shared(hashtextextended(:k, 0))": "lock",
}
_CONTROL = (SavepointClause, ReleaseSavepointClause, RollbackToSavepointClause)
# Tables that hold no company's data, each written by named infrastructure.
INFRASTRUCTURE: dict[str, str] = {}

_ROOT = os.path.dirname(os.path.abspath(__file__))
_REQUEST: ContextVar[dict | None] = ContextVar("mutation_observer_request", default=None)
_SESSIONS: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()
_WRITE, _LOCK = "observed-write", "observed-lock"


@dataclass
class Op:
    kind: str  # read, write, lock, ddl, control (savepoints), effect (outbound), unknown
    table: str | None = None
    companies: tuple[str, ...] = ()
    detail: str = ""
    name: object = None  # an advisory lock's key or name


@dataclass
class Finding:
    kind: str  # unprotected, lock-order, cross-company, unknown-sql, unowned-table, no-session
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
    sites: list[str] = field(default_factory=list)  # every app frame of a write, for site coverage


@dataclass
class Census:
    """What one run saw: per route, protected and unprotected writes, and findings."""
    findings: list[Finding] = field(default_factory=list)
    routes: dict[str, dict[str, int]] = field(default_factory=dict)
    sites: dict[str, int] = field(default_factory=dict)  # "file:line function" -> durable writes made there
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
        request = {"scope": scope, "company": None, "app": label}
        token = _REQUEST.set(request)
        try:
            return await app(scope, receive, send)
        finally:
            _REQUEST.reset(token)
            CENSUS.count(_route(request), "requests")
    return _app


class serving:
    """Request scope without an app, for the observer's own tests."""

    def __init__(self, method: str, path: str) -> None:
        self.scope = {"type": "http", "method": method, "path": path}

    def __enter__(self):
        self._token = _REQUEST.set({"scope": self.scope, "company": None})
        return self

    def __exit__(self, *exc) -> None:
        _REQUEST.reset(self._token)


def _route(request: dict) -> str:
    scope = request["scope"]
    route = scope.get("route")
    key = f"{scope.get('method', '?')} {getattr(route, 'path', None) or scope.get('path', '?')}"
    return f"{request['app']} {key}" if request.get("app") else key


# -- classification ----------------------------------------------------------------

def _bound(context, param: BindParameter):
    for params in context.compiled_parameters or ():
        if param.key in params:
            return params[param.key]
    return param.value


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


def _advisory(context, function: str) -> Op:
    """An advisory lock, named by its key: the one string it hashes, else its one number."""
    keys = [v for p in context.compiled_parameters or () for v in p.values()]
    names = [k for k in keys if isinstance(k, str)] or keys
    return Op("lock", detail=f"{function}({', '.join(map(str, keys))})",
              name=names[0] if len(names) == 1 else None)


def classify(context) -> Op:
    compiled = getattr(context, "compiled", None)
    stmt = getattr(compiled, "statement", None)
    if stmt is None:
        return Op("unknown", detail="no compiled construct (driver SQL)")
    if isinstance(stmt, ExecutableDDLElement):
        return Op("ddl")
    if isinstance(stmt, _CONTROL):
        return Op("control")
    if isinstance(stmt, (Insert, Update, Delete)):
        table = stmt.table
        key = "id" if table.name == "companies" else "company_id"
        companies: set[str] = set()
        if isinstance(stmt, Insert):
            for params in context.compiled_parameters or ():
                if params.get(key) is not None:
                    companies.add(str(params[key]))
        else:
            companies = _where_companies(context, stmt, key)
            for params in context.compiled_parameters or ():
                if params.get("company_id") is not None and table.name != "companies":
                    companies.add(str(params["company_id"]))
        return Op("write", table.name, tuple(sorted(companies)), type(stmt).__name__.upper())
    if isinstance(stmt, TextClause):
        kind = _REVIEWED_SQL.get(stmt.text, "unknown")
        if kind == "lock":
            return _advisory(context, stmt.text.split("(", 1)[0].removeprefix("SELECT "))
        return Op(kind, detail="raw SQL text")
    if isinstance(stmt, (Select, CompoundSelect)):
        functions, dml = set(), False
        for el in visitors.iterate(stmt):
            if isinstance(el, UpdateBase):
                dml = True
            elif isinstance(el, FunctionElement):
                functions.add(str(getattr(el, "name", type(el).__name__)).lower())
        if dml:
            return Op("unknown", detail="writable CTE")
        advisory = functions & _ADVISORY
        if advisory:
            return _advisory(context, min(advisory))
        impure = functions - _PURE - _ADVISORY
        if impure:
            return Op("unknown", detail=f"function {', '.join(sorted(impure))}")
        fua = getattr(stmt, "_for_update_arg", None)
        if fua is not None:
            tables = sorted(t.name for t in stmt.get_final_froms() if hasattr(t, "name"))
            mode = "share" if fua.read else "update"
            mode += " key" if fua.key_share else ""
            return Op("lock", ",".join(tables), detail=f"row {mode}")
        return Op("read")
    return Op("unknown", detail=f"construct {type(stmt).__name__}")


def _owned(context, table: str) -> bool:
    stmt = context.compiled.statement
    return table == "companies" or "company_id" in stmt.table.c


def _before_company(op: Op) -> bool:
    """The company lock itself (its row, the membership judged under it) and the locks
    the canonical order takes before it (module admission, the module-state boundary)."""
    from celerp.modules.registry import _MODULE_STATE_LOCK_KEY
    if op.table in ("companies", "user_companies"):
        return True
    return op.name == _MODULE_STATE_LOCK_KEY or (
        isinstance(op.name, str) and op.name.startswith(_BEFORE_COMPANY))


# -- session lookup ----------------------------------------------------------------

def _session_for(conn) -> Session | None:
    sessions = _SESSIONS.get(conn)
    return sessions[-1] if sessions else None


@event.listens_for(Session, "after_begin")
def _begun(session, transaction, connection) -> None:
    stack = _SESSIONS.setdefault(connection, [])
    if session not in stack:
        stack.append(session)


@event.listens_for(Session, "after_transaction_end")
def _ended(session, transaction) -> None:
    if transaction.parent is not None:
        return
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


def _app_frames() -> list[str]:
    """The app's own frames that led to the statement, outermost first, through the
    greenlets the async session runs its database work in."""
    chains, frame, child = [], sys._getframe(1), greenlet.getcurrent()
    while frame is not None:
        chain = []
        while frame is not None:
            path = _relative(frame.f_code.co_filename)
            if path:
                chain.append(f"{path}:{frame.f_lineno} {frame.f_code.co_name}")
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


@event.listens_for(Engine, "after_cursor_execute")
def _executed(conn, cursor, statement, parameters, context, executemany) -> None:
    request = _REQUEST.get()
    if request is None or context is None:
        return
    op = classify(context)
    if op.kind in ("read", "ddl", "control", "effect"):
        return
    session = _session_for(conn)
    company = _request_company(request, session)
    route = _route(request)
    if op.kind == "unknown":
        CENSUS.findings.append(Finding("unknown-sql", route, company, op.table, statement[:300], op.detail,
                                       where=_where(), test=CENSUS.test))
        return
    if session is None:
        if op.kind == "write":
            CENSUS.findings.append(Finding("no-session", route, company, op.table, statement[:300], op.detail,
                                           where=_where(), test=CENSUS.test))
        return
    if op.kind == "lock":
        locked = company is not None and holds_company_lock(session, company)
        if not locked and not _before_company(op):
            note(session, _LOCK, f"{op.table or ''} {op.detail}".strip())
        return
    if op.detail != "INSERT" and getattr(cursor, "rowcount", -1) == 0:
        return  # matched nothing: no mutation
    if not _owned(context, op.table):
        if op.table not in INFRASTRUCTURE:
            CENSUS.findings.append(Finding("unowned-table", route, company, op.table, statement[:300], op.detail,
                                           where=_where(), test=CENSUS.test))
        return
    # The statement names the companies it writes (an insert's company_id, a WHERE on it),
    # or the write is taken as the request's company and says so: an update by primary
    # key names no company, and the company lock never proves which company owns a row.
    targets, source = op.companies, "statement"
    if not targets and company:
        targets, source = (company,), "request"
    for target in targets:
        if company is not None and target != company and not holds_company_lock(session, target):
            kind = "cross-company"
        elif not holds_company_lock(session, target):
            kind = "unprotected"
        elif noted(session, _LOCK):
            kind = "lock-order"
        else:
            kind = None
        root = session.get_transaction()
        frames = _app_frames()
        note(session, _WRITE, Finding(
            kind or "protected", route, target, op.table, statement[:300], op.detail,
            earlier_locks=list(noted(session, _LOCK)), transaction=f"{id(root):x}",
            where=frames[-6:] if kind else [], test=CENSUS.test, company_from=source, sites=frames))
    if not targets:
        CENSUS.findings.append(Finding("unprotected", route, None, op.table, statement[:300],
                                       "no company for the write", where=_where(), test=CENSUS.test))


@event.listens_for(Session, "after_commit")
def _committed(session) -> None:
    if session.in_nested_transaction():
        return
    for finding in noted(session, _WRITE):
        CENSUS.count(finding.route, finding.kind)
        CENSUS.count(finding.route, f"company from {finding.company_from}")
        for site in set(finding.sites):
            CENSUS.sites[site] = CENSUS.sites.get(site, 0) + 1
        if finding.kind != "protected":
            CENSUS.findings.append(finding)


# -- census files ------------------------------------------------------------------

class Totals:
    """Everything one test process saw, merged across its tests: request and write counts
    per route, durable writes per app frame, and each distinct finding once, with how
    often it was seen and the first tests that saw it."""

    def __init__(self) -> None:
        self.routes: dict[str, dict[str, int]] = {}
        self.sites: dict[str, int] = {}
        self.findings: dict[tuple, dict] = {}

    def add(self, census: Census) -> None:
        for route, counts in census.routes.items():
            bucket = self.routes.setdefault(route, {})
            for what, n in counts.items():
                bucket[what] = bucket.get(what, 0) + n
        for site, n in census.sites.items():
            self.sites[site] = self.sites.get(site, 0) + n
        for f in census.findings:
            key = (f.kind, f.route, f.table, f.detail, f.sql[:200], tuple(f.where))
            seen = self.findings.get(key)
            if seen is None:
                self.findings[key] = {**asdict(f), "sites": [], "count": 1, "tests": [census.test]}
            else:
                seen["count"] += 1
                if len(seen["tests"]) < 3 and census.test not in seen["tests"]:
                    seen["tests"].append(census.test)

    def write(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as out:
            json.dump({"routes": self.routes, "sites": self.sites,
                       "findings": list(self.findings.values())}, out, default=str)


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
            fn = inspect.unwrap(endpoint)
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
