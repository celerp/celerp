# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""The lock rank checker sees every lock a statement can wait on, and judges every order.

Each test is a negative control: one way a lock could reach PostgreSQL, or one order of
acquisitions, that the checker must record or refuse. A checker that misses any of them
reports a clean graph while a deadlock ships."""

from __future__ import annotations

import asyncio
import json
import threading

import pytest

import lock_rank
from lock_rank import Lock, Recorder, classify


@pytest.fixture(autouse=True)
def _no_debt(tmp_path, monkeypatch):
    """A synthetic graph is judged against an empty debt file (a test that needs entries
    writes its own): the suite's entries would read as stale in every graph that lacks them."""
    monkeypatch.setattr(lock_rank, "DEBT_FILE", tmp_path / "no-debt.txt")


_PRODUCTION_SITE = lock_rank._production_site


@pytest.fixture(autouse=True)
def _statements_sited_in_production(monkeypatch):
    """The recorder census counts only statements production code sends; a synthetic
    statement here is sent from this file, so it stands for one production sends."""
    monkeypatch.setattr(lock_rank, "_production_site", lambda site: True)


def _lock(cls: str, mode: str, key: str = "1") -> Lock:
    return Lock(cls, mode, "celerp/x.py:1 f", "test_x.py:1", key)


def _kinds(recorder: Recorder) -> set[tuple[str, str]]:
    return {(v["kind"], v["cls"]) for v in recorder.violations}


def _offline_kinds(recorder: Recorder, tmp_path) -> set[tuple[str, str]]:
    """(kind, taken class) of every key the offline check of the recorded graph lists."""
    (tmp_path / "lock-graph-gw0.json").write_text(json.dumps(recorder.dump()))
    return {(line.split()[0], line.split()[3]) for line in lock_rank.debt_listing(tmp_path).splitlines()}


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
    assert ("key:companies", "key share wait") in found
    assert ("key:users", "key share wait") in found


def test_a_foreign_key_share_after_a_higher_rank_is_not_ordered():
    recorder = Recorder()
    recorder.acquire(1, _lock("row:projections", "write", key="item"), 0.0)
    recorder.acquire(1, _lock("key:companies", "key share wait", key="c"), 0.0)
    assert not recorder.violations
    assert ("row:projections", "key:companies") not in recorder.edges


def test_an_explicit_key_share_of_a_parent_row_is_the_foreign_key_checks_lock():
    # The notice writer shares, at commit, every company its notices name, after the rows it wrote.
    assert classify("SELECT id FROM companies WHERE id = $1 FOR KEY SHARE", ("c",)) == [
        ("key:companies", "key share wait")]
    recorder = Recorder()
    recorder.statement(1, _LEDGER, ("l1", "c1", "item"), 0.0)
    recorder.statement(1, _SHARE_COMPANIES, ("c1", "c2"), 0.0)
    assert not recorder.violations


def test_a_key_share_of_a_row_no_foreign_key_names_stays_a_row_lock():
    assert classify("SELECT id FROM work_centers WHERE id = $1 FOR KEY SHARE", ("w",)) == [
        ("row:work_centers", "key share wait")]


@pytest.mark.parametrize("statement", [
    "SELECT id FROM companies WHERE id = $1 FOR UPDATE",
    "DELETE FROM companies WHERE id = $1",
])
def test_taking_a_parent_key_exclusively_is_recorded_as_the_key(statement):
    assert any(c == "key:companies" for c, _ in classify(statement, ("c",)))


def test_a_lock_after_a_parent_key_taken_exclusively_is_an_order_violation():
    recorder = Recorder()
    recorder.acquire(1, _lock("key:companies", "update wait", key="c"), 0.0)
    recorder.acquire(1, _lock("row:projections", "write", key="item"), 0.0)
    assert ("order", "row:projections") in _kinds(recorder)


def test_no_key_update_on_a_parent_leaves_its_key_alone():
    assert not any(c.startswith("key:") for c, _ in
                   classify("SELECT id FROM companies WHERE id = $1 FOR NO KEY UPDATE", ("c",)))


def test_an_insert_waits_on_its_own_table_for_a_unique_key():
    found = classify("INSERT INTO user_companies (id, user_id, company_id, role) VALUES ($1, $2, $3, $4)",
                     ("i", "u", "c", "owner"))
    assert ("row:user_companies", "insert") in found


def test_an_insert_into_a_table_keyed_only_by_a_serial_id_waits_on_nothing_of_its_own():
    """Two inserts collide only on a unique key; a table whose only one is a serial id the
    database generates has none two transactions could share. Its references still wait."""
    found = classify("INSERT INTO sync_runs (company_id, connector, entity, direction, started_at, status) "
                     "VALUES ($1, $2, $3, $4, $5, $6)", ("c", "woocommerce", "orders", "inbound", "t", "ok"))
    assert not any(cls == "row:sync_runs" for cls, _ in found)
    assert ("row:sync_runs", "insert") in classify(
        "INSERT INTO sync_runs (id, company_id) VALUES ($1, $2)", (1, "c"))
    assert ("row:connector_sources", "insert") in classify(
        "INSERT INTO connector_sources (company_id, connector, store_handle) VALUES ($1, $2, $3)",
        ("c", "woocommerce", "h"))


def test_returning_to_a_held_class_after_a_higher_rank_is_an_order_violation():
    recorder = Recorder()
    recorder.acquire(1, _lock("row:companies", "update wait", key="a"), 0.0)
    recorder.acquire(1, _lock("row:projections", "write", key="item"), 0.0)
    recorder.acquire(1, _lock("row:companies", "update wait", key="b"), 0.0)
    assert ("order", "row:companies") in _kinds(recorder)
    assert ("row:projections", "row:companies") in recorder.edges


def test_one_task_waiting_on_a_second_connection_while_holding_a_lock_on_the_first_fails(tmp_path):
    recorder = Recorder()

    async def body():
        recorder.acquire(1, _lock("row:outbound_queue", "update wait"), 0.0)
        recorder.acquire(2, _lock("row:connector_configs", "update wait"), 0.0)

    asyncio.run(body())
    assert ("cross-connection", "row:connector_configs") in _offline_kinds(recorder, tmp_path)


def test_one_task_waiting_on_the_same_key_on_two_connections_is_a_self_deadlock():
    recorder = Recorder()

    async def body():
        recorder.acquire(1, _lock("row:connector_configs", "update wait", key="cfg"), 0.0)
        recorder.acquire(2, _lock("row:connector_configs", "update wait", key="cfg"), 0.0)

    asyncio.run(body())
    assert ("self-deadlock", "row:connector_configs") in _kinds(recorder)


def _second_connection_after(*statements: str) -> set[tuple[str, str]]:
    """One task locks its company on connection 1 around ``statements`` ("lock" takes the
    company), then waits for the same company on connection 2."""
    recorder = Recorder()

    async def body():
        for statement in statements:
            if statement == "lock":
                recorder.acquire(1, _lock("row:companies", "no key update wait", key="a"), 0.0)
            else:
                recorder.statement(1, statement, None, 0.0)
        recorder.acquire(2, _lock("row:companies", "no key update wait", key="a"), 0.0)

    asyncio.run(body())
    return {k for k, _ in _kinds(recorder)} & {"cross-connection", "self-deadlock"}


def test_a_lock_taken_in_a_savepoint_rolled_back_is_no_longer_held():
    assert not _second_connection_after("SAVEPOINT sa_1", "lock", "ROLLBACK TO SAVEPOINT sa_1")


def test_rolling_back_to_an_outer_savepoint_drops_the_inner_ones_locks_too():
    assert not _second_connection_after("SAVEPOINT sa_1", "SAVEPOINT sa_2", "lock",
                                        "RELEASE SAVEPOINT sa_2", "ROLLBACK TO SAVEPOINT sa_1")


@pytest.mark.parametrize("statements", [
    ("lock", "SAVEPOINT sa_1", "ROLLBACK TO SAVEPOINT sa_1"),
    ("SAVEPOINT sa_1", "lock", "RELEASE SAVEPOINT sa_1"),
    ("SAVEPOINT sa_1", "SAVEPOINT sa_2", "lock", "ROLLBACK TO SAVEPOINT sa_1", "lock"),
], ids=["taken-before-it", "released", "taken-again"])
def test_a_lock_a_savepoint_rollback_does_not_undo_is_still_held(statements):
    assert _second_connection_after(*statements)


def test_two_tasks_on_two_connections_are_not_cross_connection():
    recorder = Recorder()

    async def take(conn: int, cls: str):
        recorder.acquire(conn, _lock(cls, "update wait"), 0.0)

    async def body():
        await asyncio.gather(take(1, "row:companies"), take(2, "row:connector_configs"))

    asyncio.run(body())
    assert not {k for k, _ in _kinds(recorder)} & {"cross-connection", "self-deadlock"}


_NOTICES = "SELECT notifications.id FROM notifications ORDER BY notifications.company_id FOR UPDATE"
_COMPANY = "SELECT companies.id FROM companies WHERE companies.id = 'a' FOR UPDATE"


def _flows_on_one_connection(first: list[str], then: list[str], *, shared: bool) -> Recorder:
    """One task runs ``first`` on connection 1 and stays open; a second task then runs
    ``then`` on the same connection. "end" ends the running flow's session transaction."""
    recorder = Recorder()
    if shared:
        recorder.shared.add(1)
    opened, done = asyncio.Event(), asyncio.Event()

    def run(statements):
        for statement in statements:
            if statement == "end":
                recorder.end_flow(1)
            else:
                recorder.statement(1, statement, None, 0.0)

    async def one():
        run(first)
        opened.set()
        await done.wait()

    async def two():
        await opened.wait()
        run(then)
        done.set()

    async def body():
        await asyncio.gather(one(), two())

    asyncio.run(body())
    return recorder


def test_a_flow_on_the_shared_test_connection_holds_only_its_own_locks():
    """The test session's one connection carries the request, AI job and lifecycle sessions,
    which production runs on connections of their own: a job's notices are not held by a
    request that runs while the job is open."""
    recorder = _flows_on_one_connection([_NOTICES], [_COMPANY], shared=True)
    assert ("order", "row:companies") not in _kinds(recorder)


def test_one_flow_on_the_shared_test_connection_still_orders_its_own_locks():
    recorder = _flows_on_one_connection([], [_NOTICES, _COMPANY], shared=True)
    assert ("order", "row:companies") in _kinds(recorder)


def test_a_flow_that_ended_its_transaction_holds_nothing_after_it():
    recorder = _flows_on_one_connection([], [_NOTICES, "end", _COMPANY], shared=True)
    assert ("order", "row:companies") not in _kinds(recorder)


def test_one_flow_ending_leaves_another_flows_locks_held():
    recorder = _flows_on_one_connection([_NOTICES], ["end"], shared=True)
    assert any(lock.cls == "row:notifications" for held in recorder.held.values() for lock in held)


def test_a_connection_not_shared_is_one_transaction_whatever_the_task():
    recorder = _flows_on_one_connection([_NOTICES], [_COMPANY], shared=False)
    assert ("order", "row:companies") in _kinds(recorder)


def test_the_connection_ending_ends_every_flow_on_it():
    recorder = _flows_on_one_connection([_NOTICES], [_COMPANY], shared=True)
    recorder.end(1)
    assert not any(recorder.held.values())


def test_a_savepoint_rolled_back_by_a_shielded_close_releases_what_its_flow_took_after_it():
    """AsyncSession.close() runs shielded, in a task of its own: the savepoint it rolls back on
    the shared test connection is still the closing flow's, so what that flow took after the
    savepoint is released, as PostgreSQL releases it."""
    recorder = Recorder()
    recorder.shared.add(1)

    async def close():
        recorder.statement(1, "ROLLBACK TO SAVEPOINT sa_savepoint_1", None, 0.0)

    async def body():
        recorder.statement(1, "SAVEPOINT sa_savepoint_1", None, 0.0)
        recorder.statement(1, _NOTICES, None, 0.0)
        await asyncio.shield(close())
        recorder.statement(1, _COMPANY, None, 0.0)

    asyncio.run(body())
    assert ("order", "row:companies") not in _kinds(recorder)


_NOTICE_ROWS = "SELECT notifications.id FROM notifications ORDER BY notifications.company_id FOR UPDATE"
_COMPANY_ROW = "SELECT companies.id FROM companies WHERE companies.id = 'a' FOR UPDATE"


def _block_in_one_task(before: list[str], block: list[str], after: list[str]) -> Recorder:
    """One task on the shared connection 1 runs ``before``, then ``block`` in a block session
    (a session of its own in production, Recorder.block), whose transaction ends with the block,
    then ``after``."""
    recorder = Recorder()
    recorder.shared.add(1)

    async def body():
        for statement in before:
            recorder.statement(1, statement, None, 0.0)
        with recorder.block():
            for statement in block:
                recorder.statement(1, statement, None, 0.0)
            recorder.end_flow(1)
        for statement in after:
            recorder.statement(1, statement, None, 0.0)

    asyncio.run(body())
    return recorder


def test_a_block_that_waits_while_its_task_holds_locks_waits_across_connections(tmp_path):
    """Production opens the block's session on a connection of its own: its task waits there
    while its own transaction holds what it took before."""
    recorder = _block_in_one_task([_NOTICE_ROWS], [_COMPANY_ROW], [])
    assert ("cross-connection", "row:companies") in _offline_kinds(recorder, tmp_path)


