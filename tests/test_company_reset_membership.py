# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Which company a login lands on after a reset. A login counts as having a company only
where it can actually work: an active membership in an active company, or an owner's
membership in a deactivated or still-being-moved-in company. A membership it cannot use
never strands the login between a company it cannot open and a new one it may not start."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from company_backup_support import company, member, owner, token
from migration_support import OWNER_EMAIL, auth, count, maker, real_client, real_engine  # noqa: F401

pytestmark = pytest.mark.asyncio

RESET = "/companies/me/reset"
PASSWORD = "userpw1234"
MEMBER_EMAIL = "member@example.com"
OTHER_EMAIL = "other@example.com"


def _local_files(monkeypatch, tmp_path):
    from celerp.config import settings
    from celerp.services import attachments
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(attachments, "_backend", attachments.LocalBackend())


async def _deactivate(engine, cid) -> None:
    async with engine.begin() as conn:
        await conn.execute(text("UPDATE companies SET is_active = false WHERE id = :c"), {"c": cid})


async def _staged(engine, user_id, name: str):
    from celerp.models.company import User
    from celerp.services.provisioning import provision_migration_company
    async with maker(engine)() as s:
        made = await provision_migration_company(s, owner=await s.get(User, user_id), company_name=name)
        await s.commit()
        return made.id


async def _reset(client, engine, user_id, cid, name: str):
    r = await client.post(RESET, json={"company_name": name}, headers=auth(await token(engine, user_id, cid)))
    assert r.status_code == 200, r.text
    return r.json()


async def _login(client, email: str, password: str = PASSWORD):
    return await client.post("/auth/login", json={"email": email, "password": password})


def _company_of(body: dict) -> str:
    from celerp.services.auth import decode_access_token
    return decode_access_token(body["access_token"])["company_id"]


async def _signed_in(engine) -> set[str]:
    from celerp.services.session_tracker import active_user_ids
    async with maker(engine)() as s:
        return await active_user_ids(s)


@pytest.mark.parametrize("role", ["admin", "viewer"])
async def test_a_member_of_only_a_deactivated_company_can_start_over(real_engine, real_client, tmp_path,
                                                                     monkeypatch, role):
    _local_files(monkeypatch, tmp_path)
    boss = await owner(real_engine)
    a = await company(real_engine, boss, "Harbor Goods Ltd", "alpha")
    other = await owner(real_engine, OTHER_EMAIL, "Other")
    b = await company(real_engine, other, "Closed Shop Co", "bravo")
    await _deactivate(real_engine, b)
    worker = await owner(real_engine, MEMBER_EMAIL, "Worker")
    await member(real_engine, worker, a, role)
    await member(real_engine, worker, b, role)
    assert (await _login(real_client, MEMBER_EMAIL)).status_code == 200

    await _reset(real_client, real_engine, boss, a, "Harbor Goods Ltd")

    # The reset frees the place of a login that can no longer open any company.
    assert str(worker) not in await _signed_in(real_engine)
    login = await _login(real_client, MEMBER_EMAIL)
    assert login.status_code == 401 and login.json()["detail"] == "No active company membership"
    started = await real_client.post("/auth/start-company", json={
        "email": MEMBER_EMAIL, "password": PASSWORD, "company_name": "Fresh Start Ltd"})
    assert started.status_code == 200, started.text
    me = await real_client.get("/companies/me", headers=auth(started.json()["access_token"]))
    assert me.json()["name"] == "Fresh Start Ltd" and me.json()["current_role"] == "owner"
    # The closed company is untouched; its owner still has it.
    assert await count(real_engine, "companies", "id = :c AND is_active = false", c=str(b)) == 1


