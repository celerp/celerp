# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Factory reset on real Postgres. It resets the signed-in company and nothing else: the
owner types the company's exact name, every record of that company goes in one
transaction, and a user goes with it only when no other company still has them."""

from __future__ import annotations

import asyncio

import pytest

from migration_support import OWNER_EMAIL, OWNER_PASSWORD, auth, count, real_client, real_engine  # noqa: F401 - fixtures
from test_company_backup_ui import _page, ui  # noqa: F401 - fixture
from test_helpers import in_language

pytestmark = pytest.mark.asyncio


async def _register(client, company: str) -> str:
    r = await client.post("/auth/register", json={
        "company_name": company, "email": OWNER_EMAIL, "name": "Owner", "password": OWNER_PASSWORD})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


async def _reset(client, token: str, name: str | None):
    return await client.post("/system/factory-reset", headers=auth(token),
                             json=None if name is None else {"confirm_name": name})


async def _id(client, token: str) -> str:
    return (await client.get("/companies/me", headers=auth(token))).json()["id"]


async def _held(engine, company_id: str) -> dict:
    return {table: await count(engine, table, "company_id = :i", i=company_id)
            for table in ("ledger", "projections", "user_companies", "locations")}


async def _two_companies(client) -> tuple[str, str]:
    """Alpha with a contact and a clerk only Alpha has; Beta, owned by the same owner,
    with a contact of its own."""
    ta = await _register(client, "Alpha Co")
    r = await client.post("/crm/contacts", json={"name": "Alpha Buyer"}, headers=auth(ta))
    assert r.status_code in (200, 201), r.text
    r = await client.post("/companies/me/users", headers=auth(ta), json={
        "email": "clerk@example.com", "name": "Clerk", "role": "operator", "password": OWNER_PASSWORD})
    assert r.status_code == 200, r.text
    r = await client.post("/companies", json={"name": "Beta Co"}, headers=auth(ta))
    assert r.status_code == 200, r.text
    tb = r.json()["access_token"]
    r = await client.post("/crm/contacts", json={"name": "Beta Buyer"}, headers=auth(tb))
    assert r.status_code in (200, 201), r.text
    return ta, tb


async def test_factory_reset_wipes_the_company_on_a_real_session(real_client, real_engine):  # noqa: F811
    token = await _register(real_client, "Reset Co")
    cid = await _id(real_client, token)

    r = await _reset(real_client, token, "Reset Co")

    assert r.status_code == 200 and r.json() == {"ok": True}, r.text
    assert await count(real_engine, "companies") == 0
    assert set((await _held(real_engine, cid)).values()) == {0}
    assert await count(real_engine, "users") == 0


async def test_resetting_one_company_leaves_the_other_and_its_people(real_client, real_engine):  # noqa: F811
    ta, tb = await _two_companies(real_client)
    alpha, beta = await _id(real_client, ta), await _id(real_client, tb)
    beta_before = await _held(real_engine, beta)
    assert beta_before["ledger"] > 0
    beta_contacts = (await real_client.get("/crm/contacts", headers=auth(tb))).json()["items"]
    assert "Beta Buyer" in [c["name"] for c in beta_contacts]

    r = await _reset(real_client, ta, "Alpha Co")

    assert r.status_code == 200, r.text
    assert await count(real_engine, "companies", "id = :i", i=alpha) == 0
    assert set((await _held(real_engine, alpha)).values()) == {0}
    assert await count(real_engine, "companies", "id = :i", i=beta) == 1
    assert await _held(real_engine, beta) == beta_before
    # The owner still has Beta; the clerk had only Alpha.
    assert await count(real_engine, "users", "email = :e", e=OWNER_EMAIL) == 1
    assert await count(real_engine, "users", "email = :e", e="clerk@example.com") == 0
    r = await real_client.get("/crm/contacts", headers=auth(tb))
    assert r.json()["items"] == beta_contacts


async def test_a_failure_part_way_leaves_both_companies_as_they_were(real_client, real_engine, monkeypatch):  # noqa: F811
    import celerp.routers.system as system

    ta, tb = await _two_companies(real_client)
    alpha, beta = await _id(real_client, ta), await _id(real_client, tb)
    before = {alpha: await _held(real_engine, alpha), beta: await _held(real_engine, beta)}
    users = await count(real_engine, "users")
    wipe = system._company_deletes
    # The company row goes last, so the failure comes after every other table's delete.
    monkeypatch.setattr(system, "_company_deletes", lambda schema: [
        *wipe(schema)[:-1], "DELETE FROM no_such_table WHERE company_id = CAST(:c AS uuid)"])
    with pytest.raises(Exception):  # the in-process transport re-raises the server error
        await _reset(real_client, ta, "Alpha Co")

    assert await count(real_engine, "companies") == 2
    assert {alpha: await _held(real_engine, alpha), beta: await _held(real_engine, beta)} == before
    assert await count(real_engine, "users") == users


_MODULE_TABLES = (
    "CREATE TABLE ext_parcels (id uuid PRIMARY KEY, company_id uuid NOT NULL REFERENCES companies(id), "
    "packed_by uuid REFERENCES users(id), label text NOT NULL)",
    "CREATE TABLE ext_parcel_scans (id uuid PRIMARY KEY, "
    "parcel_id uuid NOT NULL REFERENCES ext_parcels(id), at text NOT NULL)",
)


async def test_a_reset_clears_the_tables_of_a_module_that_is_not_loaded(real_client, real_engine):  # noqa: F811
    """A module switched off keeps its tables. Its company rows, and the rows that hang
    off them, go with the company; another company's rows in the same tables stay, and
    so does a user one of those rows still names."""
    import uuid

    from sqlalchemy import text

    ta, tb = await _two_companies(real_client)
    alpha, beta = await _id(real_client, ta), await _id(real_client, tb)
    async with real_engine.begin() as conn:
        clerk = (await conn.execute(text("SELECT id FROM users WHERE email = 'clerk@example.com'"))).scalar_one()
        for ddl in _MODULE_TABLES:
            await conn.execute(text(ddl))
    try:
        async with real_engine.begin() as conn:
            for company, label in ((alpha, "alpha"), (beta, "beta")):
                parcel = uuid.uuid4()
                await conn.execute(text("INSERT INTO ext_parcels VALUES (:i, :c, :u, :l)"),
                                   {"i": parcel, "c": company, "u": clerk, "l": label})
                await conn.execute(text("INSERT INTO ext_parcel_scans VALUES (:i, :p, 'today')"),
                                   {"i": uuid.uuid4(), "p": parcel})

        r = await _reset(real_client, ta, "Alpha Co")

        assert r.status_code == 200, r.text
        assert await count(real_engine, "companies", "id = :i", i=alpha) == 0
        assert await count(real_engine, "ext_parcels", "company_id = :i", i=alpha) == 0
        assert await count(real_engine, "ext_parcels", "company_id = :i", i=beta) == 1
        assert await count(real_engine, "ext_parcel_scans") == 1
        assert await count(real_engine, "ext_parcel_scans", "parcel_id IN (SELECT id FROM ext_parcels)") == 1
        # Beta's parcel still names the clerk, so the clerk stays though Alpha was their only company.
        assert await count(real_engine, "users", "id = :i", i=clerk) == 1
    finally:
        async with real_engine.begin() as conn:
            await conn.execute(text("DROP TABLE IF EXISTS ext_parcel_scans, ext_parcels"))


async def _rows(engine, table: str) -> list:
    from sqlalchemy import text

    async with engine.connect() as conn:
        return [tuple(r) for r in (await conn.execute(text(f"SELECT * FROM {table} ORDER BY id"))).all()]


_ACTIONS = ("CASCADE", "SET NULL", "SET DEFAULT", "RESTRICT", "NO ACTION")


@pytest.mark.parametrize("table, action", [
    *(("ext_notes", a) for a in _ACTIONS),
    ("ext_note_reads", "CASCADE"), ("ext_note_reads", "SET NULL")])
async def test_a_user_another_company_still_names_stays_whatever_the_key_does(
        real_client, real_engine, table, action):  # noqa: F811
    """The clerk belongs to Alpha only. One row of Beta still names the clerk: in a module
    table with a company column, or in one hanging off it with none. Resetting Alpha
    keeps the clerk and leaves Beta's rows exactly as they were, whatever the key would
    do on delete."""
    import uuid

    from sqlalchemy import text

    def on_delete(name: str) -> str:
        return action if name == table else "RESTRICT"

    ta, tb = await _two_companies(real_client)
    beta = await _id(real_client, tb)
    async with real_engine.begin() as conn:
        clerk = (await conn.execute(text("SELECT id FROM users WHERE email = 'clerk@example.com'"))).scalar_one()
        await conn.execute(text(
            "CREATE TABLE ext_notes (id uuid PRIMARY KEY, company_id uuid NOT NULL REFERENCES companies(id), "
            f"author uuid REFERENCES users(id) ON DELETE {on_delete('ext_notes')}, body text NOT NULL)"))
        await conn.execute(text(
            "CREATE TABLE ext_note_reads (id uuid PRIMARY KEY, note_id uuid NOT NULL REFERENCES ext_notes(id), "
            f"reader uuid REFERENCES users(id) ON DELETE {on_delete('ext_note_reads')}, at text NOT NULL)"))
    try:
        async with real_engine.begin() as conn:
            note = uuid.uuid4()
            await conn.execute(text("INSERT INTO ext_notes VALUES (:i, :c, :u, 'beta note')"),
                               {"i": note, "c": beta, "u": clerk if table == "ext_notes" else None})
            await conn.execute(text("INSERT INTO ext_note_reads VALUES (:i, :n, :u, 'today')"),
                               {"i": uuid.uuid4(), "n": note, "u": clerk if table == "ext_note_reads" else None})
        before = {t: await _rows(real_engine, t) for t in ("ext_notes", "ext_note_reads")}

        r = await _reset(real_client, ta, "Alpha Co")

        assert r.status_code == 200, r.text
        assert await count(real_engine, "users", "id = :i", i=clerk) == 1
        assert {t: await _rows(real_engine, t) for t in before} == before
    finally:
        async with real_engine.begin() as conn:
            await conn.execute(text("DROP TABLE IF EXISTS ext_note_reads, ext_notes"))


async def test_a_user_only_the_reset_company_names_goes_with_their_sessions(real_client, real_engine):  # noqa: F811
    """Named only by Alpha's own rows, the clerk goes with Alpha, and their sign-in
    records, which hold no company's data, cascade away with them."""
    import uuid

    from sqlalchemy import text

    ta, _ = await _two_companies(real_client)
    alpha = await _id(real_client, ta)
    async with real_engine.begin() as conn:
        clerk = (await conn.execute(text("SELECT id FROM users WHERE email = 'clerk@example.com'"))).scalar_one()
        await conn.execute(text(
            "CREATE TABLE ext_notes (id uuid PRIMARY KEY, company_id uuid NOT NULL REFERENCES companies(id), "
            "author uuid REFERENCES users(id) ON DELETE CASCADE, body text NOT NULL)"))
        await conn.execute(text("INSERT INTO ext_notes VALUES (:i, :c, :u, 'alpha note')"),
                           {"i": uuid.uuid4(), "c": alpha, "u": clerk})
        await conn.execute(text("INSERT INTO session_registry (jti, user_id, expiry) VALUES ('clerk-jti', :u, now())"),
                           {"u": clerk})
        await conn.execute(text("INSERT INTO user_auth_state (user_id, nonce) VALUES (:u, 'n') "
                                "ON CONFLICT (user_id) DO NOTHING"), {"u": clerk})
    try:
        r = await _reset(real_client, ta, "Alpha Co")

        assert r.status_code == 200, r.text
        assert await count(real_engine, "users", "id = :i", i=clerk) == 0
        assert await count(real_engine, "ext_notes") == 0
        assert await count(real_engine, "session_registry", "user_id = :i", i=clerk) == 0
        assert await count(real_engine, "user_auth_state", "user_id = :i", i=clerk) == 0
    finally:
        async with real_engine.begin() as conn:
            await conn.execute(text("DROP TABLE IF EXISTS ext_notes"))


