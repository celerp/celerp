# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""One pacing authority, held at run time: in every test, a statement that sets an attempt
or retry-time column of deferred connector work (a column of execution.PACED) is recorded
unless execution._pace sent it, and the test fails.

It reads the SQL each statement sends, whatever wrote it (Core, the ORM's flush of an
assigned attribute, text built by concatenation, an upsert): an UPDATE or ON CONFLICT SET
of a pacing column, or an INSERT giving one a value other than its default (0 attempts,
no retry time). A test that seeds paced rows writes them inside ``seeding_pacing()``."""
from __future__ import annotations

import contextlib
import re
from contextvars import ContextVar

from sqlalchemy import event
from sqlalchemy.engine import Engine

_IN_PACE: ContextVar[bool] = ContextVar("in_pace", default=False)
FOUND: list[str] = []
_COLUMNS: dict[str, set[str]] | None = None
_PLACEHOLDER = re.compile(r"\$(\d+)|%\((\w+)\)s|%s|\?|:(\w+)")
_SET = re.compile(r"\bSET\b", re.IGNORECASE)
_UPDATE = re.compile(r"\bUPDATE\s+\"?(\w+)\"?(?:\s+(?:AS\s+)?\"?\w+\"?)?\s*$", re.IGNORECASE)
_INSERT = re.compile(r"\bINSERT\s+INTO\s+\"?(\w+)\"?\s*(?:AS\s+\w+\s*)?\(", re.IGNORECASE)
_CLAUSE_END = re.compile(r"\b(WHERE|RETURNING|FROM)\b", re.IGNORECASE)


def _columns() -> dict[str, set[str]]:
    global _COLUMNS
    if _COLUMNS is None:
        from celerp.connectors.execution import PACED

        _COLUMNS = {table: {paced.attempts, paced.retry_at} for table, paced in PACED.items()}
    return _COLUMNS


def _masked(sql: str) -> str:
    """``sql`` with the inside of every string literal blanked, so its words and
    brackets never read as SQL."""
    return re.sub(r"'(?:[^']|'')*'", lambda m: "'" + " " * (len(m.group()) - 2) + "'", sql)


def _group(sql: str, start: int) -> tuple[list[tuple[int, str]], int]:
    """The depth-0 comma-separated parts (offset, text) from ``start`` up to the bracket
    that closes the enclosing group, or the end; and where that is."""
    parts, depth, begin = [], 0, start
    for i in range(start, len(sql)):
        c = sql[i]
        if c == "(":
            depth += 1
        elif c == ")":
            if depth == 0:
                parts.append((begin, sql[begin:i]))
                return parts, i
            depth -= 1
        elif c == "," and depth == 0:
            parts.append((begin, sql[begin:i]))
            begin = i + 1
    parts.append((begin, sql[begin:]))
    return parts, len(sql)


def _name(text: str) -> str:
    return text.strip().split(".")[-1].strip('"').lower()


def _value(sql: str, offset: int, text: str, parameters):
    """The value an INSERT gives a column: its literal, or the parameter it is bound to."""
    text = re.sub(r"::[\w\s\[\]]+$", "", text.strip())  # the driver's cast of a bound value
    match = _PLACEHOLDER.fullmatch(text)
    if match is None:
        return {"NULL": None, "0": 0, "DEFAULT": None}.get(text.upper(), text)
    if match.group(1):
        return parameters[int(match.group(1)) - 1]
    if match.group(2) or match.group(3):
        return parameters[match.group(2) or match.group(3)]
    return parameters[len(_PLACEHOLDER.findall(sql[:offset]))]


def paced_writes(statement: str, parameters) -> list[str]:
    """Each pacing column ``statement`` sets with ``parameters`` (one set of them)."""
    columns = _columns()
    sql = _masked(statement)
    found = []
    for match in _SET.finditer(sql):
        before = sql[:match.start()]
        update = _UPDATE.search(before)
        table = update.group(1) if update else None
        if table is None and re.search(r"\bDO\s+UPDATE\s*$", before, re.IGNORECASE):
            insert = None
            for insert in _INSERT.finditer(before):
                pass
            table = insert.group(1) if insert else None
        if table not in columns:
            continue
        rest = sql[match.end():]
        end = _CLAUSE_END.search(rest)
        parts, _ = _group(rest[:end.start()] if end else rest, 0)
        found += [f"{table}.{_name(part.split('=')[0])} set" for _, part in parts
                  if "=" in part and _name(part.split("=")[0]) in columns[table]]
    for match in _INSERT.finditer(sql):
        table = match.group(1)
        if table not in columns:
            continue
        names, close = _group(sql, match.end())
        paced = [i for i, (_, n) in enumerate(names) if _name(n) in columns[table]]
        values = re.match(r"\s*VALUES\s*\(", sql[close + 1:], re.IGNORECASE)
        # Many rows at once: INSERT (cols) SELECT p0, ... FROM (VALUES (...), ...) AS t(p0, ...).
        batch = re.match(r"\s*SELECT\b(.*?)\bFROM\s*\(\s*VALUES\s*\(", sql[close + 1:], re.IGNORECASE | re.DOTALL)
        if values is not None:
            start, picks = close + 1 + values.end(), {i: i for i in paced}
        elif batch is not None:
            selected = [_name(re.sub(r"::.*$", "", text.strip())) for _, text in _group(batch.group(1), 0)[0]]
            start = close + 1 + batch.end()
            alias = re.search(r"\)\s*AS\s+\w+\s*\(([^)]*)\)", sql[start:], re.IGNORECASE)
            fields = [_name(f) for f in alias.group(1).split(",")] if alias else []
            picks = {i: fields.index(selected[i]) for i in paced if i < len(selected) and selected[i] in fields}
            if len(picks) != len(paced):
                picks = None
        else:
            picks = None
        if picks is None:
            found += [f"{table}.{_name(names[i][1])} inserted from a query" for i in paced]
            continue
        while True:
            row, end = _group(sql, start)
            for i, at in picks.items():
                if _value(sql, row[at][0], row[at][1], parameters) not in (None, 0):
                    found.append(f"{table}.{_name(names[i][1])} inserted")
            more = re.match(r"\s*,\s*\(", sql[end + 1:])
            if more is None:
                break
            start = end + 1 + more.end()
    return found


@event.listens_for(Engine, "before_cursor_execute")
def _observe(conn, cursor, statement, parameters, context, executemany):
    if _IN_PACE.get() or not re.search(r"\b(UPDATE|INSERT)\b", statement, re.IGNORECASE):
        return
    # An executemany sends many sets of parameters, except SQLAlchemy's batched INSERT,
    # which sends one flat set for its many rows.
    many = executemany and isinstance(parameters, (list, tuple)) and isinstance(next(iter(parameters), None), (tuple, list, dict))
    for one in (parameters if many else [parameters]):
        if found := paced_writes(statement, one if one is not None else ()):
            FOUND.append(f"{', '.join(found)} outside execution._pace: {' '.join(statement.split())[:200]}")


@contextlib.contextmanager
def seeding_pacing():
    """Statements a test sends to seed paced rows in a given state."""
    token = _IN_PACE.set(True)
    try:
        yield
    finally:
        _IN_PACE.reset(token)


@contextlib.contextmanager
def guarded():
    """Run one test with execution._pace marking its own statements; yields what the
    test wrote outside it. It patches by itself rather than through pytest's monkeypatch,
    so it never moves that fixture's undo after the test's own fixtures' teardown."""
    from celerp.connectors import execution

    real = execution._pace

    async def _pace(*args, **kwargs):
        with seeding_pacing():
            return await real(*args, **kwargs)

    execution._pace = _pace
    FOUND.clear()
    try:
        yield FOUND
    finally:
        execution._pace = real
