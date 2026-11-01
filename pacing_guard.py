# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""One pacing authority, held by the test database: a trigger on every table of
execution.PACED refuses a change to its attempt or retry-time column (an UPDATE, or an
INSERT giving one a value other than its default of 0 attempts and no retry time) unless
the transaction carries the ``celerp.pacing`` setting. Only execution._pace (wrapped here
for the test) and ``seeding_pacing()``, for a test that seeds paced rows in a given state,
set it, and only for their own statements. The database judges the row the write leaves,
so no spelling of the write gets past it.

A refusal fails the statement, and is also recorded so the test fails even when the code
under test catches the error. The guard is in each test worker's own database only, never
in the scratch databases tests build to stand for an installation, and steps out of it for
a test that backs the database up (``guarded``)."""
from __future__ import annotations

import contextlib

import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.engine import Engine

FOUND: list[str] = []
_SETTING = "celerp.pacing"
_REFUSED = "pacing written outside execution._pace"
_FUNCTION = f"""
CREATE OR REPLACE FUNCTION celerp_pacing_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    attempts text := TG_ARGV[0];
    retry_at text := TG_ARGV[1];
    after jsonb := to_jsonb(NEW);
BEGIN
    IF current_setting('{_SETTING}', true) = 'on' THEN
        RETURN NEW;
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF COALESCE((after ->> attempts)::int, 0) = 0 AND after ->> retry_at IS NULL THEN
            RETURN NEW;
        END IF;
    ELSIF after -> attempts IS NOT DISTINCT FROM to_jsonb(OLD) -> attempts
            AND after -> retry_at IS NOT DISTINCT FROM to_jsonb(OLD) -> retry_at THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION '{_REFUSED}: %.% or %', TG_TABLE_NAME, attempts, retry_at;
END $$
"""


def install(conn) -> None:
    """Put the guard on every table of PACED the database ``conn`` (a sync connection, as
    run_sync gives) has. Idempotent."""
    from celerp.connectors.execution import PACED

    conn.execute(sa.text(_FUNCTION))
    for table, paced in PACED.items():
        if not sa.inspect(conn).has_table(table):
            continue
        conn.execute(sa.text(f"DROP TRIGGER IF EXISTS celerp_pacing_guard ON {table}"))
        conn.execute(sa.text(
            f"CREATE TRIGGER celerp_pacing_guard BEFORE INSERT OR UPDATE ON {table} FOR EACH ROW "
            f"EXECUTE FUNCTION celerp_pacing_guard('{paced.attempts}', '{paced.retry_at}')"))


def _remove(conn) -> None:
    from celerp.connectors.execution import PACED

    for table in PACED:
        if sa.inspect(conn).has_table(table):
            conn.execute(sa.text(f"DROP TRIGGER IF EXISTS celerp_pacing_guard ON {table}"))
    conn.execute(sa.text("DROP FUNCTION IF EXISTS celerp_pacing_guard()"))


@event.listens_for(Engine, "handle_error")
def _refused(context) -> None:
    if _REFUSED in str(context.original_exception):
        FOUND.append(f"{context.original_exception}: {' '.join(str(context.statement).split())[:200]}")


async def _mark(s, value: str) -> None:
    await s.execute(sa.select(sa.func.set_config(_SETTING, value, True)))


@contextlib.asynccontextmanager
async def seeding_pacing(s):
    """Statements a test sends on the session ``s``, in this block, to seed paced rows in
    a given state. The mark ends with the block, or with a commit inside it."""
    await _mark(s, "on")
    yield
    await s.flush()
    await _mark(s, "")


@contextlib.contextmanager
def guarded(database_url: str):
    """Run one test with execution._pace marking its own statements; yields what the
    database refused. From the first PostgreSQL tool the test looks up (a backup, restore or
    recovery: a Celerp backup refuses a database holding objects that are not Celerp's,
    as the guard is), the guard is out of the test database; it is back on every table
    of PACED afterwards, also when the test restored them. It patches by itself rather
    than through pytest's monkeypatch, so it never moves that fixture's undo after the
    test's own fixtures' teardown."""
    from sqlalchemy.pool import NullPool

    from celerp.connectors import execution
    from celerp.services import backup

    real_pace, real_tool = execution._pace, backup._find_pg_tool
    engine = sa.create_engine(database_url.replace("+asyncpg", "+psycopg2"), poolclass=NullPool)
    removed = False

    async def _pace(s, *args, **kwargs):
        async with seeding_pacing(s):
            return await real_pace(s, *args, **kwargs)

    def _find_pg_tool(*args, **kwargs):
        nonlocal removed
        if not removed:
            with engine.begin() as conn:
                _remove(conn)
            removed = True
        return real_tool(*args, **kwargs)

    execution._pace, backup._find_pg_tool = _pace, _find_pg_tool
    FOUND.clear()
    try:
        yield FOUND
    finally:
        execution._pace, backup._find_pg_tool = real_pace, real_tool
        if removed:
            with engine.begin() as conn:
                install(conn)
        engine.dispose()
