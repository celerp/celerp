# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Reset this company: the owner removes the current company, its data, settings, chart of
accounts, attachments and memberships, on a real database. Other companies and every login
stay; the person resetting lands on another of their companies, or starts a new one."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import text

from company_backup_support import company, member, owner, snapshot, token
from migration_support import OWNER_EMAIL, auth, count, maker, real_client, real_engine  # noqa: F401

pytestmark = pytest.mark.asyncio

RESET = "/companies/me/reset"
SOLO_EMAIL = "solo@example.com"
SOLO_PASSWORD = "userpw1234"
# Rewritten on every token issue, so a reset that ends no session still touches them.
_AUTH_TABLES = {"user_auth_state", "session_registry"}


def _local_files(monkeypatch, tmp_path):
    from celerp.config import settings
    from celerp.services import attachments
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(attachments, "_backend", attachments.LocalBackend())


def _folder(tmp_path, cid):
    return tmp_path / "static" / "attachments" / str(cid)


async def _seed(engine, tmp_path, cid, user_id, marker: str) -> None:
    """A document, a work centre, a chart account, an assistant conversation with a message,
    a notification and an attachment file, on top of the item the company helper makes."""
    from celerp_accounting.models import Account

    from celerp.models.ai import AIConversation, AIMessage
    from celerp.models.company import WorkCenter
    from celerp.models.notification import Notification
    async with maker(engine)() as s:
        loc = await s.scalar(text("SELECT id FROM locations WHERE company_id = :c LIMIT 1"), {"c": cid})
        await s.execute(text(
            "INSERT INTO ledger (company_id, entity_id, entity_type, event_type, data, actor_id, location_id, "
            "source, idempotency_key) VALUES (:c, 'doc:1', 'doc', 'doc.created', CAST(:d AS json), :u, :l, 'api', :k)"),
            {"c": cid, "d": json.dumps({"doc_type": "invoice", "ref": marker}), "u": user_id, "l": loc,
             "k": f"doc-{marker}"})
        await s.execute(text(
            "INSERT INTO projections (company_id, entity_id, entity_type, state, version, location_id, updated_at) "
            "VALUES (:c, 'doc:1', 'doc', CAST(:d AS json), 1, :l, now())"),
            {"c": cid, "d": json.dumps({"doc_type": "invoice", "ref": marker}), "l": loc})
        conv = AIConversation(company_id=cid, user_id=user_id, title=marker)
        s.add_all([WorkCenter(company_id=cid, name=f"{marker} bench", wip_location_id=loc, is_default=True),
                   Account(company_id=cid, code="1111", name=f"{marker} cash", account_type="asset"),
                   Notification(company_id=cid, user_id=user_id, category="system", title=marker, body=""),
                   conv])
        await s.flush()
        s.add(AIMessage(conversation_id=conv.id, role="user", content=f"{marker} question"))
        await s.commit()
    folder = _folder(tmp_path, cid)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{marker}.pdf").write_bytes(marker.encode())


async def _two_companies(engine, tmp_path):
    """Companies A and B owned by the install owner, plus a second login that belongs to A only."""
    shared = await owner(engine)
    a = await company(engine, shared, "Harbor Goods Ltd", "alpha")
    b = await company(engine, shared, "Hillside Supply Co", "bravo")
    solo = await owner(engine, SOLO_EMAIL, "Solo")
    await member(engine, solo, a, "owner")
    await _seed(engine, tmp_path, a, shared, "alpha")
    await _seed(engine, tmp_path, b, shared, "bravo")
    return shared, solo, a, b


async def _company_tables(engine) -> list[str]:
    async with engine.connect() as conn:
        return list((await conn.execute(text(
            "SELECT c.table_name FROM information_schema.columns c JOIN information_schema.tables t "
            "ON t.table_schema = c.table_schema AND t.table_name = c.table_name "
            "WHERE c.table_schema = current_schema() AND c.column_name = 'company_id' "
            "AND t.table_type = 'BASE TABLE'"))).scalars().all())


def _without(before: dict, gone_id) -> dict:
    """The snapshot with every row that names ``gone_id`` removed."""
    return {t: [r for r in rows if str(gone_id) not in r] for t, rows in before.items()}


def _claims(access_token: str) -> dict:
    from celerp.services.auth import decode_access_token
    return decode_access_token(access_token)


