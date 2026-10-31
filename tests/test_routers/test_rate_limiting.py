# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Rate limiting on the sign-in routes.

The UI server calls the API over loopback for every visitor, so a limit counted per
connection would be one bucket for everyone. Failed password checks are counted per
account across every caller, and per caller across every account, and slow the next
try down rather than shut anyone out.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from celerp.main import app
from celerp.routers import auth as auth_router
from celerp.services.auth import CALLER_HEADER, CONNECT_CALLER, CONNECT_HEADER, request_caller
from email_codes_support import CODE, relay, short_floor  # noqa: F401 - fixtures
from migration_support import real_client, real_engine  # noqa: F401 - fixtures


@pytest.fixture
def limits_on():
    auth_router.limiter.enabled = True
    auth_router.limiter._storage.reset()
    yield
    auth_router.limiter.enabled = False
    auth_router.limiter._storage.reset()


SEND, RESET = "/auth/password-recovery/send", "/auth/password-recovery/reset"


async def _register(client, email: str) -> None:
    r = await client.post(
        "/auth/register",
        json={"company_name": "RL Co", "email": email, "name": "RL", "password": "pwvalid1"},
    )
    assert r.status_code == 200


async def _bad_logins(client, email: str, n: int, headers: dict | None = None) -> list[int]:
    return [(await client.post("/auth/login", json={"email": email, "password": "wrong-pw"}, headers=headers)).status_code
            for _ in range(n)]


def test_auth_limiter_imported_and_present():
    """celerp.routers.auth exports a module-level 'limiter' for login rate limiting."""
    assert hasattr(auth_router, "limiter"), "auth module has no module-level 'limiter'"


def test_the_api_has_no_second_limiter():
    """The sign-in limiter is the only one: the app carries no other limiter that would
    look configured while limiting nothing."""
    import celerp.main

    assert not hasattr(app.state, "limiter")
    assert not hasattr(celerp.main, "limiter")


@pytest.mark.asyncio
async def test_login_endpoint_accepts_valid_request(client):
    """Login endpoint is reachable (limiter doesn't block valid first request)."""
    await _register(client, "rl@test.example")
    r = await client.post("/auth/login", json={"email": "rl@test.example", "password": "pwvalid1"})
    assert r.status_code == 200
    assert "access_token" in r.json()


@pytest.mark.asyncio
async def test_rate_limit_exceeded_handler_returns_429(client):
    """RateLimitExceeded exception handler returns 429 JSON."""
    from slowapi.errors import RateLimitExceeded

    assert RateLimitExceeded in app.exception_handlers


@pytest.mark.asyncio
async def test_recovery_limits_count_each_requester(client, limits_on, relay, short_floor):
    """Recovery sends and resets are limited per requester: a stranger who reaches the
    limits for a person's email leaves the person free to recover."""
    await _register(client, "a@test.example")
    stranger, person = {CALLER_HEADER: "198.51.100.7"}, {CALLER_HEADER: "203.0.113.20"}

    def reset(handle, code, headers):
        return client.post(RESET, json={"email": "a@test.example", "handle": handle, "code": code,
                                        "new_password": "pwvalid2"}, headers=headers)

    sends = [await client.post(SEND, json={"email": "a@test.example"}, headers=stranger) for _ in range(4)]
    assert [r.status_code for r in sends] == [200, 200, 200, 429]
    guesses = [await reset(sends[0].json()["handle"], "00000000", stranger) for _ in range(11)]
    assert [r.status_code for r in guesses] == [400] * 10 + [429]

    sent = await client.post(SEND, json={"email": "a@test.example"}, headers=person)
    assert sent.status_code == 200, sent.text
    assert (await reset(sent.json()["handle"], CODE, person)).status_code == 200


@pytest.mark.asyncio
async def test_one_requester_cannot_use_the_installs_mail(client, limits_on, relay, short_floor, monkeypatch):
    """One requester's sends, however spaced, stop well inside the relay's mail allowance
    for this install (60 an hour per network)."""
    import limits.storage.memory

    now = [1_000_000.0]
    monkeypatch.setattr(limits.storage.memory.time, "time", lambda: now[0])
    sent = 0
    for n in range(60):
        r = await client.post(SEND, json={"email": f"n{n}@test.example"}, headers={CALLER_HEADER: "198.51.100.7"})
        sent += r.status_code == 200
        now[0] += 60
    assert 0 < sent <= 20


