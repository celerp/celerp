# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The pages of password recovery, the sign-in forms that lead to it, the notice the
first sign-in after a reset shows, an owner's reset of a staff password in Settings >
Users, and first-run setup asking the relay nothing. The UI runs in process against the real
API, so every page is checked with what the API actually answers."""
from __future__ import annotations

import re
import sys
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from celerp.main import app as api_app
from celerp.models.company import User
from email_codes_support import CODE, relay, short_floor  # noqa: F401 - fixtures
from test_helpers import invite_user, settle_background
from ui.i18n import t

pytestmark = pytest.mark.asyncio

OWNER = "owner@example.com"
STAFF = "staff@example.com"
OLD_PW = "old-password1"
NEW_PW = "new-password1"

CONSENT = "The free Celerp account is what enables Celerp to send you an email."
RESET_REASON = "Your password was reset. Sign in with your new password."
CODE_REFUSED = "That code is not right or has expired. Send a new code, or reset the password from a terminal:"
TERMINAL_STEPS = "Keep Celerp open. Open a terminal on this computer, paste the command and follow the prompts."
STAFF_RESET_DONE = "Password reset. Tell them their new password; their sessions have ended."


@asynccontextmanager
async def _ui(address: str = "127.0.0.1", **kw):
    """The UI as a visitor at *address* sees it, every API call it makes going to the
    real API in process with the headers the UI sets."""
    from ui.app import app as ui_app

    with patch("ui.api_client._get_transport", lambda: ASGITransport(app=api_app)):
        async with AsyncClient(transport=ASGITransport(app=ui_app, client=(address, 5000)),
                               base_url="http://ui", follow_redirects=False, **kw) as ui:
            yield ui


@pytest.fixture
async def ui(client):
    async with _ui() as c:
        yield c


async def _register(client, email: str = OWNER, password: str = OLD_PW) -> dict:
    r = await client.post("/auth/register", json={"company_name": "Recovery Co", "email": email,
                                                  "name": "Owner Person", "password": password})
    assert r.status_code == 200, r.text
    return r.json()


async def _hash(session, email: str) -> str:
    user = (await session.execute(select(User).where(User.email == email))).scalar_one()
    await session.refresh(user)
    return user.auth_hash


async def _can_sign_in(client, email: str, password: str) -> bool:
    return (await client.post("/auth/login", json={"email": email, "password": password})).status_code == 200


def _anchor(html: str, href: str) -> str:
    match = re.search(rf'<a\b[^>]*href="{re.escape(href)}"[^>]*>', html)
    assert match, f"no link to {href}"
    return match.group(0)


def _handle(html: str) -> str:
    """The recovery handle a code page holds in its reset form."""
    form = re.search(r'<form\b[^>]*action="/forgot-password/reset".*?</form>', html, re.S)
    assert form, "no reset form"
    match = re.search(r'<input\b[^>]*name="handle"[^>]*value="([^"]+)"', form.group(0))
    assert match and 'type="hidden"' in match.group(0), "no handle in the reset form"
    return match.group(1)


async def _code_page(ui, email: str = OWNER, handle: str = ""):
    r = await ui.post("/forgot-password", data={"email": email, "handle": handle})
    assert r.status_code == 200 and re.search(r'<input\b[^>]*name="code"', r.text), r.text
    return r


def _reset(ui, page, code: str = CODE, password: str = NEW_PW, confirm: str = NEW_PW, email: str = OWNER):
    """The reset form of *page* submitted, its hidden fields as the page holds them."""
    return ui.post("/forgot-password/reset", data={
        "email": email, "handle": _handle(page.text), "code": code, "password": password, "confirm": confirm})


# --- Recovery pages ---------------------------------------------------------------


async def test_forgot_password_link_is_plain_href(client, ui):
    """The sign-in page loads no htmx, so the link must work as a plain link."""
    await _register(client)
    page = (await ui.get("/login")).text
    link = _anchor(page, "/forgot-password")
    assert "hx-" not in link
    assert (await ui.get("/forgot-password")).status_code == 200


async def test_forgot_password_page_renders_without_script(client, ui):
    """The recovery page works with scripts off: a plain form that posts, plain links,
    and the line that says why a free Celerp account is involved."""
    await _register(client)
    r = await ui.get("/forgot-password")
    assert r.status_code == 200
    assert CONSENT in r.text
    assert re.search(r'<form\b[^>]*method="post"[^>]*action="/forgot-password"|'
                     r'<form\b[^>]*action="/forgot-password"[^>]*method="post"', r.text)
    assert re.search(r'<input\b[^>]*name="email"', r.text)
    assert " hx-" not in r.text
    _anchor(r.text, "/forgot-password/terminal")
    _anchor(r.text, "/login")


async def test_recovery_pages_back_to_sign_in(client, session, relay, short_floor):
    """Every recovery page has the way back to sign in."""
    await _register(client)
    async with _ui() as ui:
        pages = {"start": await ui.get("/forgot-password"), "code": await _code_page(ui)}
        pages |= {"refused": await _reset(ui, pages["code"], code="00000000"),
                 "terminal": await ui.get("/forgot-password/terminal"),
                 "unavailable": await ui.get("/forgot-password/terminal?reason=unavailable")}
    async with _ui("192.0.2.10") as lan:
        pages["terminal, network"] = await lan.get("/forgot-password/terminal")
    for name, r in pages.items():
        assert r.status_code == 200, (name, r.status_code)
        _anchor(r.text, "/login")


async def test_recovery_pages_never_show_the_attempt_secret(client, relay, short_floor):
    """The secret the relay ties a code to stays on the server: no recovery page, header or
    cookie carries it."""
    await _register(client)
    async with _ui() as ui:
        pages = [await _code_page(ui)]
        pages.append(await _code_page(ui, handle=_handle(pages[0].text)))
        pages.append(await _reset(ui, pages[1]))
    secrets = set(relay.attempts("/email-codes/send", OWNER) + relay.attempts("/email-codes/verify", OWNER))
    assert len(secrets) == 1, secrets
    secret = secrets.pop()
    for r in pages:
        assert secret not in r.text and secret not in str(r.headers), r.url


@pytest.mark.parametrize("path, page", [("/forgot-password/reset", "/forgot-password"), ("/login-force", "/login")])
async def test_a_form_address_opened_directly_goes_to_its_page(ui, path, page):
    """The address bar shows where a form posted to; opening that address again (a new
    tab, a bookmark, a restored session) lands on the page that starts it, not an error."""
    r = await ui.get(path)
    assert (r.status_code, r.headers.get("location")) == (303, page)


async def test_recovery_page_when_no_code_could_be_sent(client, relay, short_floor, ui):
    """A code the relay could not deliver: the page says no code was sent and offers the
    terminal reset, sending a code again and the way back to sign in."""
    await _register(client)
    relay.send_status = 503
    page = await ui.post("/forgot-password", data={"email": OWNER}, follow_redirects=True)
    assert t("recovery.unavailable") in page.text
    assert t("recovery.send_new_code") in page.text and 'href="/forgot-password"' in page.text
    assert 'href="/login"' in page.text


async def test_recovery_without_an_email_asks_for_it(relay):
    """Send code with the email left empty stays on the first form and asks for the
    email; nothing goes to Celerp."""
    async with _ui() as ui:
        r = await ui.post("/forgot-password", data={"email": "  "})
    assert r.status_code == 200
    assert t("auth.email_required") in r.text
    assert re.search(r'<form\b[^>]*action="/forgot-password"', r.text)
    assert not re.search(r'<input\b[^>]*name="code"', r.text)
    assert relay.calls == []


async def test_recovery_code_page_names_the_email_and_offers_change(client, relay):
    """The code page says which email the code went to, so a mistyped email shows,
    and Change goes back to the first form to type it again."""
    async with _ui() as ui:
        r = await ui.post("/forgot-password", data={"email": "typo@example.com"})
        await settle_background()
    assert r.status_code == 200
    assert t("recovery.code_sent", email="typo@example.com") in r.text
    assert re.search(rf'<a\b[^>]*href="/forgot-password"[^>]*>{t("auth.change")}</a>', r.text)


async def test_recovery_refusal_shows_terminal_command(client, session, relay, short_floor):
    """A refused code and an email that is not a user here both show the code page
    again with one line, the email kept and the terminal block on the page: the
    command for the person at this computer, the plain instruction for anyone else.
    Then the right code resets and goes to sign in."""
    await _register(client)
    before = await _hash(session, OWNER)
    async with _ui() as ui:
        sent = await _code_page(ui)
        await settle_background()
        for email, code in ((OWNER, "00000000"), ("nobody@example.com", CODE)):
            r = await _reset(ui, sent, code=code, email=email)
            assert r.status_code == 200, r.text
            assert CODE_REFUSED in r.text
            assert f'value="{email}"' in r.text and _handle(r.text) == _handle(sent.text)
            assert sys.executable in r.text and TERMINAL_STEPS in r.text
    async with _ui("192.0.2.10") as lan:
        r = await _reset(lan, await _code_page(lan), code="00000000")
        assert CODE_REFUSED in r.text and "celerp reset-password" in r.text
        assert sys.executable not in r.text
    assert await _hash(session, OWNER) == before

    async with _ui() as ui:
        calls = len(relay.calls)
        mismatch = await _reset(ui, sent, confirm="other-password1")
        assert t("settings.passwords_do_not_match") in mismatch.text
        assert len(relay.calls) == calls
        done = await _reset(ui, mismatch)
    assert (done.status_code, done.headers["location"]) == (303, "/login?reason=password-reset")
    assert await _can_sign_in(client, OWNER, NEW_PW)


async def test_recovery_code_page_holds_its_own_attempt(client, relay, short_floor):
    """The code page holds the visitor's recovery in a hidden field: Send a new code
    resends under it, and Change starts another."""
    await _register(client)
    async with _ui() as ui:
        page = await _code_page(ui)
        handle = _handle(page.text)
        resend = re.search(r'<form\b[^>]*action="/forgot-password"[^>]*>.*?</form>', page.text, re.S).group(0)
        assert re.search(rf'<input\b[^>]*type="hidden"[^>]*name="handle"[^>]*value="{handle}"', resend)
        assert _handle((await _code_page(ui, handle=handle)).text) == handle
        assert _handle((await _code_page(ui)).text) != handle
    first, again, other = relay.attempts("/email-codes/send", OWNER)
    assert first == again != other


async def test_stranger_on_the_form_cannot_use_up_a_persons_recovery(client, session, relay, short_floor):
    """Through the plain forms: a stranger who starts recovery for a person's email and
    guesses until refused leaves the person's own recovery working."""
    await _register(client)
    before = await _hash(session, OWNER)
    async with _ui("198.51.100.7") as stranger:
        page = await _code_page(stranger)
        for n in range(6):
            page = await _reset(stranger, page, code=f"{n:08d}")
            assert CODE_REFUSED in page.text
    assert await _hash(session, OWNER) == before
    async with _ui("203.0.113.20") as person:
        page = await _code_page(person)
        done = await _reset(person, page)
    assert (done.status_code, done.headers["location"]) == (303, "/login?reason=password-reset")
    assert await _can_sign_in(client, OWNER, NEW_PW)


