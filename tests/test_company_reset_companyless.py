# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""One login left without a company gets exactly one new company, however many requests
give it one at once, with the cloud relay present (so no single sign-in place is held).
Starting a company and restoring a backup race each other here on a real database: each
request that gets past the companyless check waits until the other request has either got
past it too or is waiting on the database for it, so the interleaving is the one under
test, not a lucky schedule. A request refused before the check (a second restore of the
same company's backups) settles it too."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
from sqlalchemy import text

from celerp.services.company_backup import OTHER_RESTORE_RUNNING

from company_backup_support import company, download, member, owner, token
from migration_support import OWNER_EMAIL, OWNER_PASSWORD, auth, count, real_client, real_engine  # noqa: F401
from test_company_reset import _local_files

pytestmark = pytest.mark.asyncio

NAME = "Harbor Goods Ltd"
START = "/auth/start-company"
READ = "/company-backups/start-company/read"
RESTORE = "/company-backups/start-company/restore"
HAS_COMPANY = "This login already has a company. Sign in instead."


def _relay(monkeypatch):
    """The cloud relay is present: sign-ins do not hold the installation's one place."""
    monkeypatch.setattr("celerp.gateway.state.get_session_token", MagicMock(return_value="relay-session"))


async def _waiting_on_a_lock(engine) -> bool:
    async with engine.connect() as conn:
        return bool(await conn.scalar(text(
            "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
            "AND wait_event_type = 'Lock'")))


def _check_together(monkeypatch, engine) -> list[int]:
    """Hold each request just past the companyless check until the other request has
    passed it as well, has answered, or is blocked by the database (inside the check, or
    before it while a restore holds the modules it turns on). Returns the list that
    ``_answered`` fills as each request answers."""
    from celerp.routers import auth as auth_router
    from celerp.services import company_backup
    from celerp.services.auth import hold_companyless_login as real
    passed: list[int] = []
    answered: list[int] = []

    async def hold(session, user_id):
        out = await real(session, user_id)
        passed.append(1)

        async def other_settled():
            while len(passed) + len(answered) < 2 and not await _waiting_on_a_lock(engine):
                await asyncio.sleep(0.02)

        await asyncio.wait_for(other_settled(), 20)
        return out

    monkeypatch.setattr(auth_router, "hold_companyless_login", hold)
    monkeypatch.setattr(company_backup, "hold_companyless_login", hold)
    return answered


async def _answered(answered: list[int], request):
    response = await request
    answered.append(1)
    return response


async def _companyless_owner_with_backups(client, engine, backups: int) -> tuple[str, list[bytes]]:
    """The owner's only company, backed up ``backups`` times and then reset."""
    boss = await owner(engine)
    a = await company(engine, boss, NAME, "alpha")
    tok = await token(engine, boss, a)
    data = [await download(client, tok) for _ in range(backups)]
    r = await client.post("/companies/me/reset", json={"company_name": NAME}, headers=auth(tok))
    assert r.status_code == 200, r.text
    assert await count(engine, "companies") == 0
    return boss, data


async def _preview(client, data: bytes) -> dict:
    r = await client.post(READ, files={"file": ("harbor.celerp-company", data)},
                          data={"email": OWNER_EMAIL, "password": OWNER_PASSWORD})
    assert r.status_code == 200, r.text
    body = r.json()
    return {"email": OWNER_EMAIL, "password": OWNER_PASSWORD, "upload_token": body["upload_token"],
            "plan_fingerprint": body["plan_fingerprint"]}


def _start(client, name: str):
    return client.post(START, json={"email": OWNER_EMAIL, "password": OWNER_PASSWORD, "company_name": name})


async def _one_company_for(engine, boss) -> None:
    assert await count(engine, "companies") == 1
    assert await count(engine, "user_companies", "user_id = :u", u=str(boss)) == 1


def _one_won(results, won: set[int], refusals: frozenset[str] = frozenset({HAS_COMPANY})) -> None:
    codes = sorted(r.status_code for r in results)
    assert codes in [sorted([w, 409]) for w in won], [r.text for r in results]
    [refusal] = [r.json()["detail"] for r in results if r.status_code == 409]
    assert refusal in refusals, refusal


async def test_two_starts_for_one_login_with_the_relay_make_one_company(real_client, real_engine,
                                                                        monkeypatch, tmp_path):
    _local_files(monkeypatch, tmp_path)
    boss, _ = await _companyless_owner_with_backups(real_client, real_engine, 0)
    _relay(monkeypatch)
    _check_together(monkeypatch, real_engine)

    results = await asyncio.gather(_start(real_client, "First Ltd"), _start(real_client, "Second Ltd"))

    _one_won(results, {200})
    await _one_company_for(real_engine, boss)


async def test_a_start_and_a_restore_for_one_login_with_the_relay_make_one_company(real_client, real_engine,
                                                                                   monkeypatch, tmp_path):
    _local_files(monkeypatch, tmp_path)
    boss, (data,) = await _companyless_owner_with_backups(real_client, real_engine, 1)
    confirm = await _preview(real_client, data)
    _relay(monkeypatch)
    _check_together(monkeypatch, real_engine)

    results = await asyncio.gather(_start(real_client, "Fresh Start Ltd"), real_client.post(RESTORE, json=confirm))

    _one_won(results, {200, 201})
    await _one_company_for(real_engine, boss)


async def test_two_restores_of_different_backups_for_one_login_with_the_relay_make_one_company(
        real_client, real_engine, monkeypatch, tmp_path):
    _local_files(monkeypatch, tmp_path)
    boss, (first, second) = await _companyless_owner_with_backups(real_client, real_engine, 2)
    confirms = [await _preview(real_client, first), await _preview(real_client, second)]
    _relay(monkeypatch)
    answered = _check_together(monkeypatch, real_engine)

    results = await asyncio.gather(*(_answered(answered, real_client.post(RESTORE, json=c)) for c in confirms))

    # Refused while the other restore runs, or once it has made the company.
    _one_won(results, {201}, frozenset({OTHER_RESTORE_RUNNING, HAS_COMPANY}))
    await _one_company_for(real_engine, boss)


async def test_a_membership_given_to_the_login_while_it_starts_a_company_waits_for_it(real_client, real_engine,
                                                                                      monkeypatch, tmp_path):
    from celerp.routers import auth as auth_router
    _local_files(monkeypatch, tmp_path)
    boss, _ = await _companyless_owner_with_backups(real_client, real_engine, 0)
    other = await owner(real_engine, "other@example.com", "Other")
    b = await company(real_engine, other, "Hillside Supply Co", "bravo")
    _relay(monkeypatch)
    reached, release = asyncio.Event(), asyncio.Event()
    real = auth_router.hold_companyless_login

    async def paused(session, user_id):
        out = await real(session, user_id)
        reached.set()
        await release.wait()
        return out

    monkeypatch.setattr(auth_router, "hold_companyless_login", paused)
    start = asyncio.create_task(_start(real_client, "Fresh Start Ltd"))
    await reached.wait()

    added = asyncio.create_task(member(real_engine, boss, b, "viewer"))

    async def settled():
        while not added.done() and not await _waiting_on_a_lock(real_engine):
            await asyncio.sleep(0.02)

    try:
        await asyncio.wait_for(settled(), 20)
        # The login's answer "no company" stays true until the start commits.
        assert not added.done()
    finally:
        release.set()
    assert (await start).status_code == 200
    await added
    assert await count(real_engine, "user_companies", "user_id = :u", u=str(boss)) == 2