@pytest.mark.asyncio
async def test_the_ui_names_the_visitor_it_sees_never_one_the_browser_claims():
    """Every call the UI makes into the API carries the address of the connection it is
    serving; a caller header sent by the browser is not what it passes on."""
    from ui import api_client

    sent = {}

    async def page(scope, receive, send):
        async with api_client._local_client(None) as c:
            sent["caller"] = c.headers.get(CALLER_HEADER)
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    ui = AsyncClient(transport=ASGITransport(app=api_client.CallerMiddleware(page), client=("198.51.100.7", 5000)),
                     base_url="http://ui")
    async with ui:
        assert (await ui.get("/", headers={CALLER_HEADER: "192.0.2.1"})).status_code == 204
    assert sent["caller"] == "198.51.100.7"
    assert api_client._caller.get() is None


_VIA = {CONNECT_HEADER: "1"}


@pytest.mark.asyncio
@pytest.mark.parametrize("peer, sent, named", [
    ("127.0.0.1", {**_VIA, CALLER_HEADER: "203.0.113.20"}, "203.0.113.20"),
    ("::1", {**_VIA, CALLER_HEADER: "2001:db8:5::20"}, "2001:db8:5::20"),
    ("127.0.0.1", {**_VIA, CALLER_HEADER: "::ffff:198.51.100.3"}, "198.51.100.3"),
    ("127.0.0.1", _VIA, CONNECT_CALLER),
    ("127.0.0.1", {**_VIA, CALLER_HEADER: ""}, CONNECT_CALLER),
    ("127.0.0.1", {**_VIA, CALLER_HEADER: "nonsense"}, CONNECT_CALLER),
    ("127.0.0.1", {**_VIA, CALLER_HEADER: "203.0.113.20, 192.0.2.1"}, CONNECT_CALLER),
    ("127.0.0.1", {**_VIA, CALLER_HEADER: "127.0.0.1"}, CONNECT_CALLER),
    ("192.168.1.50", {**_VIA, CALLER_HEADER: "203.0.113.20"}, "192.168.1.50"),
    ("127.0.0.1", {CALLER_HEADER: "203.0.113.20"}, "127.0.0.1"),
    ("192.168.1.50", {CALLER_HEADER: "203.0.113.20"}, "192.168.1.50"),
])
async def test_the_ui_names_a_connect_visitor_by_the_gateways_address_only_from_loopback(peer, sent, named):
    """A Connect visitor reaches the UI from the gateway on this computer, which names them
    by the address the relay saw. The UI takes that address only from a loopback peer that
    carries the Connect mark; a marked request without a valid one is a Connect visitor all
    the same, never the person at this computer; any other peer is named by its socket."""
    from ui import api_client

    sent_on = {}

    async def page(scope, receive, send):
        async with api_client._local_client(None) as c:
            sent_on["caller"] = c.headers.get(CALLER_HEADER)
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    ui = AsyncClient(transport=ASGITransport(app=api_client.CallerMiddleware(page), client=(peer, 5000)),
                     base_url="http://ui")
    async with ui:
        assert (await ui.get("/", headers=sent)).status_code == 204
    assert sent_on["caller"] == named


def _counted(peer: str, headers: list[tuple[str, str]]) -> str:
    from starlette.requests import Request

    return request_caller(Request({"type": "http", "client": (peer, 4000),
                                   "headers": [(k.lower().encode(), v.encode()) for k, v in headers]}))


@pytest.mark.parametrize("peer, headers, counted", [
    ("127.0.0.1", [(CONNECT_HEADER, "1"), (CALLER_HEADER, "203.0.113.20")], "203.0.113.20"),
    ("127.0.0.1", [(CONNECT_HEADER, "1"), (CALLER_HEADER, "2001:db8:5::20")], "2001:db8:5::/64"),
    ("127.0.0.1", [(CONNECT_HEADER, "1"), (CALLER_HEADER, "::ffff:198.51.100.3")], "198.51.100.3"),
    ("127.0.0.1", [(CONNECT_HEADER, "1")], CONNECT_CALLER),
    ("127.0.0.1", [(CONNECT_HEADER, "1"), (CALLER_HEADER, CONNECT_CALLER)], CONNECT_CALLER),
    ("127.0.0.1", [(CONNECT_HEADER, "1"), (CALLER_HEADER, "127.0.0.1")], CONNECT_CALLER),
    ("127.0.0.1", [(CONNECT_HEADER, "1"), (CALLER_HEADER, "")], CONNECT_CALLER),
    ("127.0.0.1", [(CONNECT_HEADER, "1"), (CALLER_HEADER, "203.0.113.20, 192.0.2.1")], CONNECT_CALLER),
    ("127.0.0.1", [(CONNECT_HEADER, "1"), (CALLER_HEADER, "203.0.113.20"), (CALLER_HEADER, "192.0.2.1")],
     CONNECT_CALLER),
    ("192.0.2.10", [(CONNECT_HEADER, "1"), (CALLER_HEADER, "203.0.113.20")], "192.0.2.10"),
    ("127.0.0.1", [(CALLER_HEADER, "192.168.1.50")], "192.168.1.50"),
    ("127.0.0.1", [], "127.0.0.1"),
])
def test_the_api_counts_a_connect_visitor_apart_from_the_person_at_this_computer(peer, headers, counted):
    """Over loopback a Connect-marked request counts as the visitor's address, an IPv6 one
    by its /64, or as the Connect caller when it names no valid one: never as this computer,
    which is the person at it."""
    assert _counted(peer, headers) == counted