@pytest.mark.parametrize("action", ["CASCADE", "NO ACTION"])
async def test_a_user_another_company_reaches_through_a_per_user_table_stays(
        real_client, real_engine, action):  # noqa: F811
    """The clerk belongs to Alpha only. A per-user table (no company column, cascading from
    users) holds the clerk's preference, and one of Beta's rows names that preference.
    Resetting Alpha keeps the clerk, so Beta's row is neither deleted nor tripped over."""
    import uuid

    from sqlalchemy import text

    ta, tb = await _two_companies(real_client)
    beta = await _id(real_client, tb)
    async with real_engine.begin() as conn:
        clerk = (await conn.execute(text("SELECT id FROM users WHERE email = 'clerk@example.com'"))).scalar_one()
        await conn.execute(text("CREATE TABLE ext_prefs (id uuid PRIMARY KEY, "
                                "user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE)"))
        await conn.execute(text("CREATE TABLE ext_uses (id uuid PRIMARY KEY, company_id uuid NOT NULL "
                                f"REFERENCES companies(id), pref_id uuid REFERENCES ext_prefs(id) ON DELETE {action})"))
        pref = uuid.uuid4()
        await conn.execute(text("INSERT INTO ext_prefs VALUES (:p, :u)"), {"p": pref, "u": clerk})
        await conn.execute(text("INSERT INTO ext_uses VALUES (:i, :c, :p)"), {"i": uuid.uuid4(), "c": beta, "p": pref})
    try:
        before = {t: await _rows(real_engine, t) for t in ("ext_prefs", "ext_uses")}

        r = await _reset(real_client, ta, "Alpha Co")

        assert r.status_code == 200, r.text
        assert await count(real_engine, "users", "id = :i", i=clerk) == 1
        assert {t: await _rows(real_engine, t) for t in before} == before
    finally:
        async with real_engine.begin() as conn:
            await conn.execute(text("DROP TABLE IF EXISTS ext_uses, ext_prefs"))