async def test_reset_removes_only_this_company(real_engine, real_client, tmp_path, monkeypatch):
    _local_files(monkeypatch, tmp_path)
    shared, solo, a, b = await _two_companies(real_engine, tmp_path)
    tok = await token(real_engine, shared, a)
    solo_tok = await token(real_engine, solo, a)
    before = await snapshot(real_engine)

    r = await real_client.post(RESET, json={"company_name": "Harbor Goods Ltd"}, headers=auth(tok))

    assert r.status_code == 200, r.text
    body = r.json()
    # The shared login lands on its other company.
    assert _claims(body["access_token"])["company_id"] == str(b)
    after = await snapshot(real_engine)
    for table in await _company_tables(real_engine):
        assert await count(real_engine, table, "company_id = :c", c=str(a)) == 0, table
    assert await count(real_engine, "companies", "id = :c", c=str(a)) == 0
    # Child rows of the removed company (the assistant message) are gone, B's message stays.
    assert [json.loads(m)["content"] for m in after["ai_messages"]] == ["bravo question"]
    # Everything that does not belong to A is untouched: B, both logins, install state.
    expected = _without(before, a)
    expected["ai_messages"] = [m for m in expected["ai_messages"] if "alpha" not in m]
    for table in after:
        if table not in _AUTH_TABLES:
            assert after[table] == expected[table], table
    assert await count(real_engine, "users") == 2
    assert not _folder(tmp_path, a).exists()
    assert (_folder(tmp_path, b) / "bravo.pdf").read_bytes() == b"bravo"
    assert await count(real_engine, "migration_cleanup_tasks") == 0
    # Sessions on the removed company end; the new session works on B.
    assert (await real_client.get("/companies/me", headers=auth(tok))).status_code == 401
    assert (await real_client.get("/companies/me", headers=auth(solo_tok))).status_code == 401
    me = await real_client.get("/companies/me", headers=auth(body["access_token"]))
    assert me.status_code == 200 and me.json()["id"] == str(b)


async def test_a_only_login_keeps_its_login_and_starts_a_new_company(real_engine, real_client, tmp_path,
                                                                        monkeypatch):
    _local_files(monkeypatch, tmp_path)
    shared, solo, a, b = await _two_companies(real_engine, tmp_path)
    tok = await token(real_engine, shared, a)
    assert (await real_client.post(RESET, json={"company_name": "Harbor Goods Ltd"},
                                   headers=auth(tok))).status_code == 200

    login = await real_client.post("/auth/login", json={"email": SOLO_EMAIL, "password": SOLO_PASSWORD})
    assert login.status_code == 401
    assert login.json()["detail"] == "No active company membership"
    started = await real_client.post("/auth/start-company", json={
        "email": SOLO_EMAIL, "password": SOLO_PASSWORD, "company_name": "Fresh Start Ltd"})
    assert started.status_code == 200, started.text
    me = await real_client.get("/companies/me", headers=auth(started.json()["access_token"]))
    assert me.json()["name"] == "Fresh Start Ltd" and me.json()["current_role"] == "owner"
    # Once the login has a company again it signs in normally, and cannot start another this way.
    again = await real_client.post("/auth/start-company", json={
        "email": SOLO_EMAIL, "password": SOLO_PASSWORD, "company_name": "Second Ltd"})
    assert again.status_code == 409
    wrong = await real_client.post("/auth/start-company", json={
        "email": OWNER_EMAIL, "password": "wrong-password", "company_name": "Nope Ltd"})
    assert wrong.status_code == 401
    assert await count(real_engine, "companies", "name IN ('Second Ltd', 'Nope Ltd')") == 0


async def _signed_in(engine, *sessions) -> set[str]:
    """Register a live session for each (user, company) in *sessions*, then return everyone
    counted as signed in."""
    import uuid
    from datetime import datetime, timedelta, timezone

    from celerp.services.session_tracker import active_user_ids, register_token
    async with maker(engine)() as s:
        for user, company_id in sessions:
            await register_token(s, uuid.uuid4().hex, str(user), company_id,
                                 datetime.now(timezone.utc) + timedelta(hours=1))
        return await active_user_ids(s)


async def test_logins_left_without_a_company_no_longer_count_as_signed_in(real_engine, real_client, tmp_path,
                                                                          monkeypatch):
    """Without the cloud relay only one person may be signed in, so a login whose last
    company was reset must stop holding that place, or no one could sign in to start over."""
    _local_files(monkeypatch, tmp_path)
    shared, solo, a, b = await _two_companies(real_engine, tmp_path)
    assert await _signed_in(real_engine, (shared, a), (solo, a)) == {str(shared), str(solo)}

    r = await real_client.post(RESET, json={"company_name": "Harbor Goods Ltd"},
                               headers=auth(await token(real_engine, shared, a)))
    assert r.status_code == 200, r.text
    assert await _signed_in(real_engine) == {str(shared)}

    r = await real_client.post(RESET, json={"company_name": "Hillside Supply Co"},
                               headers=auth(r.json()["access_token"]))
    assert r.json() == {"next": "start_company"}
    assert await _signed_in(real_engine) == set()


