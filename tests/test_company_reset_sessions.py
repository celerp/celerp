# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Sign-in against a company reset, and the single sign-in place without the cloud relay,
under real concurrency on a real database. Each race pauses one request at a named point
and lets the other run, so the interleaving is the one under test, not a lucky schedule."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest
from sqlalchemy import text

from company_backup_support import company, member, owner, token
from migration_support import OWNER_EMAIL, auth, count, maker, real_client, real_engine  # noqa: F401

pytestmark = pytest.mark.asyncio

RESET = "/companies/me/reset"
PASSWORD = "userpw1234"
MEMBER_EMAIL = "member@example.com"
SECOND_EMAIL = "second@example.com"


def _local_files(monkeypatch, tmp_path):
    from celerp.config import settings
    from celerp.services import attachments
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(attachments, "_backend", attachments.LocalBackend())


def _pause_after(monkeypatch, module, name: str):
    """Pause the first call of ``module.name`` after it returns, until released."""
    reached, release = asyncio.Event(), asyncio.Event()
    real = getattr(module, name)

    async def paused(*args, **kwargs):
        out = await real(*args, **kwargs)
        if not reached.is_set():
            reached.set()
            await release.wait()
        return out

    monkeypatch.setattr(module, name, paused)
    return reached, release


def _meet_after(monkeypatch, module, name: str, parties: int = 2, wait: float = 1.0):
    """Hold each caller of ``module.name`` until ``parties`` callers arrive, or ``wait``
    seconds pass: requests that can run together all pass the point at once."""
    arrived: list[int] = []
    everyone = asyncio.Event()
    real = getattr(module, name)

    async def meet(*args, **kwargs):
        out = await real(*args, **kwargs)
        arrived.append(1)
        if len(arrived) >= parties:
            everyone.set()
        try:
            await asyncio.wait_for(everyone.wait(), wait)
        except TimeoutError:
            pass
        return out

    monkeypatch.setattr(module, name, meet)


def _direct_mode():
    """No cloud relay: one person signed in at a time."""
    return patch("celerp.gateway.state.get_session_token", return_value=None)


async def _harbor(engine):
    """Owner of Harbor Goods (A); a member of A who also works in Hillside Supply (B)."""
    boss = await owner(engine)
    a = await company(engine, boss, "Harbor Goods Ltd", "alpha")
    other = await owner(engine, SECOND_EMAIL, "Other")
    b = await company(engine, other, "Hillside Supply Co", "bravo")
    worker = await owner(engine, MEMBER_EMAIL, "Worker")
    await member(engine, worker, a, "admin")
    await member(engine, worker, b, "viewer")
    # A sign-in takes the member's first company: make that A.
    async with engine.begin() as conn:
        for cid, pk in ((a, "00000000-0000-0000-0000-00000000000a"), (b, "00000000-0000-0000-0000-00000000000b")):
            await conn.execute(text("UPDATE user_companies SET id = :pk WHERE user_id = :u AND company_id = :c"),
                               {"pk": pk, "u": worker, "c": cid})
    return boss, worker, a, b


async def _reset(client, engine, boss, a):
    return await client.post(RESET, json={"company_name": "Harbor Goods Ltd"},
                             headers=auth(await token(engine, boss, a)))


async def _sessions(engine, user_id) -> int:
    return await count(engine, "session_registry", "user_id = :u", u=str(user_id))


async def _signed_in(engine) -> set[str]:
    from celerp.services.session_tracker import active_user_ids
    async with maker(engine)() as s:
        return await active_user_ids(s)


def _company_of(body: dict) -> str:
    from celerp.services.auth import decode_access_token
    return decode_access_token(body["access_token"])["company_id"]


async def test_a_sign_in_that_picked_the_company_before_its_reset_lands_elsewhere(real_engine, real_client,
                                                                                  tmp_path, monkeypatch):
    from celerp.routers import auth as auth_router
    _local_files(monkeypatch, tmp_path)
    boss, worker, a, b = await _harbor(real_engine)
    # The member's first company is A: the sign-in picks it, then waits.
    reached, release = _pause_after(monkeypatch, auth_router, "first_usable_company_link")
    login = asyncio.create_task(real_client.post("/auth/login", json={"email": MEMBER_EMAIL, "password": PASSWORD}))
    await reached.wait()

    reset = await _reset(real_client, real_engine, boss, a)
    assert reset.status_code == 200, reset.text
    release.set()
    r = await login

    # No session is issued for the removed company: the sign-in lands on B instead.
    assert r.status_code == 200, r.text
    assert _company_of(r.json()) == str(b)
    assert await _sessions(real_engine, worker) == 1
    me = await real_client.get("/companies/me", headers=auth(r.json()["access_token"]))
    assert me.status_code == 200 and me.json()["id"] == str(b)