def _ask(client, path: str, email: str, password: str, caller: str):
    return client.post(path, json={"email": email, "password": password, "company_name": "Next Co"},
                       headers={CALLER_HEADER: caller})


@pytest.fixture
def clock(monkeypatch):
    """The time the sign-in waits are measured on, moved on by the test instead of slept."""
    now = [1000.0]
    monkeypatch.setattr("celerp.services.auth._clock", lambda: now[0], raising=False)
    return now


async def _two_logins(client, session) -> None:
    from test_helpers import clear_sessions as clear

    r = await client.post("/auth/register", json={"company_name": "RL Co", "email": "v@test.example", "name": "V",
                                                  "password": "pwvalid1"})
    assert r.status_code == 200, r.text
    r = await client.post("/companies/me/users", headers={"Authorization": f"Bearer {r.json()['access_token']}"},
                          json={"name": "W", "email": "w@test.example", "password": "pwvalid1", "role": "operator"})
    assert r.status_code == 200, r.text
    await clear(session)


@pytest.mark.asyncio
async def test_an_account_slows_down_after_five_failures_from_every_address_together(client, session, clock):
    """Five wrong passwords go through at once, from any addresses and however the email is
    typed; then the account waits a second before the next try, answered at once with 429,
    Retry-After and when to try again. The right password waits too, and signs in once the
    wait is over; every other account signs in as before."""
    from test_helpers import clear_sessions as clear

    await _two_logins(client, session)
    names = ["v@test.example", " V@Test.Example ", "V@TEST.EXAMPLE", "v@test.example ", "v@test.example"]
    tries = [(await _ask(client, "/auth/login", names[i], "wrong-pw", f"198.51.100.{i}")).status_code
             for i in range(5)]
    assert tries == [401] * 5
    for path in ("/auth/login", "/auth/login-force", "/auth/start-company"):
        r = await _ask(client, path, "v@test.example", "pwvalid1", "192.0.2.50")
        assert r.status_code == 429, (path, r.text)
        assert r.headers["Retry-After"] == "1", r.headers
        assert "Try again in 1 second" in r.json()["detail"], r.text
    assert (await _ask(client, "/auth/login", "w@test.example", "pwvalid1", "198.51.100.1")).status_code == 200
    await clear(session)

    clock[0] += 1
    assert (await _ask(client, "/auth/login", "v@test.example", "pwvalid1", "192.0.2.50")).status_code == 200
    await clear(session)
    # Signing in starts the account's count again.
    tries = [(await _ask(client, "/auth/login", "v@test.example", "wrong-pw", f"198.51.101.{i}")).status_code
             for i in range(6)]
    assert tries == [401] * 5 + [429]


@pytest.mark.asyncio
async def test_each_further_failure_doubles_the_wait_up_to_five_minutes(client, clock):
    """Past the five free failures each one doubles the wait from one second, never past five
    minutes, so the account's owner is never shut out for long."""
    import time

    started = time.monotonic()
    assert await _bad_logins(client, "a@test.example", 5) == [401] * 5
    waits = []
    for i in range(12):
        r = await _ask(client, "/auth/login", "a@test.example", "wrong-pw", f"198.51.100.{i}")
        assert r.status_code == 429, r.text
        waits.append(int(r.headers["Retry-After"]))
        clock[0] += waits[-1]
        assert (await _ask(client, "/auth/login", "a@test.example", "wrong-pw", f"198.51.100.{i}")).status_code == 401
    assert waits == [1, 2, 4, 8, 16, 32, 64, 128, 256, 300, 300, 300]
    # Every refusal is answered at once: nothing waits on the server.
    assert time.monotonic() - started < 30


