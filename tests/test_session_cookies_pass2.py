# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Session-cookie convergence (Phase F: F10, F11).

Every route that seats or tears down a session must write both session cookies
through the shared helpers, so a new company gets a complete, sliding session and
a logout actually clears the browser's real cookies. These guards pin two
concrete regressions: create-company dropped the refresh token, and the
deactivate path deleted a cookie named "token" that never existed.
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from unittest.mock import AsyncMock, patch

from test_helpers import authed_cookies


@pytest_asyncio.fixture
async def ui_client():
    from ui.app import app as ui_app
    async with AsyncClient(
        transport=ASGITransport(app=ui_app),
        base_url="http://ui",
        follow_redirects=False,
    ) as c:
        yield c


class _FakeResp:
    def __init__(self, status: int, payload: dict):
        self.status_code = status
        self._payload = payload
        self.headers = {"content-type": "application/json"}

    def json(self) -> dict:
        return self._payload


class _FakeClient:
    """Async-context client stub that answers the deactivate route's two calls:
    DELETE /companies/me and GET /auth/my-companies."""

    def __init__(self, remaining: int):
        self._remaining = remaining

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def delete(self, path: str):
        return _FakeResp(200, {})

    async def get(self, path: str):
        return _FakeResp(200, {"total": self._remaining})


def _set_cookie_blob(response) -> str:
    """All Set-Cookie headers on a response joined into one searchable string."""
    return " ".join(response.headers.get_list("set-cookie"))


@pytest.mark.asyncio
async def test_new_company_seats_both_session_cookies(ui_client):
    """POST /setup/new-company writes the access AND refresh cookie for the new
    company, not the access cookie alone."""
    with patch(
        "ui.api_client.create_company",
        new=AsyncMock(return_value=("acc-new", "ref-new")),
    ):
        r = await ui_client.post(
            "/setup/new-company",
            data={"company_name": "Second Co"},
            cookies=authed_cookies("owner"),
        )
    assert r.status_code == 302
    assert r.headers.get("location") == "/setup/company"
    assert r.cookies.get("celerp_token") == "acc-new"
    assert r.cookies.get("celerp_refresh") == "ref-new"


@pytest.mark.asyncio
async def test_deactivate_with_remaining_companies_clears_session(ui_client):
    """Deactivating a company while others remain sends the user to login with the
    real session cookies cleared, not a phantom 'token' cookie."""
    with patch("ui.api_client._local_client", new=lambda *a, **k: _FakeClient(remaining=2)):
        r = await ui_client.request(
            "DELETE",
            "/settings/company/deactivate",
            cookies=authed_cookies("owner"),
        )
    assert r.status_code == 303
    assert r.headers.get("location") == "/login?deactivated=1"
    blob = _set_cookie_blob(r)
    assert "celerp_token=" in blob
    assert "celerp_refresh=" in blob


# ── Transport-aware Secure cookies ────────────────────────────────────────────
#
# `Secure` must follow the actual request transport, not a global flag derived
# from Connect linkage: HTTPS (native or forwarded) is Secure; direct HTTP/LAN
# stays usable. COOKIE_SECURE=true remains an explicit operator override.


def _request(scheme: str = "http", forwarded: str | None = None):
    from starlette.requests import Request
    headers = []
    if forwarded is not None:
        headers.append((b"x-forwarded-proto", forwarded.encode()))
    scope = {
        "type": "http", "method": "GET", "path": "/", "scheme": scheme,
        "headers": headers, "server": ("host", 80), "query_string": b"",
    }
    return Request(scope)


@pytest.fixture
def cookie_env(monkeypatch):
    """Neutral cookie state: no explicit override, no gateway token forcing Secure."""
    from celerp.config import settings
    monkeypatch.setattr(settings, "cookie_secure", False, raising=False)
    monkeypatch.setattr(settings, "gateway_token", "", raising=False)
    return monkeypatch


def test_secure_native_https(cookie_env):
    from ui.config import session_cookie_secure
    assert session_cookie_secure(_request(scheme="https")) is True


def test_secure_forwarded_https(cookie_env):
    from ui.config import session_cookie_secure
    assert session_cookie_secure(_request(scheme="http", forwarded="https")) is True


def test_secure_forwarded_https_multi_value(cookie_env):
    from ui.config import session_cookie_secure
    assert session_cookie_secure(_request(scheme="http", forwarded="http, https")) is True


def test_not_secure_direct_http(cookie_env):
    from ui.config import session_cookie_secure
    assert session_cookie_secure(_request(scheme="http")) is False


def test_not_secure_direct_http_with_gateway_token(cookie_env):
    """A Connect gateway token no longer forces Secure on a direct HTTP request."""
    from celerp.config import settings
    from ui.config import session_cookie_secure
    cookie_env.setattr(settings, "gateway_token", "gw-token-abc", raising=False)
    assert session_cookie_secure(_request(scheme="http")) is False


def test_explicit_cookie_secure_override_forces_secure(cookie_env):
    """COOKIE_SECURE=true forces Secure even over plain HTTP."""
    from celerp.config import settings
    from ui.config import session_cookie_secure
    cookie_env.setattr(settings, "cookie_secure", True, raising=False)
    assert session_cookie_secure(_request(scheme="http")) is True


def test_set_session_cookies_honors_transport_and_preserves_attributes(cookie_env):
    """set_session_cookies applies the transport rule and keeps HttpOnly, SameSite,
    lifetime and domain behavior."""
    from starlette.responses import Response
    from ui.config import set_session_cookies, ACCESS_COOKIE_MAX_AGE

    resp = Response()
    set_session_cookies(resp, "acc", "ref", _request(scheme="https"))
    blob = " ".join(resp.headers.getlist("set-cookie")).lower()
    assert "secure" in blob
    assert "httponly" in blob
    assert "samesite=lax" in blob
    assert f"max-age={ACCESS_COOKIE_MAX_AGE}" in blob

    resp2 = Response()
    set_session_cookies(resp2, "acc", "ref", _request(scheme="http"))
    blob2 = " ".join(resp2.headers.getlist("set-cookie")).lower()
    assert "secure" not in blob2
    assert "httponly" in blob2
    assert "samesite=lax" in blob2


@pytest.mark.asyncio
async def test_sliding_refresh_uses_same_transport_rule():
    """The refresh middleware issues cookies through the same transport rule as
    normal issuance: forwarded HTTPS -> Secure, direct HTTP -> not Secure."""
    from ui.app import TokenRefreshMiddleware

    async def _dummy_app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    mw = TokenRefreshMiddleware(_dummy_app)

    async def _drive(scheme, forwarded=None):
        headers = [(b"cookie", b"celerp_refresh=r1")]
        if forwarded is not None:
            headers.append((b"x-forwarded-proto", forwarded.encode()))
        scope = {
            "type": "http", "method": "GET", "path": "/dashboard", "scheme": scheme,
            "headers": headers, "server": ("host", 80), "query_string": b"",
        }
        sent = []

        async def receive():
            return {"type": "http.request", "body": b""}

        async def send(m):
            sent.append(m)

        with patch("ui.api_client.refresh_access_token", new=AsyncMock(return_value=("newacc", "newref"))):
            await mw(scope, receive, send)
        start = next(m for m in sent if m["type"] == "http.response.start")
        return b" ".join(v for k, v in start["headers"] if k == b"set-cookie").lower()

    secure_blob = await _drive("http", forwarded="https")
    assert b"secure" in secure_blob
    assert b"newacc" in secure_blob

    plain_blob = await _drive("http")
    assert b"secure" not in plain_blob
    assert b"newacc" in plain_blob
