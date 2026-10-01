# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Reset this company in the settings Danger Zone, and starting a new company for a login
that has none left, through the UI app on a real database."""

from __future__ import annotations

import re

import pytest
from fasthtml.common import to_xml

from company_backup_support import company, member, owner, token
from migration_support import OWNER_EMAIL, OWNER_PASSWORD, count, real_client, real_engine  # noqa: F401
from test_company_backup_ui import _page, ui  # noqa: F401
from test_company_reset import SOLO_EMAIL, SOLO_PASSWORD, _folder, _local_files

pytestmark = pytest.mark.asyncio

RESET_UI = "/settings/company/reset"
START = "/setup/start-company"


def _set_cookie(r, name: str) -> str | None:
    for header in r.headers.get_list("set-cookie"):
        if header.startswith(f"{name}="):
            return header.split(";", 1)[0].split("=", 1)[1]
    return None


def _claims(tok: str) -> dict:
    from celerp.services.auth import decode_access_token
    return decode_access_token(tok)


async def test_card_names_the_company_and_offers_its_backup():
    from ui.routes.company_backup import DOWNLOAD
    from ui.routes.settings import _company_reset_card
    html = to_xml(_company_reset_card("Harbor Goods Ltd"))
    assert "Reset this company" in html
    assert f'href="{DOWNLOAD}"' in html
    assert "/backup/export" not in html
    assert "<strong>Harbor Goods Ltd</strong>" in html
    assert 'name="company_name"' in html
    assert f'hx-post="{RESET_UI}"' in html
    # Errors land inside the dialog, where the owner is looking.
    dialog = re.search(r"<dialog\b.*?</dialog>", html, flags=re.S).group(0)
    assert 'id="company-reset-flash"' in dialog
    assert 'hx-target="#company-reset-flash"' in dialog
    assert "—" not in html
    assert "factory" not in html.lower() and "RESET" not in html


async def test_danger_zone_shows_the_reset_to_the_owner_only(ui, real_engine):
    shared = await owner(real_engine)
    a = await company(real_engine, shared, "Harbor Goods Ltd", "alpha")
    admin = await owner(real_engine, "admin@example.com", "Admin")
    await member(real_engine, admin, a, "admin")

    ui.cookies.set("celerp_token", await token(real_engine, shared, a))
    page = _page(await ui.get("/settings/general?tab=company"))
    assert "Reset this company" in page and "Reset All Data" not in page
    assert "<strong>Harbor Goods Ltd</strong>" in page

    ui.cookies.set("celerp_token", await token(real_engine, admin, a, "admin"))
    assert "Reset this company" not in _page(await ui.get("/settings/general?tab=company"))


async def test_wrong_name_shows_the_reason_inside_the_dialog(ui, real_engine):
    shared = await owner(real_engine)
    a = await company(real_engine, shared, "Harbor Goods Ltd", "alpha")
    ui.cookies.set("celerp_token", await token(real_engine, shared, a))

    r = await ui.post(RESET_UI, data={"company_name": "Harbor Goods"})

    assert r.status_code == 200
    assert "HX-Redirect" not in r.headers
    assert "The name you typed does not match this company&#x27;s name. Nothing was deleted." in r.text \
        or "The name you typed does not match this company's name. Nothing was deleted." in r.text
    assert await count(real_engine, "companies", "id = :c", c=str(a)) == 1


async def test_non_owner_is_told_in_the_dialog(ui, real_engine):
    shared = await owner(real_engine)
    a = await company(real_engine, shared, "Harbor Goods Ltd", "alpha")
    admin = await owner(real_engine, "admin@example.com", "Admin")
    await member(real_engine, admin, a, "admin")
    ui.cookies.set("celerp_token", await token(real_engine, admin, a, "admin"))

    r = await ui.post(RESET_UI, data={"company_name": "Harbor Goods Ltd"})

    assert "Owner role required." in r.text
    assert await count(real_engine, "companies", "id = :c", c=str(a)) == 1


async def test_reset_with_another_company_switches_to_it(ui, real_engine, tmp_path, monkeypatch):
    shared = await owner(real_engine)
    a = await company(real_engine, shared, "Harbor Goods Ltd", "alpha")
    b = await company(real_engine, shared, "Hillside Supply Co", "bravo")
    _local_files(monkeypatch, tmp_path)
    ui.cookies.set("celerp_token", await token(real_engine, shared, a))

    r = await ui.post(RESET_UI, data={"company_name": "Harbor Goods Ltd"})

    assert r.headers.get("HX-Redirect") == "/", r.text
    assert _claims(_set_cookie(r, "celerp_token"))["company_id"] == str(b)
    assert await count(real_engine, "companies", "id = :c", c=str(a)) == 0
    assert not _folder(tmp_path, a).exists()


async def test_reset_of_the_last_company_lands_on_starting_a_new_one(ui, real_engine):
    shared = await owner(real_engine)
    a = await company(real_engine, shared, "Harbor Goods Ltd", "alpha")
    ui.cookies.set("celerp_token", await token(real_engine, shared, a))

    r = await ui.post(RESET_UI, data={"company_name": "Harbor Goods Ltd"})

    assert r.headers.get("HX-Redirect") == START, r.text
    assert _set_cookie(r, "celerp_token") == '""' or _set_cookie(r, "celerp_token") == ""
    assert await count(real_engine, "users", "email = :e", e=OWNER_EMAIL) == 1

    ui.cookies.clear()
    page = _page(await ui.get(START))
    assert 'name="email"' in page and 'name="password"' in page and 'name="company_name"' in page
    made = await ui.post(START, data={"email": OWNER_EMAIL, "password": OWNER_PASSWORD,
                                      "company_name": "Fresh Start Ltd"})
    assert made.status_code == 302 and made.headers["location"] == "/setup/company"
    tok = _set_cookie(made, "celerp_token")
    assert await count(real_engine, "companies", "name = 'Fresh Start Ltd' AND id = :c",
                       c=_claims(tok)["company_id"]) == 1


async def test_start_company_shows_errors_on_the_page(ui, real_engine):
    shared = await owner(real_engine)
    await company(real_engine, shared, "Harbor Goods Ltd", "alpha")
    r = await ui.post(START, data={"email": OWNER_EMAIL, "password": "wrong-password", "company_name": "X Ltd"})
    assert r.status_code == 200 and "Invalid credentials" in _page(r)
    r = await ui.post(START, data={"email": OWNER_EMAIL, "password": OWNER_PASSWORD, "company_name": "X Ltd"})
    assert r.status_code == 200 and "This login already has a company. Sign in instead." in _page(r)
    assert await count(real_engine, "companies") == 1


async def test_sign_in_without_a_company_goes_to_starting_one(ui, real_engine):
    shared = await owner(real_engine)
    await company(real_engine, shared, "Harbor Goods Ltd", "alpha")
    await owner(real_engine, SOLO_EMAIL, "Solo")
    r = await ui.post("/login", data={"email": SOLO_EMAIL, "password": SOLO_PASSWORD})
    assert r.status_code == 302 and r.headers["location"] == START


async def test_the_old_reset_route_is_gone(ui, real_engine):
    shared = await owner(real_engine)
    a = await company(real_engine, shared, "Harbor Goods Ltd", "alpha")
    ui.cookies.set("celerp_token", await token(real_engine, shared, a))
    assert (await ui.post("/settings/factory-reset")).status_code == 404
    assert await count(real_engine, "companies") == 1