@pytest.mark.parametrize("writer", ["reset", "another process"])
@pytest.mark.asyncio
async def test_attempts_restart_when_hash_changes(client, session, clock, writer):
    """A new password lifts the account's wait, whichever way it was set: by the reset in
    this server, or by the terminal command writing the database from its own process."""
    from celerp.services.auth import hash_password
    from celerp.services.password_reset import reset_password, user_by_email

    await _two_logins(client, session)
    tries = [(await _ask(client, "/auth/login", "v@test.example", "wrong-pw", f"198.51.100.{i}")).status_code
             for i in range(6)]
    assert tries == [401] * 5 + [429]
    user = await user_by_email(session, "v@test.example")
    if writer == "reset":
        await reset_password(session, user, "pwvalid2", method="terminal")
    else:
        user.auth_hash = hash_password("pwvalid2")
        await session.commit()
    assert (await _ask(client, "/auth/login", "v@test.example", "pwvalid2", "192.0.2.50")).status_code == 200
    assert (await _ask(client, "/auth/login", "w@test.example", "wrong-pw", "192.0.2.51")).status_code == 401


@pytest.mark.asyncio
async def test_one_address_cannot_try_many_accounts(client, session, clock):
    """One address takes twenty failures across every account together, then waits, so it
    cannot go through account after account; another address is not held up, and a right
    password from the address never counts against it."""
    await _two_logins(client, session)
    tries = [(await _ask(client, "/auth/login", f"a{i}@test.example", "wrong-pw", "198.51.100.1")).status_code
             for i in range(19)]
    assert tries == [401] * 19
    assert (await _ask(client, "/auth/login", "v@test.example", "pwvalid1", "198.51.100.1")).status_code == 200
    assert (await _ask(client, "/auth/login", "a19@test.example", "wrong-pw", "198.51.100.1")).status_code == 401
    r = await _ask(client, "/auth/login", "a20@test.example", "wrong-pw", "198.51.100.1")
    assert r.status_code == 429 and r.headers["Retry-After"] == "1", r.text
    assert (await _ask(client, "/auth/login", "a20@test.example", "wrong-pw", "198.51.100.2")).status_code == 401


@pytest.mark.asyncio
async def test_an_ipv6_visitor_counts_by_its_64(client, clock):
    """An IPv6 visitor holds a whole /64, so every address in it is one caller; a visitor in
    the next /64 is another."""
    tries = [(await _ask(client, "/auth/login", f"a{i}@test.example", "wrong-pw", f"2001:db8::{i + 1:x}")).status_code
             for i in range(21)]
    assert tries == [401] * 20 + [429]
    assert (await _ask(client, "/auth/login", "b@test.example", "wrong-pw", "2001:db8:0:1::1")).status_code == 401


@pytest.mark.asyncio
async def test_the_caller_the_ui_names_counts_only_over_loopback(client, clock):
    """Over loopback the caller the UI names is its own address. From any other address the
    header is ignored, so no one can pick a fresh address for each attempt."""
    remote = AsyncClient(transport=ASGITransport(app=app, client=("203.0.113.9", 4000)), base_url="http://test")
    async with remote:
        tries = [(await remote.post("/auth/login", json={"email": f"c{i}@test.example", "password": "wrong-pw"},
                                    headers={CALLER_HEADER: f"198.51.100.{i}"})).status_code for i in range(21)]
    assert tries == [401] * 20 + [429]


@pytest.mark.asyncio
@pytest.mark.parametrize("shared", ["127.0.0.1", "::1"])
async def test_a_shared_local_address_never_holds_up_another_accounts_sign_in(client, session, clock, shared):
    """The desktop user and any proxy on this machine reach the UI from a loopback
    address, so it names no one visitor: failures from it never hold up an account
    with no failures of its own, while each account still slows after its own five."""
    from test_helpers import clear_sessions as clear

    await _two_logins(client, session)
    tries = [(await _ask(client, "/auth/login", f"a{i}@test.example", "wrong-pw", shared)).status_code
             for i in range(30)]
    assert tries == [401] * 30
    assert (await _ask(client, "/auth/login", "v@test.example", "pwvalid1", shared)).status_code == 200
    await clear(session)
    assert (await _ask(client, "/auth/login", "w@test.example", "pwvalid1", shared)).status_code == 200
    tries = [(await _ask(client, "/auth/login", "a0@test.example", "wrong-pw", shared)).status_code
             for i in range(5)]
    assert tries == [401] * 4 + [429]


