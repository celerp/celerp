# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Password recovery: one reset function for every way a password is reset, the
emailed code the Celerp relay sends and confirms, the terminal command for the
person at this computer, an owner's reset of a staff password, and the notice the
next sign-in shows."""
from __future__ import annotations

import asyncio
import shlex
import time
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, select

from celerp.main import app
from celerp.models.company import User
from celerp.services.auth import CALLER_HEADER
from email_codes_support import CODE, FAILURES, relay, short_floor  # noqa: F401 - fixtures
from test_helpers import invite_user, settle_background
from ui.i18n import t

pytestmark = pytest.mark.asyncio

OWNER = "owner@example.com"
STAFF = "staff@example.com"
OLD_PW = "old-password1"
NEW_PW = "new-password1"


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _register(client, email: str = OWNER, password: str = OLD_PW) -> dict:
    r = await client.post("/auth/register", json={"company_name": "Recovery Co", "email": email,
                                                  "name": "Owner Person", "password": password})
    assert r.status_code == 200, r.text
    return r.json()


async def _login(client, email: str, password: str):
    return await client.post("/auth/login", json={"email": email, "password": password})


async def _staff(client, session, owner_token: str, email: str = STAFF, role: str = "operator") -> str:
    """A member of the owner's company, signed in: their access token. Password userpw123."""
    return await invite_user(client, session, _bearer(owner_token), email, role)


async def _user(session, email: str) -> User:
    return (await session.execute(select(User).where(User.email == email))).scalar_one()


async def _hash(session, email: str) -> str:
    user = await _user(session, email)
    await session.refresh(user)
    return user.auth_hash


async def _signed_in(client, token: str) -> bool:
    return (await client.get("/companies/me", headers=_bearer(token))).status_code == 200


async def _refreshes(client, refresh: str) -> bool:
    return (await client.post("/auth/token/refresh", json={"refresh_token": refresh})).status_code == 200


def _send(client, email: str, handle: str | None = None, **kw):
    return client.post("/auth/password-recovery/send",
                       json={"email": email} | ({"handle": handle} if handle else {}), **kw)


async def _start(client, email: str, **kw) -> str:
    """A new recovery of *email*: the handle the requester holds it by."""
    r = await _send(client, email, **kw)
    assert r.json()["outcome"] == "sent", r.text
    return r.json()["handle"]


def _reset(client, email: str, handle: str, code: str = CODE, password: str = NEW_PW, **kw):
    return client.post("/auth/password-recovery/reset",
                       json={"email": email, "handle": handle, "code": code, "new_password": password}, **kw)


def _as(address: str) -> dict:
    """The headers of a request the UI makes for a visitor at *address*."""
    return {"headers": {CALLER_HEADER: address}}



async def _notices(client, token: str) -> list[dict]:
    r = await client.get("/auth/password-reset-notices", headers=_bearer(token))
    assert r.status_code == 200, r.text
    return r.json()


async def _acknowledge(client, token: str, ids: list[str]) -> None:
    r = await client.post("/auth/password-reset-notices/acknowledge", json={"ids": ids}, headers=_bearer(token))
    assert r.status_code == 200, r.text


async def _seen(client, token: str) -> list[dict]:
    """The notices, acknowledged as the user continues past them."""
    notices = await _notices(client, token)
    await _acknowledge(client, token, [n["id"] for n in notices])
    return notices


# --- The one reset function -------------------------------------------------------


async def test_reset_password_sets_hash_ends_sessions_and_audits(client, session):
    from celerp.models.auth import PasswordReset, PasswordResetNotice
    from celerp.services.password_reset import reset_password, user_by_email

    tokens = await _register(client)
    user = await user_by_email(session, OWNER)
    await reset_password(session, user, NEW_PW, method="terminal", os_user="สมชาย")

    assert not await _signed_in(client, tokens["access_token"])
    assert not await _refreshes(client, tokens["refresh_token"])
    assert (await _login(client, OWNER, OLD_PW)).status_code == 401
    assert (await _login(client, OWNER, NEW_PW)).status_code == 200
    reset = (await session.execute(select(PasswordReset))).scalar_one()
    assert (reset.user_id, reset.method, reset.os_user, reset.by_user_id) == (user.id, "terminal", "สมชาย", None)
    notices = (await session.execute(select(PasswordResetNotice))).scalars().all()
    assert [(n.reset_id, n.recipient_id) for n in notices] == [(reset.id, user.id)]