def test_a_block_ending_leaves_its_task_holding_what_it_took_before():
    recorder = _block_in_one_task([_NOTICE_ROWS], [], [_COMPANY_ROW])
    assert ("order", "row:companies") in _kinds(recorder)


def test_what_a_block_took_ends_with_it():
    recorder = _block_in_one_task([], [_NOTICE_ROWS], [_COMPANY_ROW])
    assert not _kinds(recorder)
    assert not any(lock.cls == "row:notifications" for held in recorder.held.values() for lock in held)


def test_the_outbound_identity_lock_may_be_held_on_its_own_connection():
    recorder = Recorder()

    async def body():
        recorder.acquire(1, _lock("adv:outbound", "session exclusive wait"), 0.0)
        recorder.acquire(2, _lock("row:companies", "update wait"), 0.0)

    asyncio.run(body())
    assert not {k for k, _ in _kinds(recorder)} & {"cross-connection", "self-deadlock"}


def test_a_key_every_taker_only_tries_may_be_held_while_waiting_on_another_connection():
    """A Xero fetch round holds its key on a connection of its own while its work waits on
    others: no one waits on that key, so the hold closes no cycle."""
    recorder = Recorder()

    async def body():
        recorder.acquire(1, _lock("adv:xero-attachments-round", "session exclusive try"), 0.0)
        recorder.acquire(2, _lock("row:migration_attachment_fetches", "update wait"), 0.0)

    asyncio.run(body())
    assert not {k for k, _ in _kinds(recorder)} & {"cross-connection", "self-deadlock"}


def test_a_waiting_take_of_a_key_every_taker_should_only_try_fails():
    recorder = Recorder()
    recorder.acquire(1, _lock("adv:xero-attachments-round", "session exclusive wait"), 0.0)
    assert ("try-only", "adv:xero-attachments-round") in _kinds(recorder)


def test_a_key_every_taker_takes_shared_orders_nothing_it_is_held_before():
    """Boot's create_tables runs in a guarded transaction: the version fence first, shared,
    then the schema key. No one takes the fence exclusively (versions are kept apart under
    the admit lock), so no one waits on it and the hold closes no cycle."""
    recorder = Recorder()
    recorder.acquire(1, _lock("adv:version-fence", "xact shared wait"), 0.0)
    recorder.acquire(1, _lock("adv:migration", "xact exclusive wait"), 0.0)
    assert not _kinds(recorder)


def test_a_key_every_taker_takes_shared_may_be_held_while_waiting_on_another_connection():
    """``celerp migrate`` joins the fence on a connection of its own, then waits for the
    migration key on another."""
    recorder = Recorder()

    async def body():
        recorder.acquire(1, _lock("adv:version-fence", "session shared wait"), 0.0)
        recorder.acquire(2, _lock("adv:migration", "session exclusive wait"), 0.0)

    asyncio.run(body())
    assert not {k for k, _ in _kinds(recorder)} & {"cross-connection", "self-deadlock"}


def test_an_exclusive_take_of_a_key_every_taker_should_take_shared_fails():
    recorder = Recorder()
    recorder.acquire(1, _lock("adv:version-fence", "xact exclusive wait"), 0.0)
    assert ("shared-only", "adv:version-fence") in _kinds(recorder)


def test_a_key_others_wait_on_still_fails_when_held_across_connections(tmp_path):
    """Held under the migration key, the wait is judged offline (R5); a take of the class
    outside the key, as every import runner makes, leaves it unproven. The flow also holds a
    higher rank, so ranks do not prove it either (R7)."""
    recorder = Recorder()

    async def body():
        recorder.acquire(1, _lock("adv:migration", "session exclusive try"), 0.0)
        recorder.acquire(1, _lock("row:session_registry", "write"), 0.0)
        recorder.acquire(2, _lock("row:migration_runs", "update wait"), 0.0)

    asyncio.run(body())
    recorder.acquire(3, _lock("row:migration_runs", "update wait"), 0.0)
    (tmp_path / "lock-graph-gw0.json").write_text(json.dumps(recorder.dump()))
    report = lock_rank.check(tmp_path)
    assert "- cross-connection adv:migration -> row:migration_runs celerp/x.py f" in report
    assert lock_rank.check_failed(report)


def test_a_text_key_named_like_a_fixed_key_is_unranked():
    """An import run's key once read "migration:<run>", which the recorder filed as the
    migration key itself: the two would be judged as one lock."""
    found = classify("SELECT pg_try_advisory_xact_lock(hashtext($1))", ("migration:42",))
    assert [c for c, _ in found] == ["adv:?adv:migration (named like a fixed key)"]
    assert lock_rank.rank(found[0][0]) is None


def test_the_import_run_key_has_a_class_of_its_own():
    from celerp.services.migrations import _lock_key
    found = classify("SELECT pg_try_advisory_xact_lock(hashtext($1))", (_lock_key("42"),))
    assert found == [("adv:migration-run", "xact exclusive try")]


def test_a_held_key_every_taker_only_tries_orders_nothing_on_its_own_connection():
    recorder = Recorder()
    recorder.acquire(1, _lock("adv:migration-run", "xact exclusive try"), 0.0)
    recorder.acquire(1, _lock("adv:migration", "xact exclusive wait"), 0.0)
    assert not _kinds(recorder)


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


def test_a_lock_on_a_table_only_a_test_creates_is_not_recorded():
    assert classify("INSERT INTO zz_test_only (id) VALUES ($1)", ("a",)) == []


def test_an_execution_body_taking_the_connector_guard_fails():
    recorder = Recorder()
    lock = Lock("adv:connector-runtime", "xact shared wait", "celerp/x.py:1 f", "", "1", in_execution=True)
    recorder.acquire(1, lock, 0.0)
    assert ("guard", "adv:connector-runtime") in _kinds(recorder)


def test_a_lease_taken_while_holding_more_than_the_guard_fails():
    recorder = Recorder()
    recorder.acquire(1, _lock("row:companies", "no key update wait"), 0.0)
    recorder.acquire(1, _lock("row:connector_execution_leases", "update nowait"), 0.0)
    assert ("lease-held", "row:connector_execution_leases") in _kinds(recorder)


def test_maintenance_takes_a_lease_nowait_holding_more_but_never_waiting_on_one():
    """A whole-company maintenance protocol holds the guard exclusively (no execution runs)
    and the company's rows; it may take the company's lease rows NOWAIT, then write them, but
    a waiting take, or one under the guard's shared lock, still fails."""
    recorder = Recorder()
    recorder.acquire(1, _lock("adv:connector-runtime", "xact exclusive wait"), 0.0)
    recorder.acquire(1, _lock("row:companies", "update wait"), 0.0)
    recorder.acquire(1, _lock("row:connector_execution_leases", "update nowait"), 0.0)
    recorder.acquire(1, _lock("row:connector_execution_heartbeats", "update nowait"), 0.0)
    recorder.acquire(1, _lock("row:connector_execution_leases", "write"), 0.0)
    # Its later deletes of the rows it already holds FOR UPDATE wait for nothing.
    recorder.acquire(1, _lock("row:connector_execution_leases", "delete"), 0.0)
    recorder.acquire(1, _lock("row:connector_execution_heartbeats", "delete"), 0.0)
    assert not [k for k in _kinds(recorder) if k[0].startswith("lease")]
    waiting = Recorder()
    waiting.acquire(1, _lock("adv:connector-runtime", "xact exclusive wait"), 0.0)
    waiting.acquire(1, _lock("row:companies", "update wait"), 0.0)
    waiting.acquire(1, _lock("row:connector_execution_heartbeats", "update wait"), 0.0)
    assert ("lease-held", "row:connector_execution_heartbeats") in _kinds(waiting)


def test_a_lease_taken_under_the_guard_without_nowait_fails():
    recorder = Recorder()
    recorder.acquire(1, _lock("adv:connector-runtime", "xact shared wait"), 0.0)
    recorder.acquire(1, _lock("adv:connector-owner", "xact shared wait"), 0.0)
    recorder.acquire(1, _lock("row:connector_execution_leases", "update wait"), 0.0)
    assert {k for k, _ in _kinds(recorder)} == {"lease-wait"}


def test_a_lease_row_written_without_its_for_update_fails():
    recorder = Recorder()
    recorder.acquire(1, _lock("row:connector_execution_leases", "write"), 0.0)
    assert ("lease-unlocked", "row:connector_execution_leases") in _kinds(recorder)


def test_the_guard_then_a_lease_taken_nowait_then_written_passes():
    recorder = Recorder()
    recorder.acquire(1, _lock("adv:connector-runtime", "xact shared wait"), 0.0)
    recorder.acquire(1, _lock("adv:connector-owner", "xact shared wait"), 0.0)
    recorder.acquire(1, _lock("row:connector_execution_leases", "update nowait"), 0.0)
    recorder.acquire(1, _lock("row:connector_execution_leases", "write"), 0.0)
    assert not recorder.violations


def test_an_insert_into_a_fresh_keyed_table_waits_on_nothing_of_its_own():
    found = classify("INSERT INTO notifications (id, company_id, user_id, category, title, body) "
                     "VALUES ($1, $2, $3, $4, $5, $6)", ("i", "c", None, "system", "t", "b"))
    assert not any(cls == "row:notifications" for cls, _ in found)
    assert ("key:companies", "key share wait") in found


def test_a_notice_is_written_after_every_parent_key_and_nothing_after_it():
    """Notices are their transaction's last writes: after a parent key taken exclusively they
    order nothing, and their own key ranks with their row; any lock taken after one orders."""
    after_key = Recorder()
    after_key.statement(1, "DELETE FROM migration_runs WHERE migration_runs.id = $1", ("m",), 0.0)
    after_key.statement(1, "SELECT notifications.id FROM notifications ORDER BY notifications.company_id, "
                        "notifications.id FOR UPDATE", (), 0.0)
    after_key.statement(1, "DELETE FROM notifications WHERE notifications.id = $1", ("n",), 0.0)
    assert not after_key.violations
    before = Recorder()
    before.statement(1, "DELETE FROM notifications WHERE notifications.id = $1", ("n",), 0.0)
    before.statement(1, "UPDATE work_centers SET name = $1 WHERE work_centers.id = $2", ("w", "1"), 0.0)
    assert ("order", "row:work_centers") in _kinds(before)


def _production_sources():
    root = lock_rank.Path(lock_rank._ROOT)
    for path in [*root.joinpath("celerp").rglob("*.py"), *root.joinpath("default_modules").glob("*/celerp_*/**/*.py")]:
        yield path, path.read_text()


@pytest.mark.parametrize("table", sorted(lock_rank.FRESH_KEYED))
def test_every_insert_into_a_fresh_keyed_table_draws_a_new_key(table):
    """FRESH_KEYED holds: the table's only unique key is a uuid primary key defaulting to
    uuid.uuid4, every model construction leaves it to that default or passes uuid.uuid4(),
    and every SQL insert into it takes gen_random_uuid(). Nothing builds the table through
    insert() of the model or its table, or copies a stored id into it."""
    import ast
    import uuid

    from celerp.models.base import Base

    mapper = next(m for m in Base.registry.mappers if m.local_table.name == table)
    t = mapper.local_table
    pk = list(t.primary_key.columns)
    assert len(pk) == 1 and getattr(getattr(pk[0].default, "arg", None), "__wrapped__", None) is uuid.uuid4
    assert not [c for c in t.constraints if c.__class__.__name__ == "UniqueConstraint"]
    assert not [i for i in t.indexes if i.unique] and not [c for c in t.columns if c.unique]

    model = mapper.class_.__name__
    fresh = ast.parse("uuid.uuid4()").body[0].value
    wrong = []
    for path, text in _production_sources():
        if table in text and "INSERT INTO " + table in text:
            for chunk in text.split("INSERT INTO " + table)[1:]:
                if "gen_random_uuid()" not in chunk.split(";")[0][:600]:
                    wrong.append(f"{path}: SQL insert without gen_random_uuid()")
        if model not in text:
            continue
        for node in ast.walk(ast.parse(text)):
            if not isinstance(node, ast.Call):
                continue
            name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
            args = [a for a in node.args if isinstance(a, ast.Name) and a.id == model
                    or isinstance(a, ast.Attribute) and isinstance(a.value, ast.Name) and a.value.id == model]
            if name in ("insert", "pg_insert") and args:
                wrong.append(f"{path}:{node.lineno}: insert({model})")
            if name == model:
                for kw in node.keywords:
                    if kw.arg is None or (kw.arg == pk[0].key and ast.dump(kw.value) != ast.dump(fresh)):
                        wrong.append(f"{path}:{node.lineno}: {model}(...) with a key not drawn fresh")
    assert not wrong, wrong


