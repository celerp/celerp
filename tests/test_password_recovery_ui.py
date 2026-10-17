# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The pages of password recovery, the sign-in forms that lead to it, the notice the
first sign-in after a reset shows, an owner's reset of a staff password in Settings >
Users, and the email step of first-run setup. The UI runs in process against the real
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
from email_codes_support import CODE, relay, settle, short_floor  # noqa: F401 - fixtures
from test_helpers import invite_user
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
SETUP_UNAVAILABLE = ("Celerp could not be reached to confirm your email. Check the address carefully; "
                     "you can continue without confirming.")
STAFF_RESET_DONE = "Password reset. Tell them their new password; their sessions have ended."
STAFF_ONLY = "Only staff can have their password reset here. Owners use Forgot password on the sign-in page."


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
        pages = {"start": await ui.get("/forgot-password"),
                 "code": await ui.post("/forgot-password", data={"email": OWNER}),
                 "refused": await ui.post("/forgot-password/reset", data={
                     "email": OWNER, "code": "000000", "password": NEW_PW, "confirm": NEW_PW}),
                 "terminal": await ui.get("/forgot-password/terminal"),
                 "unavailable": await ui.get("/forgot-password/terminal?reason=unavailable")}
    async with _ui("192.0.2.10") as lan:
        pages["terminal, network"] = await lan.get("/forgot-password/terminal")
    for name, r in pages.items():
        assert r.status_code == 200, (name, r.status_code)
        _anchor(r.text, "/login")


async def test_recovery_refusal_shows_terminal_command(client, session, relay, short_floor):
    """A refused code and an email that is not a user here both show the code page
    again with one line, the email kept and the terminal block on the page: the
    command for the person at this computer, the plain instruction for anyone else.
    Then the right code resets and goes to sign in."""
    await _register(client)
    before = await _hash(session, OWNER)
    async with _ui() as ui:
        sent = await ui.post("/forgot-password", data={"email": OWNER})
        assert sent.status_code == 200 and re.search(r'<input\b[^>]*name="code"', sent.text)
        await settle()
        for email, code in ((OWNER, "000000"), ("nobody@example.com", CODE)):
            r = await ui.post("/forgot-password/reset", data={
                "email": email, "code": code, "password": NEW_PW, "confirm": NEW_PW})
            assert r.status_code == 200, r.text
            assert CODE_REFUSED in r.text
            assert f'value="{email}"' in r.text
            assert sys.executable in r.text and TERMINAL_STEPS in r.text
    async with _ui("192.0.2.10") as lan:
        r = await lan.post("/forgot-password/reset", data={
            "email": OWNER, "code": "000000", "password": NEW_PW, "confirm": NEW_PW})
        assert CODE_REFUSED in r.text and "celerp reset-password" in r.text
        assert sys.executable not in r.text
    assert await _hash(session, OWNER) == before

    async with _ui() as ui:
        calls = len(relay.calls)
        mismatch = await ui.post("/forgot-password/reset", data={
            "email": OWNER, "code": CODE, "password": NEW_PW, "confirm": "other-password1"})
        assert t("settings.passwords_do_not_match") in mismatch.text
        assert len(relay.calls) == calls
        done = await ui.post("/forgot-password/reset", data={
            "email": OWNER, "code": CODE, "password": NEW_PW, "confirm": NEW_PW})
    assert (done.status_code, done.headers["location"]) == (303, "/login?reason=password-reset")
    assert await _can_sign_in(client, OWNER, NEW_PW)