async def test_reset_password_is_atomic(client, session):
    """A notice that cannot be written leaves the password, the sessions and the
    record exactly as they were: the reset happens whole or not at all."""
    from celerp.models.auth import PasswordReset, PasswordResetNotice
    from celerp.services.password_reset import reset_password, user_by_email

    tokens = await _register(client)
    before = await _hash(session, OWNER)
    user = await user_by_email(session, OWNER)

    def _to_no_one(**kw):
        return PasswordResetNotice(**{**kw, "recipient_id": uuid.uuid4()})

    with patch("celerp.services.password_reset.PasswordResetNotice", side_effect=_to_no_one), \
         pytest.raises(Exception):
        await reset_password(session, user, NEW_PW, method="terminal")
    await session.rollback()

    assert await _hash(session, OWNER) == before
    assert await _signed_in(client, tokens["access_token"])
    assert (await session.execute(select(PasswordReset))).scalars().all() == []


# --- Recovery by emailed code -----------------------------------------------------


@pytest.mark.parametrize("failure", FAILURES)
async def test_recovery_fails_closed(client, session, relay, short_floor, failure):
    """Relay down, slow, without the endpoint, broken or talking nonsense: nothing is
    reset and the answer says the code could not be sent or checked."""
    relay.failure = failure
    await _register(client)
    before = await _hash(session, OWNER)

    send = await _send(client, OWNER)
    assert (send.status_code, send.json()) == (200, {"outcome": "unavailable"}), send.text

    # Up when the code was sent, failing when it is checked.
    relay.failure = None
    handle = await _start(client, OWNER)
    await settle_background()
    relay.failure = failure
    reset = await _reset(client, OWNER, handle)
    assert reset.status_code == 503 and reset.json()["outcome"] == "unavailable", reset.text

    assert await _hash(session, OWNER) == before
    assert (await _login(client, OWNER, OLD_PW)).status_code == 200


async def test_recovery_send_is_enumeration_neutral(client, session, relay, short_floor):
    """A user of this install and an email that is not get the same answer at the same
    pace, and only the user is sent a code. The reset answers the same way for both."""
    await _register(client)

    answers, took = [], []
    for email in (OWNER, "nobody@example.com"):
        started = time.monotonic()
        answers.append(await _send(client, email))
        took.append(time.monotonic() - started)
    assert [(r.status_code, sorted(r.json()), r.json()["outcome"]) for r in answers] == \
        [(200, ["handle", "outcome"], "sent")] * 2
    assert min(took) >= short_floor
    assert relay.sent_to() == [OWNER]
    assert relay.paths().count("/email-codes/status") == 2

    relay.calls.clear()
    started = time.monotonic()
    wrong = await _reset(client, OWNER, answers[0].json()["handle"], code="00000000")
    local_took = time.monotonic() - started
    started = time.monotonic()
    foreign = await _reset(client, "nobody@example.com", answers[1].json()["handle"])
    foreign_took = time.monotonic() - started
    assert (wrong.status_code, wrong.json()) == (foreign.status_code, foreign.json())
    assert min(local_took, foreign_took) >= short_floor
    assert [b["email"] for p, b in relay.calls if p == "/email-codes/verify"] == [OWNER]


async def test_recovery_send_says_when_no_code_is_coming(client, session, relay, short_floor):
    """Any answer to a send but a plain yes means no code is coming, and the answer says
    so; an email that is not a user still answers as sent."""
    await _register(client)
    relay.send_status = 503
    assert (await _send(client, OWNER)).json()["outcome"] == "unavailable"
    assert (await _send(client, "nobody@example.com")).json()["outcome"] == "sent"

    for status in (422, 429):
        relay.send_status = status
        assert (await _send(client, OWNER)).json()["outcome"] == "unavailable"


async def test_recovery_send_undeliverable_address_answers_alike(client, session, relay, short_floor):
    """An address the relay will never mail is answered the same whether or not it is a
    user here, and never reaches the relay."""
    await _register(client, "admin@localhost")
    for email in ("admin@localhost", "nobody@localhost", "no-at-sign", "a b@example.com"):
        assert (await _send(client, email)).json()["outcome"] == "undeliverable", email
    assert relay.calls == []
    assert (await _send(client, OWNER)).json()["outcome"] == "sent"