@pytest.mark.parametrize("table", sorted(lock_rank.FRESH_PARENTED))
def test_every_insert_into_a_fresh_parented_table_names_a_row_its_function_built(table):
    """FRESH_PARENTED holds: the table's primary key is a fresh uuid, every other unique key
    holds the column, which references the FRESH_KEYED parent, and every model construction
    passes it the id of a parent the same function built. Nothing inserts it by SQL or insert()."""
    import ast
    import uuid

    from celerp.models.base import Base

    column, parent = lock_rank.FRESH_PARENTED[table]
    assert parent in lock_rank.FRESH_KEYED
    mappers = {m.local_table.name: m for m in Base.registry.mappers}
    t = mappers[table].local_table
    pk = list(t.primary_key.columns)
    assert len(pk) == 1 and getattr(getattr(pk[0].default, "arg", None), "__wrapped__", None) is uuid.uuid4
    assert {fk.column.table.name for fk in t.columns[column].foreign_keys} == {parent}
    uniques = [c.columns for c in t.constraints if c.__class__.__name__ == "UniqueConstraint"]
    uniques += [i.columns for i in t.indexes if i.unique] + [[c] for c in t.columns if c.unique]
    assert uniques and all(column in {c.name for c in u} for u in uniques)

    model, parent_model = mappers[table].class_.__name__, mappers[parent].class_.__name__
    wrong, built = [], 0
    for path, text in _production_sources():
        if "INSERT INTO " + table in text:
            wrong.append(f"{path}: SQL insert into {table}")
        if model not in text:
            continue
        for fn in ast.walk(ast.parse(text)):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            parents = {a.targets[0].id for a in ast.walk(fn) if isinstance(a, ast.Assign)
                       and isinstance(a.targets[0], ast.Name) and isinstance(a.value, ast.Call)
                       and getattr(a.value.func, "id", None) == parent_model}
            for node in ast.walk(fn):
                if not isinstance(node, ast.Call):
                    continue
                name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
                if name in ("insert", "pg_insert") and any(getattr(a, "id", None) == model for a in node.args):
                    wrong.append(f"{path}:{node.lineno}: insert({model})")
                if name != model:
                    continue
                built += 1
                given = {kw.arg: kw.value for kw in node.keywords}
                value = given.get(column)
                if None in given or pk[0].key in given or not (
                        isinstance(value, ast.Attribute) and value.attr == "id"
                        and isinstance(value.value, ast.Name) and value.value.id in parents):
                    wrong.append(f"{path}:{node.lineno}: {model}(...) not keyed on a {parent_model} built here")
    assert built and not wrong, wrong


# ── R1: rows of a company the transaction inserted; R2: inserts under the company lock ──

_COMPANY = "INSERT INTO companies (id, name, slug) VALUES ($1::UUID, $2, $3)"
_LOCK_COMPANY = "SELECT companies.id FROM companies WHERE companies.id = $1::UUID FOR NO KEY UPDATE"
_ACCOUNT = "INSERT INTO accounts (id, company_id, code) VALUES ($1::UUID, $2::UUID, $3)"
_CLAIM = ("INSERT INTO migration_runs (id, company_id, scan_claim_sha256) "
          "VALUES ($1::UUID, $2::UUID, $3)")
_TOKEN = "INSERT INTO doc_share_tokens (id, company_id, token) VALUES ($1::UUID, $2::UUID, $3)"
_PROJECTION = ("SELECT projections.entity_id FROM projections WHERE projections.company_id = $1::UUID "
               "AND projections.entity_id = $2 FOR UPDATE")
_LEDGER = "INSERT INTO ledger (id, company_id, entity_id) VALUES ($1::UUID, $2::UUID, $3)"
# A row ranked above the ledger (R8), so an ungated ledger insert after it orders against it.
_OUTBOUND = ("SELECT outbound_queue.id FROM outbound_queue WHERE outbound_queue.company_id = $1::UUID "
             "AND outbound_queue.id = $2 FOR UPDATE")


@pytest.fixture
def recorder() -> Recorder:
    return Recorder()


def _chart(recorder: Recorder, conn: int = 1) -> None:
    """An advisory key ranked between the company and its account rows."""
    recorder.acquire(conn, _lock("adv:external-product", "xact exclusive wait", key="p"), 0.0)


def test_rows_of_a_company_the_transaction_inserted_order_nothing(recorder):
    # Provisioning: nobody else can see the new company, so no lock on its rows waits.
    recorder.statement(1, _COMPANY, ("c1", "Co", "co"), 0.0)
    recorder.statement(1, _ACCOUNT, ("a1", "c1", "1000"), 0.0)
    recorder.statement(1, _LOCK_COMPANY, ("c1",), 0.0)
    _chart(recorder)
    assert not recorder.violations


def test_every_company_of_one_multi_row_insert_is_the_transactions_own(recorder):
    # Two companies flushed together are one INSERT naming both: each is as unseen as one alone.
    recorder.statement(1, "INSERT INTO companies (id, name, slug) VALUES ($1::UUID, $2, $3), ($4::UUID, $5, $6)",
                       ("c1", "A", "a", "c2", "B", "b"), 0.0)
    for company in ("c1", "c2"):
        recorder.statement(1, _ACCOUNT, ("a-" + company, company, "1000"), 0.0)
        recorder.statement(1, _LOCK_COMPANY, (company,), 0.0)
    assert not recorder.violations


def test_rows_of_a_committed_company_still_order(recorder):
    recorder.statement(1, _ACCOUNT, ("a1", "c1", "1000"), 0.0)
    recorder.statement(1, _LOCK_COMPANY, ("c1",), 0.0)
    _chart(recorder)
    assert ("order", "row:companies") in _kinds(recorder)
    assert ("order", "adv:external-product") in _kinds(recorder)


def test_an_own_companys_insert_under_a_unique_key_without_the_company_still_orders(recorder):
    # Another transaction can insert the same scan claim, whatever company it names.
    recorder.statement(1, _COMPANY, ("c1", "Co", "co"), 0.0)
    recorder.statement(1, _CLAIM, ("r1", "c1", "sha"), 0.0)
    _chart(recorder)
    assert ("order", "adv:external-product") in _kinds(recorder)


def test_an_insert_under_the_company_lock_keyed_by_a_fresh_secret_is_gated(recorder):
    # No other transaction inserts the same secret, so the company's lock gates the insert.
    recorder.statement(1, _LOCK_COMPANY, ("c1",), 0.0)
    recorder.statement(1, _TOKEN, ("t1", "c1", "tok"), 0.0)
    assert recorder.ungated_sites({"row:doc_share_tokens"}) == set()
    assert recorder.classes["row:doc_share_tokens"].get("gated") == 1


@pytest.mark.parametrize("table", sorted(lock_rank.SECRET_KEYED))
def test_every_insert_into_a_secret_keyed_table_draws_a_new_secret(table):
    """SECRET_KEYED holds: the table's unique keys are its primary key, keys holding the
    company, and the one secret column; every model construction names that column in a
    function that draws from ``secrets``; nothing inserts the table by SQL or insert(), and
    company backups do not carry it (a restore would copy a stored secret)."""
    import ast

    from celerp.models.base import Base
    from celerp.services import company_backup

    column = lock_rank.SECRET_KEYED[table]
    mapper = next(m for m in Base.registry.mappers if m.local_table.name == table)
    t = mapper.local_table
    keys = [[c.name for c in k.columns] for k in t.constraints if k.__class__.__name__ == "UniqueConstraint"]
    keys += [[c.name for c in i.columns] for i in t.indexes if i.unique]
    keys += [[c.name] for c in t.columns if c.unique]
    assert {tuple(k) for k in keys if "company_id" not in k} == {(column,)}
    assert table in company_backup.EXCLUDED_TABLES

    model, built, wrong = mapper.class_.__name__, 0, []
    for path, text in _production_sources():
        if "INSERT INTO " + table in text:
            wrong.append(f"{path}: SQL insert into {table}")
        if model not in text:
            continue
        for fn in ast.walk(ast.parse(text)):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            draws = any(isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
                        and n.value.id == "secrets" for n in ast.walk(fn))
            for node in ast.walk(fn):
                if not isinstance(node, ast.Call):
                    continue
                name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
                if name in ("insert", "pg_insert") and any(isinstance(a, ast.Name) and a.id == model for a in node.args):
                    wrong.append(f"{path}:{node.lineno}: insert({model})")
                if name == model:
                    built += 1
                    if not draws or column not in {kw.arg for kw in node.keywords}:
                        wrong.append(f"{path}:{node.lineno}: {model}(...) without a fresh secret")
    assert built and not wrong, wrong


def test_another_companys_rows_are_not_the_transactions_own(recorder):
    recorder.statement(1, _COMPANY, ("c1", "Co", "co"), 0.0)
    recorder.statement(1, _ACCOUNT, ("a1", "c2", "1000"), 0.0)
    _chart(recorder)
    assert ("order", "adv:external-product") in _kinds(recorder)


def test_an_insert_under_the_company_lock_records_no_order_violation(recorder):
    recorder.statement(1, _LOCK_COMPANY, ("c1",), 0.0)
    recorder.statement(1, _OUTBOUND, ("c1", 7), 0.0)
    recorder.statement(1, _LEDGER, ("l1", "c1", "item"), 0.0)
    assert not recorder.violations


@pytest.mark.parametrize("locked", ["c2", None])
def test_an_insert_not_under_its_companys_lock_is_an_order_violation(recorder, locked):
    if locked:
        recorder.statement(1, _LOCK_COMPANY, (locked,), 0.0)
    recorder.statement(1, _OUTBOUND, ("c1", 7), 0.0)
    recorder.statement(1, _LEDGER, ("l1", "c1", "item"), 0.0)
    assert ("order", "row:ledger") in _kinds(recorder)


# A restore's bulk insert: its rows come from a JSON array, its company is one bound value or
# whatever company the rows themselves name.
_LEDGER_SELECT = ("INSERT INTO ledger (id, company_id, entity_id) SELECT r.id, {company}, r.entity_id "
                  "FROM jsonb_array_elements(CAST($1 AS jsonb)) AS e(v), "
                  "jsonb_populate_record(NULL::ledger, e.v) AS r")


def test_an_insert_select_binding_its_company_under_the_company_lock_is_gated(recorder):
    recorder.statement(1, _LOCK_COMPANY, ("c1",), 0.0)
    recorder.statement(1, _OUTBOUND, ("c1", 7), 0.0)
    recorder.statement(1, _LEDGER_SELECT.format(company="CAST(CAST($2 AS text) AS uuid)"), ("[]", "c1"), 0.0)
    assert not recorder.violations
    assert recorder.ungated_sites({"row:ledger"}) == set()


def test_an_insert_select_taking_its_company_from_its_rows_names_no_company(recorder):
    # The rows could name any company: the insert is judged as made outside the company lock.
    recorder.statement(1, _LOCK_COMPANY, ("c1",), 0.0)
    recorder.statement(1, _OUTBOUND, ("c1", 7), 0.0)
    recorder.statement(1, _LEDGER_SELECT.format(company="r.company_id"), ("[]",), 0.0)
    assert ("order", "row:ledger") in _kinds(recorder)


@pytest.mark.parametrize("ungated", [True, False])
def test_the_offline_check_judges_a_gated_insert_against_the_tables_ungated_inserters(
        recorder, tmp_path, ungated):
    recorder.statement(1, _LOCK_COMPANY, ("c1",), 0.0)
    recorder.statement(1, _OUTBOUND, ("c1", 7), 0.0)
    recorder.statement(1, _LEDGER, ("l1", "c1", "item"), 0.0)
    if ungated:
        recorder.statement(2, _LEDGER, ("l2", "c1", "other"), 0.0)
    (tmp_path / "lock-graph-gw0.json").write_text(json.dumps(recorder.dump()))
    listed = "row:outbound_queue (63) -> row:ledger (62)" in lock_rank.check(tmp_path)
    assert listed is ungated


def test_each_edge_keeps_every_site_pair_one_call_takes_it_by(recorder):
    recorder.acquire(1, _lock("row:projections", "update wait", key="a"), 0.0)
    recorder.acquire(1, _lock("row:companies", "update wait", key="a"), 0.0)
    recorder.acquire(2, Lock("row:projections", "share wait", "celerp/y.py:2 g", "t", "b"), 0.0)
    recorder.acquire(2, Lock("row:companies", "update wait", "celerp/y.py:3 h", "t", "b"), 0.0)
    recorder.acquire(3, Lock("row:projections", "update wait", "celerp/y.py:2 g", "t", "c"), 0.0)
    recorder.acquire(3, Lock("row:companies", "update wait", "celerp/y.py:3 h", "t", "c"), 0.0)
    assert recorder.edges[("row:projections", "row:companies")]["call_sites"] == {
        "celerp/x.py:1 f -> celerp/x.py:1 f": {"update>update"},
        "celerp/y.py:2 g -> celerp/y.py:3 h": {"share>update", "update>update"}}


def _projection(recorder: Recorder, conn: int, entity: str, company: str = "c1") -> None:
    recorder.statement(conn, _PROJECTION, (company, entity), 0.0)


@pytest.mark.parametrize("locked, violates", [("c1", False), ("c2", True), (None, True)])
def test_an_unsorted_take_under_its_companys_lock_records_no_violation(recorder, locked, violates):
    # Two takers holding one company's lock never interleave, whatever order they take its rows in.
    if locked:
        recorder.statement(1, _LOCK_COMPANY, (locked,), 0.0)
    _projection(recorder, 1, "item-b")
    _projection(recorder, 1, "item-a")
    assert (("unsorted", "row:projections") in _kinds(recorder)) is violates