async def test_a_refresh_checked_before_the_reset_issues_nothing(real_engine, real_client, tmp_path, monkeypatch):
    from celerp.routers import auth as auth_router
    _local_files(monkeypatch, tmp_path)
    boss, worker, a, b = await _harbor(real_engine)
    first = await real_client.post("/auth/login", json={"email": MEMBER_EMAIL, "password": PASSWORD})
    assert _company_of(first.json()) == str(a)
    reached, release = _pause_after(monkeypatch, auth_router, "usable_company_link")
    refresh = asyncio.create_task(real_client.post(
        "/auth/token/refresh", json={"refresh_token": first.json()["refresh_token"]}))
    await reached.wait()

    reset = await _reset(real_client, real_engine, boss, a)
    assert reset.status_code == 200, reset.text
    release.set()
    r = await refresh

    assert r.status_code == 401
    assert await _sessions(real_engine, worker) == 0
    assert str(worker) not in await _signed_in(real_engine)


async def test_a_sign_in_issued_just_before_the_reset_ends_with_it(real_engine, real_client, tmp_path,
                                                                    monkeypatch):
    from celerp.services import session_tracker
    _local_files(monkeypatch, tmp_path)
    boss, worker, a, b = await _harbor(real_engine)
    # The sign-in has made its session for A and is about to save it when the reset starts.
    reached, release = _pause_after(monkeypatch, session_tracker, "register_token")
    login = asyncio.create_task(real_client.post("/auth/login", json={"email": MEMBER_EMAIL, "password": PASSWORD}))
    await reached.wait()
    reset = asyncio.create_task(_reset(real_client, real_engine, boss, a))
    await asyncio.sleep(0.5)
    release.set()
    r, done = await login, await reset

    assert done.status_code == 200, done.text
    assert r.status_code == 200 and _company_of(r.json()) == str(a)
    # The session for the removed company is gone and holds no sign-in place.
    assert (await real_client.get("/companies/me", headers=auth(r.json()["access_token"]))).status_code == 401
    assert await _sessions(real_engine, worker) == 0
    assert str(worker) not in await _signed_in(real_engine)


async def test_resetting_a_keeps_a_members_session_on_b(real_engine, real_client, tmp_path, monkeypatch):
    _local_files(monkeypatch, tmp_path)
    boss, worker, a, b = await _harbor(real_engine)
    first = await real_client.post("/auth/login", json={"email": MEMBER_EMAIL, "password": PASSWORD})
    on_b = await real_client.post(f"/auth/switch-company/{b}", headers=auth(first.json()["access_token"]))
    assert on_b.status_code == 200, on_b.text
    tok_b = on_b.json()["access_token"]
    assert await _sessions(real_engine, worker) == 2

    assert (await _reset(real_client, real_engine, boss, a)).status_code == 200

    me = await real_client.get("/companies/me", headers=auth(tok_b))
    assert me.status_code == 200 and me.json()["id"] == str(b)
    assert (await real_client.get("/companies/me", headers=auth(first.json()["access_token"]))).status_code == 401
    # Only the session on A ended; the one on B still holds its place.
    assert await _sessions(real_engine, worker) == 1
    assert str(worker) in await _signed_in(real_engine)


async def _orphans(engine):
    """Two logins whose companies were reset: neither has a company."""
    first = await owner(engine, MEMBER_EMAIL, "First")
    second = await owner(engine, SECOND_EMAIL, "Second")
    return first, second