async def test_recovery_code_field_takes_eight_digits(client, relay):
    async with _ui() as ui:
        page = await _code_page(ui, "typo@example.com")
    assert re.search(r'<input\b[^>]*name="code"[^>]*maxlength="8"', page.text)


async def test_recovery_unavailable_goes_to_terminal(client, session, relay, short_floor):
    """With the relay out of reach no code is sent and the visitor is taken to the
    terminal command, told why."""
    await _register(client)
    async with _ui() as ui:
        sent = await _code_page(ui)
        relay.failure = "down"
        r = await ui.post("/forgot-password", data={"email": OWNER})
        assert (r.status_code, r.headers["location"]) == (303, "/forgot-password/terminal?reason=unavailable")
        r = await _reset(ui, sent)
        assert (r.status_code, r.headers["location"]) == (303, "/forgot-password/terminal?reason=unavailable")
        page = await ui.get("/forgot-password/terminal?reason=unavailable")
    assert sys.executable in page.text and TERMINAL_STEPS in page.text
    assert t("recovery.unavailable") in page.text


async def test_recovery_undeliverable_address_goes_to_terminal(client, session, relay, short_floor):
    """An address that cannot receive codes is told so, with the terminal command, and
    is never told to try again later."""
    await _register(client)
    async with _ui() as ui:
        r = await ui.post("/forgot-password", data={"email": "admin@localhost"})
        assert (r.status_code, r.headers["location"]) == (303, "/forgot-password/terminal?reason=undeliverable")
        page = await ui.get("/forgot-password/terminal?reason=undeliverable")
    assert sys.executable in page.text and TERMINAL_STEPS in page.text
    assert t("recovery.undeliverable") in page.text
    assert t("recovery.unavailable") not in page.text and t("recovery.send_new_code") not in page.text
    assert relay.calls == []