@pytest.mark.parametrize("action", ["NO ACTION", "CASCADE"])
async def test_a_user_another_user_names_stays(real_client, real_engine, action):  # noqa: F811
    """A module column on users names who invited each user, and the clerk, Alpha's
    only, invited the others. Resetting Alpha keeps the clerk those rows still name, so
    the owner, still Beta's, is neither deleted with the clerk nor tripped over."""
    from sqlalchemy import text

    ta, tb = await _two_companies(real_client)
    beta = await _id(real_client, tb)
    async with real_engine.begin() as conn:
        clerk = (await conn.execute(text("SELECT id FROM users WHERE email = 'clerk@example.com'"))).scalar_one()
        await conn.execute(text(
            f"ALTER TABLE users ADD COLUMN ext_invited_by uuid REFERENCES users(id) ON DELETE {action}"))
        await conn.execute(text("UPDATE users SET ext_invited_by = :u WHERE id <> :u"), {"u": clerk})
    try:
        members = await _rows(real_engine, "user_companies")

        r = await _reset(real_client, ta, "Alpha Co")

        assert r.status_code == 200, r.text
        assert await count(real_engine, "users", "id = :i", i=clerk) == 1
        assert [m for m in await _rows(real_engine, "user_companies")] == [m for m in members if beta in map(str, m)]
    finally:
        async with real_engine.begin() as conn:
            await conn.execute(text("ALTER TABLE users DROP COLUMN IF EXISTS ext_invited_by"))


async def test_a_user_whose_sessions_another_row_names_stays(real_client, real_engine):  # noqa: F811
    """The clerk, Alpha's only, has a session in a per-user table that cascades from
    users, and an audit row with no company names that session by a key that does not
    cascade. Deleting the clerk would trip over the audit row, so the reset keeps the
    clerk and leaves both rows as they were."""
    import uuid

    from sqlalchemy import text

    ta, _ = await _two_companies(real_client)
    async with real_engine.begin() as conn:
        clerk = (await conn.execute(text("SELECT id FROM users WHERE email = 'clerk@example.com'"))).scalar_one()
        await conn.execute(text("CREATE TABLE ext_sessions (id uuid PRIMARY KEY, "
                                "user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE)"))
        await conn.execute(text("CREATE TABLE ext_audit (id uuid PRIMARY KEY, "
                                "session_id uuid NOT NULL REFERENCES ext_sessions(id))"))
        session = uuid.uuid4()
        await conn.execute(text("INSERT INTO ext_sessions VALUES (:s, :u)"), {"s": session, "u": clerk})
        await conn.execute(text("INSERT INTO ext_audit VALUES (:i, :s)"), {"i": uuid.uuid4(), "s": session})
    try:
        before = {t: await _rows(real_engine, t) for t in ("ext_sessions", "ext_audit")}

        r = await _reset(real_client, ta, "Alpha Co")

        assert r.status_code == 200, r.text
        assert await count(real_engine, "users", "id = :i", i=clerk) == 1
        assert {t: await _rows(real_engine, t) for t in before} == before
    finally:
        async with real_engine.begin() as conn:
            await conn.execute(text("DROP TABLE IF EXISTS ext_audit, ext_sessions"))


async def test_a_tree_of_rows_no_user_reaches_keeps_no_user(real_client, real_engine):  # noqa: F811
    """A shared category tree (each category cascading from its parent, no company, no
    user) with a label naming one category. Nothing deleting a user reaches the tree, so
    the clerk, Alpha's only, goes with Alpha. A second Alpha-only user has an assignment,
    cascading from users, that a row of Beta's logs: that user stays, and so does the log."""
    import uuid

    from sqlalchemy import text

    ta, tb = await _two_companies(real_client)
    beta = await _id(real_client, tb)
    r = await real_client.post("/companies/me/users", headers=auth(ta), json={
        "email": "keeper@example.com", "name": "Keeper", "role": "operator", "password": OWNER_PASSWORD})
    assert r.status_code == 200, r.text
    category, assignment = uuid.uuid4(), uuid.uuid4()
    async with real_engine.begin() as conn:
        clerk = (await conn.execute(text("SELECT id FROM users WHERE email = 'clerk@example.com'"))).scalar_one()
        keeper = (await conn.execute(text("SELECT id FROM users WHERE email = 'keeper@example.com'"))).scalar_one()
        await conn.execute(text("CREATE TABLE ext_cats (id uuid PRIMARY KEY, "
                                "parent_id uuid REFERENCES ext_cats(id) ON DELETE CASCADE)"))
        await conn.execute(text("CREATE TABLE ext_cat_labels (id uuid PRIMARY KEY, "
                                "cat_id uuid NOT NULL REFERENCES ext_cats(id))"))
        await conn.execute(text("CREATE TABLE ext_assign (id uuid PRIMARY KEY, "
                                "user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE)"))
        await conn.execute(text("CREATE TABLE ext_assign_log (id uuid PRIMARY KEY, company_id uuid NOT NULL "
                                "REFERENCES companies(id), assign_id uuid NOT NULL REFERENCES ext_assign(id) "
                                "ON DELETE CASCADE)"))
        await conn.execute(text("INSERT INTO ext_cats VALUES (:c, NULL)"), {"c": category})
        await conn.execute(text("INSERT INTO ext_cat_labels VALUES (:i, :c)"), {"i": uuid.uuid4(), "c": category})
        await conn.execute(text("INSERT INTO ext_assign VALUES (:a, :u)"), {"a": assignment, "u": keeper})
        await conn.execute(text("INSERT INTO ext_assign_log VALUES (:i, :b, :a)"),
                           {"i": uuid.uuid4(), "b": beta, "a": assignment})
    try:
        r = await _reset(real_client, ta, "Alpha Co")

        assert r.status_code == 200, r.text
        assert await count(real_engine, "users", "id = :i", i=clerk) == 0
        assert await count(real_engine, "users", "id = :i", i=keeper) == 1
        assert await count(real_engine, "ext_assign_log", "company_id = :b", b=beta) == 1
        assert await count(real_engine, "ext_cat_labels") == 1
    finally:
        async with real_engine.begin() as conn:
            await conn.execute(text("DROP TABLE IF EXISTS ext_assign_log, ext_assign, ext_cat_labels, ext_cats"))