@pytest.mark.parametrize("second, listed", [
    ([("item-a", None), ("item-b", None)], True),   # two rows without the company lock
    ([("item-a", "c1"), ("item-b", "c1")], False),  # two rows under it
    ([("item-a", None)], False),                    # one row: it waits on one thing only
])
def test_the_offline_check_fails_a_gated_unsorted_class_with_an_ungated_multi_row_taker(
        recorder, tmp_path, second, listed):
    recorder.statement(1, _LOCK_COMPANY, ("c1",), 0.0)
    _projection(recorder, 1, "item-b")
    _projection(recorder, 1, "item-a")
    for entity, locked in second:
        if locked:
            recorder.statement(2, _LOCK_COMPANY, (locked,), 0.0)
        _projection(recorder, 2, entity)
    (tmp_path / "lock-graph-gw0.json").write_text(json.dumps(recorder.dump()))
    assert ("- row:projections:" in lock_rank.check(tmp_path)) is listed


@pytest.mark.parametrize("strength, listed", [("FOR SHARE", False), ("FOR UPDATE", True)])
def test_the_offline_check_passes_an_ungated_multi_row_taker_that_conflicts_with_no_unsorted_take(
        recorder, tmp_path, strength, listed):
    shared = _PROJECTION.replace("FOR UPDATE", "FOR SHARE")
    recorder.statement(1, _LOCK_COMPANY, ("c1",), 0.0)
    recorder.statement(1, shared, ("c1", "item-b"), 0.0)
    recorder.statement(1, shared, ("c1", "item-a"), 0.0)
    for entity in ("item-a", "item-b"):
        recorder.statement(2, _PROJECTION.replace("FOR UPDATE", strength), ("c1", entity), 0.0)
    (tmp_path / "lock-graph-gw0.json").write_text(json.dumps(recorder.dump()))
    assert ("- row:projections:" in lock_rank.check(tmp_path)) is listed


_ACCOUNT_ROW = ("SELECT accounts.id FROM accounts WHERE accounts.company_id = $1::UUID "
                "AND accounts.code = $2 FOR UPDATE")


@pytest.mark.parametrize("locked, violates", [
    ("c1", False),  # the company's lock gates its account rows
    ("c2", True),   # another company's lock gates nothing here
    (None, True),
])
def test_unsorted_account_rows_under_the_companys_lock_record_no_violation(recorder, locked, violates):
    if locked:
        recorder.statement(1, _LOCK_COMPANY, (locked,), 0.0)
    recorder.statement(1, _ACCOUNT_ROW, ("c1", "1200"), 0.0)
    recorder.statement(1, _ACCOUNT_ROW, ("c1", "1100"), 0.0)
    assert (("unsorted", "row:accounts") in _kinds(recorder)) is violates


@pytest.mark.parametrize("locked, listed", [(True, False), (False, True)])
def test_the_offline_check_fails_unsorted_account_rows_with_a_multi_row_taker_outside_the_company_lock(
        recorder, tmp_path, locked, listed):
    recorder.statement(1, _LOCK_COMPANY, ("c1",), 0.0)
    recorder.statement(1, _ACCOUNT_ROW, ("c1", "1200"), 0.0)
    recorder.statement(1, _ACCOUNT_ROW, ("c1", "1100"), 0.0)
    if locked:
        recorder.statement(2, _LOCK_COMPANY, ("c1",), 0.0)
    for code in ("1100", "1200"):
        recorder.statement(2, _ACCOUNT_ROW, ("c1", code), 0.0)
    (tmp_path / "lock-graph-gw0.json").write_text(json.dumps(recorder.dump()))
    assert ("- row:accounts:" in lock_rank.check(tmp_path)) is listed


def test_a_statement_sent_through_a_conftest_wrapper_is_sited_at_its_caller():
    """The suite's shared session wraps every operation in a conftest.py; the site is the
    application frame that called it, or every lock would carry the wrapper's line."""
    def compiled(path: str, source: str, name: str):
        namespace = {"site": lock_rank._site}
        exec(compile(source, f"{lock_rank._ROOT}/{path}", "exec"), namespace)
        return namespace[name]
    probe = compiled(".venv/lib/driver.py", "def probe():\n    return site()\n", "probe")
    wrapper = compiled("conftest.py", "def call(f):\n    return f()\n", "call")
    caller = compiled("celerp/x.py", "def caller(call, probe):\n    return call(probe)\n", "caller")
    assert caller(wrapper, probe) == "celerp/x.py:2 caller"


def test_a_test_statement_sent_through_a_conftest_wrapper_is_sited_at_the_test():
    """With no application frame, the site is the test that sent it, not the wrapper."""
    def compiled(path: str, source: str, name: str):
        namespace = {"site": lock_rank._site}
        exec(compile(source, f"{lock_rank._ROOT}/{path}", "exec"), namespace)
        return namespace[name]
    probe = compiled(".venv/lib/driver.py", "def probe():\n    return site()\n", "probe")
    wrapper = compiled("conftest.py", "def call(f):\n    return f()\n", "call")
    caller = compiled("tests/test_x.py", "def test_y(call, probe):\n    return call(probe)\n", "test_y")
    assert caller(wrapper, probe) == "tests/test_x.py:2 test_y"


# ── R4: edges between rows of one company, under that company's exclusive lock ──

_DELETE_COMPANY = "SELECT companies.id FROM companies WHERE companies.id = $1::UUID FOR UPDATE"
_HOLD_COMPANY = "SELECT companies.id FROM companies WHERE companies.id = $1::UUID FOR KEY SHARE"
_TABLE = "LOCK TABLE projections IN SHARE ROW EXCLUSIVE MODE"


def _gated_inversion(recorder: Recorder, gate: str | None = "c1", held: str = "c1", taken: str = "c1",
                     gate_first: bool = True) -> None:
    """Outbound (63) then projections (lower), both of one company, under that company's lock."""
    if gate and gate_first:
        recorder.statement(1, _DELETE_COMPANY, (gate,), 0.0)
    recorder.statement(1, _OUTBOUND, (held, 7), 0.0)
    if gate and not gate_first:
        recorder.statement(1, _DELETE_COMPANY, (gate,), 0.0)
    _projection(recorder, 1, "item", company=taken)


@pytest.mark.parametrize("query", [_DELETE_COMPANY, _LOCK_COMPANY])
def test_an_inversion_between_rows_of_the_locked_company_records_no_violation(recorder, query):
    recorder.statement(1, query, ("c1",), 0.0)
    recorder.statement(1, _OUTBOUND, ("c1", 7), 0.0)
    _projection(recorder, 1, "item")
    assert not recorder.violations
    assert recorder.edges[("row:outbound_queue", "row:projections")]["company_gated"] == {"row:projections"}


@pytest.mark.parametrize("case", [
    {"gate": None},                    # no company lock
    {"gate": "c2"},                    # another company's lock
    {"taken": "", "gate": "c1"},       # the acquired row names no one company
])
def test_an_inversion_whose_acquired_row_is_not_under_its_companys_lock_is_an_order_violation(recorder, case):
    _gated_inversion(recorder, **case)
    assert ("order", "row:projections") in _kinds(recorder)


@pytest.mark.parametrize("case", [
    {"held": "c2", "gate": "c1"},      # the held row is another company's
    {"gate_first": False},             # the company lock came after the held row
])
def test_an_inversion_is_gated_by_the_acquired_rows_company_lock_alone(recorder, case):
    """A cycle through the edge needs another transaction holding the acquired row while it
    waits; every taker of that row holds its company's lock, which this transaction holds, so
    none can. What this transaction holds, and of which company, does not enter it."""
    _gated_inversion(recorder, **case)
    assert ("order", "row:projections") not in _kinds(recorder)
    assert recorder.edges[("row:outbound_queue", "row:projections")]["company_gated"] == {"row:projections"}


def test_a_table_lock_under_the_company_lock_still_orders(recorder):
    recorder.statement(1, _DELETE_COMPANY, ("c1",), 0.0)
    recorder.statement(1, _OUTBOUND, ("c1", 7), 0.0)
    recorder.statement(1, _TABLE, (), 0.0)
    assert ("order", "tbl:projections") in _kinds(recorder)


def test_a_session_advisory_lock_is_not_the_company_gate(recorder):
    recorder.acquire(1, Lock("adv:company", "session exclusive wait", "celerp/x.py:1 f", "t", "c1", company="c1"), 0.0)
    recorder.statement(1, _OUTBOUND, ("c1", 7), 0.0)
    _projection(recorder, 1, "item")
    assert ("order", "row:projections") in _kinds(recorder)


def test_every_company_keyed_row_taken_without_its_company_lock_is_counted(recorder):
    recorder.statement(1, _LOCK_COMPANY, ("c1",), 0.0)
    _projection(recorder, 1, "gated")
    _projection(recorder, 2, "ungated")
    recorder.statement(3, "SELECT projections.entity_id FROM projections WHERE projections.entity_id = $1 FOR SHARE",
                       ("unknown-company",), 0.0)
    recorder.statement(4, _HOLD_COMPANY, ("c1",), 0.0)
    _projection(recorder, 4, "held")
    info = recorder.dump()["classes"]["row:projections"]
    assert info["company_ungated"] == 3
    assert info["ungated_takes"] == ["share|unknown", "update|keyshare", "update|none"]


@pytest.mark.parametrize("other, listed", [
    (None, False),                                                          # every taker holds the company
    ((_PROJECTION, None), True),                                            # projections without it
    ((_OUTBOUND, None), False),                                             # the held class is not judged
    ((_PROJECTION.replace("FOR UPDATE", "FOR KEY SHARE"), None), True),     # key share conflicts with FOR UPDATE
    ((_PROJECTION, "c1"), False),                                           # under the company lock
])
def test_the_offline_check_fails_a_company_gated_inversion_whose_classes_have_an_ungated_taker(
        recorder, tmp_path, other, listed):
    _gated_inversion(recorder)
    if other:
        query, locked = other
        if locked:
            recorder.statement(2, _LOCK_COMPANY, (locked,), 0.0)
        recorder.statement(2, query, ("c1", 9), 0.0)
    assert not recorder.violations
    (tmp_path / "lock-graph-gw0.json").write_text(json.dumps(recorder.dump()))
    assert ("- gated row:outbound_queue -> row:projections" in lock_rank.check(tmp_path)) is listed


@pytest.mark.parametrize("gate, holder, listed", [
    (_DELETE_COMPANY, _HOLD_COMPANY, False),  # the deletion gate waits for every holder of the company
    (_LOCK_COMPANY, _HOLD_COMPANY, True),     # a company writer's lock does not wait for a key share
    (_DELETE_COMPANY, None, True),            # a take holding nothing of the company
])
def test_a_take_outside_the_gate_is_proven_only_when_it_holds_a_company_lock_the_gate_waits_for(
        recorder, tmp_path, gate, holder, listed):
    recorder.statement(1, gate, ("c1",), 0.0)
    recorder.statement(1, _OUTBOUND, ("c1", 7), 0.0)
    _projection(recorder, 1, "item")
    if holder:
        recorder.statement(2, holder, ("c1",), 0.0)
    _projection(recorder, 2, "other")
    (tmp_path / "lock-graph-gw0.json").write_text(json.dumps(recorder.dump()))
    assert ("- gated row:outbound_queue -> row:projections" in lock_rank.check(tmp_path)) is listed


def test_an_inserts_foreign_key_share_on_its_company_holds_the_company(recorder, tmp_path):
    recorder.statement(1, _DELETE_COMPANY, ("c1",), 0.0)
    recorder.statement(1, _OUTBOUND, ("c1", 7), 0.0)
    _projection(recorder, 1, "item")
    recorder.statement(2, _LEDGER, ("l1", "c1", "item"), 0.0)
    recorder.statement(2, _OUTBOUND, ("c1", 8), 0.0)
    assert recorder.dump()["classes"]["row:outbound_queue"]["ungated_takes"] == ["update|keyshare"]
    (tmp_path / "lock-graph-gw0.json").write_text(json.dumps(recorder.dump()))
    assert "- gated row:outbound_queue -> row:projections" not in lock_rank.check(tmp_path)


_NOWAIT_OUTBOUND = _OUTBOUND.replace("FOR UPDATE", "FOR UPDATE NOWAIT")
_WRITE_PROJECTION = "UPDATE projections SET state=$1 WHERE projections.company_id = $2::UUID AND projections.entity_id = $3"


def _leaf_listed(recorder: Recorder, tmp_path) -> bool:
    (tmp_path / "lock-graph-gw0.json").write_text(json.dumps(recorder.dump()))
    return "- gated row:outbound_queue -> row:projections" in lock_rank.check(tmp_path)


@pytest.mark.parametrize("after", [
    (),                                                   # takes the row, commits
    ((2, _WRITE_PROJECTION, ("s", "c1", "other")),),      # writes the row it holds: a retake
    ((2, _NOWAIT_OUTBOUND, ("c1", 8)),),                  # a NOWAIT take cannot wait
])
@pytest.mark.parametrize("before", [False, True])
def test_a_take_its_transaction_holds_while_waiting_on_nothing_is_proof_neutral(recorder, tmp_path, after,
                                                                                 before):
    """A cycle through a gated edge needs a transaction holding the taken row while it waits. One
    that waits on nothing from that take until it ends never does, whatever it held before."""
    _gated_inversion(recorder)
    if before:
        recorder.statement(2, _OUTBOUND, ("c1", 8), 0.0)
    _projection(recorder, 2, "other")
    for conn, query, params in after:
        recorder.statement(conn, query, params, 0.0)
    recorder.statement(2, "COMMIT", (), 0.0)
    assert not _leaf_listed(recorder, tmp_path)
    info = recorder.dump()["classes"]["row:projections"]
    assert set(info["leaf_sites"]) <= set(info["ungated_sites"]) and info["leaf_sites"]
    assert not info["ungated_takes"] and not info["counted_sites"]