async def test_login_shows_password_reset_reason(client, ui):
    await _register(client)
    assert RESET_REASON in (await ui.get("/login?reason=password-reset")).text
    assert RESET_REASON not in (await ui.get("/login")).text


# --- Sign-in forms --------------------------------------------------------------------


async def test_sign_in_fields_do_not_block_submit():
    """A field the browser refuses blocks the submit with no message the desktop shows,
    so the fields leave every check to the server."""
    from fasthtml.common import Div, to_xml

    from ui.components.shell import sign_in_fields

    html = to_xml(Div(*sign_in_fields("สมชาย@example.com")))
    inputs = re.findall(r"<input\b[^>]*>", html)
    assert len(inputs) == 2
    assert all("required" not in i for i in inputs)
    assert not any('type="email"' in i for i in inputs)
    assert 'inputmode="email"' in inputs[0] and 'autocomplete="email"' in inputs[0]
    assert 'value="สมชาย@example.com"' in inputs[0]
    assert "hx-" not in _anchor(html, "/forgot-password")


async def test_every_sign_in_refusal_shows_message_and_recovery_link(client, ui):
    """Every way of signing in answers a refusal with a message and the plain link to
    recovery, and offers the link before any refusal too."""
    await _register(client)
    refusals = {
        "empty": await ui.post("/login", data={"email": "", "password": ""}),
        "wrong": await ui.post("/login", data={"email": OWNER, "password": "wrong-password"}),
        "thai email": await ui.post("/login", data={"email": "สมชาย@example.com", "password": "wrong-password"}),
        "no at sign": await ui.post("/login", data={"email": "owner", "password": "wrong-password"}),
        "forced": await ui.post("/login-force", data={"email": OWNER, "password": "wrong-password"}),
        "start company": await ui.post("/setup/start-company", data={
            "email": OWNER, "password": "wrong-password", "company_name": "Next Co"}),
    }
    for name, r in refusals.items():
        assert r.status_code == 200, (name, r.status_code)
        message = t("auth.email_password_required") if name == "empty" else t("auth.invalid_credentials")
        assert message in r.text, name
        assert "hx-" not in _anchor(r.text, "/forgot-password"), name

    for path in ("/login", "/setup/start-company", "/setup/start-company/migrate",
                 "/setup/start-company/restore-backup"):
        r = await ui.get(path)
        assert r.status_code == 200, (path, r.status_code)
        assert "hx-" not in _anchor(r.text, "/forgot-password"), path


