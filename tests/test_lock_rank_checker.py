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


def test_a_key_others_wait_on_still_fails_when_held_across_connections(tmp_path):
    """Held under the migration key, the wait is judged offline (R5); a take of the class
    outside the key, as every import runner makes, leaves it unproven."""
    recorder = Recorder()

    async def body():
        recorder.acquire(1, _lock("adv:migration", "session exclusive try"), 0.0)
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
    insert() or copies a stored id into it."""
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
            args = [a for a in node.args if isinstance(a, ast.Name) and a.id == model]
            if name in ("insert", "pg_insert") and args:
                wrong.append(f"{path}:{node.lineno}: insert({model})")
            if name == model:
                for kw in node.keywords:
                    if kw.arg is None or (kw.arg == pk[0].key and ast.dump(kw.value) != ast.dump(fresh)):
                        wrong.append(f"{path}:{node.lineno}: {model}(...) with a key not drawn fresh")
    assert not wrong, wrong


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
        "items": {("ins", "insert"), ("upd", "write"), ("lock", "lock"), ("get", "lock"),
                  ("raw", "write"), ("raw_insert", "insert"), ("inner", "delete")},
        "tags": {("dele", "delete"), ("raw_lock", "lock")},
    }


def test_the_census_names_the_production_writers_the_record_sites():
    writers = lock_rank.static_writers()
    assert "celerp/projections/engine.py _locked_projection" in writers["row:projections"]
    assert "celerp/services/company_lock.py lock_company" in writers["row:companies"]


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


def _stamp_across_connections(recorder: Recorder) -> None:
    """A flow holds the migration key on one connection and stamps on another, as every
    alembic run does: its wait on the stamp crosses connections."""
    async def body():
        recorder.statement(9, _TAKE_KEY, (), 0.0)
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
    _stamp_across_connections(recorder)
    recorder.acquire(3, _lock("row:alembic_version", "write"), 0.0)
    (tmp_path / "lock-graph-gw0.json").write_text(json.dumps(recorder.dump()))
    assert _CROSS_STAMP in lock_rank.debt_listing(tmp_path)
    report = lock_rank.check(tmp_path)
    assert f"- {_CROSS_STAMP}" in report and lock_rank.check_failed(report)


def test_a_cross_connection_wait_without_a_serializing_key_stays_a_live_violation(recorder):
    async def body():
        recorder.acquire(9, _lock("adv:external-product", "session exclusive wait", key="p"), 0.0)
        recorder.acquire(2, _lock("row:alembic_version", "write"), 0.0)

    asyncio.run(body())
    assert ("cross-connection", "row:alembic_version") in _kinds(recorder)


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
