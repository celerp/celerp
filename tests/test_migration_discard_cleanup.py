# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Discard never waits on file storage, and never loses track of the files.

The discard transaction records a cleanup task (company id and run ids only) with
the deletes; after the commit the run sources and the company's attachment files
are removed and the task is deleted. A storage failure keeps the task for the
startup sweep, while the user carries on as if the discard were complete."""

from __future__ import annotations

import uuid

import pytest

from migration_support import (
    OWNER_EMAIL,
    OWNER_PASSWORD,
    auth,
    count,
    fake_bytes,
    load_run,
    maker,
    migrate_as_owner,
    migration_env,  # noqa: F401 - fixture
    real_client,  # noqa: F401 - fixture
    real_engine,  # noqa: F401 - fixture
    save_decisions,
    scan_upload,
)
from test_factory_reset_live import _PARTITIONED, _PT
from test_helpers import create_item, default_location_id, in_language, register_admin

TASKS = "migration_cleanup_tasks"


def _source(env, run_id):
    return env["data_dir"] / "migration_runs" / str(run_id)


def _attachments(env, company_id):
    return env["data_dir"] / "static" / "attachments" / str(company_id)


async def _staged(client, engine, env) -> tuple[str, str, str]:
    """A company owner's staged migration with a source and a stored attachment file.
    Returns (owner token, run id, company id)."""
    token = await register_admin(client)
    run_id = await migrate_as_owner(client, token)
    run = await load_run(engine, uuid.UUID(run_id))
    stored = _attachments(env, run.company_id)
    stored.mkdir(parents=True)
    (stored / "receipt.bin").write_bytes(b"x")
    assert _source(env, run_id).is_dir()
    return token, run_id, str(run.company_id)


async def _tasks(engine) -> list[tuple]:
    from sqlalchemy import text
    async with maker(engine)() as s:
        return [tuple(r) for r in (await s.execute(text(f"SELECT company_id, run_ids FROM {TASKS}"))).all()]


async def _sweep(engine):
    from celerp.services import migrations
    async with maker(engine)() as s:
        await migrations.housekeeping(s)


def _fail_source_delete(monkeypatch):
    from celerp.services import migration_scan_store as store

    def refuse(path):
        raise OSError("device busy")
    monkeypatch.setattr(store, "_remove_tree", refuse)


def _fail_attachment_delete(monkeypatch):
    from celerp.services import attachments

    async def refuse(self, company_id):
        raise OSError("device busy")
    monkeypatch.setattr(attachments.LocalBackend, "delete_company", refuse)


@pytest.mark.asyncio
async def test_discard_removes_the_files_and_its_cleanup_task(real_client, real_engine, migration_env):
    token, run_id, company_id = await _staged(real_client, real_engine, migration_env)
    r = await real_client.post(f"/migrations/{run_id}/discard", headers=auth(token))
    assert r.status_code == 200 and r.json() == {"redirect": "/"}, r.text
    assert await count(real_engine, "companies", "id = :c", c=company_id) == 0
    assert not _source(migration_env, run_id).exists()
    assert not _attachments(migration_env, company_id).exists()
    assert await _tasks(real_engine) == []


@pytest.mark.asyncio
async def test_discard_removes_the_notices_the_staged_company_was_told(real_client, real_engine, migration_env):
    """RED before the change: a notice told to every company (a start that held its updates
    back, say) reached the staged company too, and discard refused it as data it could not
    remove."""
    from celerp.notifications.service import create

    token, run_id, company_id = await _staged(real_client, real_engine, migration_env)
    async with maker(real_engine)() as s:
        await create(s, uuid.UUID(company_id), "system", "Held back", "Updates were held back.")
        await s.commit()
    assert await count(real_engine, "notifications", "company_id = :c", c=company_id) == 1

    r = await real_client.post(f"/migrations/{run_id}/discard", headers=auth(token))
    assert r.status_code == 200, r.text
    assert await count(real_engine, "companies", "id = :c", c=company_id) == 0
    assert await count(real_engine, "notifications", "company_id = :c", c=company_id) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("fail", [_fail_source_delete, _fail_attachment_delete])
async def test_a_storage_failure_keeps_the_task_and_startup_finishes_it(real_client, real_engine, migration_env,
                                                                       monkeypatch, caplog, fail):
    import logging

    token, run_id, company_id = await _staged(real_client, real_engine, migration_env)
    with monkeypatch.context() as m:
        fail(m)
        caplog.set_level(logging.WARNING, logger="celerp.services.migrations")
        r = await real_client.post(f"/migrations/{run_id}/discard", headers=auth(token))
        assert r.status_code == 200 and r.json() == {"redirect": "/"}, r.text
        assert await count(real_engine, "companies", "id = :c", c=company_id) == 0
        assert await count(real_engine, "migration_runs") == 0
        assert await _tasks(real_engine) == [(uuid.UUID(company_id), [run_id])]
        warnings = [rec.getMessage() for rec in caplog.records if rec.name == "celerp.services.migrations"]
        assert warnings and all("receipt" not in w and "artifact" not in w for w in warnings), warnings

        # A sweep while storage is still failing keeps the task.
        await _sweep(real_engine)
        assert len(await _tasks(real_engine)) == 1

    # The next startup retries and clears the task; a repeat finds nothing to do.
    await _sweep(real_engine)
    assert await _tasks(real_engine) == []
    assert not _source(migration_env, run_id).exists()
    assert not _attachments(migration_env, company_id).exists()
    await _sweep(real_engine)
    assert await _tasks(real_engine) == []


@pytest.mark.asyncio
async def test_cleanup_is_idempotent_when_files_are_already_gone(real_engine, migration_env):
    """Missing files count as deleted, so a task whose files are gone just completes."""
    from celerp.models.migration import MigrationCleanupTask
    from celerp.services import migrations

    async with maker(real_engine)() as s:
        task = MigrationCleanupTask(company_id=uuid.uuid4(), run_ids=[str(uuid.uuid4())])
        s.add(task)
        await s.commit()
        assert await migrations.run_cleanup_task(s, task.id) is True
        assert await migrations.run_cleanup_task(s, task.id) is True
    assert await _tasks(real_engine) == []


@pytest.mark.asyncio
async def test_bootstrap_discard_with_a_storage_failure_returns_to_setup(real_client, real_engine, migration_env,
                                                                        monkeypatch):
    r = await scan_upload(real_client, fake_bytes())
    scan_token = r.json()["scan_token"]
    assert (await save_decisions(real_client, scan_token)).status_code == 200
    r = await real_client.post("/migrations/bootstrap/start", json={
        "scan_token": scan_token, "company_name": "Moved Co", "name": "Owner",
        "email": OWNER_EMAIL, "password": OWNER_PASSWORD})
    assert r.status_code == 201, r.text
    token, run_id = r.json()["access_token"], r.json()["run_id"]

    _fail_source_delete(monkeypatch)
    _fail_attachment_delete(monkeypatch)
    r = await real_client.post(f"/migrations/{run_id}/discard", headers=auth(token))
    assert r.status_code == 200 and r.json() == {"redirect": "/setup"}, r.text
    assert (await real_client.get("/auth/bootstrap-status")).json()["bootstrapped"] is False
    assert len(await _tasks(real_engine)) == 1
    # First-run setup is open again: a new scan is accepted.
    assert (await scan_upload(real_client, fake_bytes())).status_code == 200


async def _bootstrapped_beside_another_company(client, engine):
    """A staged first-run migration, and a company its owner is no member of."""
    from sqlalchemy import text

    r = await scan_upload(client, fake_bytes())
    scan_token = r.json()["scan_token"]
    assert (await save_decisions(client, scan_token)).status_code == 200
    r = await client.post("/migrations/bootstrap/start", json={
        "scan_token": scan_token, "company_name": "Moved Co", "name": "Owner",
        "email": OWNER_EMAIL, "password": OWNER_PASSWORD})
    assert r.status_code == 201, r.text
    other = uuid.uuid4()
    async with engine.begin() as conn:
        owner = (await conn.execute(text("SELECT id FROM users"))).scalar_one()
        await conn.execute(text("INSERT INTO companies (id, name, slug, settings, is_active, created_at) "
                                "VALUES (:c, 'Other Co', 'other-co', '{}', true, now())"), {"c": other})
    return r.json()["access_token"], r.json()["run_id"], owner, other


@pytest.mark.asyncio
async def test_bootstrap_discard_keeps_an_owner_another_company_still_names(real_client, real_engine, migration_env):
    """Discarding a first-run migration deletes its owner only when no other company's
    row names them, whatever that row's key does on delete: here a module row of another
    company would otherwise cascade away with the owner."""
    from sqlalchemy import text

    token, run_id, owner, other = await _bootstrapped_beside_another_company(real_client, real_engine)
    async with real_engine.begin() as conn:
        await conn.execute(text(
            "CREATE TABLE ext_notes (id uuid PRIMARY KEY, company_id uuid NOT NULL REFERENCES companies(id), "
            "author uuid REFERENCES users(id) ON DELETE CASCADE, body text NOT NULL)"))
        await conn.execute(text("INSERT INTO ext_notes VALUES (:i, :c, :u, 'other note')"),
                           {"i": uuid.uuid4(), "c": other, "u": owner})
    try:
        r = await real_client.post(f"/migrations/{run_id}/discard", headers=auth(token))

        assert r.status_code == 200 and r.json() == {"redirect": "/"}, r.text
        assert await count(real_engine, "users", "id = :i", i=owner) == 1
        assert await count(real_engine, "ext_notes", "company_id = :c AND author = :u", c=other, u=owner) == 1
    finally:
        async with real_engine.begin() as conn:
            await conn.execute(text("DROP TABLE IF EXISTS ext_notes"))


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["CASCADE", "NO ACTION"])
async def test_bootstrap_discard_keeps_an_owner_another_company_reaches_through_a_per_user_table(
        real_client, real_engine, migration_env, action):
    """The owner's preference sits in a per-user table that cascades from users, and
    another company's row names it. The discard keeps the owner, so that row is neither
    deleted nor tripped over."""
    from sqlalchemy import text

    token, run_id, owner, other = await _bootstrapped_beside_another_company(real_client, real_engine)
    async with real_engine.begin() as conn:
        await conn.execute(text("CREATE TABLE ext_prefs (id uuid PRIMARY KEY, "
                                "user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE)"))
        await conn.execute(text("CREATE TABLE ext_uses (id uuid PRIMARY KEY, company_id uuid NOT NULL "
                                f"REFERENCES companies(id), pref uuid REFERENCES ext_prefs(id) ON DELETE {action})"))
        pref = uuid.uuid4()
        await conn.execute(text("INSERT INTO ext_prefs VALUES (:p, :u)"), {"p": pref, "u": owner})
        await conn.execute(text("INSERT INTO ext_uses VALUES (:i, :c, :p)"), {"i": uuid.uuid4(), "c": other, "p": pref})
    try:
        r = await real_client.post(f"/migrations/{run_id}/discard", headers=auth(token))

        assert r.status_code == 200 and r.json() == {"redirect": "/"}, r.text
        assert await count(real_engine, "users", "id = :i", i=owner) == 1
        assert await count(real_engine, "ext_uses", "company_id = :c AND pref = :p", c=other, p=pref) == 1
    finally:
        async with real_engine.begin() as conn:
            await conn.execute(text("DROP TABLE IF EXISTS ext_uses, ext_prefs"))


@pytest.mark.asyncio
async def test_bootstrap_discard_goes_through_a_cascading_key_into_a_partitioned_table(
        real_client, real_engine, migration_env):
    """A module keeps a partitioned table and a table referring to it by a cascading key.
    The discard reads the key once, on the partitioned table, and deletes the owner."""
    from sqlalchemy import text

    r = await scan_upload(real_client, fake_bytes())
    assert (await save_decisions(real_client, r.json()["scan_token"])).status_code == 200
    r = await real_client.post("/migrations/bootstrap/start", json={
        "scan_token": r.json()["scan_token"], "company_name": "Moved Co", "name": "Owner",
        "email": OWNER_EMAIL, "password": OWNER_PASSWORD})
    assert r.status_code == 201, r.text
    token, run_id = r.json()["access_token"], r.json()["run_id"]
    async with real_engine.begin() as conn:
        for statement in _PARTITIONED:
            await conn.execute(text(statement))
    try:
        r = await real_client.post(f"/migrations/{run_id}/discard", headers=auth(token))

        assert r.status_code == 200 and r.json() == {"redirect": "/setup"}, r.text
        assert await count(real_engine, "users") == 0
    finally:
        async with real_engine.begin() as conn:
            await conn.execute(text("DROP TABLE IF EXISTS ext_event_refs, ext_events"))


# A table changed outside Celerp whose row of another company names the owner, each with
# the table holding that row, and the refusal naming the table discard cannot read.
_OUTSIDE_SHAPES = {
    "a key added to a partition": ((
        _PT, "CREATE TABLE ext_pt_a PARTITION OF ext_pt FOR VALUES IN ('a')",
        "ALTER TABLE ext_pt_a ADD FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE",
        "INSERT INTO ext_pt SELECT gen_random_uuid(), 'a', :c, id FROM users"),
        "ext_pt", ("migration.discard_partition_key", "ext_pt_a")),
    "a partition in another schema of a table naming users": ((
        "CREATE SCHEMA ext", "CREATE TABLE ext_tok (id int PRIMARY KEY, user_id uuid NOT NULL REFERENCES users(id) "
        "ON DELETE CASCADE) PARTITION BY RANGE (id)",
        "CREATE TABLE ext.ext_tok_0 PARTITION OF ext_tok FOR VALUES FROM (0) TO (100)",
        "CREATE TABLE ext_tok_log (id uuid PRIMARY KEY, company_id uuid NOT NULL REFERENCES companies(id), "
        "tok int NOT NULL REFERENCES ext.ext_tok_0(id) ON DELETE CASCADE)",
        "INSERT INTO ext_tok SELECT 7, id FROM users",
        "INSERT INTO ext_tok_log VALUES (gen_random_uuid(), :c, 7)"),
        "ext_tok_log", ("migration.discard_outside_reference", "ext.ext_tok_0")),
    "a table in another schema naming users": ((
        "CREATE SCHEMA ext", "CREATE TABLE ext.notes (id uuid PRIMARY KEY, company_id uuid NOT NULL, "
        "user_id uuid NOT NULL REFERENCES public.users(id) ON DELETE CASCADE)",
        "INSERT INTO ext.notes SELECT gen_random_uuid(), :c, id FROM users"),
        "ext.notes", ("migration.discard_outside_reference", "ext.notes")),
    "a table with no keys inheriting from a table naming users": ((
        "CREATE TABLE ext_note (id uuid PRIMARY KEY, user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE)",
        "CREATE TABLE ext_note_b (company_id uuid NOT NULL) INHERITS (ext_note)",
        "INSERT INTO ext_note_b SELECT gen_random_uuid(), id, :c FROM users"),
        "ext_note_b", ("migration.discard_partition_key", "ext_note_b")),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", list(_OUTSIDE_SHAPES))
async def test_bootstrap_discard_is_refused_while_a_table_changed_outside_celerp_names_the_owner(
        real_client, real_engine, migration_env, shape):
    """Another company's row in a table changed outside Celerp names the owner, through a key
    discard cannot read. The discard is refused naming that table, and the owner, the
    company and that row are all kept."""
    from sqlalchemy import text

    statements, other_table, (key, table) = _OUTSIDE_SHAPES[shape]
    r = await scan_upload(real_client, fake_bytes())
    assert (await save_decisions(real_client, r.json()["scan_token"])).status_code == 200
    r = await real_client.post("/migrations/bootstrap/start", json={
        "scan_token": r.json()["scan_token"], "company_name": "Moved Co", "name": "Owner",
        "email": OWNER_EMAIL, "password": OWNER_PASSWORD})
    assert r.status_code == 201, r.text
    token, run_id = r.json()["access_token"], r.json()["run_id"]
    other = uuid.uuid4()
    async with real_engine.begin() as conn:
        await conn.execute(text("INSERT INTO companies (id, name, slug, settings, is_active, created_at) "
                                "VALUES (:c, 'Other Co', 'other-co', '{}', true, now())"), {"c": other})
        for statement in statements:
            await conn.execute(text(statement), {"c": other})
    try:
        r = await real_client.post(f"/migrations/{run_id}/discard", headers=auth(token))

        assert r.status_code == 409, r.text
        detail = r.json()["detail"]
        assert (detail["message_key"], detail["params"]) == (key, {"table": table})
        assert table in in_language("de", detail) != detail["message"]
        assert await count(real_engine, "users", "email = :e", e=OWNER_EMAIL) == 1
        assert await count(real_engine, "companies", "name = 'Moved Co'") == 1
        assert await count(real_engine, other_table, "company_id = :c", c=other) == 1
    finally:
        async with real_engine.begin() as conn:
            await conn.execute(text("DROP TABLE IF EXISTS ext_tok_log, ext_tok, ext_pt, ext_note CASCADE"))
            await conn.execute(text("DROP SCHEMA IF EXISTS ext CASCADE"))
            await conn.execute(text("DELETE FROM companies WHERE id = :c"), {"c": other})


@pytest.mark.asyncio
async def test_bootstrap_discard_is_refused_when_a_table_inherits_from_users_after_the_check(
        real_client, real_engine, migration_env, monkeypatch):
    """Another connection makes a table holding a row naming the owner inherit from users
    after the discard has checked for tables changed outside Celerp, so deleting the owner
    would delete that row too. The discard is refused naming that table, and the owner,
    the company and that row are all kept."""
    from sqlalchemy import text

    from celerp import db_catalog

    r = await scan_upload(real_client, fake_bytes())
    assert (await save_decisions(real_client, r.json()["scan_token"])).status_code == 200
    r = await real_client.post("/migrations/bootstrap/start", json={
        "scan_token": r.json()["scan_token"], "company_name": "Moved Co", "name": "Owner",
        "email": OWNER_EMAIL, "password": OWNER_PASSWORD})
    assert r.status_code == 201, r.text
    token, run_id = r.json()["access_token"], r.json()["run_id"]
    async with real_engine.begin() as conn:
        await conn.execute(text("CREATE TABLE ext_user_copy (LIKE users INCLUDING CONSTRAINTS)"))
        await conn.execute(text("INSERT INTO ext_user_copy SELECT * FROM users WHERE email = :e"), {"e": OWNER_EMAIL})
    changed_outside = db_catalog.changed_outside

    async def then_inherit(session):
        found = await changed_outside(session)
        monkeypatch.setattr(db_catalog, "changed_outside", changed_outside)
        async with real_engine.begin() as conn:
            await conn.execute(text("SET LOCAL lock_timeout = '1s'"))
            await conn.execute(text("ALTER TABLE ext_user_copy INHERIT users"))
        return found

    monkeypatch.setattr(db_catalog, "changed_outside", then_inherit)
    try:
        r = await real_client.post(f"/migrations/{run_id}/discard", headers=auth(token))

        assert r.status_code == 409, r.text
        detail = r.json()["detail"]
        assert (detail["message_key"], detail["params"]) == ("migration.discard_partition_key",
                                                             {"table": "ext_user_copy"})
        assert await count(real_engine, "ONLY users", "email = :e", e=OWNER_EMAIL) == 1
        assert await count(real_engine, "companies", "name = 'Moved Co'") == 1
        assert await count(real_engine, "ext_user_copy", "email = :e", e=OWNER_EMAIL) == 1
    finally:
        async with real_engine.begin() as conn:
            await conn.execute(text("DROP TABLE IF EXISTS ext_user_copy"))


@pytest.mark.asyncio
async def test_discard_is_refused_while_the_company_has_records_discard_does_not_remove(
        real_client, real_engine, migration_env):
    """A table discard does not know holds a row of the staged company. The discard is
    refused naming that table, in the user's language, and the company, the row and the
    run's files are all kept."""
    from sqlalchemy import text

    token, run_id, company_id = await _staged(real_client, real_engine, migration_env)
    async with real_engine.begin() as conn:
        await conn.execute(text("CREATE TABLE ext_notes (id uuid PRIMARY KEY, company_id uuid NOT NULL)"))
        await conn.execute(text("INSERT INTO ext_notes VALUES (gen_random_uuid(), :c)"), {"c": company_id})
    try:
        r = await real_client.post(f"/migrations/{run_id}/discard", headers=auth(token))

        assert r.status_code == 409, r.text
        detail = r.json()["detail"]
        assert (detail["message_key"], detail["params"]) == ("migration.discard_unsafe_data", {"table": "ext_notes"})
        assert "ext_notes" in in_language("de", detail) != detail["message"]
        assert await count(real_engine, "companies", "id = :c", c=company_id) == 1
        assert await count(real_engine, "ext_notes", "company_id = :c", c=company_id) == 1
        assert _source(migration_env, run_id).is_dir()
    finally:
        async with real_engine.begin() as conn:
            await conn.execute(text("DROP TABLE IF EXISTS ext_notes"))


