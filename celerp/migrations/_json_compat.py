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
            conn.execute(upd, {"__v": json.dumps(data), "__ctid": str(row["ctid"])})
            n += 1
    return n


def is_sql_ascii(conn) -> bool:
    """Whether the connected PostgreSQL database uses the legacy SQL_ASCII encoding."""
    value = conn.execute(sa.text("SHOW server_encoding")).scalar()
    return str(value or "").upper() == "SQL_ASCII"


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
    return _update_json_column(conn, "ledger", "data", mutate, where, params, extra_cols)


def set_if_blank(d: dict, key: str, value) -> bool:
    """Set ``d[key]`` when absent, JSON null, or empty string. Returns if changed."""
    if d.get(key) in (None, ""):
        d[key] = value
        return True
    return False