async def test_a_user_whose_rows_head_a_tree_another_company_names_stays(real_client, real_engine):  # noqa: F811
    """A thread tree cascades from users and from each reply's parent. The clerk, Alpha's
    only, started a thread; a reply by nobody in particular hangs off it, and a row of
    Beta's names that reply by a cascading key. Deleting the clerk would cascade down the
    tree into Beta's row, so the clerk stays and every row with them."""
    import uuid

    from sqlalchemy import text

    ta, tb = await _two_companies(real_client)
    beta = await _id(real_client, tb)
    thread, reply = uuid.uuid4(), uuid.uuid4()
    async with real_engine.begin() as conn:
        clerk = (await conn.execute(text("SELECT id FROM users WHERE email = 'clerk@example.com'"))).scalar_one()
        await conn.execute(text("CREATE TABLE ext_threads (id uuid PRIMARY KEY, "
                                "user_id uuid REFERENCES users(id) ON DELETE CASCADE, "
                                "parent_id uuid REFERENCES ext_threads(id) ON DELETE CASCADE)"))
        await conn.execute(text("CREATE TABLE ext_pins (id uuid PRIMARY KEY, company_id uuid NOT NULL "
                                "REFERENCES companies(id), thread_id uuid REFERENCES ext_threads(id) ON DELETE CASCADE)"))
        await conn.execute(text("INSERT INTO ext_threads VALUES (:t, :u, NULL), (:r, NULL, :t)"),
                           {"t": thread, "r": reply, "u": clerk})
        await conn.execute(text("INSERT INTO ext_pins VALUES (:i, :b, :r)"), {"i": uuid.uuid4(), "b": beta, "r": reply})
    try:
        before = {t: await _rows(real_engine, t) for t in ("ext_threads", "ext_pins")}

        r = await _reset(real_client, ta, "Alpha Co")

        assert r.status_code == 200, r.text
        assert await count(real_engine, "users", "id = :i", i=clerk) == 1
        assert {t: await _rows(real_engine, t) for t in before} == before
    finally:
        async with real_engine.begin() as conn:
            await conn.execute(text("DROP TABLE IF EXISTS ext_pins, ext_threads"))


async def test_a_user_a_table_named_u_names_stays(real_client, real_engine):  # noqa: F811
    """A module table is named ``u`` and one of its rows names the clerk, Alpha's only.
    The table's name cannot be confused with the users being deleted: resetting Alpha
    keeps the clerk and the row."""
    import uuid

    from sqlalchemy import text

    ta, _ = await _two_companies(real_client)
    async with real_engine.begin() as conn:
        clerk = (await conn.execute(text("SELECT id FROM users WHERE email = 'clerk@example.com'"))).scalar_one()
        await conn.execute(text("CREATE TABLE u (id uuid PRIMARY KEY, user_id uuid NOT NULL REFERENCES users(id))"))
        await conn.execute(text("INSERT INTO u VALUES (:i, :u)"), {"i": uuid.uuid4(), "u": clerk})
    try:
        before = await _rows(real_engine, "u")

        r = await _reset(real_client, ta, "Alpha Co")

        assert r.status_code == 200, r.text
        assert await count(real_engine, "users", "id = :i", i=clerk) == 1
        assert await _rows(real_engine, "u") == before
    finally:
        async with real_engine.begin() as conn:
            await conn.execute(text("DROP TABLE IF EXISTS u"))


@pytest.mark.parametrize("action", ["SET NULL", "SET DEFAULT"])
async def test_a_row_whose_key_clears_outlives_the_reset_company(real_client, real_engine, action):  # noqa: F811
    """A module row with no company column names Alpha through a key that clears on
    delete, and nothing else. Resetting Alpha leaves the row with that key cleared, as
    the schema declares."""
    import uuid

    from sqlalchemy import text

    ta, _ = await _two_companies(real_client)
    alpha = await _id(real_client, ta)
    async with real_engine.begin() as conn:
        await conn.execute(text(_TRANSFERS.format(action=action)))
        await conn.execute(text("INSERT INTO ext_transfers VALUES (:i, :a, NULL, 'alpha only')"),
                           {"i": uuid.uuid4(), "a": alpha})
    try:
        r = await _reset(real_client, ta, "Alpha Co")

        assert r.status_code == 200, r.text
        async with real_engine.connect() as conn:
            rows = (await conn.execute(text("SELECT from_company, to_company, label FROM ext_transfers"))).all()
        assert [tuple(map(str, row)) for row in rows] == [("None", "None", "alpha only")]
    finally:
        async with real_engine.begin() as conn:
            await conn.execute(text("DROP TABLE IF EXISTS ext_transfers"))


_TRANSFERS = ("CREATE TABLE ext_transfers (id uuid PRIMARY KEY, "
              "from_company uuid REFERENCES companies(id) ON DELETE {action}, "
              "to_company uuid REFERENCES companies(id) ON DELETE {action}, label text NOT NULL)")
_ITEMS = ("CREATE TABLE ext_items (id uuid PRIMARY KEY, company_id uuid NOT NULL REFERENCES companies(id), "
          "label text NOT NULL)")


async def _refused_untouched(client, engine, token: str, company: str, tables: tuple[str, ...], table: str):
    """Reset is refused naming ``table``, and nothing anywhere has changed."""
    before = {t: await _rows(engine, t) for t in tables}
    held = await _held(engine, company)

    r = await _reset(client, token, "Alpha Co")

    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert (detail["message_key"], detail["params"]) == ("system.factory_reset.held_elsewhere", {"table": table})
    assert await count(engine, "companies", "id = :i", i=company) == 1
    assert await _held(engine, company) == held
    assert {t: await _rows(engine, t) for t in tables} == before


@pytest.mark.parametrize("action", _ACTIONS)
async def test_a_reset_is_refused_while_another_company_points_at_its_rows(
        real_client, real_engine, action):  # noqa: F811
    """Beta's row names one of Alpha's rows. Whatever that key does on delete, resetting
    Alpha would delete, change or trip over Beta's row, so it is refused, naming the
    table, and nothing is deleted."""
    import uuid

    from sqlalchemy import text

    ta, tb = await _two_companies(real_client)
    alpha, beta = await _id(real_client, ta), await _id(real_client, tb)
    async with real_engine.begin() as conn:
        await conn.execute(text(_ITEMS))
        await conn.execute(text(
            "CREATE TABLE ext_links (id uuid PRIMARY KEY, company_id uuid NOT NULL REFERENCES companies(id), "
            f"item_id uuid REFERENCES ext_items(id) ON DELETE {action}, label text NOT NULL)"))
        item = uuid.uuid4()
        await conn.execute(text("INSERT INTO ext_items VALUES (:i, :c, 'alpha item')"), {"i": item, "c": alpha})
        await conn.execute(text("INSERT INTO ext_links VALUES (:i, :c, :t, 'beta link')"),
                           {"i": uuid.uuid4(), "c": beta, "t": item})
    try:
        await _refused_untouched(real_client, real_engine, ta, alpha, ("ext_items", "ext_links"), "ext_links")
    finally:
        async with real_engine.begin() as conn:
            await conn.execute(text("DROP TABLE IF EXISTS ext_links, ext_items"))