async def test_two_logins_without_a_company_cannot_both_start_one_without_the_relay(real_engine, real_client,
                                                                                    monkeypatch):
    from celerp.routers import auth as auth_router
    await _orphans(real_engine)
    _meet_after(monkeypatch, auth_router, "provision_additional_company")
    with _direct_mode():
        results = await asyncio.gather(*(
            real_client.post("/auth/start-company", json={
                "email": email, "password": PASSWORD, "company_name": f"{email} Ltd"})
            for email in (MEMBER_EMAIL, SECOND_EMAIL)))

    assert sorted(r.status_code for r in results) == [200, 409], [r.text for r in results]
    assert [r.json()["detail"] for r in results if r.status_code == 409] == ["direct_connection_limit"]
    assert await count(real_engine, "companies") == 1
    assert len(await _signed_in(real_engine)) == 1


async def test_two_people_cannot_both_sign_in_without_the_relay(real_engine, real_client, monkeypatch):
    from celerp.routers import auth as auth_router
    boss = await owner(real_engine)
    a = await company(real_engine, boss, "Harbor Goods Ltd", "alpha")
    worker = await owner(real_engine, MEMBER_EMAIL, "Worker")
    await member(real_engine, worker, a, "admin")
    _meet_after(monkeypatch, auth_router, "first_usable_company_link")
    with _direct_mode():
        results = await asyncio.gather(
            real_client.post("/auth/login", json={"email": OWNER_EMAIL, "password": "ownerpw123"}),
            real_client.post("/auth/login", json={"email": MEMBER_EMAIL, "password": PASSWORD}))

    assert sorted(r.status_code for r in results) == [200, 409], [r.text for r in results]
    assert len(await _signed_in(real_engine)) == 1


async def test_taking_over_the_sign_in_place_leaves_one_person_signed_in(real_engine, real_client, monkeypatch):
    from celerp.routers import auth as auth_router
    boss = await owner(real_engine)
    a = await company(real_engine, boss, "Harbor Goods Ltd", "alpha")
    worker = await owner(real_engine, MEMBER_EMAIL, "Worker")
    await member(real_engine, worker, a, "admin")
    _meet_after(monkeypatch, auth_router, "first_usable_company_link")
    with _direct_mode():
        results = await asyncio.gather(
            real_client.post("/auth/login-force", json={"email": OWNER_EMAIL, "password": "ownerpw123"}),
            real_client.post("/auth/login", json={"email": MEMBER_EMAIL, "password": PASSWORD}))

    assert len(await _signed_in(real_engine)) == 1, [r.status_code for r in results]


async def test_taking_over_ends_a_session_this_server_has_already_checked(real_engine, real_client):
    boss = await owner(real_engine)
    a = await company(real_engine, boss, "Harbor Goods Ltd", "alpha")
    worker = await owner(real_engine, MEMBER_EMAIL, "Worker")
    await member(real_engine, worker, a, "admin")
    first = await real_client.post("/auth/login", json={"email": OWNER_EMAIL, "password": "ownerpw123"})
    seen = first.json()["access_token"]
    assert (await real_client.get("/companies/me", headers=auth(seen))).status_code == 200

    with _direct_mode():
        took = await real_client.post("/auth/login-force", json={"email": MEMBER_EMAIL, "password": PASSWORD})
    assert took.status_code == 200, took.text

    assert (await real_client.get("/companies/me", headers=auth(seen))).status_code == 401
    assert await _signed_in(real_engine) == {str(worker)}


async def test_with_the_relay_several_people_sign_in_at_once(real_engine, real_client, monkeypatch):
    from celerp.routers import auth as auth_router
    boss = await owner(real_engine)
    a = await company(real_engine, boss, "Harbor Goods Ltd", "alpha")
    worker = await owner(real_engine, MEMBER_EMAIL, "Worker")
    await member(real_engine, worker, a, "admin")
    await owner(real_engine, "third@example.com", "Third")
    _meet_after(monkeypatch, auth_router, "first_usable_company_link", parties=2, wait=2.0)
    results = await asyncio.gather(
        real_client.post("/auth/login", json={"email": OWNER_EMAIL, "password": "ownerpw123"}),
        real_client.post("/auth/login", json={"email": MEMBER_EMAIL, "password": PASSWORD}),
        real_client.post("/auth/start-company", json={
            "email": "third@example.com", "password": PASSWORD, "company_name": "Third Ltd"}))

    assert [r.status_code for r in results] == [200, 200, 200], [r.text for r in results]
    assert len(await _signed_in(real_engine)) == 3