async def test_auth_pages_escape_blurs_field(client, ui):
    from ui.components.shell import ESC_BLURS_FIELD

    assert ESC_BLURS_FIELD in (await ui.get("/setup")).text
    await _register(client)
    for path in ("/login", "/forgot-password", "/forgot-password/terminal", "/setup/start-company"):
        assert ESC_BLURS_FIELD in (await ui.get(path)).text, path


# --- The notice at the first sign-in after a reset -----------------------------------


async def _sign_in_at(site: str, ui, client) -> object:
    """Sign in through *site* with the reset password; the response."""
    if site == "sign-in":
        return await ui.post("/login", data={"email": OWNER, "password": NEW_PW, "next": "/docs"})
    if site == "forced sign-in":
        return await ui.post("/login-force", data={"email": OWNER, "password": NEW_PW, "next": "/docs"})
    tokens = (await client.post("/auth/login", json={"email": OWNER, "password": NEW_PW})).json()
    pair = (tokens["access_token"], tokens["refresh_token"])
    if site == "start company":
        with patch("ui.routes.auth.api_start_company", new=AsyncMock(return_value=pair)):
            return await ui.post("/setup/start-company", data={
                "email": OWNER, "password": NEW_PW, "company_name": "Next Co"})
    if site == "migration start":
        from ui.routes.migrations import SCAN_COOKIE
        started = {"run_id": "run-1", "access_token": pair[0], "refresh_token": pair[1]}
        with patch("ui.api_client.migration_start_company_start", new=AsyncMock(return_value=started)):
            ui.cookies.update({SCAN_COOKIE: "scan-1"})
            return await ui.post("/setup/start-company/migrate/start", data={
                "email": OWNER, "password": NEW_PW, "company_name": "Next Co"})
    from ui.routes.company_backup import UPLOAD_COOKIE
    done = {"access_token": pair[0], "refresh_token": pair[1], "outcome": "created"}
    with patch("ui.api_client.company_backup_start_restore", new=AsyncMock(return_value=done)):
        ui.cookies.update({UPLOAD_COOKIE: "up-1"})
        return await ui.post("/setup/start-company/restore-backup/restore", data={
            "email": OWNER, "password": NEW_PW, "company_name": "Next Co"})