@pytest.mark.parametrize("action", ["SET NULL", "SET DEFAULT"])
async def test_a_reset_is_refused_while_another_companys_row_would_have_a_key_cleared(
        real_client, real_engine, action):  # noqa: F811
    """A module row with no company column names Alpha and Beta through keys that clear
    on delete. Resetting Alpha would change a row Beta still has, so it is refused,
    naming the table, and nothing is cleared."""
    import uuid

    from sqlalchemy import text

    ta, tb = await _two_companies(real_client)
    alpha, beta = await _id(real_client, ta), await _id(real_client, tb)
    async with real_engine.begin() as conn:
        await conn.execute(text(_TRANSFERS.format(action=action)))
        await conn.execute(text("INSERT INTO ext_transfers VALUES (:i, :a, :b, 'alpha to beta')"),
                           {"i": uuid.uuid4(), "a": alpha, "b": beta})
    try:
        await _refused_untouched(real_client, real_engine, ta, alpha, ("ext_transfers",), "ext_transfers")
    finally:
        async with real_engine.begin() as conn:
            await conn.execute(text("DROP TABLE IF EXISTS ext_transfers"))


@pytest.mark.parametrize("action, beta_action", [
    ("CASCADE", "CASCADE"), ("NO ACTION", "NO ACTION"), ("CASCADE", "SET NULL"), ("CASCADE", "SET DEFAULT")])
async def test_a_reset_is_refused_while_a_row_hangs_off_both_companies(
        real_client, real_engine, action, beta_action):  # noqa: F811
    """A row with no company column hangs off one of Alpha's rows and names one of Beta's,
    by a key of any kind. It is Beta's as much as Alpha's, so resetting Alpha is refused,
    naming its table."""
    import uuid

    from sqlalchemy import text

    ta, tb = await _two_companies(real_client)
    alpha, beta = await _id(real_client, ta), await _id(real_client, tb)
    async with real_engine.begin() as conn:
        await conn.execute(text(_ITEMS))
        await conn.execute(text(
            "CREATE TABLE ext_pairs (id uuid PRIMARY KEY, "
            f"left_item uuid NOT NULL REFERENCES ext_items(id) ON DELETE {action}, "
            f"right_item uuid REFERENCES ext_items(id) ON DELETE {beta_action})"))
        mine, theirs = uuid.uuid4(), uuid.uuid4()
        await conn.execute(text("INSERT INTO ext_items VALUES (:i, :c, 'alpha item'), (:j, :d, 'beta item')"),
                           {"i": mine, "c": alpha, "j": theirs, "d": beta})
        await conn.execute(text("INSERT INTO ext_pairs VALUES (:i, :a, :b)"),
                           {"i": uuid.uuid4(), "a": mine, "b": theirs})
    try:
        await _refused_untouched(real_client, real_engine, ta, alpha, ("ext_items", "ext_pairs"), "ext_pairs")
    finally:
        async with real_engine.begin() as conn:
            await conn.execute(text("DROP TABLE IF EXISTS ext_pairs, ext_items"))


@pytest.mark.parametrize("action", ["CASCADE", "NO ACTION"])
async def test_a_reset_is_refused_naming_users_while_a_user_of_another_company_hangs_off_it(
        real_client, real_engine, action):  # noqa: F811
    """A module column on users names each user's home company, and the owner, still
    Beta's, has Alpha as home. Beta's own records name the owner as they always do; the
    row tying the owner to Alpha is in users, so the refusal names users."""
    from sqlalchemy import text

    ta, tb = await _two_companies(real_client)
    alpha = await _id(real_client, ta)
    async with real_engine.begin() as conn:
        await conn.execute(text(f"ALTER TABLE users ADD COLUMN ext_home uuid REFERENCES companies(id) ON DELETE {action}"))
        await conn.execute(text("UPDATE users SET ext_home = :a WHERE email = :e"), {"a": alpha, "e": OWNER_EMAIL})
    try:
        await _refused_untouched(real_client, real_engine, ta, alpha, ("users", "user_companies"), "users")
    finally:
        async with real_engine.begin() as conn:
            await conn.execute(text("ALTER TABLE users DROP COLUMN IF EXISTS ext_home"))


def _commit_elsewhere(*statements: str, **params):
    """Start committing ``statements`` on a connection of its own, in another thread. The
    returned dict gets ``committed``, or the ``error`` the commit raised."""
    import threading

    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    from migration_support import DATABASE_URL

    outcome: dict = {}

    def run():
        async def go():
            engine = create_async_engine(DATABASE_URL)
            try:
                async with engine.begin() as conn:
                    for statement in statements:
                        await conn.execute(text(statement), params)
                outcome["committed"] = True
            except Exception as exc:  # noqa: BLE001 - the outcome under test
                outcome["error"] = exc
            finally:
                await engine.dispose()
        asyncio.run(go())

    writer = threading.Thread(target=run)
    writer.start()
    return writer, outcome


@pytest.mark.parametrize("noted", ["alpha", "beta"])
async def test_a_row_hanging_off_a_note_whose_key_clears_is_held_only_by_the_company_it_names(
        real_client, real_engine, noted):  # noqa: F811
    """A note (no company column) names an item by a key that clears, and a tag names
    Alpha's item and that note. With the note naming Alpha's item too, everything is
    Alpha's: the reset deletes the tag and keeps the note with its key cleared. With the
    note naming Beta's item, the tag is Beta's as much as Alpha's: the reset is refused."""
    import uuid

    from sqlalchemy import text

    ta, tb = await _two_companies(real_client)
    alpha, beta = await _id(real_client, ta), await _id(real_client, tb)
    mine, theirs, note = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with real_engine.begin() as conn:
        await conn.execute(text(_ITEMS))
        await conn.execute(text("CREATE TABLE ext_notes (id uuid PRIMARY KEY, "
                                "item_id uuid REFERENCES ext_items(id) ON DELETE SET NULL)"))
        await conn.execute(text("CREATE TABLE ext_note_tags (id uuid PRIMARY KEY, "
                                "item_id uuid REFERENCES ext_items(id) ON DELETE CASCADE, "
                                "note_id uuid REFERENCES ext_notes(id) ON DELETE CASCADE)"))
        await conn.execute(text("INSERT INTO ext_items VALUES (:i, :a, 'alpha item'), (:j, :b, 'beta item')"),
                           {"i": mine, "a": alpha, "j": theirs, "b": beta})
        await conn.execute(text("INSERT INTO ext_notes VALUES (:n, :i)"),
                           {"n": note, "i": mine if noted == "alpha" else theirs})
        await conn.execute(text("INSERT INTO ext_note_tags VALUES (gen_random_uuid(), :i, :n)"), {"i": mine, "n": note})
    try:
        if noted == "beta":
            await _refused_untouched(real_client, real_engine, ta, alpha,
                                     ("ext_items", "ext_notes", "ext_note_tags"), "ext_note_tags")
            return

        r = await _reset(real_client, ta, "Alpha Co")

        assert r.status_code == 200, r.text
        assert await count(real_engine, "ext_note_tags") == 0
        assert await _rows(real_engine, "ext_notes") == [(note, None)]
    finally:
        async with real_engine.begin() as conn:
            await conn.execute(text("DROP TABLE IF EXISTS ext_note_tags, ext_notes, ext_items"))


