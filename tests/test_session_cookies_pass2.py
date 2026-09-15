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