_ROUTES = {
    "/auth/login": lambda e, p: {"json": {"email": e, "password": p}},
    "/auth/login-force": lambda e, p: {"json": {"email": e, "password": p}},
    "/auth/start-company": lambda e, p: {"json": {"email": e, "password": p, "company_name": "Next Co"}},
    "/company-backups/start-company/read": lambda e, p: {
        "files": {"file": ("x.celerp-company", b"x")}, "data": {"email": e, "password": p}},
    "/company-backups/start-company/restore": lambda e, p: {
        "json": {"email": e, "password": p, "upload_token": "t", "plan_fingerprint": "f"}},
    "/migrations/start-company/scan": lambda e, p: {"auth": (e, p), "content": b""},
    "/migrations/start-company/start": lambda e, p: {
        "json": {"email": e, "password": p, "scan_token": "t", "company_name": "Next Co", "timezone": "UTC"}},
}


def _count_checks(monkeypatch) -> list:
    """Every password check made, on whichever module makes it."""
    from celerp.services import auth as auth_service

    checks = []
    real = auth_service.verify_password

    def counted(plain, hashed):
        checks.append(plain)
        return real(plain, hashed)

    for where in ("celerp.services.auth", "celerp.routers.auth", "celerp.services.company_backup"):
        monkeypatch.setattr(f"{where}.verify_password", counted, raising=False)
    return checks


@pytest.mark.asyncio
@pytest.mark.parametrize("path", list(_ROUTES))
async def test_concurrent_wrong_passwords_check_only_the_free_five(real_engine, real_client, monkeypatch, clock, path):
    """Twelve wrong passwords for one account arrive at once, each from its own address: five
    are checked and refused, the other seven are told to wait without being checked, on every
    route that takes a password."""
    from celerp.models.company import User
    from celerp.services.auth import hash_password
    from migration_support import maker

    email = f"burst-{uuid.uuid4().hex[:6]}@test.example"
    async with maker(real_engine)() as s:
        s.add(User(id=uuid.uuid4(), email=email, name="U", auth_hash=hash_password("pwvalid1"), is_active=True))
        await s.commit()
    checks = _count_checks(monkeypatch)
    replies = await asyncio.gather(*[
        real_client.post(path, headers={CALLER_HEADER: f"198.51.100.{i + 1}"}, **_ROUTES[path](email, f"guess{i}"))
        for i in range(12)])
    codes = sorted(r.status_code for r in replies)
    assert len(checks) == 5, (len(checks), codes)
    assert codes == [401] * 5 + [429] * 7, [r.text[:120] for r in replies]


@pytest.mark.asyncio
async def test_concurrent_wrong_current_passwords_check_only_the_free_five(client, monkeypatch, clock):
    """Changing a password checks the current one, so it is one more way to try a password."""
    r = await client.post("/auth/register", json={"company_name": "RL Co", "email": "v@test.example", "name": "V",
                                                  "password": "pwvalid1"})
    headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
    checks = _count_checks(monkeypatch)
    codes = [(await client.post("/auth/change-password", headers={**headers, CALLER_HEADER: f"198.51.100.{i}"},
                                json={"current_password": "wrong-pw", "new_password": "pwvalid2"})).status_code
             for i in range(7)]
    assert codes == [400] * 5 + [429] * 2 and len(checks) == 5
    clock[0] += 1
    r = await client.post("/auth/change-password", headers=headers,
                          json={"current_password": "pwvalid1", "new_password": "pwvalid2"})
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_concurrent_wrong_passwords_reopening_a_restore_check_only_the_free_five(real_engine, monkeypatch, clock):
    """A first owner reopening a finished restore gives a password too, counted the same way."""
    from celerp.models.company import Company, User
    from celerp.models.accounting import UserCompany
    from celerp.services import company_backup as cb
    from celerp.services.auth import hash_password
    from migration_support import maker

    email = f"reopen-{uuid.uuid4().hex[:6]}@test.example"
    company_id, user_id = uuid.uuid4(), uuid.uuid4()
    async with maker(real_engine)() as s:
        s.add(User(id=user_id, email=email, name="U", auth_hash=hash_password("pwvalid1"), is_active=True))
        await s.flush()
        s.add(Company(id=company_id, name="R Co", slug=f"r-{company_id.hex[:8]}"))
        await s.flush()
        s.add(UserCompany(user_id=user_id, company_id=company_id, role="owner", is_active=True))
        await s.commit()
    checks = _count_checks(monkeypatch)

    async def reopen(i):
        try:
            return await cb.reopen_restored(str(company_id), owner_account={"email": email, "password": f"g{i}"},
                                            caller=f"198.51.100.{i + 1}")
        except Exception as exc:  # the wait is an HTTP refusal
            return getattr(exc, "status_code", exc)

    got = await asyncio.gather(*[reopen(i) for i in range(12)])
    assert len(checks) == 5, got
    assert sorted(map(str, got)) == ["401"] * 5 + ["429"] * 7
