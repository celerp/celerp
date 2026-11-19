# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Usage rows recorded per user and operation upgrade to one row per operation of a
company: duplicates merge into the earliest row, which keeps its user and takes a settled
charge another row of the operation names; the unique key is then (company, operation),
on a SQL_ASCII database as on a UTF8 one, and the downgrade restores the old key."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, text

from .conftest import throwaway_db, upgrade_to

_REV, _PREV = "x7c8d9e0f1a2", "w6x7b8s9l0n1"
T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _seed(conn) -> dict:
    company, first, second, third = (str(uuid.uuid4()) for _ in range(4))
    for user in (first, second, third):
        conn.execute(text("INSERT INTO users (id, email, name, is_active, created_at) "
                          "VALUES (:id, :email, 'User', true, now())"), {"id": user, "email": f"{user}@example.com"})
    conn.execute(text("INSERT INTO companies (id, name, slug, settings, is_active, created_at) "
                      "VALUES (:id, 'Co', :slug, CAST('{}' AS json), true, now())"),
                 {"id": company, "slug": f"co-{company[:8]}"})
    shared, settled_later, alone = (str(uuid.uuid4()) for _ in range(3))
    rows = [  # (user, operation, credits, pending, unresolved, created after T0 in seconds)
        (second, shared, 4, False, False, 5), (first, shared, 4, False, False, 1), (third, shared, 0, True, False, 9),
        (first, settled_later, 0, True, True, 1), (second, settled_later, 7, False, False, 2),
        (first, alone, 2, True, False, 3),
    ]
    for user, operation, credits, pending, unresolved, after in rows:
        at = T0 + timedelta(seconds=after)
        conn.execute(text("INSERT INTO ai_usage (id, company_id, user_id, operation_id, credits, pending, unresolved, "
                          "reconcile_at, created_at) VALUES (:id, :c, :u, :o, :cr, :p, :un, :at, :at)"),
                     {"id": str(uuid.uuid4()), "c": company, "u": user, "o": operation, "cr": credits, "p": pending,
                      "un": unresolved, "at": at})
    return {"company": company, "first": first, "second": second, "shared": shared,
            "settled_later": settled_later, "alone": alone}


def _rows(conn, company) -> set:
    return {(str(r[0]), str(r[1]), r[2], r[3], r[4]) for r in conn.execute(text(
        "SELECT user_id, operation_id, credits, pending, unresolved FROM ai_usage WHERE company_id = :c"),
        {"c": company})}


def _key(conn) -> str:
    return conn.execute(text(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'uq_ai_usage_operation'")).scalar_one()


@pytest.mark.parametrize("encoding", ["SQL_ASCII", "UTF8"])
def test_duplicate_usage_rows_merge_into_the_earliest_and_the_key_is_company_and_operation(encoding):
    from alembic_support import alembic_run

    with throwaway_db("aiusage", encoding=encoding) as (_, sync_url):
        upgrade_to(sync_url, _PREV)
        eng = create_engine(sync_url)
        try:
            with eng.begin() as conn:
                s = _seed(conn)
            upgrade_to(sync_url, _REV)
            with eng.connect() as conn:
                up, up_key = _rows(conn, s["company"]), _key(conn)
            alembic_run(sync_url, "downgrade", _PREV)
            with eng.connect() as conn:
                down, down_key = _rows(conn, s["company"]), _key(conn)
        finally:
            eng.dispose()

    assert up == {(s["first"], s["shared"], 4, False, False),
                  (s["first"], s["settled_later"], 7, False, False),
                  (s["first"], s["alone"], 2, True, False)}
    assert up_key == "UNIQUE (company_id, operation_id)"
    assert down == up and down_key == "UNIQUE (company_id, user_id, operation_id)"
