# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Moving a company in saves the timezone the browser reports, as setup does, so the
moved-in company's "today" is its own day and not the UTC day."""

from __future__ import annotations

import re
import uuid

import pytest
from sqlalchemy import text

from company_backup_support import company, owner, token
from migration_support import (  # noqa: F401
    OWNER_EMAIL,
    OWNER_PASSWORD,
    auth,
    count,
    creator_run,
    fake_bytes,
    finalize_run,
    load_run,
    maker,
    migration_env,
    real_client,
    real_engine,
    save_decisions,
    scan_upload,
)
from test_company_backup_ui import _hidden, ui  # noqa: F401
from test_company_reset import _local_files
from test_company_reset_migration import _companyless

pytestmark = pytest.mark.asyncio

MOVED = "Moved Co"


async def _first_owner(engine) -> dict:
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE users, companies RESTART IDENTITY CASCADE"))
    return {"name": "Owner", "email": OWNER_EMAIL, "password": OWNER_PASSWORD, "confirm_password": OWNER_PASSWORD}


async def _signed_in_owner(engine, ui) -> dict:
    boss = await owner(engine)
    ui.cookies.set("celerp_token", await token(engine, boss, await company(engine, boss, "Home Co", "alpha")))
    return {}


async def _login_with_no_company(engine, ui, client) -> dict:
    await _companyless(client, engine)
    return {"email": OWNER_EMAIL, "password": OWNER_PASSWORD}


async def _settings(engine) -> dict:
    async with engine.connect() as c:
        return (await c.execute(text("SELECT settings FROM companies WHERE name = :n"), {"n": MOVED})).scalar_one()


async def _move_in(ui, base: str, sent: str, account: dict, scan_fields: dict):
    r = await ui.post(f"{base}/scan", data=scan_fields, files={"files": ("books.fake", fake_bytes())})
    assert r.status_code == 303, r.text
    assert (await ui.post(f"{base}/decisions", data={"mode": "full_history"})).status_code == 303
    review = (await ui.get(f"{base}/review")).text
    # The review form carries the timezone the browser fills in, as the setup form does.
    assert _hidden(review).get("timezone") == ""
    assert re.search(r"querySelectorAll\('input\[name=\"timezone\"\]'\)", review)
    return await ui.post(f"{base}/start", data={"company_name": MOVED, "timezone": sent, **account})


@pytest.mark.parametrize("sent, saved", [("Asia/Bangkok", "Asia/Bangkok"), ("", "UTC")])
@pytest.mark.parametrize("mode", ["bootstrap", "company", "start_company"])
async def test_a_moved_in_company_keeps_the_browser_timezone(ui, real_client, real_engine, migration_env,
                                                             monkeypatch, tmp_path, mode, sent, saved):
    _local_files(monkeypatch, tmp_path)
    if mode == "bootstrap":
        base, account, scan_fields = "/setup/migrate", await _first_owner(real_engine), {}
    elif mode == "company":
        base, account, scan_fields = "/setup/new-company/migrate", await _signed_in_owner(real_engine, ui), {}
    else:
        account = await _login_with_no_company(real_engine, ui, real_client)
        base, scan_fields = "/setup/start-company/migrate", account

    r = await _move_in(ui, base, sent, account, scan_fields)

    assert r.status_code == 303 and re.fullmatch(r"/migrations/[0-9a-f-]{36}", r.headers["location"]), r.text
    # Without a zone from the browser the default business timezone applies, as at setup.
    assert (await _settings(real_engine)).get("timezone") == saved


async def test_the_timezone_outlasts_the_migration(ui, real_client, real_engine, migration_env):
    from celerp.services import migrations

    r = await _move_in(ui, "/setup/migrate", "Pacific/Honolulu", await _first_owner(real_engine), {})
    run_id = uuid.UUID(r.headers["location"].rsplit("/", 1)[1])
    await migrations.run_migration(run_id)
    assert (await load_run(real_engine, run_id)).status == "ready_to_finalize"
    async with maker(real_engine)() as s:
        await finalize_run(s, await creator_run(s, run_id))
    assert (await _settings(real_engine)).get("timezone") == "Pacific/Honolulu"


async def test_an_unknown_timezone_is_refused_and_creates_nothing(real_client, real_engine, migration_env):
    boss = await owner(real_engine)
    tok = await token(real_engine, boss, await company(real_engine, boss, "Home Co", "alpha"))
    scan_token = (await scan_upload(real_client, fake_bytes(), token=tok)).json()["scan_token"]
    assert (await save_decisions(real_client, scan_token, token=tok)).status_code == 200
    companies = await count(real_engine, "companies")

    r = await real_client.post("/migrations/start-from-scan", headers=auth(tok), json={
        "scan_token": scan_token, "company_name": MOVED, "timezone": "Mars/Olympus"})

    assert r.status_code == 422 and "Mars/Olympus" in r.json()["detail"]["timezone"], r.text
    assert await count(real_engine, "companies") == companies