async def test_a_row_another_company_adds_during_the_reset_is_never_lost(
        real_client, real_engine, monkeypatch):  # noqa: F811
    """Beta adds a row naming Alpha's item after the reset has checked for such rows and
    before it deletes. Beta's write waits for the reset and then fails on the missing
    item; it never commits only to be deleted with Alpha."""
    import uuid

    from sqlalchemy import text

    from celerp.routers import system

    ta, tb = await _two_companies(real_client)
    alpha, beta = await _id(real_client, ta), await _id(real_client, tb)
    item = uuid.uuid4()
    async with real_engine.begin() as conn:
        await conn.execute(text(_ITEMS))
        await conn.execute(text(
            "CREATE TABLE ext_links (id uuid PRIMARY KEY, company_id uuid NOT NULL REFERENCES companies(id), "
            "item_id uuid REFERENCES ext_items(id) ON DELETE CASCADE, label text NOT NULL)"))
        await conn.execute(text("INSERT INTO ext_items VALUES (:i, :c, 'alpha item')"), {"i": item, "c": alpha})
    deletes = system._company_deletes
    started: dict = {}

    def after_the_check(schema):
        started["writer"], started["outcome"] = _commit_elsewhere(
            "INSERT INTO ext_links VALUES (gen_random_uuid(), :c, :t, 'beta link')", c=beta, t=item)
        started["writer"].join(timeout=3)
        return deletes(schema)

    monkeypatch.setattr(system, "_company_deletes", after_the_check)
    try:
        r = await _reset(real_client, ta, "Alpha Co")
        started["writer"].join()
        beta_write = started["outcome"]

        assert r.status_code == 200, r.text
        assert "committed" not in beta_write and "ext_links_item_id_fkey" in str(beta_write["error"])
        assert await count(real_engine, "ext_links") == 0
    finally:
        async with real_engine.begin() as conn:
            await conn.execute(text("DROP TABLE IF EXISTS ext_links, ext_items"))


async def test_a_table_added_before_the_reset_locks_is_refused_as_busy(
        real_client, real_engine, monkeypatch):  # noqa: F811
    """A module adds a table naming Alpha's items after the reset read the tables and
    before it locked them, and Beta links one of Alpha's items. The reset no longer
    knows every table, so it is refused as busy and Beta's link stays."""
    import uuid

    from sqlalchemy import text

    from celerp.routers import system

    ta, tb = await _two_companies(real_client)
    alpha, beta = await _id(real_client, ta), await _id(real_client, tb)
    item = uuid.uuid4()
    async with real_engine.begin() as conn:
        await conn.execute(text(_ITEMS))
        await conn.execute(text("INSERT INTO ext_items VALUES (:i, :c, 'alpha item')"), {"i": item, "c": alpha})
    lock = system._lock_writers

    def before_the_locks(schema):
        writer, _ = _commit_elsewhere(
            "CREATE TABLE ext_links (id uuid PRIMARY KEY, company_id uuid NOT NULL REFERENCES companies(id), "
            "item_id uuid REFERENCES ext_items(id) ON DELETE CASCADE)",
            "INSERT INTO ext_links VALUES (gen_random_uuid(), :b, :t)", b=beta, t=item)
        writer.join()
        return lock(schema)

    monkeypatch.setattr(system, "_lock_writers", before_the_locks)
    try:
        held = await _held(real_engine, alpha)

        r = await _reset(real_client, ta, "Alpha Co")

        _refused_busy(r)
        assert await _held(real_engine, alpha) == held
        assert await count(real_engine, "ext_links", "company_id = :b AND item_id = :t", b=beta, t=item) == 1
    finally:
        async with real_engine.begin() as conn:
            await conn.execute(text("DROP TABLE IF EXISTS ext_links, ext_items"))


@pytest.mark.parametrize("table, key, names", [
    ("ext_partners", "partner_id uuid REFERENCES companies(id) ON DELETE CASCADE", "alpha"),
    ("ext_seats", "user_id uuid REFERENCES users(id) ON DELETE CASCADE", "clerk")], ids=["company", "user"])
async def test_a_table_added_while_the_reset_holds_its_locks_waits_for_it(
        real_client, real_engine, monkeypatch, table, key, names):  # noqa: F811
    """While the reset holds its locks, a module adds a table whose key names Alpha or
    the clerk, Alpha's only user, and Beta writes a row naming them. The new table waits
    for the reset; Beta's row then fails on the row that is gone and is never deleted
    with Alpha."""
    from sqlalchemy import text

    from celerp.routers import system

    ta, tb = await _two_companies(real_client)
    alpha, beta = await _id(real_client, ta), await _id(real_client, tb)
    async with real_engine.begin() as conn:
        clerk = (await conn.execute(text("SELECT id FROM users WHERE email = 'clerk@example.com'"))).scalar_one()
    check = system._held_elsewhere
    started: dict = {}

    def after_the_locks(schema):
        started["writer"], started["outcome"] = _commit_elsewhere(
            f"CREATE TABLE {table} (id uuid PRIMARY KEY, company_id uuid NOT NULL REFERENCES companies(id), {key})",
            f"INSERT INTO {table} VALUES (gen_random_uuid(), :b, :n)", b=beta,
            n={"alpha": alpha, "clerk": clerk}[names])
        started["writer"].join(timeout=3)
        return check(schema)

    monkeypatch.setattr(system, "_held_elsewhere", after_the_locks)
    try:
        r = await _reset(real_client, ta, "Alpha Co")
        started["writer"].join()

        assert r.status_code == 200, r.text
        assert "committed" not in started["outcome"], started["outcome"]
        assert "violates foreign key constraint" in str(started["outcome"]["error"])
    finally:
        async with real_engine.begin() as conn:
            await conn.execute(text(f"DROP TABLE IF EXISTS {table}"))


def _refused_busy(r) -> None:
    """The reset was refused because other work was saving at the same time."""
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["message_key"] == "system.factory_reset.busy", detail
    assert detail["message"] == ("This company could not be reset because other changes were being saved "
                                 "at the same time. Nothing was deleted. Try again.")
    assert in_language("de", detail) != detail["message"]


