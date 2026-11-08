# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A signed-in page under /setup renews a lapsed session the way every other page does.

/setup holds the first-run pages, which need no session, beside pages a signed-in
person opens (adding a company, finishing the company profile, connecting to the
cloud). A signed-in person whose access token has lapsed keeps the refresh cookie, so
opening one of those pages renews the session rather than answering with the sign-in
page. The guard runs over every /setup page the app registers: a page that sends a
browser with no cookies on a fresh install to the sign-in page is a signed-in page,
and only those renew.
"""
from __future__ import annotations

import re
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

import ui.api_client as api
from ui.config import REFRESH_COOKIE_NAME


def _setup_pages() -> list[str]:
    from ui.app import app
    paths = {re.sub(r"\{[^}]+\}", "x", r.path) for r in app.routes
             if getattr(r, "path", "").startswith("/setup") and "GET" in (getattr(r, "methods", None) or ())}
    return sorted(paths)


async def _sends_to_sign_in(path: str) -> bool:
    """Whether the page answers a browser with no cookies, on a fresh install, with the sign-in page."""
    from ui.app import app
    with patch("ui.routes.auth.bootstrap_status", AsyncMock(return_value=False)):
        async with AsyncClient(transport=ASGITransport(app=app, raise_app_exceptions=False),
                               base_url="http://ui", follow_redirects=False) as c:
            r = await c.get(path)
    return r.status_code in (302, 303, 307) and r.headers.get("location", "").split("?", 1)[0] == "/login"


async def _renews(path: str) -> bool:
    """Whether the session middleware renews a refresh-only session on the path, before any route runs."""
    from ui.app import TokenRefreshMiddleware

    async def _route(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def _receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def _send(message):
        pass

    renew = AsyncMock(return_value=("renewed-access", "renewed-refresh"))
    scope = {"type": "http", "method": "GET", "path": path, "query_string": b"",
             "headers": [(b"cookie", f"{REFRESH_COOKIE_NAME}=lapsed-session-refresh".encode())]}
    with patch.object(api, "refresh_access_token", renew):
        await TokenRefreshMiddleware(_route)(scope, _receive, _send)
    return renew.await_count == 1


@pytest.mark.asyncio
async def test_every_signed_in_setup_page_renews_a_lapsed_session():
    pages = _setup_pages()
    assert "/setup/new-company/migrate" in pages and "/setup" in pages, pages
    wrong = []
    for path in pages:
        signed_in = await _sends_to_sign_in(path)
        if await _renews(path) != signed_in:
            wrong.append((path, "signed-in" if signed_in else "first run"))
    assert not wrong, f"pages whose session renewal does not match whether they need a session: {wrong}"