async def test_recovery_matches_users_exactly_as_sign_in(client, session, relay, short_floor):
    """Recovery finds a user exactly where sign-in does: another spelling of the email
    or a deactivated login is not a user of this install for either."""
    tokens = await _register(client)
    await _staff(client, session, tokens["access_token"])
    gone = await _user(session, STAFF)
    gone.is_active = False
    await session.commit()

    for email in (OWNER.upper(), f" {OWNER}", f"{OWNER} ", STAFF):
        assert (await _login(client, email, "userpw123" if email == STAFF else OLD_PW)).status_code == 401
        relay.calls.clear()
        handle = await _start(client, email)
        await settle_background()
        assert relay.sent_to() == [], email
        assert (await _reset(client, email, handle)).status_code == 400, email
        assert "/email-codes/verify" not in relay.paths(), email

    relay.calls.clear()
    await _send(client, OWNER)
    await settle_background()
    assert relay.sent_to() == [OWNER]
    assert (await _login(client, OWNER, OLD_PW)).status_code == 200


async def test_recovery_reset_resets_only_the_matching_user(client, session, relay, short_floor):
    tokens = await _register(client)
    await _staff(client, session, tokens["access_token"])
    owner_before = await _hash(session, OWNER)

    handle = await _start(client, STAFF)
    await settle_background()
    r = await _reset(client, STAFF, handle)
    assert (r.status_code, r.json()) == (200, {"ok": True}), r.text

    assert (await _login(client, STAFF, NEW_PW)).status_code == 200
    assert (await _login(client, STAFF, "userpw123")).status_code == 401
    assert await _hash(session, OWNER) == owner_before
    assert (await _login(client, OWNER, OLD_PW)).status_code == 200


async def test_recovery_reset_wrong_code_is_generic(client, session, relay, short_floor):
    """A wrong, malformed or used code and an email that is not a user here all get the
    one message, which says what to do next; nothing is reset."""
    await _register(client)
    before = await _hash(session, OWNER)
    generic = t("recovery.code_refused")
    handle = await _start(client, OWNER)
    await settle_background()

    answers = [await _reset(client, OWNER, handle, code="00000000"),
               await _reset(client, "nobody@example.com", handle),
               await _reset(client, OWNER, handle, code="12ab5678"),
               await _reset(client, OWNER, handle, code=CODE[:6]),
               await _reset(client, OWNER, handle, code="")]
    assert [(r.status_code, r.json()["detail"]) for r in answers] == [(400, generic)] * 5
    assert await _hash(session, OWNER) == before

    assert (await _reset(client, OWNER, handle)).status_code == 200
    used = await _reset(client, OWNER, handle, password="another-password1")
    assert (used.status_code, used.json()["detail"]) == (400, generic)
    assert (await _login(client, OWNER, NEW_PW)).status_code == 200


async def test_recovery_reset_never_touches_connect_state(client, session, relay, short_floor, monkeypatch):
    """Only the password changes: the Connect settings, the config file, the user's
    other fields and memberships stay as they were, and the relay is asked about the
    code under this install's existing id and nothing else."""
    import os

    from celerp.config import settings
    from celerp.gateway import state
    from celerp.models.accounting import UserCompany

    monkeypatch.setattr(settings, "gateway_instance_id", "inst-recovery-1")
    monkeypatch.setattr(settings, "gateway_token", "connect-token")
    monkeypatch.setattr(settings, "celerp_public_url", "https://recovery.example.com")
    tokens = await _register(client)
    config = os.environ.get("CELERP_CONFIG")
    config_before = Path(config).read_bytes() if config and Path(config).exists() else None
    connect_before = (settings.gateway_instance_id, settings.gateway_token, settings.celerp_public_url,
                      state.get_instance_id(), state.get_session_token())
    user = await _user(session, OWNER)
    fields = ("id", "email", "name", "is_active", "is_install_owner")
    user_before = {f: getattr(user, f) for f in fields}
    links_before = [(m.company_id, m.role, m.is_active) for m in (await session.execute(
        select(UserCompany).where(UserCompany.user_id == user.id))).scalars()]

    handle = await _start(client, OWNER)
    await settle_background()
    assert (await _reset(client, OWNER, handle)).status_code == 200

    await session.refresh(user)
    assert {f: getattr(user, f) for f in fields} == user_before
    assert [(m.company_id, m.role, m.is_active) for m in (await session.execute(
        select(UserCompany).where(UserCompany.user_id == user.id))).scalars()] == links_before
    assert (settings.gateway_instance_id, settings.gateway_token, settings.celerp_public_url,
            state.get_instance_id(), state.get_session_token()) == connect_before
    assert (Path(config).read_bytes() if config and Path(config).exists() else None) == config_before
    assert set(relay.paths()) == {"/email-codes/status", "/email-codes/send", "/email-codes/verify"}
    assert {b["instance_id"] for p, b in relay.calls if p != "/email-codes/status"} == {"inst-recovery-1"}
    assert not await _signed_in(client, tokens["access_token"])