@pytest.mark.parametrize("case", ["held weaker before", "waits after", "another connection waits", "still open"])
def test_a_take_that_can_hold_its_row_while_waiting_stays_unproven(recorder, tmp_path, case):
    _gated_inversion(recorder)
    if case == "held weaker before":  # the weaker hold is kept while the upgrade waits
        recorder.statement(2, _PROJECTION.replace("FOR UPDATE", "FOR KEY SHARE"), ("c1", "other"), 0.0)
    _projection(recorder, 2, "other")
    if case == "waits after":
        recorder.statement(2, _OUTBOUND, ("c1", 8), 0.0)
    if case == "another connection waits":
        recorder.statement(3, _OUTBOUND, ("c1", 8), 0.0)
    if case != "still open":
        recorder.statement(2, "COMMIT", (), 0.0)
    assert _leaf_listed(recorder, tmp_path)


@pytest.mark.parametrize("listed_in_debt", [False, True])
def test_the_lock_order_job_fails_on_an_unproven_gated_inversion_the_debt_file_lacks(
        recorder, tmp_path, monkeypatch, listed_in_debt):
    debt_file = tmp_path / "debt.txt"
    keys = ("gated key:companies -> row:projections", "gated row:outbound_queue -> row:projections")
    debt_file.write_text("".join(f"{k}  # protocol\n" for k in keys) if listed_in_debt else "")
    monkeypatch.setattr(lock_rank, "DEBT_FILE", debt_file)
    _gated_inversion(recorder)
    _projection(recorder, 2, "other")
    (tmp_path / "lock-graph-gw0.json").write_text(json.dumps(recorder.dump()))
    assert lock_rank.check_failed(lock_rank.check(tmp_path)) is not listed_in_debt


# ── The debt inventory: exact keys, printed for review, never written ──

def _debt_run(tmp_path, monkeypatch, entries: str):
    debt_file = tmp_path / "debt.txt"
    debt_file.write_text(entries)
    monkeypatch.setattr(lock_rank, "DEBT_FILE", debt_file)
    recorder = Recorder()
    recorder.acquire(1, _lock("row:projections", "update wait"), 0.0)
    recorder.acquire(1, Lock("row:companies", "update wait", "celerp/x.py:9 g", "t", "1"), 0.0)
    graphs = tmp_path / "graphs"
    graphs.mkdir()
    (graphs / "lock-graph-gw0.json").write_text(json.dumps(recorder.dump()))
    return debt_file, graphs, recorder


def test_the_debt_listing_prints_every_recorded_key_with_its_owner_and_writes_nothing(tmp_path, monkeypatch):
    entries = "order row:projections -> row:companies celerp/x.py g  # 402 F1\n"
    debt_file, graphs, _ = _debt_run(tmp_path, monkeypatch, entries)
    assert lock_rank.debt_listing(graphs) == "order row:projections -> row:companies celerp/x.py g  # 402 F1\n"
    assert debt_file.read_text() == entries


def test_the_debt_diff_lists_added_and_removed_keys(tmp_path, monkeypatch):
    _, graphs, _ = _debt_run(tmp_path, monkeypatch, "order row:ledger -> row:companies celerp/y.py h  # 402 G2\n")
    out = lock_rank.debt_diff(graphs)
    assert "Added: 1\n+ order row:projections -> row:companies celerp/x.py g\n" in out
    assert "Removed: 1\n- order row:ledger -> row:companies celerp/y.py h  # 402 G2\n" in out


def test_a_debt_entry_matches_only_its_exact_key(tmp_path, monkeypatch):
    # The same kind, classes and file in another function is a new violation.
    _, _, recorder = _debt_run(tmp_path, monkeypatch, "order row:projections -> row:companies celerp/x.py f  # 402 F1\n")
    monkeypatch.setattr(lock_rank, "RECORDER", recorder)
    assert [lock_rank.violation_key(v) for v in lock_rank.new_violations()] == [
        "order row:projections -> row:companies celerp/x.py g"]


# ── Census from code: a writer the run never exercised leaves its class unproven ──

def _site_writer(site: str) -> str:
    """A recorded site ("path:line function") as the census names a writer ("path function")."""
    where, function = site.split(" ", 1)
    return f"{where.rsplit(':', 1)[0]} {function}"


@pytest.mark.parametrize("exercised, listed", [(True, False), (False, True)])
def test_a_gated_inversion_is_unproven_while_a_writer_of_its_class_is_unexercised(
        recorder, tmp_path, exercised, listed):
    _gated_inversion(recorder)
    graph = recorder.dump()
    seen = _site_writer(graph["classes"]["row:projections"]["sites"][0])
    graph["writers"] = {"row:projections": [seen] if exercised else [seen, "celerp/x.py never_run"]}
    (tmp_path / "lock-graph-gw0.json").write_text(json.dumps(graph))
    out = lock_rank.check(tmp_path)
    assert ("- gated row:outbound_queue -> row:projections" in out) is listed
    assert ("- row:projections: celerp/x.py never_run" in out) is listed


def test_the_census_finds_every_statement_that_locks_rows(tmp_path):
    (tmp_path / "m.py").write_text('''
class Item(Base):
    __tablename__ = "items"
Tags = Table("tags", meta)

async def ins(s): await s.execute(insert(Item).values(a=1))
async def upd(s): await s.execute(sa.update(Item.__table__).where(Item.id == 1))
async def dele(s): await s.execute(delete(Tags))
async def lock(s): await s.execute(select(Item.id).where(Item.id == 1).with_for_update())
async def get(s): await s.get(Item, 1, with_for_update=True)
async def get_nokey(s): await s.get(Item, 1, with_for_update={"key_share": True})
async def get_plain(s): await s.get(Item, 1, with_for_update=False)
async def nokey(s): await s.execute(select(Item.id).where(Item.id == 1).with_for_update(key_share=True))
async def raw(s): await s.execute(text("UPDATE items SET a = 1"))
async def raw_lock(s, t): await s.execute(text(f"SELECT id FROM tags WHERE t = {t} FOR NO KEY UPDATE"))
async def raw_insert(s): await s.execute(text("INSERT INTO items (a) VALUES (1)"))
async def outer(s):
    async def inner(): await s.execute(delete(Item))
    await inner()
async def reads(s, d):
    await s.execute(select(Item)); await s.get(Item, 1); d.update(Item); await s.execute(text("SELECT 1"))
''')
    found = lock_rank._scan_writers([tmp_path / "m.py"])
    path = lock_rank.os.path.relpath(tmp_path / "m.py", lock_rank._ROOT)
    assert {t: {(f, k) for p, f, k in v if p == path} for t, v in found.items()} == {
        "items": {("ins", "insert"), ("upd", "write"), ("lock", "key lock"), ("get", "key lock"),
                  ("get_nokey", "lock"), ("nokey", "lock"),
                  ("raw", "write"), ("raw_insert", "insert"), ("inner", "delete")},
        "tags": {("dele", "delete"), ("raw_lock", "lock")},
    }


def test_the_census_names_the_production_writers_the_record_sites():
    writers = lock_rank.static_writers()
    assert "celerp/projections/engine.py _locked_projection" in writers["row:projections"]
    assert "celerp/services/company_lock.py lock_company" in writers["row:companies"]


def test_the_census_writers_are_production_files_whatever_a_test_patches():
    """The writers are computed once per process and dumped with every worker's record, so a
    census hook a test patches (as this file's fixture does) must not change which files they
    come from: a test file in them would read as a writer no test ran."""
    lock_rank.static_writers.cache_clear()
    try:
        tests = sorted(w for found in lock_rank.static_writers().values() for w in found
                       if "/tests/" in w or w.startswith("tests/"))
    finally:
        lock_rank.static_writers.cache_clear()
    assert tests == []


def test_the_census_reads_a_parent_rows_key_share_as_the_recorder_does(tmp_path):
    """A FOR KEY SHARE of a parent row is the foreign-key check's lock (key:<t>), so the census
    of row:<t> writers leaves it out, as the recorder never counts it there; a share of a
    table no foreign key names stays a row lock in both."""
    (tmp_path / "m.py").write_text('''
class Company(Base):
    __tablename__ = "companies"
class Tag(Base):
    __tablename__ = "tags"

async def orm(s): await s.execute(select(Company.id).with_for_update(read=True, key_share=True))
async def raw(s): await s.execute(text("SELECT id FROM companies WHERE id = 1 FOR KEY SHARE"))
async def share(s): await s.execute(select(Company.id).with_for_update(read=True))
async def other(s): await s.execute(select(Tag.id).with_for_update(read=True, key_share=True))
''')
    found = lock_rank._scan_writers([tmp_path / "m.py"])
    path = lock_rank.os.path.relpath(tmp_path / "m.py", lock_rank._ROOT)
    assert {t: {f for p, f, _ in v if p == path} for t, v in found.items()} == {
        "companies": {"share"}, "tags": {"other"}}
    assert "celerp/notifications/service.py _write_pending" not in lock_rank.static_writers()["row:companies"]


def test_every_worker_dump_carries_the_census(tmp_path):
    lock_rank.dump(tmp_path / "lock-graph-gw0.json")
    assert json.loads((tmp_path / "lock-graph-gw0.json").read_text())["writers"] == lock_rank.static_writers()
    assert lock_rank._merge(tmp_path)["writers"] == {k: set(v) for k, v in lock_rank.static_writers().items()}


# ── Serialized classes: an order no rank can give, safe only under one exclusive key ──

_TAKE_KEY = "SELECT pg_advisory_lock(4207320001)"
_DROP_KEY = "SELECT pg_advisory_unlock(4207320001)"


def _migration_run(recorder: Recorder, conn: int = 1) -> None:
    """One alembic run: a migration's table, then the stamp, in one transaction."""
    recorder.acquire(conn, _lock("row:company_peer_grants", "write"), 0.0)
    recorder.acquire(conn, _lock("row:alembic_version", "write"), 0.0)


def _serialized_listed(recorder: Recorder, tmp_path) -> bool:
    (tmp_path / "lock-graph-gw0.json").write_text(json.dumps(recorder.dump()))
    return "- serialized row:company_peer_grants -> row:alembic_version" in lock_rank.check(tmp_path)


def test_a_session_key_held_on_another_connection_of_the_flow_gates_its_takes(recorder, tmp_path):
    recorder.statement(9, _TAKE_KEY, (), 0.0)
    _migration_run(recorder)
    recorder.end(1)
    recorder.acquire(2, _lock("row:alembic_version", "write"), 0.0)
    recorder.statement(9, _DROP_KEY, (), 0.0)
    assert not recorder.violations
    (tmp_path / "lock-graph-gw0.json").write_text(json.dumps(recorder.dump()))
    report = lock_rank.check(tmp_path)
    assert "- serialized row:company_peer_grants -> row:alembic_version" not in report
    assert "- row:alembic_version by adv:migration" in report
    assert not lock_rank.check_failed(report)


def test_one_take_of_the_class_outside_the_key_fails_the_serialized_inversion(recorder, tmp_path):
    recorder.statement(9, _TAKE_KEY, (), 0.0)
    _migration_run(recorder)
    recorder.end(1)
    recorder.statement(9, _DROP_KEY, (), 0.0)
    recorder.acquire(2, _lock("row:alembic_version", "write"), 0.0)
    assert not recorder.violations
    assert _serialized_listed(recorder, tmp_path)
    assert lock_rank.check_failed(lock_rank.check(tmp_path))


def test_a_released_key_gates_nothing(recorder):
    recorder.statement(9, _TAKE_KEY, (), 0.0)
    recorder.statement(9, _DROP_KEY, (), 0.0)
    _migration_run(recorder)
    assert ("order", "row:alembic_version") in _kinds(recorder)


def test_a_key_held_by_another_thread_gates_nothing(recorder):
    taker = threading.Thread(target=recorder.statement, args=(9, _TAKE_KEY, (), 0.0))
    taker.start()
    taker.join()
    _migration_run(recorder)
    assert ("order", "row:alembic_version") in _kinds(recorder)


def test_a_key_of_many_values_is_no_serializing_gate(recorder):
    recorder.acquire(9, _lock("adv:external-product", "session exclusive wait", key="p"), 0.0)
    _migration_run(recorder)
    assert ("order", "row:alembic_version") in _kinds(recorder)


def _stamp_across_connections(recorder: Recorder, higher: bool = False) -> None:
    """A flow holds the migration key on one connection and stamps on another, as every
    alembic run does: its wait on the stamp crosses connections. *higher*: the flow also holds
    a class ranked above the stamp, so ranks do not prove the wait (R7)."""
    async def body():
        recorder.statement(9, _TAKE_KEY, (), 0.0)
        if higher:
            recorder.acquire(9, _lock("row:session_registry", "write"), 0.0)
        recorder.acquire(2, _lock("row:alembic_version", "write"), 0.0)
        recorder.end(2)
        recorder.statement(9, _DROP_KEY, (), 0.0)

    asyncio.run(body())


_CROSS_STAMP = "cross-connection adv:migration -> row:alembic_version celerp/x.py f"


