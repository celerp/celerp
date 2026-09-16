# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The UI TokenRefreshMiddleware must tell a dead session apart from a blip.

A refresh that comes back 401 means the credential is revoked or expired: there
is no session left, so the middleware converges to /login and clears the rejected
cookies instead of letting the route bounce on its own. A transient upstream
failure (5xx, connection error) is not a revoked session, so the request still
reaches the route with whatever access cookie it already had. The old middleware
swallowed both cases identically, which is the regression these guards pin.
"""
from __future__ import annotations

import httpx
import pytest

import ui.api_client as api


async def _drive_middleware(cookies: dict, refresh_impl, needs_refresh: bool = False) -> dict:
    """Run TokenRefreshMiddleware over a stub downstream app and report what the
    middleware emitted: whether the route ran, the response start message, and any
    Set-Cookie headers it wrote."""
    from unittest.mock import patch

    from ui.app import TokenRefreshMiddleware

    seen: dict = {"downstream_ran": False, "start": None, "set_cookies": []}

    async def _downstream(scope, receive, send):
        seen["downstream_ran"] = True
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    mw = TokenRefreshMiddleware(_downstream)
    cookie_header = "; ".join(f"{k}={v}" for k, v in cookies.items()).encode()
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/dashboard",
        "headers": [(b"cookie", cookie_header)] if cookie_header else [],
        "query_string": b"",
    }

    async def _receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def _send(message):
        if message["type"] == "http.response.start":
            seen["start"] = message
            for key, val in message.get("headers", []):
                if key.lower() == b"set-cookie":
                    seen["set_cookies"].append(val.decode())

    with patch.object(api, "refresh_access_token", refresh_impl):
        if needs_refresh:
            with patch("ui.app._token_needs_refresh", lambda _t: True):
                await mw(scope, _receive, _send)
        else:
            await mw(scope, _receive, _send)
    return seen


def _location(start_message: dict) -> str:
    for key, val in start_message.get("headers", []):
        if key.lower() == b"location":
            return val.decode()
    return ""


@pytest.mark.asyncio
async def test_refresh_middleware_diverts_on_401_but_serves_on_transient():
    """The middleware discriminates by failure kind. A 401 short-circuits to
    /login?reason=expired with both cookies cleared (Case 1: no access token;
    Case 2: a present-but-stale token). A 5xx or connection error keeps serving
    the route with no divert and no cookie clearing, so a blip never logs anyone
    out. The old middleware swallowed all four, so the 401 legs are red at merge
    base while the transient legs pin that the divert stays narrow."""
    from ui.app import COOKIE_NAME, REFRESH_COOKIE_NAME

    async def _reject(refresh_token: str):
        raise api.APIError(401, "refresh rejected")

    # 401, Case 1: no access token, refresh cookie present -> divert + clear.
    seen = await _drive_middleware({REFRESH_COOKIE_NAME: "dead-refresh"}, _reject)
    assert seen["downstream_ran"] is False
    assert seen["start"]["status"] == 302
    assert "reason=expired" in _location(seen["start"])
    blob = " ".join(seen["set_cookies"])
    assert "celerp_token=" in blob
    assert "celerp_refresh=" in blob

    # 401, Case 2: still-present access token past its sliding half-life -> divert.
    seen = await _drive_middleware(
        {COOKIE_NAME: "still.present.token", REFRESH_COOKIE_NAME: "dead-refresh"},
        _reject,
        needs_refresh=True,
    )
    assert seen["downstream_ran"] is False
    assert seen["start"]["status"] == 302
    assert "reason=expired" in _location(seen["start"])

    async def _five_hundred(refresh_token: str):
        raise api.APIError(503, "upstream unavailable")

    async def _connect_error(refresh_token: str):
        raise httpx.ConnectError("cannot reach api")

    for impl in (_five_hundred, _connect_error):
        # Transient, Case 1: no access token -> still serve, no cookies written.
        seen = await _drive_middleware({REFRESH_COOKIE_NAME: "some-refresh"}, impl)
        assert seen["downstream_ran"] is True
        assert seen["start"]["status"] == 200
        assert seen["set_cookies"] == []

        # Transient, Case 2: valid access token past half-life -> still serve.
        seen = await _drive_middleware(
            {COOKIE_NAME: "still.valid.token", REFRESH_COOKIE_NAME: "some-refresh"},
            impl,
            needs_refresh=True,
        )
        assert seen["downstream_ran"] is True
        assert seen["start"]["status"] == 200
        assert seen["set_cookies"] == []
