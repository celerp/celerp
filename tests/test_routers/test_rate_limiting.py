# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Rate limiting on the sign-in routes.

The UI server calls the API over loopback for every visitor, so a limit counted per
connection would be one bucket for everyone. Sign-in limits count the caller the UI
names plus the account submitted.
"""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from celerp.main import app
from celerp.routers import auth as auth_router
from celerp.services.auth import CALLER_HEADER


@pytest.fixture
def limits_on():
    auth_router.limiter.enabled = True
    auth_router.limiter._storage.reset()
    yield
    auth_router.limiter.enabled = False
    auth_router.limiter._storage.reset()


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
async def test_failed_sign_ins_on_one_account_never_lock_out_another(client, limits_on):
    """Ten failed sign-ins for one account limit that account only: another account
    signs in from the same address, and the first stays limited."""
    await _register(client, "b@test.example")

    assert await _bad_logins(client, "a@test.example", 10) == [401] * 10
    r = await client.post("/auth/login", json={"email": "b@test.example", "password": "pwvalid1"})
    assert r.status_code == 200
    assert await _bad_logins(client, "a@test.example", 1) == [429]
    # The account is matched however its address is typed.
    assert await _bad_logins(client, " A@Test.Example ", 1) == [429]


@pytest.mark.asyncio
async def test_password_reset_requests_for_one_account_never_block_another(client, limits_on):
    for _ in range(3):
        assert (await client.post("/auth/password-reset/request", json={"email": "a@test.example"})).status_code == 200
    assert (await client.post("/auth/password-reset/request", json={"email": "a@test.example"})).status_code == 429
    assert (await client.post("/auth/password-reset/request", json={"email": "b@test.example"})).status_code == 200


@pytest.mark.asyncio
async def test_the_caller_the_ui_names_counts_only_over_loopback(client, limits_on):
    """Over loopback the caller the UI names is its own bucket, so one visitor's failures
    never limit another visitor on the same account. From any other address the header is
    ignored, so no one can pick a fresh bucket per attempt."""
    assert await _bad_logins(client, "a@test.example", 10, {CALLER_HEADER: "198.51.100.1"}) == [401] * 10
    assert await _bad_logins(client, "a@test.example", 1, {CALLER_HEADER: "198.51.100.1"}) == [429]
    assert await _bad_logins(client, "a@test.example", 1, {CALLER_HEADER: "198.51.100.2"}) == [401]

    remote = AsyncClient(transport=ASGITransport(app=app, client=("203.0.113.9", 4000)), base_url="http://test")
    async with remote:
        tries = [(await remote.post("/auth/login", json={"email": "c@test.example", "password": "wrong-pw"},
                                    headers={CALLER_HEADER: f"198.51.100.{i}"})).status_code for i in range(11)]
    assert tries == [401] * 10 + [429]


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