@pytest.mark.parametrize("site", ["sign-in", "forced sign-in", "start company", "migration start", "backup restore"])
async def test_sign_in_after_reset_shows_notice(client, session, ui, site):
    """Wherever a sign-in starts a session, the first one after a reset shows what
    happened, with the session already started and Continue to where it was going;
    the next sign-in goes straight through."""
    from celerp.services.password_reset import reset_password, user_by_email
    from ui.config import COOKIE_NAME

    await _register(client)
    await reset_password(session, await user_by_email(session, OWNER), NEW_PW, method="terminal",
                         os_user="สมชาย")
    r = await _sign_in_at(site, ui, client)
    assert r.status_code == 200, (site, r.status_code, r.headers.get("location"))
    assert "Your password was reset on" in r.text and "สมชาย" in r.text
    assert COOKIE_NAME in r.headers.get("set-cookie", "")
    assert t("btn.continue") in r.text
    form = re.search(r'<form[^>]*action="/password-reset-notices/acknowledge"(.*?)</form>', r.text, re.S)
    fields = re.findall(r'name="(id|next)" value="([^"]*)"', form.group(1))
    dest = dict(fields)["next"]
    assert dest != "/login" and [k for k, _ in fields].count("id") == 1

    # A sign-in whose page never arrived, or that left without Continue, shows it again.
    unseen = await _sign_in_at(site, ui, client)
    assert unseen.status_code == 200 and "Your password was reset on" in unseen.text, site

    ui.cookies.update({COOKIE_NAME: unseen.cookies[COOKIE_NAME]})
    went_on = await ui.post("/password-reset-notices/acknowledge",
                            data={"id": [v for k, v in fields if k == "id"], "next": dest})
    assert (went_on.status_code, went_on.headers["location"]) == (303, dest)
    again = await _sign_in_at(site, ui, client)
    assert again.status_code in (302, 303), site


async def test_reset_notice_shows_the_company_local_time(client, session, ui):
    from datetime import datetime, timezone

    from sqlalchemy import update

    from celerp.models.auth import PasswordReset
    from celerp.services.password_reset import reset_password, user_by_email

    await _register(client)
    await reset_password(session, await user_by_email(session, OWNER), NEW_PW, method="terminal")
    await session.execute(update(PasswordReset).values(created_at=datetime(2031, 3, 31, 17, 30, tzinfo=timezone.utc)))
    await session.commit()
    token = (await client.post("/auth/login", json={"email": OWNER, "password": NEW_PW})).json()["access_token"]
    await client.patch("/companies/me", headers={"Authorization": f"Bearer {token}"},
                       json={"settings": {"timezone": "Asia/Bangkok"}})

    r = await _sign_in_at("sign-in", ui, client)
    assert "2031-04-01 00:30 Asia/Bangkok" in r.text and " UTC" not in r.text