async def test_recovery_reset_validates_password_before_relay(client, session, relay, short_floor):
    await _register(client)
    before = await _hash(session, OWNER)
    r = await _reset(client, OWNER, "any-handle", password="short")
    assert r.status_code == 422, r.text
    assert "8" in r.json()["detail"]
    assert relay.calls == []
    assert await _hash(session, OWNER) == before


# --- The terminal command ---------------------------------------------------------


async def _terminal(address: str = "127.0.0.1", headers: dict | None = None):
    async with AsyncClient(transport=ASGITransport(app=app, client=(address, 4321)), base_url="http://test") as c:
        r = await c.get("/auth/password-recovery/terminal", headers=headers or {})
    assert r.status_code == 200, r.text
    return r.json()["command"]


async def test_recovery_terminal_command_only_for_loopback(client):
    """The person at this computer is shown the command; a caller over the network,
    whether it reaches the API itself or through the UI, is not shown this
    computer's folders."""
    from celerp.services.password_reset import terminal_command

    assert await _terminal() == terminal_command()
    assert await _terminal("::1") == terminal_command()
    assert await _terminal("192.0.2.10") is None
    assert await _terminal(headers={CALLER_HEADER: "192.0.2.10"}) is None


async def test_recovery_terminal_command_not_for_connect_visitor(client):
    """A visitor through Connect is not shown the command, at the API or on the page."""
    from celerp.services.auth import CONNECT_HEADER
    from ui.app import app as ui_app

    assert await _terminal(headers={CONNECT_HEADER: "1"}) is None
    assert await _terminal("192.0.2.10", headers={CONNECT_HEADER: "1"}) is None

    with patch("ui.api_client._get_transport", lambda: ASGITransport(app=app)):
        async with AsyncClient(transport=ASGITransport(app=ui_app, client=("127.0.0.1", 5000)),
                               base_url="http://ui") as ui:
            local = await ui.get("/forgot-password/terminal")
            marked = await ui.get("/forgot-password/terminal", headers={CONNECT_HEADER: "1"})
    import sys
    assert local.status_code == marked.status_code == 200
    assert sys.executable in local.text
    assert sys.executable not in marked.text
    assert "celerp reset-password" in marked.text and 'href="/login"' in marked.text


@pytest.mark.parametrize("config", [None, "/srv/celerp/celerp.toml"])
async def test_terminal_command_renders_real_paths(monkeypatch, config):
    """The command carries this process's own interpreter, app folder, data folder and
    config file, quoted for the shell it is pasted into, whatever characters they hold.
    The config file is named even when it comes from the config folder rather than
    CELERP_CONFIG, so the terminal cannot fall back to another install's config."""
    import sys

    import celerp
    from celerp.config import settings
    from celerp.services.password_reset import terminal_command

    app_dir = str(Path(celerp.__file__).resolve().parent.parent)
    monkeypatch.setattr(sys, "executable", "/opt/it's here/bin/python3")
    monkeypatch.setattr(settings, "data_dir", Path("/data/O'Brien ข้อมูล"))
    if config:
        monkeypatch.setenv("CELERP_CONFIG", config)
    else:
        monkeypatch.delenv("CELERP_CONFIG", raising=False)
        monkeypatch.setenv("XDG_CONFIG_HOME", "/home/สมชาย/.config")
        monkeypatch.setenv("APPDATA", "/home/สมชาย/.config")
        config = "/home/สมชาย/.config/celerp/config.toml"

    posix = terminal_command(windows=False)
    words = shlex.split(posix)
    assert words[:3] == ["cd", app_dir, "&&"]
    assert "CELERP_DATA_DIR=/data/O'Brien ข้อมูล" in words
    assert f"CELERP_CONFIG={config}" in words
    assert words[words.index("/opt/it's here/bin/python3"):] == [
        "/opt/it's here/bin/python3", "-E", "-X", "utf8", "-m", "celerp", "reset-password"]

    monkeypatch.setattr(sys, "executable", r"C:\Users\สมชาย O'Neil\AppData\Local\Celerp\python\python.exe")
    monkeypatch.setattr(settings, "data_dir", Path(r"C:\Users\สมชาย O'Neil\AppData\Roaming\Celerp"))
    windows = terminal_command(windows=True)
    assert windows.startswith(f"Set-Location -LiteralPath '{app_dir.replace(chr(39), chr(39) * 2)}'; ")
    assert "$env:CELERP_DATA_DIR='C:\\Users\\สมชาย O''Neil\\AppData\\Roaming\\Celerp'" in windows
    assert f"$env:CELERP_CONFIG='{config}'" in windows
    assert windows.endswith(
        "& 'C:\\Users\\สมชาย O''Neil\\AppData\\Local\\Celerp\\python\\python.exe' -E -X utf8 -m celerp reset-password")


