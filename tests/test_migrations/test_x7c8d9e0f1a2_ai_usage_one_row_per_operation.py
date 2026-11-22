# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Usage rows recorded per user and operation upgrade to one row per operation of a
company: the rows of an operation merge into the earliest, which keeps its id and user
and takes the strongest charge evidence of the group (a settled charge, else the largest
pending charge, unresolved only when every row is, asked about again at the group's
latest reconcile time); single rows stay as they are; the unique key is then (company,
operation), on a SQL_ASCII database as on a UTF8 one, and the downgrade restores the old
key. A seeded set of generated groups is checked against an independent oracle of that
rule, and one operation is seeded by the live usage writer itself: a settled charge
replaces the larger pending one (test_ai_usage_pending holds that contract)."""

from __future__ import annotations

import asyncio
import random
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from .conftest import throwaway_db, upgrade_to

_REV, _PREV = "x7c8d9e0f1a2", "w6x7b8s9l0n1"
T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
_COLS = "id, company_id, user_id, operation_id, credits, pending, unresolved, reconcile_at, created_at"

# Hand cases: name -> rows of (user index, credits, pending, unresolved, created s, reconcile s).
CASES = {
    "pending_zero_then_pending_three": [(0, 0, True, False, 1, 10), (1, 3, True, False, 2, 20)],
    "settled_then_pending": [(0, 5, False, False, 1, 10), (1, 9, True, False, 2, 20)],
    "pending_then_settled": [(0, 9, True, False, 1, 10), (1, 5, False, False, 2, 20)],
    "settled_rows_disagree": [(0, 4, False, False, 1, 10), (1, 6, False, False, 2, 20), (2, 8, True, True, 3, 5)],
    "all_unresolved": [(0, 2, True, True, 1, 10), (1, 1, True, True, 2, 30)],
    "unresolved_and_pending_expired": [(0, 2, True, True, 1, 10), (1, 1, True, False, 2, 3)],
    "three_rows": [(2, 1, True, False, 5, 50), (0, 0, True, True, 1, 10), (1, 7, True, False, 3, 30)],
    "single_pending": [(0, 2, True, False, 1, 10)],
    "single_unresolved": [(1, 4, True, True, 2, 20)],
}


def _row(company, user, operation, credits, pending, unresolved, created, reconcile, row_id=None) -> dict:
    return {"id": row_id or uuid.uuid4(), "company_id": company, "user_id": user, "operation_id": operation,
            "credits": credits, "pending": pending, "unresolved": unresolved,
            "reconcile_at": T0 + timedelta(seconds=reconcile), "created_at": T0 + timedelta(seconds=created)}


def _generated(companies, users, groups=240, seed=429) -> list[dict]:
    """Duplicate groups over credits, pending, unresolved, reconcile times and created-at ties."""
    rng = random.Random(seed)
    rows = []
    for _ in range(groups):
        company, operation = rng.choice(companies), uuid.uuid4()
        for user in rng.sample(users, rng.randint(1, len(users))):  # the old key: one row per user
            pending = rng.random() < 0.7
            rows.append(_row(company, user, operation, rng.randint(0, 6), pending,
                             pending and rng.random() < 0.4, rng.randint(0, 3), rng.randint(0, 60),
                             row_id=uuid.UUID(int=rng.getrandbits(128), version=4)))
    return rows


def _oracle(rows: list[dict]) -> dict:
    """The merged usage per (company, operation), written out independently of the SQL."""
    groups: dict = {}
    for r in rows:
        groups.setdefault((r["company_id"], r["operation_id"]), []).append(r)
    out = {}
    for key, group in groups.items():
        kept = dict(min(group, key=lambda r: (r["created_at"], str(r["id"]))))
        if len(group) > 1:
            settled = [r["credits"] for r in group if not r["pending"]]
            kept.update(credits=max(settled) if settled else max(r["credits"] for r in group),
                        pending=not settled, unresolved=all(r["unresolved"] for r in group),
                        reconcile_at=max(r["reconcile_at"] for r in group))
        out[key] = kept
    return out


def _seed(conn) -> tuple[list[dict], dict]:
    users, companies = [uuid.uuid4() for _ in range(5)], [uuid.uuid4() for _ in range(3)]
    for user in users:
        conn.execute(text("INSERT INTO users (id, email, name, is_active, created_at) "
                          "VALUES (:id, :email, 'User', true, now())"), {"id": user, "email": f"{user}@example.com"})
    for company in companies:
        conn.execute(text("INSERT INTO companies (id, name, slug, settings, is_active, created_at) "
                          "VALUES (:id, 'Co', :slug, CAST('{}' AS json), true, now())"),
                     {"id": company, "slug": f"co-{str(company)[:8]}"})
    cases, rows = {}, []
    for name, spec in CASES.items():
        cases[name] = (companies[0], uuid.uuid4())
        rows += [_row(companies[0], users[u], cases[name][1], *rest) for u, *rest in spec]
    rows += _generated(companies, users)
    for r in rows:
        conn.execute(text(f"INSERT INTO ai_usage ({_COLS}) VALUES (:id, :company_id, :user_id, :operation_id, "
                          ":credits, :pending, :unresolved, :reconcile_at, :created_at)"), r)
    return rows, {"users": users, "cases": cases}


async def _written(async_url: str, company, users) -> uuid.UUID:
    """One operation as the usage writer records it under the old key: the first user's
    replies report 3 and 4 while the charge is unnamed, then the second user's retry gets
    the operation's whole charge, 5, named by the service."""
    from celerp.ai import usage
    from migration_support import maker

    engine, op = create_async_engine(async_url, poolclass=NullPool), uuid.uuid4()
    try:
        async with maker(engine)() as s:
            for user, credits, known in ((users[0], 3, False), (users[0], 4, False), (users[1], 5, True)):
                await usage.record_usage(s, company_id=company, user_id=user, operation_id=op,
                                         credits=credits, known=known)
                await s.commit()
    finally:
        await engine.dispose()
    return op