# --- Settings > Users ------------------------------------------------------------------


async def test_users_tab_offers_reset_password(client, session, owner_ui):
    """Owners see Reset password on every row; it opens a form in the cell that Escape
    and Cancel close, and saving it resets the staff member's password and says what
    happened. A row that cannot be reset answers why in the cell as soon as it is clicked."""
    await invite_user(client, session, dict(owner_ui.api.headers), STAFF, "operator")
    users = (await owner_ui.api.get("/companies/me/users")).json()["items"]
    staff_id = next(u["id"] for u in users if u["email"] == STAFF)
    owner_id = next(u["id"] for u in users if u["role"] == "owner")

    with patch("ui.api_client._get_transport", lambda: ASGITransport(app=api_app)):
        tab = await owner_ui.get("/settings/general?tab=users")
        assert tab.status_code == 200
        for user_id in (staff_id, owner_id):
            assert f"/settings/users/{user_id}/password/edit" in tab.text

        form = await owner_ui.get(f"/settings/users/{staff_id}/password/edit")
        assert "cell--editing" in form.text and "Escape" in form.text
        assert re.search(r'<input\b[^>]*type="password"[^>]*name="password"|'
                         r'<input\b[^>]*name="password"[^>]*type="password"', form.text)

        own = await owner_ui.get(f"/settings/users/{owner_id}/password/edit")
        assert t("users.reset_password_not_self") in own.text and 'name="password"' not in own.text
        assert re.search(r'<a\b[^>]*href="/settings/general\?tab=password"', own.text), own.text
        assert "Escape" in own.text and f"/settings/users/{owner_id}/actions" in own.text

        mismatch = await owner_ui.post(f"/settings/users/{staff_id}/password",
                                       data={"password": NEW_PW, "confirm": "other-password1"})
        assert t("settings.passwords_do_not_match") in mismatch.text
        assert await _can_sign_in(client, STAFF, "userpw123")

        refused = await owner_ui.post(f"/settings/users/{owner_id}/password",
                                      data={"password": NEW_PW, "confirm": NEW_PW})
        assert t("users.reset_password_not_self") in refused.text
        assert re.search(r'<a\b[^>]*href="/settings/general\?tab=password"[^>]*>\s*'
                         + re.escape(t("settings.change_password")), refused.text), refused.text

        saved = await owner_ui.post(f"/settings/users/{staff_id}/password",
                                    data={"password": NEW_PW, "confirm": NEW_PW})
    assert saved.status_code == 200
    assert STAFF_RESET_DONE in saved.text
    assert await _can_sign_in(client, STAFF, NEW_PW)


async def test_change_password_tab_fields_look_like_every_other_form(owner_ui):
    """The tab an owner is sent to for their own password styles its fields as the shared form fields."""
    html = (await owner_ui.get("/settings/general?tab=password")).text
    for name in ("current_password", "new_password", "confirm_password"):
        field = re.search(rf'<input\b[^>]*name="{name}"[^>]*>', html)
        assert field and re.search(r'class="[^"]*\bform-input\b', field.group(0)), name