async def test_a_reset_waiting_too_long_behind_another_writer_is_refused_as_busy(real_client, real_engine):  # noqa: F811
    """Beta's transaction has written ext_links and stays open for longer than a request
    may wait for a lock. The reset gives up waiting and is refused as busy, never a
    server error, and nothing of Alpha is deleted."""
    import uuid

    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    from migration_support import DATABASE_URL

    ta, tb = await _two_companies(real_client)
    alpha, beta = await _id(real_client, ta), await _id(real_client, tb)
    async with real_engine.begin() as conn:
        await conn.execute(text("CREATE TABLE ext_links (id uuid PRIMARY KEY, "
                                "company_id uuid NOT NULL REFERENCES companies(id))"))
    beta_engine = create_async_engine(DATABASE_URL)
    try:
        held = await _held(real_engine, alpha)
        async with beta_engine.begin() as beta_conn:
            await beta_conn.execute(text("INSERT INTO ext_links VALUES (:i, :c)"), {"i": uuid.uuid4(), "c": beta})
            r = await _reset(real_client, ta, "Alpha Co")

        _refused_busy(r)
        assert await count(real_engine, "companies", "id = :i", i=alpha) == 1
        assert await _held(real_engine, alpha) == held
        assert await count(real_engine, "ext_links", "company_id = :c", c=beta) == 1
    finally:
        await beta_engine.dispose()
        async with real_engine.begin() as conn:
            await conn.execute(text("DROP TABLE IF EXISTS ext_links"))


async def test_a_reset_caught_in_a_deadlock_is_refused_and_deletes_nothing(real_client, real_engine):  # noqa: F811
    """Beta is part way through a transaction that wrote ext_links when the reset starts
    locking: the reset waits for ext_links while holding ext_items, and Beta then writes
    ext_items. Postgres aborts the reset to break the deadlock. The reset is refused as
    interrupted, without claiming another company holds the records, nothing of Alpha
    is deleted, and Beta's transaction commits."""
    import uuid

    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    from migration_support import DATABASE_URL

    ta, tb = await _two_companies(real_client)
    alpha, beta = await _id(real_client, ta), await _id(real_client, tb)
    async with real_engine.begin() as conn:
        await conn.execute(text(_ITEMS))
        await conn.execute(text(
            "CREATE TABLE ext_links (id uuid PRIMARY KEY, company_id uuid NOT NULL REFERENCES companies(id), "
            "item_id uuid REFERENCES ext_items(id), label text NOT NULL)"))
        await conn.execute(text("INSERT INTO ext_items VALUES (:i, :c, 'alpha item')"), {"i": uuid.uuid4(), "c": alpha})
    beta_engine = create_async_engine(DATABASE_URL)
    try:
        held = await _held(real_engine, alpha)
        async with beta_engine.begin() as beta_conn:
            await beta_conn.execute(text("INSERT INTO ext_links VALUES (:i, :c, NULL, 'beta link')"),
                                    {"i": uuid.uuid4(), "c": beta})
            reset = asyncio.create_task(_reset(real_client, ta, "Alpha Co"))
            for _ in range(500):
                await asyncio.sleep(0.01)
                async with real_engine.connect() as conn:
                    if await conn.scalar(text(
                            "SELECT count(*) FROM pg_locks l JOIN pg_class c ON c.oid = l.relation "
                            "WHERE NOT l.granted AND c.relname = 'ext_links'")):
                        break
            else:
                pytest.fail("the reset never waited for ext_links")
            await beta_conn.execute(text("INSERT INTO ext_items VALUES (:i, :c, 'beta item')"),
                                    {"i": uuid.uuid4(), "c": beta})
        r = await reset

        _refused_busy(r)
        assert await count(real_engine, "companies", "id = :i", i=alpha) == 1
        assert await _held(real_engine, alpha) == held
        assert await count(real_engine, "ext_items", "company_id = :c", c=alpha) == 1
        assert await count(real_engine, "ext_items", "company_id = :c", c=beta) == 1
        assert await count(real_engine, "ext_links", "company_id = :c", c=beta) == 1
    finally:
        await beta_engine.dispose()
        async with real_engine.begin() as conn:
            await conn.execute(text("DROP TABLE IF EXISTS ext_links, ext_items"))


async def test_a_reset_is_refused_when_tables_refer_to_each_other_in_a_loop(real_client, real_engine):  # noqa: F811
    """Two module tables name each other, so no delete order exists. The reset is refused
    naming both, and nothing is deleted."""
    import uuid

    from sqlalchemy import text

    ta, _ = await _two_companies(real_client)
    alpha = await _id(real_client, ta)
    async with real_engine.begin() as conn:
        await conn.execute(text("CREATE TABLE ext_a (id uuid PRIMARY KEY, "
                                "company_id uuid NOT NULL REFERENCES companies(id), b_id uuid)"))
        await conn.execute(text("CREATE TABLE ext_b (id uuid PRIMARY KEY, "
                                "company_id uuid NOT NULL REFERENCES companies(id), a_id uuid REFERENCES ext_a(id))"))
        await conn.execute(text("ALTER TABLE ext_a ADD FOREIGN KEY (b_id) REFERENCES ext_b(id)"))
        a, b = uuid.uuid4(), uuid.uuid4()
        await conn.execute(text("INSERT INTO ext_a VALUES (:a, :c, NULL)"), {"a": a, "c": alpha})
        await conn.execute(text("INSERT INTO ext_b VALUES (:b, :c, :a)"), {"b": b, "c": alpha, "a": a})
        await conn.execute(text("UPDATE ext_a SET b_id = :b"), {"b": b})
    try:
        before = {t: await _rows(real_engine, t) for t in ("ext_a", "ext_b")}
        held = await _held(real_engine, alpha)

        r = await _reset(real_client, ta, "Alpha Co")

        assert r.status_code == 409, r.text
        detail = r.json()["detail"]
        assert (detail["message_key"], detail["params"]) == (
            "system.factory_reset.reference_cycle", {"tables": "ext_a, ext_b"})
        assert await count(real_engine, "companies", "id = :i", i=alpha) == 1
        assert await _held(real_engine, alpha) == held
        assert {t: await _rows(real_engine, t) for t in before} == before
    finally:
        async with real_engine.begin() as conn:
            await conn.execute(text("ALTER TABLE IF EXISTS ext_a DROP COLUMN IF EXISTS b_id"))
            await conn.execute(text("DROP TABLE IF EXISTS ext_b, ext_a"))