@pytest.mark.asyncio
async def test_discard_is_refused_while_row_security_hides_records_of_the_company(
        real_client, real_engine, migration_env):
    """Row security forced on a table discard does not know hides its row of the staged
    company from every read and delete. The discard is refused naming that table, and the
    company and the row are kept."""
    from sqlalchemy import text

    token, run_id, company_id = await _staged(real_client, real_engine, migration_env)
    async with real_engine.begin() as conn:
        for statement in ("CREATE TABLE ext_notes (id uuid PRIMARY KEY, company_id uuid NOT NULL)",
                          "INSERT INTO ext_notes VALUES (gen_random_uuid(), :c)",
                          "ALTER TABLE ext_notes ENABLE ROW LEVEL SECURITY",
                          "ALTER TABLE ext_notes FORCE ROW LEVEL SECURITY",
                          "CREATE POLICY ext_rule ON ext_notes USING (false)"):
            await conn.execute(text(statement), {"c": company_id})
    try:
        r = await real_client.post(f"/migrations/{run_id}/discard", headers=auth(token))

        assert r.status_code == 409, r.text
        detail = r.json()["detail"]
        assert (detail["message_key"], detail["params"]) == ("migration.discard_partition_key", {"table": "ext_notes"})
        assert await count(real_engine, "companies", "id = :c", c=company_id) == 1
        async with real_engine.begin() as conn:
            await conn.execute(text("ALTER TABLE ext_notes NO FORCE ROW LEVEL SECURITY"))
        assert await count(real_engine, "ext_notes", "company_id = :c", c=company_id) == 1
    finally:
        async with real_engine.begin() as conn:
            await conn.execute(text("DROP TABLE IF EXISTS ext_notes"))