async def test_recovery_unavailable_goes_to_terminal(client, session, relay, short_floor):
    """With the relay out of reach no code is sent and the visitor is taken to the
    terminal command, told why."""
    await _register(client)
    relay.failure = "down"
    async with _ui() as ui:
        r = await ui.post("/forgot-password", data={"email": OWNER})
        assert (r.status_code, r.headers["location"]) == (303, "/forgot-password/terminal?reason=unavailable")
        r = await ui.post("/forgot-password/reset", data={
            "email": OWNER, "code": CODE, "password": NEW_PW, "confirm": NEW_PW})
        assert (r.status_code, r.headers["location"]) == (303, "/forgot-password/terminal?reason=unavailable")
        page = await ui.get("/forgot-password/terminal?reason=unavailable")
    assert sys.executable in page.text and TERMINAL_STEPS in page.text
    assert t("recovery.unavailable") in page.text


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

    assert ESC_BLURS_FIELD in (await ui.get("/setup/email")).text
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
            return await ui.post("/setup/start-company/migrate/start", cookies={SCAN_COOKIE: "scan-1"}, data={
                "email": OWNER, "password": NEW_PW, "company_name": "Next Co"})
    from ui.routes.company_backup import UPLOAD_COOKIE
    done = {"access_token": pair[0], "refresh_token": pair[1], "outcome": "created"}
    with patch("ui.api_client.company_backup_start_restore", new=AsyncMock(return_value=done)):
        return await ui.post("/setup/start-company/restore-backup/restore", cookies={UPLOAD_COOKIE: "up-1"}, data={
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
    continue_to = re.search(r'<a\b[^>]*href="([^"]+)"[^>]*>\s*' + re.escape(t("btn.continue")), r.text)
    assert continue_to and continue_to.group(1) != "/login"

    again = await _sign_in_at(site, ui, client)
    assert again.status_code in (302, 303), site


# --- Settings > Users ------------------------------------------------------------------


async def test_users_tab_offers_reset_password(client, session, owner_ui):
    """Owners see Reset password on every row; it opens a form in the cell that Escape
    and Cancel close, and saving it resets the staff member's password and says what
    happened. A row that cannot be reset answers why in the cell."""
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

        mismatch = await owner_ui.post(f"/settings/users/{staff_id}/password",
                                       data={"password": NEW_PW, "confirm": "other-password1"})
        assert t("settings.passwords_do_not_match") in mismatch.text
        assert await _can_sign_in(client, STAFF, "userpw123")

        refused = await owner_ui.post(f"/settings/users/{owner_id}/password",
                                      data={"password": NEW_PW, "confirm": NEW_PW})
        assert "Change your own password in your profile." in refused.text or STAFF_ONLY in refused.text

        saved = await owner_ui.post(f"/settings/users/{staff_id}/password",
                                    data={"password": NEW_PW, "confirm": NEW_PW})
    assert saved.status_code == 200
    assert STAFF_RESET_DONE in saved.text
    assert await _can_sign_in(client, STAFF, NEW_PW)


# --- Setup confirms the owner's email ------------------------------------------------------


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


async def _confirm(ui, email: str = OWNER) -> None:
    sent = await ui.post("/setup/email", data={"action": "send", "email": email})
    assert sent.status_code == 200 and re.search(r'<input\b[^>]*name="code"', sent.text), sent.text
    await settle()
    r = await ui.post("/setup/email", data={"action": "verify", "email": email, "code": CODE})
    assert (r.status_code, r.headers.get("location")) == (303, "/setup"), r.text


async def test_setup_requires_confirmed_email(client, relay, setup_mocks):
    """The first owner confirms their email before the account is made: setup goes to
    the email step first, refuses an email other than the confirmed one, and shows the
    confirmed email fixed, with Change to go back."""
    async with _ui() as ui:
        r = await ui.get("/setup")
        assert (r.status_code, r.headers.get("location")) == (303, "/setup/email")
        step = await ui.get("/setup/email")
        assert CONSENT in step.text
        r = await ui.post("/setup", data=_SETUP)
        assert (r.status_code, r.headers.get("location")) == (303, "/setup/email")
        assert not await _bootstrapped(client)

        wrong = await ui.post("/setup/email", data={"action": "verify", "email": OWNER, "code": "000000"})
        assert wrong.status_code == 200 and "/setup/email" in wrong.text
        await _confirm(ui)
        form = await ui.get("/setup")
        assert form.status_code == 200
        assert re.search(rf'<input\b[^>]*value="{re.escape(OWNER)}"[^>]*readonly|'
                         rf'<input\b[^>]*readonly[^>]*value="{re.escape(OWNER)}"', form.text)
        _anchor(form.text, "/setup/email")

        r = await ui.post("/setup", data={**_SETUP, "email": "other@example.com"})
        assert (r.status_code, r.headers.get("location")) == (303, "/setup/email")
        assert not await _bootstrapped(client)
        r = await ui.post("/setup", data=_SETUP)
        assert r.status_code in (302, 303) and r.headers["location"] != "/setup/email"
    assert await _bootstrapped(client)
    assert await _can_sign_in(client, OWNER, NEW_PW)


async def test_setup_continue_without_confirming_only_when_relay_unavailable(client, relay, setup_mocks):
    """An offline first run is never stuck: when Celerp cannot be reached the step says
    so and offers to continue without confirming; at any other time it does not."""
    async with _ui() as ui:
        sent = await ui.post("/setup/email", data={"action": "send", "email": OWNER})
        assert t("setup_email.continue_without") not in sent.text
        r = await ui.post("/setup/email", data={"action": "skip", "email": OWNER})
        assert r.headers.get("location") != "/setup"
        r = await ui.post("/setup", data=_SETUP)
        assert (r.status_code, r.headers.get("location")) == (303, "/setup/email")

        relay.failure = "down"
        down = await ui.post("/setup/email", data={"action": "send", "email": OWNER})
        assert SETUP_UNAVAILABLE in down.text and t("setup_email.continue_without") in down.text
        r = await ui.post("/setup/email", data={"action": "skip", "email": OWNER})
        assert (r.status_code, r.headers.get("location")) == (303, "/setup")
        r = await ui.post("/setup", data=_SETUP)
        assert r.status_code in (302, 303) and r.headers["location"] != "/setup/email"
    assert await _bootstrapped(client)


async def test_migrate_bootstrap_requires_confirmed_email(client, relay):
    """Moving in from another system as the first owner confirms the email the same way."""
    from ui.routes.migrations import SCAN_COOKIE

    form = {k: _SETUP[k] for k in ("company_name", "name", "email", "password", "confirm_password")}
    started = {"run_id": "run-1", "access_token": "a", "refresh_token": "r"}
    with patch("ui.api_client.migration_bootstrap_start", new=AsyncMock(return_value=started)) as start, \
         patch("ui.routes.migrations.setup_code_required", new=AsyncMock(return_value=False)):
        async with _ui(cookies={SCAN_COOKIE: "scan-1"}) as ui:
            r = await ui.post("/setup/migrate/start", data=form)
            assert (r.status_code, r.headers.get("location")) == (303, "/setup/email")
            await _confirm(ui)
            r = await ui.post("/setup/migrate/start", data={**form, "email": "other@example.com"})
            assert (r.status_code, r.headers.get("location")) == (303, "/setup/email")
            start.assert_not_awaited()
            r = await ui.post("/setup/migrate/start", data=form)
    start.assert_awaited_once()
    assert r.headers.get("location") == "/migrations/run-1"
