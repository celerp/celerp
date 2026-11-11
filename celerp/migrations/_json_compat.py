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


def _update_json_column(conn, table, col, mutate, where, params, extra_cols) -> int:
    # Update by ctid (the physical row id, on every Postgres table): no index or
    # column-type assumptions, and stable within the migration's transaction.
    select = ", ".join(("ctid", col, *extra_cols))
    sql = f"SELECT {select} FROM {table}"
    if where:
        sql += f" WHERE {where}"
    rows = conn.execute(sa.text(sql), params or {}).mappings().all()
    upd = sa.text(
        f"UPDATE {table} SET {col} = CAST(:__v AS json) WHERE ctid = CAST(:__ctid AS tid)"
    )
    n = 0
    for row in rows:
        data = decode_json_value(row[col])
        if data is None:
            continue
        if mutate(data, row):
            conn.execute(upd, {"__v": dump_existing_json(data), "__ctid": str(row["ctid"])})
            n += 1
    return n


def _non_ascii(text: str) -> int:
    return sum(not c.isascii() for c in text)


def unescape_json_text(conn) -> int:
    """Rewrite every json value an earlier version stored with characters past ASCII as \\u
    escapes so it holds those characters as UTF-8 text (dump_existing_json), on every encoding:
    a SQL_ASCII database cannot read such an escape back with ->>, and a UTF8 install's backup
    holding one cannot be restored into a SQL_ASCII install. Escapes with no text form stay as
    they are (NUL and other control characters, a lone surrogate), so such a row reads as
    before. Every json column of the schema is covered, each locked for the pass; a value with
    no escape to turn into text is left as it is, so a second run changes nothing. Returns the
    number of values rewritten. A no-op on any other database."""
    if conn.dialect.name != "postgresql":
        return 0
    columns = conn.execute(sa.text(
        "SELECT table_name, column_name FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND data_type = 'json' ORDER BY table_name, column_name")).all()
    if columns:
        lock_tables(conn, *{table for table, _ in columns})
    quote = conn.dialect.identifier_preparer.quote
    n = 0
    for table, column in columns:
        t, c = quote(table), quote(column)
        upd = sa.text(f"UPDATE {t} SET {c} = CAST(:v AS json) WHERE ctid = CAST(:ctid AS tid)")
        for ctid, text in conn.execute(sa.text(
                f"SELECT ctid, CAST({c} AS text) FROM {t} WHERE CAST({c} AS text) LIKE '%\\\\u%'")).all():
            # An escape past ASCII turned into its character adds one; nothing else does.
            if _non_ascii(new := dump_existing_json(json.loads(text))) > _non_ascii(text):
                conn.execute(upd, {"v": new, "ctid": str(ctid)})
                n += 1
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