async def test_a_row_naming_a_table_in_another_schema_goes_with_the_company(real_client, real_engine):  # noqa: F811
    """Alpha's and Beta's rows name a row of a table kept in another schema, which has the
    name and the key of one of Alpha's tables in Celerp's own. Beta's row names nothing
    of Alpha's: resetting Alpha deletes Alpha's rows only, and the row they named stays."""
    import uuid

    from sqlalchemy import text

    ta, tb = await _two_companies(real_client)
    alpha, beta = await _id(real_client, ta), await _id(real_client, tb)
    thing = uuid.uuid4()
    async with real_engine.begin() as conn:
        await conn.execute(text("CREATE SCHEMA ext"))
        await conn.execute(text("CREATE TABLE ext.things (id uuid PRIMARY KEY)"))
        await conn.execute(text("CREATE TABLE things (id uuid PRIMARY KEY, company_id uuid NOT NULL "
                                "REFERENCES companies(id))"))
        await conn.execute(text("CREATE TABLE ext_refs (id uuid PRIMARY KEY, company_id uuid NOT NULL "
                                "REFERENCES companies(id), thing_id uuid REFERENCES ext.things(id))"))
        await conn.execute(text("INSERT INTO ext.things VALUES (:t)"), {"t": thing})
        await conn.execute(text("INSERT INTO things VALUES (:t, :a)"), {"t": thing, "a": alpha})
        await conn.execute(text("INSERT INTO ext_refs VALUES (gen_random_uuid(), :a, :t), (gen_random_uuid(), :b, :t)"),
                           {"a": alpha, "b": beta, "t": thing})
    try:
        r = await _reset(real_client, ta, "Alpha Co")

        assert r.status_code == 200, r.text
        assert await count(real_engine, "ext_refs", "company_id = :a", a=alpha) == 0
        assert await count(real_engine, "ext_refs", "company_id = :b", b=beta) == 1
        assert await count(real_engine, "things") == 0
        assert await count(real_engine, "ext.things", "id = :t", t=thing) == 1
    finally:
        async with real_engine.begin() as conn:
            await conn.execute(text("DROP TABLE IF EXISTS ext_refs, things"))
            await conn.execute(text("DROP SCHEMA IF EXISTS ext CASCADE"))


_PARTITIONED = (
    "CREATE TABLE ext_events (id uuid NOT NULL, PRIMARY KEY (id)) PARTITION BY HASH (id)",
    "CREATE TABLE ext_events_p0 PARTITION OF ext_events FOR VALUES WITH (MODULUS 2, REMAINDER 0)",
    "CREATE TABLE ext_events_p1 PARTITION OF ext_events FOR VALUES WITH (MODULUS 2, REMAINDER 1)",
    "CREATE TABLE ext_event_refs (id uuid PRIMARY KEY, event_id uuid REFERENCES ext_events(id) ON DELETE CASCADE)")


async def test_a_cascading_key_into_a_partitioned_table_does_not_stop_the_reset(real_client, real_engine):  # noqa: F811
    """A module keeps a partitioned table and a table referring to it by a cascading key.
    Postgres keeps a copy of that key for each partition; the reset reads the key once, on
    the partitioned table, and goes through."""
    from sqlalchemy import text

    ta, _ = await _two_companies(real_client)
    alpha = await _id(real_client, ta)
    async with real_engine.begin() as conn:
        for statement in _PARTITIONED:
            await conn.execute(text(statement))
    try:
        r = await _reset(real_client, ta, "Alpha Co")

        assert r.status_code == 200, r.text
        assert await count(real_engine, "companies", "id = :a", a=alpha) == 0
    finally:
        async with real_engine.begin() as conn:
            await conn.execute(text("DROP TABLE IF EXISTS ext_event_refs, ext_events"))


async def test_a_reset_is_refused_while_a_table_in_another_schema_names_its_tables(
        real_client, real_engine):  # noqa: F811
    """A table kept in another schema names Alpha by a key into Celerp's own tables. The
    reset cannot see whose rows those are, so it is refused naming that table, and
    nothing is deleted."""
    import uuid

    from sqlalchemy import text

    ta, _ = await _two_companies(real_client)
    alpha = await _id(real_client, ta)
    async with real_engine.begin() as conn:
        await conn.execute(text("CREATE SCHEMA ext"))
        await conn.execute(text("CREATE TABLE ext.notes (id uuid PRIMARY KEY, "
                                "company_id uuid NOT NULL REFERENCES public.companies(id))"))
        await conn.execute(text("INSERT INTO ext.notes VALUES (:i, :c)"), {"i": uuid.uuid4(), "c": alpha})
    try:
        held = await _held(real_engine, alpha)

        r = await _reset(real_client, ta, "Alpha Co")

        assert r.status_code == 409, r.text
        detail = r.json()["detail"]
        assert detail["message_key"] == "system.factory_reset.outside_reference", detail
        assert detail["message"] == ("This company cannot be reset because ext.notes, a table outside "
                                     "Celerp's own, refers to Celerp's records. Nothing was deleted.")
        assert "ext.notes" in in_language("de", detail) != detail["message"]
        assert await _held(real_engine, alpha) == held
        assert await count(real_engine, "ext.notes") == 1
    finally:
        async with real_engine.begin() as conn:
            await conn.execute(text("DROP SCHEMA IF EXISTS ext CASCADE"))


@pytest.mark.parametrize("typed", [None, "", "RESET", "alpha co", "Alpha Co "])
async def test_reset_needs_the_exact_company_name(real_client, real_engine, typed):  # noqa: F811
    token = await _register(real_client, "Alpha Co")
    cid = await _id(real_client, token)
    before = await _held(real_engine, cid)

    r = await _reset(real_client, token, typed)

    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert detail["message_key"] == "system.factory_reset.name_mismatch", detail
    assert detail["message"] == "Type the company name exactly as shown to reset this company."
    assert in_language("de", detail) != detail["message"]
    assert await count(real_engine, "companies", "id = :i", i=cid) == 1
    assert await _held(real_engine, cid) == before


# --- The settings page: the modal names the company and the typed name is what is sent ---


@pytest.mark.parametrize("lang", ["en", "de"])
async def test_the_reset_modal_names_the_company_to_type(ui, real_client, lang):  # noqa: F811
    token = await _register(real_client, "Alpha & Sons")
    ui.cookies.set("celerp_token", token)
    ui.cookies.set("celerp_lang", lang)

    r = await ui.get("/settings/general?tab=company")
    page = _page(r)

    assert "<strong>Alpha & Sons</strong>" in page
    assert 'name="confirm_name" data-expected="Alpha &amp; Sons"' in r.text
    assert 'hx-include="#factory-reset-confirm-input"' in page
    assert "RESET" not in page


@pytest.mark.parametrize("lang", ["en", "de"])
async def test_a_wrong_name_typed_in_the_modal_resets_nothing(ui, real_client, real_engine, lang):  # noqa: F811
    token = await _register(real_client, "Alpha Co")
    cid = await _id(real_client, token)
    before = await _held(real_engine, cid)
    ui.cookies.set("celerp_token", token)
    ui.cookies.set("celerp_lang", lang)

    r = await ui.post("/settings/factory-reset", data={"confirm_name": "RESET"}, headers={"HX-Request": "true"})

    refusal = {"message": "Type the company name exactly as shown to reset this company.",
               "message_key": "system.factory_reset.name_mismatch", "params": {}}
    assert in_language(lang, refusal) in _page(r)
    assert "HX-Redirect" not in r.headers
    assert await _held(real_engine, cid) == before


async def test_the_name_typed_in_the_modal_resets_the_company(ui, real_client, real_engine):  # noqa: F811
    token = await _register(real_client, "Alpha Co")
    ui.cookies.set("celerp_token", token)

    r = await ui.post("/settings/factory-reset", data={"confirm_name": "Alpha Co"}, headers={"HX-Request": "true"})

    assert r.status_code == 200 and r.headers["HX-Redirect"] == "/setup", r.text
    assert await count(real_engine, "companies") == 0