# --- The notice at the next sign-in ------------------------------------------------


async def test_reset_notices_stay_until_acknowledged(client, session):
    """Reading the notices changes nothing, so a page that never arrived shows them at the
    next sign-in; acknowledging removes only the ones named, and repeating it is harmless."""
    from celerp.services.password_reset import reset_password, user_by_email

    await _register(client)
    await reset_password(session, await user_by_email(session, OWNER), NEW_PW, method="terminal", os_user="tester")
    token = (await _login(client, OWNER, NEW_PW)).json()["access_token"]

    first = await _notices(client, token)
    assert [(n["user_email"], n["method"], n["os_user"], n["by_name"]) for n in first] == [
        (OWNER, "terminal", "tester", None)]
    assert first[0]["user_name"] == "Owner Person" and first[0]["created_at"]
    assert await _notices(client, token) == first

    await reset_password(session, await user_by_email(session, OWNER), "third-password-3", method="terminal")
    token = (await _login(client, OWNER, "third-password-3")).json()["access_token"]
    both = await _notices(client, token)
    assert len(both) == 2
    await _acknowledge(client, token, [first[0]["id"]])
    assert [n["id"] for n in await _notices(client, token)] == [n["id"] for n in both if n["id"] != first[0]["id"]]
    await _acknowledge(client, token, [n["id"] for n in both])
    await _acknowledge(client, token, [n["id"] for n in both])
    assert await _notices(client, token) == []
    assert (await client.get("/auth/password-reset-notices")).status_code == 401
    assert (await client.post("/auth/password-reset-notices/acknowledge", json={"ids": []})).status_code == 401


async def test_reset_notice_is_timed_in_the_company_timezone(client, session):
    from datetime import datetime, timezone

    from sqlalchemy import update

    from celerp.models.auth import PasswordReset
    from celerp.services.password_reset import reset_password, user_by_email

    await _register(client)
    await reset_password(session, await user_by_email(session, OWNER), NEW_PW, method="terminal")
    await session.execute(update(PasswordReset).values(created_at=datetime(2031, 3, 31, 17, 30, tzinfo=timezone.utc)))
    await session.commit()
    token = (await _login(client, OWNER, NEW_PW)).json()["access_token"]
    assert (await client.patch("/companies/me", headers=_bearer(token),
                               json={"settings": {"timezone": "Asia/Bangkok"}})).status_code == 200

    [notice] = await _notices(client, token)
    # Already the next day in Bangkok.
    assert (notice["created_at"], notice["timezone"]) == ("2031-04-01T00:30:00+07:00", "Asia/Bangkok")


async def test_reset_notice_reaches_other_owners(client, session):
    """The installation owner and every owner of the user's companies hear of a reset;
    other members do not, and an owner who did the reset is not told what they did."""
    from celerp.services.password_reset import reset_password, user_by_email

    owner = (await _register(client))["access_token"]
    second = await _staff(client, session, owner, "second-owner@example.com", "owner")
    viewer = await _staff(client, session, owner, "viewer@example.com", "viewer")
    await _staff(client, session, owner)

    staff = await user_by_email(session, STAFF)
    await reset_password(session, staff, NEW_PW, method="terminal")
    staff_token = (await _login(client, STAFF, NEW_PW)).json()["access_token"]
    assert [n["user_email"] for n in await _seen(client, staff_token)] == [STAFF]
    assert [n["user_email"] for n in await _seen(client, owner)] == [STAFF]
    assert [n["user_email"] for n in await _seen(client, second)] == [STAFF]
    assert await _notices(client, viewer) == []

    by = await user_by_email(session, "second-owner@example.com")
    await reset_password(session, await user_by_email(session, STAFF), "third-password1", method="owner", by_user=by)
    staff_token = (await _login(client, STAFF, "third-password1")).json()["access_token"]
    assert [(n["method"], n["by_name"]) for n in await _notices(client, staff_token)] == [("owner", by.name)]
    assert [n["user_email"] for n in await _notices(client, owner)] == [STAFF]
    assert await _notices(client, second) == []


