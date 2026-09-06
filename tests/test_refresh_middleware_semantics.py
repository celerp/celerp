# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The UI TokenRefreshMiddleware must swallow a refresh failure and still serve.

This behavior does not change when the underlying refresh call is made
single-flight. Both refresh scenarios the middleware handles - a request with no
access token but a refresh cookie, and a request whose access token is past its
sliding half-life - must continue to the downstream app when the refresh upstream
fails, exactly as before. A coalescing rewrite that let one waiter's exception
escape the middleware would break authenticated browsing under load, so this
guard is deliberately green at merge base and stays green after the change.
"""
from __future__ import annotations

import pytest

import ui.api_client as api


async def _drive_middleware(cookies: dict) -> dict:
    """Run TokenRefreshMiddleware over a stub downstream app; report whether the
    downstream ran and whether the middleware raised."""
    from ui.app import TokenRefreshMiddleware

    seen = {"downstream_ran": False, "raised": None}

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
        pass

    try:
        await mw(scope, _receive, _send)
    except Exception as exc:  # noqa: BLE001 - the guard asserts nothing escapes
        seen["raised"] = exc
    return seen


@pytest.mark.asyncio
async def test_refresh_middleware_failure_semantics_unchanged(monkeypatch):
    from ui.app import COOKIE_NAME, REFRESH_COOKIE_NAME

    async def _always_fail(refresh_token: str):
        raise api.APIError(401, "refresh rejected")

    # The middleware imports refresh_access_token lazily from ui.api_client on each
    # request, so patching the source module is what it resolves at call time.
    monkeypatch.setattr(api, "refresh_access_token", _always_fail)

    # Case 1: no access token, refresh token present, refresh fails. The failure is
    # swallowed and the downstream app still runs; its own auth guard handles the
    # unauthenticated request from there.
    seen1 = await _drive_middleware({REFRESH_COOKIE_NAME: "some-refresh"})
    assert seen1["raised"] is None
    assert seen1["downstream_ran"] is True

    # Case 2: a still-valid access token past its sliding half-life, refresh fails.
    # The failure is swallowed and the downstream runs with the old, still-valid
    # token.
    monkeypatch.setattr("ui.app._token_needs_refresh", lambda _t: True)
    seen2 = await _drive_middleware(
        {COOKIE_NAME: "still.valid.token", REFRESH_COOKIE_NAME: "some-refresh"}
    )
    assert seen2["raised"] is None
    assert seen2["downstream_ran"] is True
