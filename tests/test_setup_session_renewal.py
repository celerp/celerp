# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A signed-in page under /setup renews a lapsed session the way every other page does.

/setup holds the first-run pages, which need no session, beside pages a signed-in
person opens (adding a company, finishing the company profile, connecting to the
cloud). A signed-in person whose access token has lapsed keeps the refresh cookie, so
opening one of those pages renews the session rather than answering with the sign-in
page. The guard runs over every /setup page the app registers: a page that sends a
browser with no cookies to the sign-in page is a signed-in page, and only those renew.
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


async def _get(path: str, cookies: dict) -> tuple[int, str]:
    from ui.app import app
    async with AsyncClient(transport=ASGITransport(app=app, raise_app_exceptions=False),
                           base_url="http://ui", follow_redirects=False, cookies=cookies) as c:
        r = await c.get(path)
    return r.status_code, r.headers.get("location", "")


def _to_sign_in(status: int, location: str) -> bool:
    return status in (302, 303, 307) and location.split("?", 1)[0] == "/login"


@pytest.mark.asyncio
async def test_every_signed_in_setup_page_renews_a_lapsed_session():
    pages = _setup_pages()
    assert "/setup/new-company/migrate" in pages and "/setup" in pages, pages
    renew = AsyncMock(return_value=("renewed-access", "renewed-refresh"))
    wrong = []
    with patch.object(api, "refresh_access_token", renew):
        for path in pages:
            signed_in = _to_sign_in(*await _get(path, {}))
            renew.reset_mock()
            await _get(path, {REFRESH_COOKIE_NAME: "lapsed-session-refresh"})
            if renew.await_count != (1 if signed_in else 0):
                wrong.append((path, "signed-in" if signed_in else "public", renew.await_count))
    assert not wrong, f"pages whose session renewal does not match whether they need a session: {wrong}"