def test_a_cross_connection_wait_on_a_class_the_flow_serializes_is_proven_offline(recorder, tmp_path):
    _stamp_across_connections(recorder)
    assert not recorder.violations
    (tmp_path / "lock-graph-gw0.json").write_text(json.dumps(recorder.dump()))
    assert _CROSS_STAMP not in lock_rank.debt_listing(tmp_path)
    assert not lock_rank.check_failed(lock_rank.check(tmp_path))


def test_one_take_of_the_class_outside_the_key_fails_the_cross_connection_wait(recorder, tmp_path):
    _stamp_across_connections(recorder, higher=True)
    recorder.acquire(3, _lock("row:alembic_version", "write"), 0.0)
    (tmp_path / "lock-graph-gw0.json").write_text(json.dumps(recorder.dump()))
    assert _CROSS_STAMP in lock_rank.debt_listing(tmp_path)
    report = lock_rank.check(tmp_path)
    assert f"- {_CROSS_STAMP}" in report and lock_rank.check_failed(report)


def _closure_across_connections(recorder: Recorder, holder_waits: bool) -> None:
    """A company reset holds the connector guard and its company's lock on one connection and
    writes a payment closure on its own connection; another transaction writes a closure too
    and, when *holder_waits*, then waits on a company row while it still holds the closure."""
    async def body():
        recorder.acquire(9, _lock("adv:connector-runtime", "xact exclusive wait"), 0.0)
        recorder.acquire(9, _lock("key:companies", "update wait"), 0.0)
        recorder.acquire(2, _lock("row:payment_closures", "write"), 0.0)
        recorder.end(2)
        recorder.end(9)

    asyncio.run(body())
    recorder.acquire(3, _lock("row:payment_closures", "write", key="2"), 0.0)
    if holder_waits:
        recorder.acquire(3, _lock("row:companies", "update wait"), 0.0)
    recorder.end(3)


_CROSS_CLOSURE = "cross-connection adv:connector-runtime -> row:payment_closures celerp/x.py f"


def test_a_cross_connection_wait_on_a_class_whose_holders_never_wait_is_proven_offline(recorder, tmp_path):
    """Whatever the waiting flow holds, a row no transaction ever waits while holding is
    released by its holder's commit: the wait always ends."""
    _closure_across_connections(recorder, holder_waits=False)
    assert not recorder.violations
    (tmp_path / "lock-graph-gw0.json").write_text(json.dumps(recorder.dump()))
    assert _CROSS_CLOSURE not in lock_rank.debt_listing(tmp_path)
    assert not lock_rank.check_failed(lock_rank.check(tmp_path))


def test_one_holder_of_the_class_that_waits_fails_the_cross_connection_wait(recorder, tmp_path):
    _closure_across_connections(recorder, holder_waits=True)
    (tmp_path / "lock-graph-gw0.json").write_text(json.dumps(recorder.dump()))
    assert _CROSS_CLOSURE in lock_rank.debt_listing(tmp_path)
    report = lock_rank.check(tmp_path)
    assert f"- {_CROSS_CLOSURE}" in report and lock_rank.check_failed(report)


@pytest.mark.parametrize("waiter, proven", [("write", True), ("insert", False)])
def test_a_holder_that_waits_holding_only_rows_it_inserted_blocks_only_an_inserting_wait(
        recorder, tmp_path, waiter, proven):
    """A row another transaction inserted is invisible until it commits: an update or a locking
    read never waits on it, but an insert of the same key does."""
    recorder.acquire(3, _lock("row:payment_recoveries", "insert", key="2"), 0.0)
    recorder.acquire(3, _lock("row:companies", "update wait"), 0.0)
    recorder.end(3)

    async def body():
        recorder.acquire(9, _lock("adv:connector-runtime", "xact exclusive wait"), 0.0)
        recorder.acquire(9, _lock("key:companies", "update wait"), 0.0)
        recorder.acquire(2, _lock("row:payment_recoveries", waiter), 0.0)
        recorder.end(2)
        recorder.end(9)

    asyncio.run(body())
    listed = ("cross-connection", "row:payment_recoveries") in _offline_kinds(recorder, tmp_path)
    assert listed is not proven


def test_a_cross_connection_wait_whose_holder_is_still_open_fails_offline(recorder, tmp_path):
    """A transaction still open when the graph is read may yet wait holding the row."""
    async def body():
        recorder.acquire(9, _lock("adv:external-product", "session exclusive wait", key="p"), 0.0)
        recorder.acquire(2, _lock("row:alembic_version", "write"), 0.0)

    asyncio.run(body())
    assert ("cross-connection", "row:alembic_version") in _offline_kinds(recorder, tmp_path)


_SHARE_COMPANIES = ("SELECT companies.id FROM companies WHERE companies.id IN ($1::UUID, $2::UUID) "
                    "ORDER BY companies.id FOR KEY SHARE")
_SHARE_TWO_COMPANIES = _SHARE_COMPANIES.replace("FOR KEY SHARE", "FOR SHARE")


@pytest.mark.parametrize("held, violates", [
    (("c1", "c2"), False),  # both rows already held more strongly: granted at once
    (("c1",), True),        # c2 is not held, so the statement can wait on it
])
def test_one_statement_over_rows_each_already_held_is_a_retake(recorder, held, violates):
    for company in held:
        recorder.statement(1, _LOCK_COMPANY, (company,), 0.0)
    _chart(recorder)
    recorder.statement(1, _SHARE_TWO_COMPANIES, ("c1", "c2"), 0.0)
    assert (("order", "row:companies") in _kinds(recorder)) is violates


@pytest.mark.parametrize("named, violates", [
    (("c1", "c2"), False),  # the statement named c1 among its keys: c1 is held
    (("c2", "c3"), True),   # c1 was never named, so taking it can wait
])
def test_a_row_one_statement_locked_among_several_keys_is_held_by_its_own_key(recorder, named, violates):
    recorder.statement(1, _SHARE_COMPANIES.replace("FOR KEY SHARE", "FOR UPDATE"), named, 0.0)
    _chart(recorder)
    recorder.statement(1, _LOCK_COMPANY, ("c1",), 0.0)
    assert (("order", "row:companies") in _kinds(recorder)) is violates


async def test_every_in_process_client_of_the_app_gets_a_transaction_per_request(client, auth):
    """Production closes each request's session when the request ends; the suite's requests
    share the test's session, so the `client` fixture ends a transaction per request for any
    in-process client of the app (the UI's bridge, a test's own), and a lock one request
    took is not read as held by the next."""
    from fastapi import Depends
    from httpx import ASGITransport, AsyncClient

    from celerp.db import get_session
    from celerp.main import app
    from celerp.services.company_lock import lock_company

    def companies_held() -> int:
        return sum(lock.cls == "row:companies" for held in lock_rank.RECORDER.held.values() for lock in held)

    async def reads_under_the_lock(session=Depends(get_session)) -> dict:
        await lock_company(session, auth["company_id"])
        return {"held": companies_held()}

    app.add_api_route("/lock-rank-probe", reads_under_the_lock, methods=["GET"])
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as bridge:
            response = await bridge.get("/lock-rank-probe")
    finally:
        app.router.routes.pop()
    assert response.json() == {"held": 1}
    assert companies_held() == 0


# ── A write by primary key is named by the row's company ──

_FLUSH_UPDATE = "UPDATE outbound_queue SET status=$1 WHERE outbound_queue.id = $2::UUID"
_FLUSH_DELETE = "DELETE FROM outbound_queue WHERE outbound_queue.id = $1::UUID"


@pytest.mark.parametrize("statement, params, strength", [
    (_FLUSH_UPDATE, ("done", "r1"), "nokey"),
    (_FLUSH_DELETE, ("r1",), "update"),
])
@pytest.mark.parametrize("locked, named, counted", [
    ("c1", "c1", None),              # the row's company is locked: gated
    ("c1", None, "unknown"),         # nothing names the row: still unknown
    ("c2", "c1", "none"),            # another company's lock: ungated, holding nothing of c1
    (None, "c1", "none"),            # no lock
])
def test_a_write_by_primary_key_is_judged_under_the_rows_company(recorder, statement, params, strength,
                                                                  locked, named, counted):
    """An UPDATE or DELETE the ORM flushes by primary key names no company in its WHERE; the
    row's company is the one the ORM's object carries, so the census judges it under that
    company's lock rather than as a take of an unknown company."""
    if locked:
        recorder.statement(1, _LOCK_COMPANY, (locked,), 0.0)
    if named:
        recorder.name_row(1, "outbound_queue", "r1", named)
    recorder.statement(1, statement, params, 0.0)
    info = recorder.dump()["classes"]["row:outbound_queue"]
    assert info.get("ungated_takes", []) == ([f"{strength}|{counted}"] if counted else [])


def test_a_row_named_in_an_ended_transaction_names_nothing_in_the_next(recorder):
    recorder.statement(1, _LOCK_COMPANY, ("c1",), 0.0)
    recorder.name_row(1, "outbound_queue", "r1", "c1")
    recorder.statement(1, "COMMIT", (), 0.0)
    recorder.statement(1, _LOCK_COMPANY, ("c1",), 0.0)
    recorder.statement(1, _FLUSH_UPDATE, ("done", "r1"), 0.0)
    assert recorder.dump()["classes"]["row:outbound_queue"]["ungated_takes"] == ["nokey|unknown"]


def test_a_test_boundary_leaves_the_transactions_of_other_threads_open(recorder):
    """A test starts holding nothing, but an in-process server's thread may be inside a
    transaction across that boundary: it still holds its company lock, and is not retaken
    (lock_company skips a lock its transaction holds), so forgetting it would read every
    later take of that transaction as outside the lock."""
    def server():
        recorder.statement(2, _LOCK_COMPANY, ("c1",), 0.0)

    thread = threading.Thread(target=server)
    thread.start()
    thread.join()
    recorder.statement(1, _LOCK_COMPANY, ("c2",), 0.0)
    recorder.clear_transactions()
    assert dict(recorder.company_rows) == {2: {"c1": {"nokey"}}}


async def test_the_orm_names_the_company_of_every_row_it_flushes(session, auth):
    """End to end: an account changed under its company's lock and flushed by the ORM is
    counted as gated, not as a take of an unknown company."""
    from sqlalchemy import select

    from celerp_accounting.models import Account
    from celerp.services.company_lock import lock_company

    lock_rank.RECORDER.clear_transactions()
    before = dict(lock_rank.RECORDER.classes.get("row:accounts", {}))
    await lock_company(session, auth["company_id"])
    acc = (await session.execute(select(Account).where(Account.company_id == auth["company_id"])
                                 .limit(1).with_for_update())).scalar_one()
    acc.name = acc.name + " (renamed)"
    await session.flush()
    after = lock_rank.RECORDER.classes["row:accounts"]
    assert after.get("company_ungated", 0) == before.get("company_ungated", 0)


# ── A table without a company column whose rows belong to its parent's company ──

_RESET_STEPS = ("DELETE FROM ai_job_steps WHERE (job_id) IN (SELECT id FROM ai_batch_jobs "
                "WHERE company_id = CAST(CAST($1 AS text) AS uuid))")
_STEP_BY_JOB = "UPDATE ai_job_steps SET status=$1 WHERE ai_job_steps.job_id = $2::UUID"


@pytest.mark.parametrize("locked, counted", [
    ("c1", None),          # the parent's company is locked: gated
    ("c2", "none"),        # another company's lock
    (None, "none"),        # no lock
])
def test_a_child_row_is_judged_under_its_parents_company(recorder, locked, counted):
    """ai_job_steps has no company column: a step is its job's. A statement that picks the
    steps through the job's company names that company, so the census judges the take, and
    a take without that company's lock is counted rather than left unmeasured."""
    if locked:
        recorder.statement(1, _LOCK_COMPANY, (locked,), 0.0)
    recorder.statement(1, _RESET_STEPS, ("c1",), 0.0)
    info = recorder.dump()["classes"]["row:ai_job_steps"]
    assert info.get("ungated_takes", []) == ([f"update|{counted}"] if counted else [])


def test_a_child_row_under_its_companys_deletion_lock_is_company_gated(recorder):
    recorder.statement(1, _DELETE_COMPANY, ("c1",), 0.0)
    recorder.statement(1, _RESET_STEPS, ("c1",), 0.0)
    assert ("order", "row:ai_job_steps") not in _kinds(recorder)
    assert recorder.edges[("key:companies", "row:ai_job_steps")]["company_gated"] == {"row:ai_job_steps"}


@pytest.mark.parametrize("named, counted", [
    ("c1", None),          # the job was named in this transaction as c1's: gated
    ("c2", "none"),        # the job is another company's
    (None, "unknown"),     # nothing names the job: unknown, never assumed gated
])
def test_a_child_row_keyed_by_its_parent_is_judged_under_the_parents_company(recorder, named, counted):
    recorder.statement(1, _LOCK_COMPANY, ("c1",), 0.0)
    if named:
        recorder.name_row(1, "ai_batch_jobs", "j1", named)
    recorder.statement(1, _STEP_BY_JOB, ("done", "j1"), 0.0)
    info = recorder.dump()["classes"]["row:ai_job_steps"]
    assert info.get("ungated_takes", []) == ([f"nokey|{counted}"] if counted else [])


# ── A write joined to other tables names its company only through its own table ──