@pytest.mark.asyncio
async def test_discard_reads_the_tables_beside_a_schema_named_after_the_database_role(
        real_client, real_engine, migration_env):
    """A schema named after the role Celerp connects as exists and holds none of Celerp's
    tables. Discard still finds the staged company's row in a table it does not know, is
    refused naming that table, and keeps the company and the row."""
    from sqlalchemy import text

    token, run_id, company_id = await _staged(real_client, real_engine, migration_env)
    async with real_engine.begin() as conn:
        role = (await conn.execute(text("SELECT quote_ident(current_user)"))).scalar_one()
        await conn.execute(text(f"CREATE SCHEMA {role}"))
        await conn.execute(text("CREATE TABLE public.ext_notes (id uuid PRIMARY KEY, company_id uuid NOT NULL)"))
        await conn.execute(text("INSERT INTO public.ext_notes VALUES (gen_random_uuid(), :c)"), {"c": company_id})
    try:
        r = await real_client.post(f"/migrations/{run_id}/discard", headers=auth(token))

        assert r.status_code == 409, r.text
        detail = r.json()["detail"]
        assert (detail["message_key"], detail["params"]) == ("migration.discard_unsafe_data", {"table": "ext_notes"})
        assert await count(real_engine, "companies", "id = :c", c=company_id) == 1
        assert await count(real_engine, "public.ext_notes", "company_id = :c", c=company_id) == 1
    finally:
        async with real_engine.begin() as conn:
            await conn.execute(text(f"DROP SCHEMA IF EXISTS {role} CASCADE"))
            await conn.execute(text("DROP TABLE IF EXISTS public.ext_notes"))