def _read(conn) -> dict:
    return {(r["company_id"], r["operation_id"]): dict(r)
            for r in conn.execute(text(f"SELECT {_COLS} FROM ai_usage")).mappings()}


def _key(conn) -> str:
    return conn.execute(text(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'uq_ai_usage_operation'")).scalar_one()


@pytest.fixture(scope="module", params=["SQL_ASCII", "UTF8"])
def migrated(request):
    from alembic_support import alembic_run

    with throwaway_db("aiusage", encoding=request.param) as (async_url, sync_url):
        upgrade_to(sync_url, _PREV)
        eng = create_engine(sync_url)
        try:
            with eng.begin() as conn:
                rows, ctx = _seed(conn)
            company = ctx["cases"]["single_pending"][0]
            ctx["written"] = (company, asyncio.run(_written(async_url, company, ctx["users"])))
            with eng.connect() as conn:
                ctx["written_rows"] = [dict(r) for r in conn.execute(text(
                    f"SELECT {_COLS} FROM ai_usage WHERE operation_id = :op"), {"op": ctx["written"][1]}).mappings()]
            rows = rows + ctx["written_rows"]
            upgrade_to(sync_url, _REV)
            with eng.connect() as conn:
                up, up_key = _read(conn), _key(conn)
            alembic_run(sync_url, "downgrade", _PREV)
            with eng.connect() as conn:
                down, down_key = _read(conn), _key(conn)
        finally:
            eng.dispose()
    return {"rows": rows, "up": up, "up_key": up_key, "down": down, "down_key": down_key, **ctx}


def _case(m, name) -> tuple:
    r = m["up"][m["cases"][name]]
    users = m["users"]
    return (users.index(r["user_id"]), r["credits"], r["pending"], r["unresolved"],
            int((r["reconcile_at"] - T0).total_seconds()))


@pytest.mark.parametrize("name, expected", [  # (user index, credits, pending, unresolved, reconcile s)
    ("pending_zero_then_pending_three", (0, 3, True, False, 20)),
    ("settled_then_pending", (0, 5, False, False, 20)),
    ("pending_then_settled", (0, 5, False, False, 20)),
    ("settled_rows_disagree", (0, 6, False, False, 20)),
    ("all_unresolved", (0, 2, True, True, 30)),
    ("unresolved_and_pending_expired", (0, 2, True, False, 10)),
    ("three_rows", (0, 7, True, False, 50)),
    ("single_pending", (0, 2, True, False, 10)),
    ("single_unresolved", (1, 4, True, True, 20)),
])
def test_an_operation_merges_into_its_earliest_row_with_the_strongest_charge_evidence(migrated, name, expected):
    assert _case(migrated, name) == expected


def test_the_writers_settled_charge_wins_over_its_larger_pending_charge(migrated):
    before = sorted((r["credits"], r["pending"]) for r in migrated["written_rows"])
    assert before == [(5, False), (7, True)]
    r = migrated["up"][migrated["written"]]
    assert (migrated["users"].index(r["user_id"]), r["credits"], r["pending"], r["unresolved"]) == (0, 5, False, False)


def test_every_merged_row_equals_the_oracle_and_single_rows_are_untouched(migrated):
    expected = _oracle(migrated["rows"])
    assert migrated["up"] == expected
    singles = {k for k, v in expected.items()
               if sum(1 for r in migrated["rows"] if (r["company_id"], r["operation_id"]) == k) == 1}
    originals = {(r["company_id"], r["operation_id"]): r for r in migrated["rows"]}
    assert len(singles) > 20 and all(migrated["up"][k] == originals[k] for k in singles)


def test_each_company_total_is_the_strongest_charge_of_each_operation(migrated):
    totals: dict = {}
    for (company, _), r in migrated["up"].items():
        totals[company] = totals.get(company, 0) + r["credits"]
    expected: dict = {}
    for (company, _), r in _oracle(migrated["rows"]).items():
        expected[company] = expected.get(company, 0) + r["credits"]
    assert totals == expected and len(totals) == 3


def test_duplicate_usage_rows_merge_into_the_earliest_and_the_key_is_company_and_operation(migrated):
    assert len(migrated["up"]) == len({(r["company_id"], r["operation_id"]) for r in migrated["rows"]})
    assert migrated["up_key"] == "UNIQUE (company_id, operation_id)"
    assert migrated["down"] == migrated["up"]
    assert migrated["down_key"] == "UNIQUE (company_id, user_id, operation_id)"