async def test_an_owner_keeps_their_deactivated_company(real_engine, real_client, tmp_path, monkeypatch):
    _local_files(monkeypatch, tmp_path)
    boss = await owner(real_engine)
    a = await company(real_engine, boss, "Harbor Goods Ltd", "alpha")
    b = await company(real_engine, boss, "Closed Shop Co", "bravo")
    await _deactivate(real_engine, b)

    body = await _reset(real_client, real_engine, boss, a, "Harbor Goods Ltd")

    assert _company_of(body) == str(b)
    assert _company_of((await _login(real_client, OWNER_EMAIL, "ownerpw123")).json()) == str(b)
    blocked = await real_client.post("/auth/start-company", json={
        "email": OWNER_EMAIL, "password": "ownerpw123", "company_name": "Another Ltd"})
    assert blocked.status_code == 409


async def test_a_member_of_another_active_company_lands_there(real_engine, real_client, tmp_path, monkeypatch):
    _local_files(monkeypatch, tmp_path)
    boss = await owner(real_engine)
    a = await company(real_engine, boss, "Harbor Goods Ltd", "alpha")
    other = await owner(real_engine, OTHER_EMAIL, "Other")
    b = await company(real_engine, other, "Hillside Supply Co", "bravo")
    worker = await owner(real_engine, MEMBER_EMAIL, "Worker")
    await member(real_engine, worker, a, "admin")
    await member(real_engine, worker, b, "viewer")

    await _reset(real_client, real_engine, boss, a, "Harbor Goods Ltd")

    login = await _login(real_client, MEMBER_EMAIL)
    assert login.status_code == 200 and _company_of(login.json()) == str(b)
    me = await real_client.get("/companies/me", headers=auth(login.json()["access_token"]))
    assert me.status_code == 200 and me.json()["current_role"] == "viewer"


async def test_an_owned_company_still_being_moved_in_is_where_the_owner_lands(real_engine, real_client, tmp_path,
                                                                              monkeypatch):
    _local_files(monkeypatch, tmp_path)
    boss = await owner(real_engine)
    a = await company(real_engine, boss, "Harbor Goods Ltd", "alpha")
    staged = await _staged(real_engine, boss, "Moving In Ltd")

    body = await _reset(real_client, real_engine, boss, a, "Harbor Goods Ltd")

    assert _company_of(body) == str(staged)
    tok = body["access_token"]
    # The session reaches the migration routes only, which is how the owner gets back to the move.
    assert (await real_client.get("/companies/me", headers=auth(tok))).status_code == 403
    # Authenticated, and no run has been started for it yet.
    assert (await real_client.get("/migrations/staged", headers=auth(tok))).status_code == 404
    assert _company_of((await _login(real_client, OWNER_EMAIL, "ownerpw123")).json()) == str(staged)


async def test_a_working_company_is_picked_before_one_still_being_moved_in(real_engine, real_client, tmp_path,
                                                                           monkeypatch):
    _local_files(monkeypatch, tmp_path)
    boss = await owner(real_engine)
    await _staged(real_engine, boss, "Moving In Ltd")
    a = await company(real_engine, boss, "Harbor Goods Ltd", "alpha")
    assert _company_of((await _login(real_client, OWNER_EMAIL, "ownerpw123")).json()) == str(a)


@pytest.mark.parametrize("closed", ["company", "membership"])
async def test_a_membership_the_login_cannot_use_never_blocks_starting_a_company(real_engine, real_client,
                                                                                 closed):
    other = await owner(real_engine, OTHER_EMAIL, "Other")
    b = await company(real_engine, other, "Closed Shop Co", "bravo")
    worker = await owner(real_engine, MEMBER_EMAIL, "Worker")
    await member(real_engine, worker, b, "admin", active=closed == "company")
    if closed == "company":
        await _deactivate(real_engine, b)

    login = await _login(real_client, MEMBER_EMAIL)
    assert login.status_code == 401 and login.json()["detail"] == "No active company membership"
    started = await real_client.post("/auth/start-company", json={
        "email": MEMBER_EMAIL, "password": PASSWORD, "company_name": "Fresh Start Ltd"})
    assert started.status_code == 200, started.text
    assert _company_of(started.json()) != str(b)