@pytest.mark.asyncio
async def test_bootstrap_discard_keeps_an_owner_whose_sessions_another_row_names(
        real_client, real_engine, migration_env):
    """The owner has a session in a per-user table that cascades from users, and an audit
    row with no company names that session by a key that does not cascade. The discard
    keeps the owner rather than trip over the audit row."""
    from sqlalchemy import text

    token, run_id, owner, _ = await _bootstrapped_beside_another_company(real_client, real_engine)
    async with real_engine.begin() as conn:
        await conn.execute(text("CREATE TABLE ext_sessions (id uuid PRIMARY KEY, "
                                "user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE)"))
        await conn.execute(text("CREATE TABLE ext_audit (id uuid PRIMARY KEY, "
                                "session_id uuid NOT NULL REFERENCES ext_sessions(id))"))
        session = uuid.uuid4()
        await conn.execute(text("INSERT INTO ext_sessions VALUES (:s, :u)"), {"s": session, "u": owner})
        await conn.execute(text("INSERT INTO ext_audit VALUES (:i, :s)"), {"i": uuid.uuid4(), "s": session})
    try:
        r = await real_client.post(f"/migrations/{run_id}/discard", headers=auth(token))

        assert r.status_code == 200 and r.json() == {"redirect": "/"}, r.text
        assert await count(real_engine, "users", "id = :i", i=owner) == 1
        assert await count(real_engine, "ext_audit", "session_id = :s", s=session) == 1
    finally:
        async with real_engine.begin() as conn:
            await conn.execute(text("DROP TABLE IF EXISTS ext_audit, ext_sessions"))


@pytest.mark.asyncio
async def test_additional_company_discard_with_a_storage_failure_keeps_the_active_company(
        real_client, real_engine, migration_env, monkeypatch):
    token, run_id, _ = await _staged(real_client, real_engine, migration_env)
    _fail_source_delete(monkeypatch)
    _fail_attachment_delete(monkeypatch)
    r = await real_client.post(f"/migrations/{run_id}/discard", headers=auth(token))
    assert r.status_code == 200 and r.json() == {"redirect": "/"}, r.text
    assert len(await _tasks(real_engine)) == 1

    headers = auth(token)
    me = await real_client.get("/companies/me", headers=headers)
    assert me.status_code == 200 and me.json()["name"] == "Perm Co"
    location_id = await default_location_id(real_client, headers)
    await create_item(real_client, headers, location_id, sku="SKU-AFTER-DISCARD")
    # Another migration can start straight away.
    assert await migrate_as_owner(real_client, token)
