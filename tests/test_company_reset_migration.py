# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A login whose last company was reset moves its books in from another system instead of
starting an empty company: it signs in with its email and password at the upload and at
the start, and the migration creates its company, with no placeholder company before it."""

from __future__ import annotations

import asyncio
import base64
import re

import pytest
from sqlalchemy import text

from company_backup_support import company, owner, token
from migration_support import (  # noqa: F401
    OWNER_EMAIL,
    OWNER_PASSWORD,
    auth,
    count,
    fake_bytes,
    migrate_as_owner,
    migration_env,
    multipart,
    real_client,
    real_engine,
)
from test_company_backup_ui import _cookie, _follow, _page, ui  # noqa: F401
from test_company_reset import _local_files

pytestmark = pytest.mark.asyncio

API = "/migrations/start-company"
WIZARD = "/setup/start-company/migrate"
CHOOSER = "/setup/start-company"
NAME = "Harbor Goods Ltd"
HAS_COMPANY = "This login already has a company. Sign in instead."
EXPIRED = "This scan has expired. Upload the file again."


def _basic(email: str = OWNER_EMAIL, password: str = OWNER_PASSWORD) -> dict:
    return {"Authorization": "Basic " + base64.b64encode(f"{email}:{password}".encode()).decode()}


async def _companyless(client, engine):
    """The owner's only company, reset: returns the owner's id."""
    boss = await owner(engine)
    a = await company(engine, boss, NAME, "alpha")
    r = await client.post("/companies/me/reset", json={"company_name": NAME}, headers=auth(await token(engine, boss, a)))
    assert r.status_code == 200, r.text
    assert await count(engine, "companies") == 0
    return boss


async def _scan(client, *, email: str = OWNER_EMAIL, password: str = OWNER_PASSWORD):
    return await client.post(f"{API}/scan", files=multipart(("books.fake", fake_bytes())),
                             headers=_basic(email, password))


async def _decide(client, scan_token: str):
    return await client.post(f"{API}/decisions", json={
        "scan_token": scan_token, "mode": "full_history", "cutover_date": None, "mappings": {},
        "prepared_by": None})


async def _start(client, scan_token: str, *, password: str = OWNER_PASSWORD, company_name: str = "Moved Co"):
    return await client.post(f"{API}/start", json={
        "email": OWNER_EMAIL, "password": password, "scan_token": scan_token, "company_name": company_name})


async def _ready_scan(client) -> str:
    r = await _scan(client)
    assert r.status_code == 200, r.text
    scan_token = r.json()["scan_token"]
    assert (await _decide(client, scan_token)).status_code == 200
    return scan_token


async def _companies(engine) -> list[tuple[str, bool]]:
    async with engine.connect() as c:
        return [tuple(r) for r in await c.execute(text("SELECT name, is_migration_staged FROM companies"))]


async def test_a_login_with_no_company_moves_its_books_in(real_client, real_engine, migration_env,
                                                          monkeypatch, tmp_path):
    _local_files(monkeypatch, tmp_path)
    boss = await _companyless(real_client, real_engine)
    scan_token = await _ready_scan(real_client)
    read = await real_client.post(f"{API}/scan/read", json={"scan_token": scan_token})
    assert read.status_code == 200 and read.json()["scan"]["decisions"], read.text

    r = await _start(real_client, scan_token)

    assert r.status_code == 201, r.text
    body = r.json()
    from celerp.services.auth import decode_access_token
    claims = decode_access_token(body["access_token"])
    assert claims["sub"] == str(boss)
    # The migration's own staged company is the login's company: nothing was made before it.
    assert await _companies(real_engine) == [("Moved Co", True)]
    async with real_engine.connect() as c:
        run_company = await c.scalar(text("SELECT company_id FROM migration_runs WHERE id = :r"), {"r": body["run_id"]})
    assert claims["company_id"] == str(run_company)
    assert [str(r) for r in migration_env["scheduled"]] == [body["run_id"]]


async def test_wrong_credentials_store_and_create_nothing(real_client, real_engine, migration_env,
                                                          monkeypatch, tmp_path):
    _local_files(monkeypatch, tmp_path)
    await _companyless(real_client, real_engine)

    assert (await _scan(real_client, password="not-the-password")).status_code == 401
    assert not any((tmp_path / "migration_scans").glob("*/scan.json"))

    scan_token = await _ready_scan(real_client)
    r = await _start(real_client, scan_token, password="not-the-password")

    assert r.status_code == 401
    assert await count(real_engine, "companies") == 0


async def test_a_login_that_has_a_company_is_told_to_sign_in(real_client, real_engine, migration_env):
    boss = await owner(real_engine)
    await company(real_engine, boss, NAME, "alpha")

    r = await _scan(real_client)

    assert r.status_code == 409 and r.json()["detail"] == HAS_COMPANY


async def test_two_starts_at_once_make_one_company(real_client, real_engine, migration_env, monkeypatch, tmp_path):
    _local_files(monkeypatch, tmp_path)
    await _companyless(real_client, real_engine)
    scan_token = await _ready_scan(real_client)

    results = await asyncio.gather(_start(real_client, scan_token), _start(real_client, scan_token))

    assert sorted(r.status_code for r in results) == [201, 409], [r.text for r in results]
    assert await count(real_engine, "companies") == 1
    assert await count(real_engine, "migration_runs") == 1


async def test_a_start_after_success_is_told_to_sign_in(real_client, real_engine, migration_env,
                                                        monkeypatch, tmp_path):
    _local_files(monkeypatch, tmp_path)
    await _companyless(real_client, real_engine)
    scan_token = await _ready_scan(real_client)
    first = await _start(real_client, scan_token)
    assert first.status_code == 201, first.text

    again = await _start(real_client, scan_token)

    assert again.status_code == 409 and again.json()["detail"] == HAS_COMPANY
    assert await count(real_engine, "companies") == 1
    # The answer was lost: signing in lands on the company being moved in.
    login = await real_client.post("/auth/login", json={"email": OWNER_EMAIL, "password": OWNER_PASSWORD})
    assert login.status_code == 200, login.text
    from celerp.services.auth import decode_access_token
    assert decode_access_token(login.json()["access_token"])["company_id"] == \
        decode_access_token(first.json()["access_token"])["company_id"]


async def test_a_company_owners_scan_is_not_readable_without_a_session(real_client, real_engine, migration_env):
    boss = await owner(real_engine)
    a = await company(real_engine, boss, NAME, "alpha")
    tok = await token(real_engine, boss, a)
    r = await real_client.post("/migrations/scan", files=multipart(("books.fake", fake_bytes())), headers=auth(tok))
    scan_token = r.json()["scan_token"]

    for path in (f"{API}/scan/read", f"{API}/decisions"):
        r = await real_client.post(path, json={"scan_token": scan_token, "mode": "full_history"})
        assert r.status_code == 410 and r.json()["detail"] == EXPIRED, (path, r.text)


async def test_discarding_the_move_returns_the_login_to_start_company(real_client, real_engine, migration_env,
                                                                     monkeypatch, tmp_path):
    _local_files(monkeypatch, tmp_path)
    await _companyless(real_client, real_engine)
    started = (await _start(real_client, await _ready_scan(real_client))).json()

    r = await real_client.post(f"/migrations/{started['run_id']}/discard", headers=auth(started["access_token"]))

    assert r.status_code == 200 and r.json()["redirect"] == CHOOSER, r.text
    assert await count(real_engine, "companies") == 0
    assert await count(real_engine, "users", "email = :e", e=OWNER_EMAIL) == 1


async def test_start_company_page_offers_every_way_back_in(ui):
    page = _page(await ui.get(CHOOSER))

    for href in (WIZARD, f"{CHOOSER}/restore-backup"):
        assert f'href="{href}"' in page, href
    assert "Move from another system" in page and "Restore a company backup" in page
    assert "still exists" in page


async def test_the_wizard_moves_a_login_with_no_company_in(ui, real_client, real_engine, migration_env,
                                                           monkeypatch, tmp_path):
    _local_files(monkeypatch, tmp_path)
    boss = await _companyless(real_client, real_engine)
    upload = _page(await ui.get(WIZARD))
    assert 'name="email"' in upload and 'name="password"' in upload

    r = await ui.post(f"{WIZARD}/scan", data={"email": OWNER_EMAIL, "password": OWNER_PASSWORD},
                      files={"files": ("books.fake", fake_bytes())})
    assert r.status_code == 303 and r.headers["location"] == f"{WIZARD}/coverage", r.text
    r = await ui.post(f"{WIZARD}/decisions", data={"mode": "full_history"})
    assert r.status_code == 303 and r.headers["location"] == f"{WIZARD}/review", r.text
    review = _page(await ui.get(f"{WIZARD}/review"))
    assert f'value="{OWNER_EMAIL}"' in review and 'name="password"' in review

    wrong = await ui.post(f"{WIZARD}/start", data={"company_name": "Moved Co", "email": OWNER_EMAIL,
                                                   "password": "not-the-password"})
    assert wrong.status_code == 200 and 'name="password"' in _page(wrong)
    assert await count(real_engine, "companies") == 0

    r = await ui.post(f"{WIZARD}/start", data={"company_name": "Moved Co", "email": OWNER_EMAIL,
                                               "password": OWNER_PASSWORD})

    assert r.status_code == 303 and re.fullmatch(r"/migrations/[0-9a-f-]{36}", r.headers["location"]), r.text
    from celerp.services.auth import decode_access_token
    assert decode_access_token(_cookie(r, "celerp_token"))["sub"] == str(boss)
    assert (await _follow(ui, r)).status_code == 200
    assert await _companies(real_engine) == [("Moved Co", True)]


async def test_the_wizard_sends_a_signed_in_user_home(ui, real_engine):
    boss = await owner(real_engine)
    ui.cookies.set("celerp_token", await token(real_engine, boss, await company(real_engine, boss, NAME, "alpha")))

    r = await ui.get(WIZARD)

    assert r.status_code == 302 and r.headers["location"] == "/"


async def test_discarding_a_move_beside_another_company_returns_home(real_client, real_engine, migration_env):
    boss = await owner(real_engine)
    tok = await token(real_engine, boss, await company(real_engine, boss, NAME, "alpha"))
    run_id = await migrate_as_owner(real_client, tok)

    r = await real_client.post(f"/migrations/{run_id}/discard", headers=auth(tok))

    assert r.status_code == 200 and r.json()["redirect"] == "/", r.text