# --- The attempt secret ------------------------------------------------------------


async def test_recovery_attempt_secret_ties_send_to_verify(client, session, relay, short_floor, caplog):
    """Each recovery carries one secret that only this install holds: a resend under its
    handle and its verify name the same one, a send without the handle starts another, and
    it never shows in an answer or a log."""
    caplog.set_level("DEBUG")
    tokens = await _register(client)
    await _staff(client, session, tokens["access_token"])
    answers = [await _send(client, OWNER)]
    handle = answers[0].json()["handle"]
    answers += [await _send(client, OWNER, handle), await _send(client, STAFF)]
    answers.append(await _reset(client, OWNER, handle))
    assert answers[1].json()["handle"] == handle
    assert [r.status_code for r in answers] == [200, 200, 200, 200], [r.text for r in answers]
    assert (await _login(client, OWNER, NEW_PW)).status_code == 200

    sends = relay.attempts("/email-codes/send", OWNER)
    assert len(sends) == 2 and len(set(sends)) == 1
    assert relay.attempts("/email-codes/verify", OWNER) == sends[:1]
    other = await _start(client, OWNER)
    assert other != handle and relay.attempts("/email-codes/send", OWNER)[-1] != sends[0]
    staff = relay.attempts("/email-codes/send", STAFF)
    assert len(staff) == 1 and staff[0] != sends[0]
    for secret in (sends[0], staff[0]):
        assert all(secret not in r.text and secret not in str(r.headers) for r in answers)
        assert secret not in caplog.text


async def test_recovery_after_a_code_is_used_starts_a_new_attempt(client, relay, short_floor):
    """A code that reset the password ends its attempt, even for a resend under its handle."""
    await _register(client)
    handle = await _start(client, OWNER)
    assert (await _reset(client, OWNER, handle)).status_code == 200
    assert (await _reset(client, OWNER, handle, password="another-password1")).status_code == 400
    assert await _start(client, OWNER, handle=handle) != handle
    first, second = relay.attempts("/email-codes/send", OWNER)
    assert first != second


async def test_recovery_attempt_expires_with_its_code(client, relay, short_floor, monkeypatch):
    """Past a code's life the secret goes with it, so a later request starts a new attempt."""
    from celerp.services import email_codes
    await _register(client)
    monkeypatch.setattr(email_codes, "ATTEMPT_TTL_S", 0.0)
    handle = await _start(client, OWNER)
    verifies = relay.paths().count("/email-codes/verify")
    assert (await _reset(client, OWNER, handle)).status_code == 400
    assert relay.paths().count("/email-codes/verify") == verifies
    assert await _start(client, OWNER, handle=handle) != handle
    first, second = relay.attempts("/email-codes/send", OWNER)
    assert first != second


async def test_recovery_lost_attempt_is_a_wrong_or_expired_code(client, session, relay, short_floor):
    """A secret this install no longer holds (it restarted) is answered as a wrong or expired
    code, and a new code works."""
    from celerp.services import email_codes
    await _register(client)
    before = await _hash(session, OWNER)
    handle = await _start(client, OWNER)
    email_codes._attempts.clear()
    r = await _reset(client, OWNER, handle)
    assert (r.status_code, r.json()["detail"]) == (400, t("recovery.code_refused")), r.text
    assert await _hash(session, OWNER) == before
    assert (await _reset(client, OWNER, await _start(client, OWNER))).status_code == 200


# --- Each recovery belongs to its requester ------------------------------------------

STRANGER, PERSON = "198.51.100.7", "203.0.113.20"