async def test_resetting_the_last_company_starts_over(real_engine, real_client, tmp_path, monkeypatch):
    _local_files(monkeypatch, tmp_path)
    shared, solo, a, b = await _two_companies(real_engine, tmp_path)
    solo_only = await company(real_engine, solo, "Solo Trading", "solo")
    await real_client.post(RESET, json={"company_name": "Harbor Goods Ltd"},
                           headers=auth(await token(real_engine, shared, a)))

    r = await real_client.post(RESET, json={"company_name": "Solo Trading"},
                               headers=auth(await token(real_engine, solo, solo_only)))

    assert r.status_code == 200, r.text
    assert r.json() == {"next": "start_company"}
    assert await count(real_engine, "companies", "id = :c", c=str(solo_only)) == 0
    assert await count(real_engine, "users", "email = :e", e=SOLO_EMAIL) == 1
    assert await count(real_engine, "companies", "id = :c", c=str(b)) == 1


async def test_installation_upgrade_markers_survive_a_reset(real_engine, real_client, tmp_path, monkeypatch):
    """The upgrade markers table is created at runtime rather than by a model; it belongs
    to the installation, so a reset keeps it and its rows."""
    from celerp.migrations._data_reconcile import BACKFILL_VERSION_KEY, get_meta, set_meta
    _local_files(monkeypatch, tmp_path)
    shared, solo, a, b = await _two_companies(real_engine, tmp_path)
    async with real_engine.begin() as conn:
        await conn.run_sync(set_meta, BACKFILL_VERSION_KEY, "9.9.9")

    r = await real_client.post(RESET, json={"company_name": "Harbor Goods Ltd"},
                               headers=auth(await token(real_engine, shared, a)))

    assert r.status_code == 200, r.text
    async with real_engine.connect() as conn:
        assert await conn.run_sync(get_meta, BACKFILL_VERSION_KEY) == "9.9.9"


async def test_wrong_name_is_refused_and_nothing_changes(real_engine, real_client, tmp_path, monkeypatch):
    _local_files(monkeypatch, tmp_path)
    shared, solo, a, b = await _two_companies(real_engine, tmp_path)
    tok = await token(real_engine, shared, a)
    before = await snapshot(real_engine)

    for typed in ("harbor goods ltd", "Harbor Goods Ltd ", "Hillside Supply Co", ""):
        r = await real_client.post(RESET, json={"company_name": typed}, headers=auth(tok))
        assert r.status_code == 422, (typed, r.text)
        assert r.json()["detail"] == ("The name you typed does not match this company's name. "
                                      "Nothing was deleted.")
    assert (await real_client.post(RESET, json={}, headers=auth(tok))).status_code == 422

    assert await snapshot(real_engine) == before
    assert _folder(tmp_path, a).exists()


async def test_only_the_owner_can_reset(real_engine, real_client, tmp_path, monkeypatch):
    _local_files(monkeypatch, tmp_path)
    shared, solo, a, b = await _two_companies(real_engine, tmp_path)
    admin = await owner(real_engine, "admin@example.com", "Admin")
    await member(real_engine, admin, a, "admin")
    admin_token = await token(real_engine, admin, a, "admin")
    before = await snapshot(real_engine)

    r = await real_client.post(RESET, json={"company_name": "Harbor Goods Ltd"}, headers=auth(admin_token))
    assert r.status_code == 403
    assert (await real_client.post(RESET, json={"company_name": "Harbor Goods Ltd"})).status_code == 401
    assert await snapshot(real_engine) == before