@pytest.mark.parametrize("statement, strength, counted", [
    # The company term qualified by the written table's alias or name: c1's rows, gated.
    ("UPDATE ledger l SET actor_id = u.id FROM users u WHERE l.company_id = $1 AND l.actor_id IS NULL",
     "nokey", None),
    ("UPDATE ledger SET actor_id = u.id FROM users u WHERE ledger.company_id = $1", "nokey", None),
    ("DELETE FROM ledger l USING users u WHERE l.company_id = $1 AND l.actor_id = u.id", "update", None),
    # A joined table's company term, or one that names no table: no company, never assumed gated.
    ("UPDATE ledger l SET actor_id = p.id FROM projections p WHERE p.company_id = $1", "nokey", "unknown"),
    ("UPDATE ledger l SET actor_id = u.id FROM users u WHERE company_id = $1", "nokey", "unknown"),
    ("DELETE FROM ledger l USING projections p WHERE p.company_id = $1", "update", "unknown"),
])
def test_a_joined_write_names_the_company_its_own_table_is_bound_to(recorder, statement, strength, counted):
    """An UPDATE ... FROM or DELETE ... USING reads other tables, so a bare company column may
    be theirs: only a term qualified by the written table's alias (or name) binds its rows'
    company, and the take is judged under that company's lock."""
    recorder.statement(1, _LOCK_COMPANY, ("c1",), 0.0)
    recorder.statement(1, statement, ("c1",), 0.0)
    info = recorder.dump()["classes"]["row:ledger"]
    assert info.get("ungated_takes", []) == ([f"{strength}|{counted}"] if counted else [])


_CONVERSATION = ("SELECT ai_conversations.id FROM ai_conversations WHERE ai_conversations.id = $1::UUID "
                 "AND ai_conversations.company_id = $2::UUID FOR KEY SHARE")
_CREDITS = ("UPDATE ai_messages SET credits_used=$1 WHERE ai_messages.id = $2::UUID "
            "AND ai_messages.conversation_id = $3::UUID")
_MESSAGE = "INSERT INTO ai_messages (id, conversation_id, role, content) VALUES ($1::UUID, $2::UUID, $3, $4)"
_JOB = "SELECT ai_batch_jobs.id FROM ai_batch_jobs WHERE ai_batch_jobs.company_id = $1::UUID FOR NO KEY UPDATE"


@pytest.mark.parametrize("named, counted", [
    ("c1", None),          # the conversation was locked naming c1: its messages are c1's, gated
    ("c2", "none"),        # the lock named another company
    (None, "unknown"),     # nothing names the conversation: unknown, never assumed gated
])
def test_a_parent_locked_by_its_key_and_company_names_its_childrens_company(recorder, named, counted):
    """A lock statement that binds both a row's key and its company names that row's company,
    as an ORM write does (name_row): a child row keyed by it is judged under that company."""
    recorder.statement(1, _LOCK_COMPANY, ("c1",), 0.0)
    if named:
        recorder.statement(1, _CONVERSATION, ("v1", named), 0.0)
    recorder.statement(1, _CREDITS, (5, "m1", "v1"), 0.0)
    info = recorder.dump()["classes"]["row:ai_messages"]
    assert info.get("ungated_takes", []) == ([f"nokey|{counted}"] if counted else [])


@pytest.mark.parametrize("named, gated", [("c1", True), ("c2", False), (None, False)])
def test_an_insert_of_a_child_row_is_judged_under_its_parents_company(recorder, named, gated):
    """An insert into a table whose rows are their parent's company names the parent by its
    link column; under that company's lock it is gated (R2) like an insert naming the company."""
    recorder.statement(1, _LOCK_COMPANY, ("c1",), 0.0)
    if named:
        recorder.statement(1, _CONVERSATION, ("v1", named), 0.0)
    recorder.statement(1, _JOB, ("c1",), 0.0)
    recorder.statement(1, _MESSAGE, ("m1", "v1", "user", "q"), 0.0)
    assert (("order", "row:ai_messages") not in _kinds(recorder)) is gated


@pytest.mark.parametrize("named, counted", [
    ("c1", None),          # the conversation was locked naming c1: the share is of c1's row, gated
    ("c2", "none"),        # the lock named another company
    (None, "unknown"),     # nothing names the conversation: unknown, never assumed gated
])
def test_a_foreign_key_share_of_one_parent_is_judged_under_the_parents_company(recorder, named, counted):
    """The key share an insert takes on the one parent row it references is a lock of that
    row's company: once the transaction named the row's company, the share is judged under it."""
    recorder.statement(1, _LOCK_COMPANY, ("c1",), 0.0)
    if named:
        recorder.statement(1, _CONVERSATION, ("v1", named), 0.0)
    recorder.statement(1, _MESSAGE, ("m1", "v1", "user", "q"), 0.0)
    recorder.statement(1, _JOB, ("c1",), 0.0)  # a lock that can wait follows: no leaf
    info = recorder.dump()["classes"]["key:ai_conversations"]
    assert info.get("ungated_takes", []) == ([f"keyshare|{counted}"] if counted else [])


_CLAIM = ("SELECT ai_messages.id, ai_messages.tools_called FROM ai_messages JOIN ai_conversations "
          "ON ai_messages.conversation_id = ai_conversations.id WHERE ai_messages.id = $1::UUID "
          "AND ai_messages.conversation_id = $2::UUID AND ai_conversations.company_id = $3::UUID "
          "FOR UPDATE OF ai_messages")
_CLAIMED = "UPDATE ai_messages SET tools_called=$1::JSONB WHERE ai_messages.id = $2::UUID"


@pytest.mark.parametrize("named, counted", [
    ("c1", None),          # the conversation was locked naming c1: the message is c1's, gated
    (None, "unknown"),     # nothing names the conversation: unknown, never assumed gated
])
def test_a_child_row_locked_through_a_join_and_written_by_its_key_is_judged_under_its_parents_company(
        recorder, named, counted):
    """A confirmation locks the message FOR UPDATE OF its own table in a join with its
    conversation, then writes it by its key alone: the lock names the row and its parent by
    columns qualified by the locked table, and the write is judged under the company the lock
    found, as the lock itself is."""
    recorder.statement(1, _LOCK_COMPANY, ("c1",), 0.0)
    if named:
        recorder.statement(1, _CONVERSATION, ("v1", named), 0.0)
    recorder.statement(1, _CLAIM, ("m1", "v1", "c1"), 0.0)
    recorder.statement(1, _CLAIMED, ("[]", "m1"), 0.0)
    recorder.statement(1, _JOB, ("c1",), 0.0)  # a lock that can wait follows: no leaf
    sites = recorder.classes["row:ai_messages"].get("ungated_sites", set())
    assert sorted(site.rsplit(" [", 1)[1] for site in sites) == (
        [f"nokey|{counted}]", f"update|{counted}]"] if counted else [])


def test_every_table_belongs_to_a_company_or_is_declared_an_installation_table():
    """The census measures a table only when it can name the company of its rows. A table
    with neither a company column nor one foreign key to a company's table would be left out
    silently (zero ungated takes meaning unmeasured), so each such table is declared."""
    tables = [t for t, m in lock_rank._app_tables().items() if m is not None]
    unowned = {t for t in tables if not lock_rank._company_owned(t)}
    assert unowned == lock_rank.INSTALLATION_TABLES
    assert lock_rank._company_link("ai_job_steps") == ("job_id", "ai_batch_jobs")
    assert lock_rank._company_link("notification_reads") == ("notification_id", "notifications")
    assert "row:ai_job_steps" in lock_rank.static_writers()


def test_a_closed_sync_connection_leaves_no_session_lock_behind():
    # Postgres drops a closed connection's session locks; a later connection given the same
    # object id (the version fence takes its lock again on a new one) must not inherit them.
    import os

    import sqlalchemy as sa

    from celerp.db_url import sync_url
    from celerp.migrations.compatibility import _FENCE_NAMESPACE

    assert lock_rank._INSTALLED
    engine = sa.create_engine(sync_url(os.environ["DATABASE_URL"]), poolclass=sa.pool.NullPool)
    with engine.connect() as conn:
        key = id(conn.connection.dbapi_connection)
        conn.execute(sa.text(f"SELECT pg_advisory_lock_shared({_FENCE_NAMESPACE}, 1)"))
        conn.commit()
        assert [h.cls for h in lock_rank.RECORDER.session_held.get(key, ())] == ["adv:version-fence"]
    engine.dispose()
    assert not lock_rank.RECORDER.session_held.get(key)


_LOCK_RUN = ("SELECT migration_runs.id FROM migration_runs WHERE migration_runs.id = $1::UUID "
             "AND migration_runs.company_id = $2::UUID FOR UPDATE")
_MAP = "INSERT INTO migration_entity_maps (id, migration_run_id, source_type) VALUES ($1::UUID, $2::UUID, $3)"
_MAPS = _MAP + ", ($4::UUID, $5::UUID, $6)"


@pytest.mark.parametrize("new_company", [False, True])  # the company locked, or inserted by this transaction
@pytest.mark.parametrize("lock_run", [_LOCK_RUN, _LOCK_RUN.replace("FOR UPDATE", "FOR NO KEY UPDATE"),
                                      _LOCK_RUN.replace("FOR UPDATE", "FOR KEY SHARE")])
@pytest.mark.parametrize("insert, runs, retake", [
    (_MAP, ("r1",), True), (_MAP, ("r2",), False),
    (_MAPS, ("r1", "r1"), True), (_MAPS, ("r1", "r2"), False),  # one statement inserting two rows
])
def test_a_foreign_key_share_of_a_row_its_transaction_locked_by_key_is_a_retake(recorder, new_company, lock_run,
                                                                                insert, runs, retake):
    """A child insert shares its parent's key. When the transaction already locked that parent
    row by its primary key (with its company named), the share is granted at once: it waits on
    nothing, so it is no take outside the company lock."""
    if new_company:
        recorder.statement(1, _COMPANY, ("c1", "n", "s"), 0.0)
        recorder.statement(1, _CLAIM, ("r1", "c1", "x"), 0.0)
    else:
        recorder.statement(1, _LOCK_COMPANY, ("c1",), 0.0)
    recorder.statement(1, lock_run, ("r1", "c1"), 0.0)
    recorder.statement(1, insert, tuple(v for i, run in enumerate(runs) for v in (f"m{i}", run, "x")), 0.0)
    recorder.statement(1, _LOCK_COMPANY, ("c2",), 0.0)  # a later take that can wait
    recorder.statement(1, "COMMIT", (), 0.0)
    takes = recorder.dump()["classes"]["key:migration_runs"].get("ungated_takes", [])
    assert takes == ([] if retake else ["keyshare|unknown"])


def _guard_then_ranked_wait(recorder: Recorder, more: str | None = None) -> None:
    """A recovery holds the connector guard on a connection of its own for the whole run and
    ends every session on another (*more*: a class it also holds there); a sign-in holds one
    login's state and then waits on its sessions, a higher rank."""
    recorder.acquire(3, _lock("row:user_auth_state", "update wait", key="2"), 0.0)
    recorder.acquire(3, _lock("row:session_registry", "update wait", key="2"), 0.0)
    recorder.end(3)

    async def body():
        recorder.acquire(9, _lock("adv:connector-runtime", "session exclusive wait"), 0.0)
        if more:
            recorder.acquire(9, _lock(more, "update wait"), 0.0)
        recorder.acquire(2, _lock("row:user_auth_state", "update wait"), 0.0)
        recorder.end(2)
        recorder.end(9)

    asyncio.run(body())


def test_a_cross_connection_wait_ranked_above_all_its_task_holds_is_proven_offline(recorder, tmp_path):
    """R7: a holder of the class waits only on higher ranks, so no chain of waits from it
    reaches what the task holds: the wait ends although the holders do wait."""
    _guard_then_ranked_wait(recorder)
    assert not recorder.violations
    assert ("cross-connection", "row:user_auth_state") not in _offline_kinds(recorder, tmp_path)
    assert not lock_rank.check_failed(lock_rank.check(tmp_path))


def test_a_cross_connection_wait_ranked_below_a_class_its_task_holds_stays_unproven(recorder, tmp_path):
    _guard_then_ranked_wait(recorder, more="key:companies")
    assert ("cross-connection", "row:user_auth_state") in _offline_kinds(recorder, tmp_path)


def test_a_debt_edge_down_to_what_the_task_holds_leaves_the_ranked_wait_unproven(
        recorder, tmp_path, monkeypatch):
    """A debt edge is a decreasing wait Postgres breaks only when the whole cycle is its own;
    one through a task's two connections would hang, so ranks prove nothing below it."""
    debt = tmp_path / "debt.txt"
    debt.write_text("order row:session_registry -> adv:connector-runtime celerp/x.py f  # maintenance\n")
    monkeypatch.setattr(lock_rank, "DEBT_FILE", debt)
    _guard_then_ranked_wait(recorder)
    assert ("cross-connection", "row:user_auth_state") in _offline_kinds(recorder, tmp_path)


_HOLD_LOGIN = "SELECT users.id FROM users WHERE users.id = $1::UUID FOR NO KEY UPDATE"
_DELETE_LOGINS = "DELETE FROM users AS u WHERE u.id = ANY(:members)"