async def test_stranger_cannot_use_up_a_persons_recovery(client, session, relay, short_floor):
    """A stranger who starts recovery for a person's email and guesses until refused uses
    up only their own attempt: the person then starts recovery, gets their own handle and
    resets with their code."""
    await _register(client)
    before = await _hash(session, OWNER)
    stranger = await _start(client, OWNER, **_as(STRANGER))
    guesses = [await _reset(client, OWNER, stranger, code=f"{n:08d}", **_as(STRANGER)) for n in range(6)]
    assert [r.status_code for r in guesses] == [400] * 6
    assert await _hash(session, OWNER) == before
    assert (await _reset(client, OWNER, stranger, **_as(STRANGER))).status_code == 400

    person = await _start(client, OWNER, **_as(PERSON))
    assert person != stranger
    assert (await _reset(client, OWNER, person, **_as(PERSON))).status_code == 200
    assert (await _login(client, OWNER, NEW_PW)).status_code == 200


async def test_reset_with_a_handle_not_for_this_recovery_never_asks_the_relay(client, session, relay, short_floor):
    """Another address's handle, a made-up one and an expired one are refused as a wrong or
    expired code, without a word to the relay."""
    tokens = await _register(client)
    await _staff(client, session, tokens["access_token"])
    staff = await _start(client, STAFF)
    await _start(client, OWNER)
    await settle_background()
    relay.calls.clear()
    generic = (400, t("recovery.code_refused"))
    for handle in (staff, "x" * 32, ""):
        r = await _reset(client, OWNER, handle)
        assert (r.status_code, r.json()["detail"]) == generic, handle
    assert relay.calls == []
    assert (await _reset(client, STAFF, staff)).status_code == 200


async def test_an_older_yes_leaves_a_newer_recovery_alone(client, relay, short_floor):
    """Two recoveries of one email: the first one's code resetting the password leaves the
    second one's attempt in place, so its code still works."""
    await _register(client)
    first = await _start(client, OWNER, **_as(STRANGER))
    second = await _start(client, OWNER, **_as(PERSON))
    assert (await _reset(client, OWNER, first, **_as(STRANGER))).status_code == 200
    r = await _reset(client, OWNER, second, password="another-password1", **_as(PERSON))
    assert r.status_code == 200, r.text
    assert (await _login(client, OWNER, "another-password1")).status_code == 200



# --- Only recovery asks the relay for codes ------------------------------------------


async def test_only_recovery_asks_the_relay_for_codes(client, relay):
    """Setting up asks the relay nothing, and a recovery code request names only the
    email, this installation and the attempt's secret."""
    assert (await client.post("/auth/setup-email/send", json={"email": OWNER})).status_code == 404
    assert (await client.post("/auth/setup-email/verify", json={"email": OWNER, "code": CODE})).status_code == 404
    await _register(client)
    await settle_background()
    assert relay.calls == []
    assert (await _send(client, OWNER)).json()["outcome"] == "sent"
    await settle_background()
    assert [sorted(body) for path, body in relay.calls if path == "/email-codes/send"] == [["attempt", "email", "instance_id"]]


# --- An owner resets a staff password ------------------------------------------------


def _staff_reset(client, token: str, user_id, password: str = NEW_PW):
    return client.post(f"/companies/me/users/{user_id}/password", json={"new_password": password},
                       headers=_bearer(token))


async def test_owner_resets_staff_password(client, session):
    """The installation owner, or any owner of the company, sets a staff member's new
    password; the staff member's sessions end and they are told who did it."""
    from celerp.models.auth import PasswordReset

    owner = (await _register(client))["access_token"]
    second = await _staff(client, session, owner, "second-owner@example.com", "owner")
    staff_token = await _staff(client, session, owner)
    staff = await _user(session, STAFF)

    r = await _staff_reset(client, owner, staff.id)
    assert (r.status_code, r.json()) == (200, {"ok": True}), r.text
    assert not await _signed_in(client, staff_token)
    assert (await _login(client, STAFF, NEW_PW)).status_code == 200

    r = await _staff_reset(client, second, staff.id, "third-password1")
    assert r.status_code == 200, r.text
    resets = (await session.execute(select(PasswordReset).order_by(PasswordReset.created_at))).scalars().all()
    assert [(x.user_id, x.method, x.by_user_id) for x in resets] == [
        (staff.id, "owner", (await _user(session, OWNER)).id),
        (staff.id, "owner", (await _user(session, "second-owner@example.com")).id)]
    token = (await _login(client, STAFF, "third-password1")).json()["access_token"]
    assert {n["by_name"] for n in await _notices(client, token)} == {"Owner Person", "Owner"}