async def test_users_tab_explains_reset_password_to_an_admin(client, session, owner_ui):
    """An admin who is not an owner sees the same Reset password; clicking it says at
    once that only a company owner can set a new password and to ask one, with no form
    to fill. A reset sent anyway is refused the same way, and nothing changes."""
    from ui.app import app as ui_app

    admin_token = await invite_user(client, session, dict(owner_ui.api.headers), "admin@example.com", "admin")
    await invite_user(client, session, dict(owner_ui.api.headers), STAFF, "operator")
    users = (await owner_ui.api.get("/companies/me/users")).json()["items"]
    staff_id = next(u["id"] for u in users if u["email"] == STAFF)

    async with AsyncClient(transport=ASGITransport(app=ui_app), base_url="http://ui",
                           cookies={"celerp_token": admin_token}) as admin_ui:
        tab = await admin_ui.get("/settings/general?tab=users")
        assert f"/settings/users/{staff_id}/password/edit" in tab.text
        clicked = await admin_ui.get(f"/settings/users/{staff_id}/password/edit")
        assert t("users.reset_password_owner_only") in clicked.text
        assert 'name="password"' not in clicked.text and f"/settings/users/{staff_id}/actions" in clicked.text
        refused = await admin_ui.post(f"/settings/users/{staff_id}/password",
                                      data={"password": NEW_PW, "confirm": NEW_PW})
    assert t("users.reset_password_owner_only") in refused.text
    assert await _can_sign_in(client, STAFF, "userpw123")


# --- Setup asks nothing of the relay -----------------------------------------------------


_SETUP = {"company_name": "Setup Co", "name": "Owner Person", "email": OWNER, "password": NEW_PW,
          "confirm_password": NEW_PW, "currency": "USD", "vertical": "x", "timezone": "UTC"}


@pytest.fixture
def setup_mocks():
    with patch("ui.api_client.setup_code_required", new=AsyncMock(return_value=False)), \
         patch("ui.routes.setup.company_setup_error", return_value=None), \
         patch("ui.routes.setup.apply_company_setup", new=AsyncMock(return_value="/")):
        yield


async def _bootstrapped(client) -> bool:
    return (await client.get("/auth/bootstrap-status")).json()["bootstrapped"]


async def test_setup_goes_straight_to_the_form_and_never_calls_the_relay(client, relay, setup_mocks):
    """First-run setup is the setup form itself: no email step, no code and no free
    Celerp account; only Forgot password ever asks Celerp for a code."""
    async with _ui() as ui:
        r = await ui.get("/setup")
        assert r.status_code == 200 and re.search(r'<input\b[^>]*name="email"', r.text)
        assert CONSENT not in r.text
        assert (await ui.get("/setup/email")).status_code == 404
        r = await ui.post("/setup", data=_SETUP)
        assert r.status_code in (302, 303), r.text
    await settle_background()
    assert await _bootstrapped(client)
    assert await _can_sign_in(client, OWNER, NEW_PW)
    assert relay.calls == []


async def test_migrate_bootstrap_never_calls_the_relay(client, relay):
    """Moving in from another system as the first owner starts without an email step too."""
    from ui.routes.migrations import SCAN_COOKIE

    form = {k: _SETUP[k] for k in ("company_name", "name", "email", "password", "confirm_password")}
    started = {"run_id": "run-1", "access_token": "a", "refresh_token": "r"}
    with patch("ui.api_client.migration_bootstrap_start", new=AsyncMock(return_value=started)) as start, \
         patch("ui.routes.migrations.setup_code_required", new=AsyncMock(return_value=False)):
        async with _ui(cookies={SCAN_COOKIE: "scan-1"}) as ui:
            r = await ui.post("/setup/migrate/start", data=form)
    start.assert_awaited_once()
    assert r.headers.get("location") == "/migrations/run-1"
    await settle_background()
    assert relay.calls == []


async def test_a_refused_new_code_keeps_the_code_page_and_its_handle(client, relay, short_floor, ui):
    """Send a new code refused for too many sends answers the code page with the reason,
    still holding the visitor's handle, so the code already in the mailbox can still be used."""
    from celerp.routers import auth as auth_router

    await _register(client)
    page = await _code_page(ui)
    handle = _handle(page.text)
    auth_router.limiter.enabled = True
    auth_router.limiter._storage.reset()
    try:
        for _ in range(3):
            await _code_page(ui, handle=handle)
        refused = await ui.post("/forgot-password", data={"email": OWNER, "handle": handle})
    finally:
        auth_router.limiter.enabled = False
        auth_router.limiter._storage.reset()
    assert refused.status_code == 200
    assert t("error.rate_limited") in refused.text
    assert _handle(refused.text) == handle
    assert (await _reset(ui, refused)).status_code == 303
    assert await _can_sign_in(client, OWNER, NEW_PW)
