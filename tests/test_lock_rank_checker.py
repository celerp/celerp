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
    assert ("key:companies", "key share wait") in found
    assert ("key:users", "key share wait") in found


def test_a_foreign_key_share_after_a_higher_rank_is_not_ordered():
    recorder = Recorder()
    recorder.acquire(1, _lock("row:projections", "write", key="item"), 0.0)
    recorder.acquire(1, _lock("key:companies", "key share wait", key="c"), 0.0)
    assert not recorder.violations
    assert ("row:projections", "key:companies") not in recorder.edges


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


# ── R1: rows of a company the transaction inserted; R2: inserts under the company lock ──

_COMPANY = "INSERT INTO companies (id, name, slug) VALUES ($1::UUID, $2, $3)"
_LOCK_COMPANY = "SELECT companies.id FROM companies WHERE companies.id = $1::UUID FOR NO KEY UPDATE"
_ACCOUNT = "INSERT INTO accounts (id, company_id, code) VALUES ($1::UUID, $2::UUID, $3)"
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
    recorder.acquire(conn, _lock("adv:chart", "xact exclusive wait", key="chart"), 0.0)


def test_rows_of_a_company_the_transaction_inserted_order_nothing(recorder):
    # Provisioning: nobody else can see the new company, so no lock on its rows waits.
    recorder.statement(1, _COMPANY, ("c1", "Co", "co"), 0.0)
    recorder.statement(1, _ACCOUNT, ("a1", "c1", "1000"), 0.0)
    recorder.statement(1, _LOCK_COMPANY, ("c1",), 0.0)
    _chart(recorder)
    assert not recorder.violations


@pytest.mark.parametrize("chart_of, violates", [("c1", False), ("c2", True)])
def test_the_chart_lock_of_a_company_the_transaction_inserted_orders_nothing(recorder, chart_of, violates):
    from celerp.services.company_lock import chart_key

    recorder.statement(1, _COMPANY, ("c1", "Co", "co"), 0.0)
    recorder.acquire(1, _lock("adv:chart", "xact exclusive wait", key=str(chart_key(chart_of))), 0.0)
    recorder.acquire(1, _lock("row:user_auth_state", "no key update wait", key="u"), 0.0)
    assert (("order", "row:user_auth_state") in _kinds(recorder)) is violates


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
    assert ("order", "adv:chart") in _kinds(recorder)


def test_an_own_companys_insert_under_a_unique_key_without_the_company_still_orders(recorder):
    # Another transaction can insert the same token, whatever company it names.
    recorder.statement(1, _COMPANY, ("c1", "Co", "co"), 0.0)
    recorder.statement(1, _TOKEN, ("t1", "c1", "tok"), 0.0)
    _chart(recorder)
    assert ("order", "adv:chart") in _kinds(recorder)


def test_another_companys_rows_are_not_the_transactions_own(recorder):
    recorder.statement(1, _COMPANY, ("c1", "Co", "co"), 0.0)
    recorder.statement(1, _ACCOUNT, ("a1", "c2", "1000"), 0.0)
    _chart(recorder)
    assert ("order", "adv:chart") in _kinds(recorder)


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
