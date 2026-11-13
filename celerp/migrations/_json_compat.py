# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Encoding-safe in-Python edits for JSON columns in data-backfill migrations.

Server-side ``json -> jsonb`` casts decode ``\\uXXXX`` escapes into the server
encoding, which fails on ``SQL_ASCII`` clusters. These helpers do the
read-modify-write in Python instead, so backfills work on any encoding.
See https://github.com/celerp/celerp/issues/189
"""
from __future__ import annotations

import json
from typing import Callable

import sqlalchemy as sa

from celerp.json_text import dump_existing_json
from celerp.migrations._locks import lock_tables


def decode_json_value(value):
    """Decode a JSON column value returned either parsed or as raw text."""
    # psycopg2 returns json pre-parsed; asyncpg returns raw text. Handle both.
    if value is None:
        return None
    if isinstance(value, (str, bytes, bytearray)):
        return json.loads(value)
    return value


# Rows a rewrite holds at a time: one FETCH from its cursor, one UPDATE back. Memory stays flat
# however many rows a table has.
BATCH = 1000


def _rewrite_rows(conn, table: str, column: str, select: str, where: str, params: dict | None,
                  rewrite: Callable[["sa.engine.RowMapping"], str | None]) -> int:
    """Write ``rewrite(row)`` into *column* for every matching row of *table* that it returns
    text for, and return how many. The rows stream through a server-side cursor BATCH at a time
    and each batch goes back in one UPDATE, so memory does not grow with the table. Rows are
    addressed by ctid (the physical row id, on every PostgreSQL table: no index or column-type
    assumption). The cursor reads the snapshot taken when it opened, inside the revision's own
    transaction, so a row the pass has rewritten is not read again and every row is seen once;
    an interrupted pass rolls back with that transaction and a rerun starts clean."""
    sql = f"DECLARE celerp_json_rewrite NO SCROLL CURSOR FOR SELECT ctid, {select} FROM {table}"
    conn.execute(sa.text(f"{sql} WHERE {where}" if where else sql), params or {})
    fetch = sa.text(f"FETCH FORWARD {int(BATCH)} FROM celerp_json_rewrite")
    write = sa.text(
        f"UPDATE {table} AS t SET {column} = CAST(v.value AS json) "
        "FROM unnest(CAST(:ctids AS tid[]), CAST(:values AS text[])) AS v(row_id, value) WHERE t.ctid = v.row_id")
    n = 0
    while rows := conn.execute(fetch).mappings().all():
        changed = [(str(row["ctid"]), new) for row in rows if (new := rewrite(row)) is not None]
        if changed:
            conn.execute(write, {"ctids": [c for c, _ in changed], "values": [v for _, v in changed]})
            n += len(changed)
    conn.execute(sa.text("CLOSE celerp_json_rewrite"))
    return n


def _update_json_column(conn, table, col, mutate, where, params, extra_cols) -> int:
    def rewrite(row):
        data = decode_json_value(row[col])
        return dump_existing_json(data) if data is not None and mutate(data, row) else None

    return _rewrite_rows(conn, table, col, ", ".join((col, *extra_cols)), where, params, rewrite)


def _non_ascii(text: str) -> int:
    return sum(not c.isascii() for c in text)


def unescape_json_text(conn) -> int:
    """Rewrite every json value an earlier version stored with characters past ASCII as \\u
    escapes so it holds those characters as UTF-8 text (dump_existing_json), on every encoding:
    a SQL_ASCII database cannot read such an escape back with ->>, and a UTF8 install's backup
    holding one cannot be restored into a SQL_ASCII install. Escapes with no text form stay as
    they are (NUL and other control characters, a lone surrogate), so such a row reads as
    before. A value with no escape to turn into text is left as it is, so a second run changes
    nothing. Returns the number of values rewritten. A no-op on any other database.

    Every json column of the schema is covered, BATCH rows at a time (_rewrite_rows), inside the
    revision's one transaction. Its tables are locked EXCLUSIVE, all in one statement before the
    first read, for the rest of that transaction: plain reads go on, but no writer can add or
    change a value behind the pass, so no escaped value is left once it commits. EXCLUSIVE is the
    weakest mode that also keeps out a request taking rows FOR UPDATE, which the pass's UPDATE
    would wait on. Locking each table only when the pass reaches it would not be safe: a revision
    also runs inside a serving process (a restore reconciles its schema), where a request could
    hold a row of a table not yet locked while waiting on one already locked, a deadlock that
    would fail the upgrade."""
    if conn.dialect.name != "postgresql":
        return 0
    columns = conn.execute(sa.text(
        "SELECT table_name, column_name FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND data_type = 'json' ORDER BY table_name, column_name")).all()
    if columns:
        lock_tables(conn, *{table for table, _ in columns})
    quote = conn.dialect.identifier_preparer.quote

    def rewrite(row):
        text = row["value"]
        # An escape past ASCII turned into its character adds one; nothing else does.
        return new if _non_ascii(new := dump_existing_json(json.loads(text))) > _non_ascii(text) else None

    n = 0
    for table, column in columns:
        t, c = quote(table), quote(column)
        n += _rewrite_rows(conn, t, c, f"CAST({c} AS text) AS value", f"CAST({c} AS text) LIKE '%\\\\u%'",
                           None, rewrite)
    return n


def update_company_settings(
    conn,
    mutate: Callable[[dict, "sa.engine.RowMapping"], bool],
    *,
    where: str = "",
    params: dict | None = None,
    extra_cols: tuple[str, ...] = (),
) -> int:
    """Like update_projection_state(), for companies.settings."""
    return _update_json_column(conn, "companies", "settings", mutate, where, params, extra_cols)


def update_projection_state(
    conn,
    mutate: Callable[[dict, "sa.engine.RowMapping"], bool],
    *,
    where: str = "",
    params: dict | None = None,
    extra_cols: tuple[str, ...] = (),
) -> int:
    """Read each matching projection's ``state`` into a dict, call
    ``mutate(state, row)``, write it back when ``mutate`` returns True.

    ``where`` must reference only real (non-JSON) columns; ``extra_cols`` are extra
    SELECT expressions exposed to ``mutate`` via the row mapping. Returns the
    number of rows updated.
    """
    lock_tables(conn, "projections")
    return _update_json_column(conn, "projections", "state", mutate, where, params, extra_cols)


def update_ledger_data(
    conn,
    mutate: Callable[[dict, "sa.engine.RowMapping"], bool],
    *,
    where: str = "",
    params: dict | None = None,
    extra_cols: tuple[str, ...] = (),
) -> int:
    """Like :func:`update_projection_state`, for ``ledger.data``."""
    lock_tables(conn, "ledger")
    return _update_json_column(conn, "ledger", "data", mutate, where, params, extra_cols)


def set_if_blank(d: dict, key: str, value) -> bool:
    """Set ``d[key]`` when absent, JSON null, or empty string. Returns if changed."""
    if d.get(key) in (None, ""):
        d[key] = value
        return True
    return False