@pytest.mark.parametrize("held, violates", [
    ("u1", False),  # its row is held FOR NO KEY UPDATE: the delete waits only on key sharers
    ("u2", True),   # another login is held: the deleted row can wait on any row lock
    (None, True),
])
def test_deleting_a_login_this_transaction_holds_waits_only_on_its_key(recorder, held, violates):
    """The rows a DELETE ... = ANY(:array) names are the array's members, each as a lock
    naming it alone. A row held FOR NO KEY UPDATE leaves other transactions only its key
    share, so deleting it waits only as its key does, which the delete takes as well."""
    if held:
        recorder.statement(1, _HOLD_LOGIN, (held,), 0.0)
    recorder.statement(1, _LOCK_COMPANY, ("c1",), 0.0)
    recorder.statement(1, _DELETE_LOGINS, {"members": ["u1"]}, 0.0)
    assert (("order", "row:users") in _kinds(recorder)) is violates
    assert ("order", "key:users") not in _kinds(recorder)


@pytest.mark.parametrize("notice_last, violates", [(False, False), (True, True)])
def test_a_login_is_deleted_after_its_company_notices_and_nothing_is_written_after(recorder, notice_last,
                                                                                   violates):
    """A login's key is the last lock of the one transaction that deletes it (a bootstrap
    discard, after its company's notices): a notice written after it inverts the order. The
    notices are another company's, so no company lock gates the edge."""
    recorder.statement(1, _HOLD_LOGIN, ("u1",), 0.0)
    recorder.statement(1, _LOCK_COMPANY, ("c1",), 0.0)
    if not notice_last:
        recorder.statement(1, "DELETE FROM notifications WHERE company_id = :c", {"c": "c2"}, 0.0)
    recorder.statement(1, _DELETE_LOGINS, {"members": ["u1"]}, 0.0)
    if notice_last:
        recorder.statement(1, "DELETE FROM notifications WHERE company_id = :c", {"c": "c2"}, 0.0)
    assert bool({k for k in _kinds(recorder) if k[0] == "order"}) is violates


@pytest.mark.parametrize("site, production", [
    ("celerp/services/permissions.py:263 read_authority", True),
    ("default_modules/celerp-accounting/celerp_accounting/routes.py:3207 reopen_reconciliation", True),
    ("tests/test_firm_view.py:131 test_firm_view", False),
    ("tests/company_backup_support.py:81 member", False),
    ("default_modules/celerp-ai/tests/test_batch.py:107 user", False),
    ("?", False),
])
def test_a_site_is_production_only_in_the_files_the_census_scans(site, production):
    assert _PRODUCTION_SITE(site) is production


def test_a_take_a_test_sends_outside_the_company_lock_proves_nothing_against_production(
        recorder, monkeypatch):
    """R4 asks whether a production transaction can hold a company's row without that
    company's lock while it waits. A seed or edit the test itself sends is no such
    transaction, so it is not counted; the same take sent from production code is."""
    monkeypatch.setattr(lock_rank, "_production_site", _PRODUCTION_SITE)
    for site, counted in (("tests/test_seed.py:5 seed", []), ("celerp/x.py:1 f", ["nokey|unknown"])):
        monkeypatch.setattr(lock_rank, "_site", lambda site=site: site)
        rec = Recorder()
        rec.statement(1, _FLUSH_UPDATE, ("done", "r1"), 0.0)
        rec.statement(1, _LOCK_COMPANY, ("c2",), 0.0)
        assert rec.dump()["classes"]["row:outbound_queue"].get("ungated_takes", []) == counted


_LEGACY_ROW = "SELECT connector_configs.id FROM connector_configs WHERE connector_configs.company_id = $1::VARCHAR AND connector_configs.connector = $2::VARCHAR FOR UPDATE"


@pytest.mark.parametrize("company, counted", [
    ("legacy-iid", None),       # the install's own id: no company's row, nothing to gate
    ("c1", "none"),             # any company's id is still counted
])
def test_a_take_naming_the_instance_id_alone_is_no_companys_take(recorder, monkeypatch, company, counted):
    """A legacy connection row names the install's instance id, which no companies row ever
    holds: a take naming that id alone meets no take of a company's row, so the census does not
    count it, while a take naming any other company stays counted."""
    monkeypatch.setattr("celerp.connectors.ownership.ensure_instance_id", lambda: "legacy-iid")
    recorder.statement(1, _LEGACY_ROW, (company, "woocommerce"), 0.0)
    info = recorder.dump()["classes"].get("row:connector_configs", {})
    assert info.get("ungated_takes", []) == ([f"update|{counted}"] if counted else [])
    assert bool(info.get("counted_sites")) is bool(counted)


_UPDATE_ACCOUNT = "UPDATE accounts SET cash_flow_category = $1 WHERE accounts.code = $2"
_UPDATE_LOCATION = "UPDATE locations SET name = $1 WHERE locations.name = $2"


@pytest.mark.parametrize("mode, counted", [
    ("EXCLUSIVE", []),                              # no other transaction can hold a row of it
    ("ACCESS EXCLUSIVE", []),
    ("SHARE ROW EXCLUSIVE", ["nokey|unknown"]),     # FOR UPDATE still gets past it
])
def test_a_take_under_its_tables_exclusive_lock_is_no_companys_take(monkeypatch, mode, counted):
    """A migration's data step locks the tables it writes, EXCLUSIVE, before its first row: while
    the lock is held no other transaction holds or takes a row of the table, so a row the step
    then takes never waits on a row and is not counted, however many follow it. A row of a table
    it did not lock stays counted."""
    monkeypatch.setattr(lock_rank, "_production_site", _PRODUCTION_SITE)
    monkeypatch.setattr(lock_rank, "_site", lambda: "celerp/migrations/versions/x.py:1 upgrade")
    rec = Recorder()
    rec.statement(1, f"LOCK TABLE accounts IN {mode} MODE", (), 0.0)
    rec.statement(1, _UPDATE_ACCOUNT, ("investing", "1200"), 0.0)
    rec.statement(1, _UPDATE_ACCOUNT, ("financing", "2200"), 0.0)
    rec.statement(1, _UPDATE_LOCATION, ("Main", "Default"), 0.0)
    rec.statement(1, _UPDATE_LOCATION, ("Store", "Shop"), 0.0)
    classes = rec.dump()["classes"]
    assert classes["row:accounts"].get("ungated_takes", []) == counted
    assert classes["row:locations"].get("ungated_takes", []) == ["nokey|unknown"]


_LOCK_GENERATION = "SELECT user_auth_state.user_id FROM user_auth_state WHERE user_auth_state.user_id = $1::UUID FOR UPDATE"
_INSERT_SESSION = ("INSERT INTO session_registry (jti, user_id, company_id, expiry) "
                   "VALUES ($1::VARCHAR, $2::UUID, $3::UUID, $4::TIMESTAMP WITH TIME ZONE)")
_DELETE_SESSIONS = "DELETE FROM session_registry WHERE session_registry.user_id = $1::UUID"


@pytest.mark.parametrize("generation, counted", [
    (True, []),                                     # every writer of a login's sessions waits here first
    (False, ["insert|none", "update|unknown"]),
])
def test_a_session_row_taken_under_its_logins_generation_is_no_companys_take(monkeypatch, generation, counted):
    """A login's sessions are gated by its session generation, not by a company: issuance,
    sign-out, the company reset and the global sign-outs each take the login's generation FOR
    UPDATE before a session row, so two takers of one login's sessions meet there first. A
    session row taken under a generation is not counted; one taken without it is."""
    monkeypatch.setattr(lock_rank, "_production_site", _PRODUCTION_SITE)
    monkeypatch.setattr(lock_rank, "_site", lambda: "celerp/services/session_tracker.py:1 register_token")
    rec = Recorder()
    if generation:
        rec.statement(1, _LOCK_GENERATION, ("00000000-0000-0000-0000-000000000001",), 0.0)
    rec.statement(1, _INSERT_SESSION, ("jti", "00000000-0000-0000-0000-000000000001",
                                       "00000000-0000-0000-0000-000000000002", "2026-01-01"), 0.0)
    rec.statement(1, _DELETE_SESSIONS, ("00000000-0000-0000-0000-000000000001",), 0.0)
    rec.statement(1, _UPDATE_LOCATION, ("Main", "Default"), 0.0)
    classes = rec.dump()["classes"]
    assert sorted(classes["row:session_registry"].get("ungated_takes", [])) == counted



def _recorder_calls(takes: int) -> int:
    """Function calls the recorder makes for ``takes`` row locks of alternating classes, each in
    a savepoint released after it (as an import's sinks take one per record), in one transaction."""
    import sys

    recorder = Recorder()
    calls = 0

    def count(frame, event, arg):
        nonlocal calls
        calls += event in ("call", "c_call")

    sys.setprofile(count)
    try:
        for i in range(takes):
            recorder.statement(1, f"SAVEPOINT sa_{i}", None, 0.0)
            cls = ("row:notifications", "row:connector_configs")[i % 2]
            recorder.acquire(1, _lock(cls, "no key update wait", key=str(i)), 0.0)
            recorder.statement(1, f"RELEASE SAVEPOINT sa_{i}", None, 0.0)
    finally:
        sys.setprofile(None)
    return calls


def test_the_recorder_does_per_take_work_that_does_not_grow_with_what_the_transaction_holds():
    """A long migration transaction takes thousands of locks, each record in a savepoint: a
    savepoint that copied the transaction's state, or a take that walked every lock held, made
    the recorder quadratic and pushed a xero import test past its CI timeout. Four times the
    takes must cost about four times the calls (quadratic work costs sixteen)."""
    small, large = _recorder_calls(200), _recorder_calls(800)
    assert large < 6 * small, (small, large)


def _transaction(recorder: Recorder, conn_key: int = 1) -> list:
    """A deep copy of what every transaction state holds for ``conn_key``."""
    import copy

    return [copy.deepcopy(state.get(conn_key)) for state in recorder._transaction_state()]


def test_rolling_back_to_a_savepoint_puts_back_exactly_the_state_it_was_made_with():
    """The savepoint journal: entries changed inside a savepoint, in one released into it, and
    after a rollback to it, are all put back to what they were when it was made."""
    recorder = Recorder()
    recorder.acquire(1, _lock("row:notifications", "no key update wait", key="a"), 0.0)
    recorder.name_row(1, "items", "1", "c1")
    before = _transaction(recorder)
    recorder.statement(1, "SAVEPOINT sa_1", None, 0.0)
    recorder.acquire(1, _lock("row:notifications", "update wait", key="a"), 0.0)
    recorder.statement(1, "SAVEPOINT sa_2", None, 0.0)
    recorder.acquire(1, _lock("row:connector_configs", "no key update wait", key="b"), 0.0)
    recorder.acquire(1, Lock("row:companies", "insert", "celerp/x.py:1 f", "test_x.py:1", "c2", company="c2"), 0.0)
    recorder.name_row(1, "items", "1", "c3")
    recorder.statement(1, "RELEASE SAVEPOINT sa_2", None, 0.0)
    assert _transaction(recorder) != before
    recorder.statement(1, "ROLLBACK TO SAVEPOINT sa_1", None, 0.0)
    assert _transaction(recorder) == before
    recorder.acquire(1, _lock("row:connector_configs", "no key update wait", key="b"), 0.0)
    recorder.statement(1, "ROLLBACK TO SAVEPOINT sa_1", None, 0.0)
    assert _transaction(recorder) == before


async def test_a_session_opened_on_another_sessions_connection_ends_alone(session, auth):
    """A session the code opens on another session's bind (migration_core_sink.stage_file_cleanup)
    is, in production, a transaction on a connection of its own. Its commit ends what it took,
    never the company lock its caller still holds."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession

    from celerp.services.company_lock import lock_company

    lock_rank.RECORDER.clear_transactions()
    await lock_company(session, auth["company_id"])
    conn = id((await (await session.connection()).get_raw_connection()).dbapi_connection)
    key = ("row:companies", repr((str(auth["company_id"]),)))

    def held() -> bool:
        return any(k == key for k in lock_rank.RECORDER.taken.get(lock_rank.RECORDER.flow(conn), {}))

    assert held()
    async with AsyncSession(bind=session.bind) as other:
        await other.execute(text("SELECT 1"))
        await other.commit()
    assert held()


async def test_a_second_session_left_open_in_the_same_transaction_is_a_finding(session, auth):
    """A second session on the shared connection that is not opened as a block would merge
    into its task's recorded transaction, so its end would end the other's locks: refused."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession

    await session.execute(text("SELECT 1"))
    other = AsyncSession(bind=session.bind)
    try:
        await other.execute(text("SELECT 1"))
        assert [v["kind"] for v in lock_rank.RECORDER.take_violations()] == ["shared-flow"]
    finally:
        await other.close()


@pytest.mark.parametrize("mode, violates", [("exclusive nowait", False), ("share row exclusive nowait", True)])
def test_a_row_of_a_table_held_exclusive_waits_on_nothing(mode, violates):
    """R4t: while a transaction holds a table EXCLUSIVE, no other holds a row of it (every row
    lock takes ROW SHARE on its table), so a row it then takes there orders nothing. SHARE ROW
    EXCLUSIVE lets a row locker in, so the same take after a higher class still violates."""
    recorder = Recorder()
    recorder.acquire(1, _lock("row:ledger", "update wait", key="e"), 0.0)
    recorder.acquire(1, _lock("tbl:projections", mode), 0.0)
    recorder.acquire(1, _lock("row:projections", "write", key="item"), 0.0)
    assert (("order", "row:projections") in _kinds(recorder)) is violates
