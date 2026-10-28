# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""The lock rank checker sees every lock a statement can wait on, and judges every order.

Each test is a negative control: one way a lock could reach PostgreSQL, or one order of
acquisitions, that the checker must record or refuse. A checker that misses any of them
reports a clean graph while a deadlock ships."""

from __future__ import annotations

import asyncio
import json

import pytest

import lock_rank
from lock_rank import Lock, Recorder, classify


def _lock(cls: str, mode: str, key: str = "1") -> Lock:
    return Lock(cls, mode, "celerp/x.py:1 f", "test_x.py:1", key)


def _kinds(recorder: Recorder) -> set[tuple[str, str]]:
    return {(v["kind"], v["cls"]) for v in recorder.violations}


def test_a_write_on_an_unranked_table_is_an_unknown_lock():
    recorder = Recorder()
    recorder.acquire(1, _lock("row:zz_unranked", "write"), 0.0)
    assert ("unknown", "row:zz_unranked") in _kinds(recorder)


def test_the_offline_check_lists_a_table_that_was_only_written(tmp_path):
    (tmp_path / "lock-graph-gw0.json").write_text(json.dumps({
        "edges": [], "contention": [], "violations": [],
        "classes": {"row:zz_unranked": {"count": 1, "modes": ["write"], "sites": ["celerp/x.py:1 f"]}},
    }))
    assert "row:zz_unranked" in lock_rank.check(tmp_path).splitlines()[0]


@pytest.mark.parametrize("statement, cls, strength", [
    ("SELECT id FROM companies WHERE id = $1 FOR UPDATE;", "row:companies", "update"),
    ("SELECT id FROM companies WHERE id = $1 FOR UPDATE -- the company first", "row:companies", "update"),
    ("SELECT id FROM companies WHERE id = $1 FOR UPDATE /* first */", "row:companies", "update"),
    ("WITH c AS (SELECT id FROM companies WHERE id = $1 FOR UPDATE) SELECT id FROM c", "row:companies", "update"),
    ("UPDATE projections SET state = $1 WHERE company_id IN "
     "(SELECT id FROM companies WHERE id = $2 FOR SHARE)", "row:companies", "share"),
    ("SELECT p.id FROM projections p JOIN companies c ON c.id = p.company_id "
     "WHERE p.id = $1 FOR NO KEY UPDATE OF p\n", "row:projections", "no key update"),
])
def test_a_row_lock_clause_is_seen_wherever_it_stands(statement, cls, strength):
    assert any(c == cls and mode.startswith(strength) for c, mode in classify(statement, ("a", "b")))


@pytest.mark.parametrize("statement, tables, mode", [
    ("LOCK TABLE projections IN SHARE ROW EXCLUSIVE MODE", ["projections"], "share row exclusive"),
    ('LOCK TABLE ONLY "ledger", projections IN ACCESS EXCLUSIVE MODE NOWAIT',
     ["ledger", "projections"], "access exclusive"),
    ("lock projections in exclusive mode", ["projections"], "exclusive"),
])
def test_a_table_lock_is_recorded_as_its_own_class(statement, tables, mode):
    found = classify(statement, ())
    assert {c for c, _ in found} == {f"tbl:{t}" for t in tables}
    assert all(m.startswith(mode) for _, m in found)


def test_an_insert_takes_key_share_on_every_row_it_references():
    found = classify("INSERT INTO user_companies (id, user_id, company_id, role) VALUES ($1, $2, $3, $4)",
                     ("i", "u", "c", "owner"))
    assert ("row:companies", "key share wait") in found
    assert ("row:users", "key share wait") in found


def test_an_insert_waits_on_its_own_table_for_a_unique_key():
    found = classify("INSERT INTO user_companies (id, user_id, company_id, role) VALUES ($1, $2, $3, $4)",
                     ("i", "u", "c", "owner"))
    assert ("row:user_companies", "insert") in found


def test_returning_to_a_held_class_after_a_higher_rank_is_an_order_violation():
    recorder = Recorder()
    recorder.acquire(1, _lock("row:companies", "update wait", key="a"), 0.0)
    recorder.acquire(1, _lock("row:projections", "write", key="item"), 0.0)
    recorder.acquire(1, _lock("row:companies", "update wait", key="b"), 0.0)
    assert ("order", "row:companies") in _kinds(recorder)
    assert ("row:projections", "row:companies") in recorder.edges


def test_one_task_waiting_on_a_second_connection_while_holding_a_lock_on_the_first_fails():
    recorder = Recorder()

    async def body():
        recorder.acquire(1, _lock("row:companies", "update wait"), 0.0)
        recorder.acquire(2, _lock("row:connector_configs", "update wait"), 0.0)

    asyncio.run(body())
    assert ("cross-connection", "row:connector_configs") in _kinds(recorder)


def test_one_task_waiting_on_the_same_key_on_two_connections_is_a_self_deadlock():
    recorder = Recorder()

    async def body():
        recorder.acquire(1, _lock("row:connector_configs", "update wait", key="cfg"), 0.0)
        recorder.acquire(2, _lock("row:connector_configs", "update wait", key="cfg"), 0.0)

    asyncio.run(body())
    assert ("self-deadlock", "row:connector_configs") in _kinds(recorder)


def test_two_tasks_on_two_connections_are_not_cross_connection():
    recorder = Recorder()

    async def take(conn: int, cls: str):
        recorder.acquire(conn, _lock(cls, "update wait"), 0.0)

    async def body():
        await asyncio.gather(take(1, "row:companies"), take(2, "row:connector_configs"))

    asyncio.run(body())
    assert not {k for k, _ in _kinds(recorder)} & {"cross-connection", "self-deadlock"}


def test_the_outbound_identity_lock_may_be_held_on_its_own_connection():
    recorder = Recorder()

    async def body():
        recorder.acquire(1, _lock("adv:outbound", "session exclusive wait"), 0.0)
        recorder.acquire(2, _lock("row:companies", "update wait"), 0.0)

    asyncio.run(body())
    assert not {k for k, _ in _kinds(recorder)} & {"cross-connection", "self-deadlock"}


def test_the_offline_check_judges_edges_seen_across_test_statements(tmp_path):
    (tmp_path / "lock-graph-gw0.json").write_text(json.dumps({
        "contention": [], "violations": [],
        "classes": {c: {"count": 1, "modes": ["update wait"], "sites": ["celerp/x.py:1 f"]}
                    for c in ("row:companies", "row:projections")},
        "edges": [{"from": "row:projections", "to": "row:companies", "count": 1, "waits": True,
                   "gate": [], "modes": ["update>update"], "call_modes": [],
                   "example": {"held": "celerp/x.py:1 f", "acquired": "celerp/y.py:2 g", "test": "t"}}],
    }))
    out = lock_rank.check(tmp_path)
    assert "row:projections (60) -> row:companies (30)" in out
