# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A login whose last company was reset restores a company backup instead of starting a
new company: it signs in with its email and password, previews the backup, and restores
it as its company."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text

from celerp.services.company_backup import RESTORE_RUNNING

from company_backup_support import company, download, owner, snapshot, token
from migration_support import OWNER_EMAIL, OWNER_PASSWORD, auth, count, real_client, real_engine  # noqa: F401
from test_company_backup_ui import (  # noqa: F401
    NOTHING_WRITTEN,
    _claims,
    _cookie,
    _follow,
    _hidden,
    _link,
    _page,
    _upload_and_preview,
    ui,
)
from test_company_reset import SOLO_EMAIL, SOLO_PASSWORD, _local_files
from ui.i18n import t

pytestmark = pytest.mark.asyncio

READ = "/company-backups/start-company/read"
RESTORE = "/company-backups/start-company/restore"
START = "/setup/start-company"
BASE = "/setup/start-company/restore-backup"
NAME = "Harbor Goods Ltd"
HAS_COMPANY = t("auth.has_company", "en")
RESTORE_RUNNING = t(RESTORE_RUNNING, "en")


async def _reset_last_company(client, engine):
    """The owner's only company, backed up and then reset: returns (owner id, backup bytes)."""
    boss = await owner(engine)
    a = await company(engine, boss, NAME, "alpha")
    tok = await token(engine, boss, a)
    data = await download(client, tok)
    r = await client.post("/companies/me/reset", json={"company_name": NAME}, headers=auth(tok))
    assert r.status_code == 200, r.text
    assert await count(engine, "companies") == 0
    return boss, data


async def _read(client, data: bytes, email: str = OWNER_EMAIL, password: str = OWNER_PASSWORD):
    return await client.post(READ, files={"file": ("harbor.celerp-company", data)},
                             data={"email": email, "password": password})


def _confirm(preview, email: str = OWNER_EMAIL, password: str = OWNER_PASSWORD) -> dict:
    body = preview.json()
    return {"email": email, "password": password, "upload_token": body["upload_token"],
            "plan_fingerprint": body["plan_fingerprint"]}


async def _companies(engine) -> list[tuple[str, str]]:
    async with engine.connect() as conn:
        return [(str(i), n) for i, n in (await conn.execute(text("SELECT id, name FROM companies"))).all()]


async def test_a_login_whose_last_company_was_reset_restores_its_backup(real_client, real_engine,
                                                                         monkeypatch, tmp_path):
    _local_files(monkeypatch, tmp_path)
    boss, data = await _reset_last_company(real_client, real_engine)

    preview = await _read(real_client, data)
    assert preview.status_code == 200, preview.text
    assert preview.json()["action"] == "create" and preview.json()["company_name"] == NAME
    assert await count(real_engine, "companies") == 0  # a preview writes nothing

    r = await real_client.post(RESTORE, json=_confirm(preview))

    assert r.status_code == 201, r.text
    [(cid, name)] = await _companies(real_engine)
    assert name == NAME and r.json()["company_id"] == cid
    assert await count(real_engine, "user_companies", "user_id = :u AND company_id = :c AND role = 'owner' "
                       "AND is_active", u=boss, c=cid) == 1
    me = await real_client.get("/companies/me", headers=auth(r.json()["access_token"]))
    assert me.status_code == 200 and me.json()["id"] == cid


async def test_wrong_credentials_restore_nothing(real_client, real_engine, monkeypatch, tmp_path):
    _local_files(monkeypatch, tmp_path)
    _, data = await _reset_last_company(real_client, real_engine)
    preview = await _read(real_client, data)
    before = await snapshot(real_engine)

    bad_read = await _read(real_client, data, password="wrong-password")
    bad_restore = await real_client.post(RESTORE, json=_confirm(preview, password="wrong-password"))

    assert bad_read.status_code == 401 and bad_read.json()["detail"] == t("auth.invalid_credentials", "en")
    assert bad_restore.status_code == 401 and bad_restore.json()["detail"] == t("auth.invalid_credentials", "en")
    assert await count(real_engine, "companies") == 0
    assert {t: rows for t, rows in (await snapshot(real_engine)).items() if t not in ("user_auth_state",)} == \
        {t: rows for t, rows in before.items() if t not in ("user_auth_state",)}


async def test_a_login_that_has_a_company_is_told_to_sign_in(real_client, real_engine, monkeypatch, tmp_path):
    _local_files(monkeypatch, tmp_path)
    boss = await owner(real_engine)
    a = await company(real_engine, boss, NAME, "alpha")
    data = await download(real_client, await token(real_engine, boss, a))

    r = await _read(real_client, data)

    assert r.status_code == 409 and r.json()["detail"] == HAS_COMPANY
    assert await count(real_engine, "companies") == 1