async def test_staff_reset_refusals(client, session):
    """Each refusal says why, the same when asked before a password is typed, and
    changes nothing: an owner, the installation owner,
    yourself, a member of a company the caller does not own, someone outside the
    company, and a caller who is not an owner."""
    owner = (await _register(client))["access_token"]
    second = await _staff(client, session, owner, "second-owner@example.com", "owner")
    admin = await _staff(client, session, owner, "admin@example.com", "admin")
    await _staff(client, session, owner)
    ids = {e: (await _user(session, e)).id for e in (OWNER, "second-owner@example.com", "admin@example.com", STAFF)}

    # The staff member also works for a second company only the installation owner owns.
    other = (await client.post("/companies", json={"name": "Other Co"}, headers=_bearer(owner))).json()
    r = await client.post("/companies/me/users", headers=_bearer(other["access_token"]),
                          json={"email": STAFF, "name": "Staff", "role": "operator", "password": "userpw123"})
    assert r.status_code == 200, r.text
    outsider = (await client.post("/companies/me/users", headers=_bearer(other["access_token"]),
                                  json={"email": "outsider@example.com", "name": "Out", "role": "operator",
                                        "password": "userpw123"})).json()["id"]

    hashes = {e: await _hash(session, e) for e in ids}
    cases = [
        (second, ids[OWNER], 403),                      # the installation owner
        (owner, ids["second-owner@example.com"], 403),  # an owner
        (second, ids["second-owner@example.com"], 403),  # yourself
        (second, ids[STAFF], 403),                      # also in a company the caller does not own
        (admin, ids[STAFF], 403),                       # the caller is not an owner
        (owner, outsider, 404),                         # not in this company
        (owner, uuid.uuid4(), 404),                     # no such user
    ]
    for token, target, status in cases:
        asked = await client.get(f"/companies/me/users/{target}/password", headers=_bearer(token))
        r = await _staff_reset(client, token, target)
        assert r.status_code == status, (target, r.text)
        assert isinstance(r.json()["detail"], str) and r.json()["detail"], r.text
        assert (asked.status_code, asked.json()) == (r.status_code, r.json()), (target, asked.text)
    r = await _staff_reset(client, owner, ids[STAFF], "short")
    assert r.status_code == 422 and "8" in r.json()["detail"], r.text
    assert {e: await _hash(session, e) for e in ids} == hashes

    # The installation owner owns every company the staff member is in.
    asked = await client.get(f"/companies/me/users/{ids[STAFF]}/password", headers=_bearer(owner))
    assert (asked.status_code, asked.json()) == (200, {"ok": True})
    assert {e: await _hash(session, e) for e in ids} == hashes
    assert (await _staff_reset(client, owner, ids[STAFF])).status_code == 200


async def test_staff_reset_locks_target(client, session):
    """The staff member's login row is locked before their memberships and roles are
    read, the order a role change keeps, so a promotion to owner made at the same
    moment either waits for the reset or is seen by it. FOR NO KEY UPDATE, so a sign-in
    of theirs holding their session state can still record its session."""
    owner = (await _register(client))["access_token"]
    await _staff(client, session, owner)
    target = str((await _user(session, STAFF)).id)
    statements: list[str] = []

    def _record(conn, cursor, statement, parameters, context, executemany):
        if target in str(parameters) or target.replace("-", "") in str(parameters):
            statements.append(" ".join(statement.split()))

    sync_conn = (await session.connection()).sync_connection
    event.listen(sync_conn, "before_cursor_execute", _record)
    try:
        r = await _staff_reset(client, owner, target)
    finally:
        event.remove(sync_conn, "before_cursor_execute", _record)
    assert r.status_code == 200, r.text
    first = statements[0]
    assert "FROM users" in first and "FOR NO KEY UPDATE" in first, statements


# --- The reset link is gone ------------------------------------------------------------


async def test_reset_link_routes_gone(client, owner_ui):
    for path, body in (("/auth/password-reset/request", {"email": OWNER}),
                       ("/auth/password-reset/confirm", {"token": "x", "new_password": NEW_PW})):
        assert (await client.post(path, json=body)).status_code == 404, path
    assert (await owner_ui.get("/reset-password?token=x")).status_code == 404
    assert (await owner_ui.post("/reset-password", data={"token": "x"})).status_code in (404, 405)