async def test_a_failure_part_way_leaves_the_company_intact(real_engine, real_client, tmp_path, monkeypatch):
    _local_files(monkeypatch, tmp_path)
    shared, solo, a, b = await _two_companies(real_engine, tmp_path)
    tok = await token(real_engine, shared, a)
    # The company row is deleted last, so this fails after every other table was emptied.
    async with real_engine.begin() as conn:
        await conn.execute(text(
            "CREATE FUNCTION zz_refuse() RETURNS trigger AS $$ BEGIN RAISE EXCEPTION 'refused'; END $$ "
            "LANGUAGE plpgsql"))
        await conn.execute(text("CREATE TRIGGER zz_refuse BEFORE DELETE ON companies "
                                "FOR EACH ROW EXECUTE FUNCTION zz_refuse()"))
    try:
        before = await snapshot(real_engine)
        r = await real_client.post(RESET, json={"company_name": "Harbor Goods Ltd"}, headers=auth(tok))
        assert r.status_code == 500
        assert r.json()["detail"] == "The company could not be reset. Nothing was deleted."
        assert await snapshot(real_engine) == before
        assert (_folder(tmp_path, a) / "alpha.pdf").exists()
        assert (await real_client.get("/companies/me", headers=auth(tok))).status_code == 200
    finally:
        async with real_engine.begin() as conn:
            await conn.execute(text("DROP TRIGGER zz_refuse ON companies"))
            await conn.execute(text("DROP FUNCTION zz_refuse()"))


async def test_a_table_it_cannot_place_stops_the_reset_before_any_write(real_engine, real_client, tmp_path,
                                                                        monkeypatch):
    _local_files(monkeypatch, tmp_path)
    shared, solo, a, b = await _two_companies(real_engine, tmp_path)
    tok = await token(real_engine, shared, a)
    async with real_engine.begin() as conn:
        await conn.execute(text("CREATE TABLE zz_unplaced (id integer PRIMARY KEY, note text)"))
    try:
        before = await snapshot(real_engine)
        r = await real_client.post(RESET, json={"company_name": "Harbor Goods Ltd"}, headers=auth(tok))
        assert r.status_code == 409
        assert "zz_unplaced" in r.json()["detail"]
        assert r.json()["detail"].endswith("Nothing was deleted.")
        assert await snapshot(real_engine) == before
        assert _folder(tmp_path, a).exists()
    finally:
        async with real_engine.begin() as conn:
            await conn.execute(text("DROP TABLE zz_unplaced"))


async def test_a_connected_store_must_be_disconnected_first(real_engine, real_client, tmp_path, monkeypatch):
    _local_files(monkeypatch, tmp_path)
    shared, solo, a, b = await _two_companies(real_engine, tmp_path)
    tok = await token(real_engine, shared, a)
    from celerp.models.connector_config import ConnectorConfig
    async with maker(real_engine)() as s:
        s.add(ConnectorConfig(company_id=str(a), connector="shopify"))
        await s.commit()
    before = await snapshot(real_engine)

    r = await real_client.post(RESET, json={"company_name": "Harbor Goods Ltd"}, headers=auth(tok))

    assert r.status_code == 409
    assert r.json()["detail"] == ("Disconnect shopify before resetting this company. Nothing was deleted.")
    assert await snapshot(real_engine) == before


async def test_the_reset_everything_route_is_gone(real_engine, real_client):
    shared = await owner(real_engine)
    a = await company(real_engine, shared, "Harbor Goods Ltd", "alpha")
    r = await real_client.post("/system/factory-reset", headers=auth(await token(real_engine, shared, a)))
    assert r.status_code == 404
    from celerp.main import app
    assert not [p for p in (getattr(r, "path", "") for r in app.routes) if "factory-reset" in p]
    assert await count(real_engine, "companies") == 1


async def test_a_hidden_star_card_stays_hidden_after_a_reset(real_engine, real_client, tmp_path, monkeypatch):
    """Hiding the GitHub star card is for the whole installation, so resetting a company
    never brings it back for anyone."""
    from celerp.routers import stars

    async def no_relay(medium, lang):
        return None
    monkeypatch.setattr(stars, "get_star_cta", no_relay)
    _local_files(monkeypatch, tmp_path)
    shared, solo, a, b = await _two_companies(real_engine, tmp_path)
    assert (await real_client.post("/stars/dismiss", headers=auth(await token(real_engine, shared, a)))).status_code == 200

    r = await real_client.post(RESET, json={"company_name": "Harbor Goods Ltd"},
                               headers=auth(await token(real_engine, shared, a)))

    assert r.status_code == 200, r.text
    async with real_engine.connect() as conn:
        state = (await conn.execute(text("SELECT value FROM system_runtime_state"))).scalars().all()
    assert [s for s in state if (json.loads(s) if isinstance(s, str) else s).get("star_prompt_dismissed")], state
    cta = await real_client.get("/stars/cta", params={"medium": "dashboard"},
                                headers=auth(await token(real_engine, shared, b)))
    assert cta.status_code == 200, cta.text
    assert cta.json()["dismissed"] is True, cta.json()