async def test_a_company_started_after_the_preview_stops_the_restore(real_client, real_engine,
                                                                     monkeypatch, tmp_path):
    _local_files(monkeypatch, tmp_path)
    _, data = await _reset_last_company(real_client, real_engine)
    preview = await _read(real_client, data)
    started = await real_client.post("/auth/start-company", json={
        "email": OWNER_EMAIL, "password": OWNER_PASSWORD, "company_name": "Fresh Start Ltd"})
    assert started.status_code == 200, started.text

    r = await real_client.post(RESTORE, json=_confirm(preview))

    assert r.status_code == 409 and r.json()["detail"] == HAS_COMPANY
    assert [n for _, n in await _companies(real_engine)] == ["Fresh Start Ltd"]


async def test_two_restores_at_once_make_one_company(real_client, real_engine, monkeypatch, tmp_path):
    _local_files(monkeypatch, tmp_path)
    _, data = await _reset_last_company(real_client, real_engine)
    first, second = await _read(real_client, data), await _read(real_client, data)

    results = await asyncio.gather(real_client.post(RESTORE, json=_confirm(first)),
                                   real_client.post(RESTORE, json=_confirm(second)))

    assert sorted(r.status_code for r in results) == [201, 409], [r.text for r in results]
    # Refused while the first runs, or once it has made the company.
    [refusal] = [r.json()["detail"] for r in results if r.status_code == 409]
    assert refusal in {RESTORE_RUNNING, HAS_COMPANY}, refusal
    assert [n for _, n in await _companies(real_engine)] == [NAME]


async def test_another_logins_upload_cannot_be_restored(real_client, real_engine, monkeypatch, tmp_path):
    _local_files(monkeypatch, tmp_path)
    _, data = await _reset_last_company(real_client, real_engine)
    await owner(real_engine, SOLO_EMAIL, "Solo")
    preview = await _read(real_client, data)

    r = await real_client.post(RESTORE, json=_confirm(preview, SOLO_EMAIL, SOLO_PASSWORD))

    assert r.status_code == 409 and "Choose the file again" in r.json()["detail"]
    assert await count(real_engine, "companies") == 0


async def test_start_company_page_offers_restoring_a_backup(ui, real_client, real_engine, monkeypatch, tmp_path):
    _local_files(monkeypatch, tmp_path)
    _, data = await _reset_last_company(real_client, real_engine)
    ui.cookies.clear()

    page = _page(await ui.get(START))
    assert _link(page, BASE, "Restore a company backup")
    r = await ui.get(BASE)
    assert r.status_code == 200, r.text
    for field in ('name="email"', 'name="password"', 'type="file"'):
        assert field in r.text, field

    r = await _upload_and_preview(ui, BASE, data, email=OWNER_EMAIL, password=OWNER_PASSWORD)
    preview = _page(r)
    assert NAME in preview and NOTHING_WRITTEN in preview
    assert 'name="password"' in preview and OWNER_PASSWORD not in preview
    assert await count(real_engine, "companies") == 0

    r = await ui.post(f"{BASE}/restore", data={**_hidden(preview), "email": OWNER_EMAIL,
                                                "password": OWNER_PASSWORD})
    assert r.status_code == 303, r.text
    [(cid, _)] = await _companies(real_engine)
    assert _claims(_cookie(r, "celerp_token"))["company_id"] == cid
    assert NAME in _page(await _follow(ui, r))


async def test_start_company_restore_shows_errors_on_the_page(ui, real_client, real_engine, monkeypatch, tmp_path):
    _local_files(monkeypatch, tmp_path)
    _, data = await _reset_last_company(real_client, real_engine)
    ui.cookies.clear()

    r = await ui.post(f"{BASE}/read", files={"file": ("harbor.celerp-company", data, "application/octet-stream")},
                      data={"email": OWNER_EMAIL, "password": "wrong-password"})
    assert r.status_code == 401 and t("auth.invalid_credentials", "en") in _page(r)

    r = await _upload_and_preview(ui, BASE, data, email=OWNER_EMAIL, password=OWNER_PASSWORD)
    r = await ui.post(f"{BASE}/restore", data={**_hidden(_page(r)), "email": OWNER_EMAIL,
                                                "password": "wrong-password"})
    assert r.status_code == 200 and t("auth.invalid_credentials", "en") in _page(r)
    assert 'name="password"' in _page(r)  # corrected on the preview, the upload kept
    assert await count(real_engine, "companies") == 0
