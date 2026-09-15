# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""UI permission-gate fail-closed behavior (F6).

The shared UI permission gate must never trust the unsigned client cookie for
the caller's role: it reads the DB-authoritative role from the authenticated
API (``GET /companies/me``). Failure handling splits by caller intent:

  - no token, or the API rejects the token (401): converge to ``/login``;
  - transient API failure (5xx / connection): a privileged/mutation gate
    hard-denies to ``/dashboard``; a page-view gate returns None so the route
    renders its own neutral/empty state instead of bouncing the user;
  - a forged cookie role never grants: the API's ``current_role`` governs.

The account surface (`/account/*`) shares this fail-closed rule through
``_account_allowed``.
"""

from __future__ import annotations

import pytest
from starlette.requests import Request
from starlette.responses import RedirectResponse
from unittest.mock import AsyncMock, patch

from test_helpers import make_test_token
from ui.api_client import APIError
from ui.routes.settings import _check_permission
from ui.routes.account import _account_allowed


def _req(token: str | None = None) -> Request:
    """Minimal GET request carrying (optionally) a session cookie."""
    headers: list[tuple[bytes, bytes]] = []
    if token:
        headers.append((b"cookie", f"celerp_token={token}".encode()))
    return Request({
        "type": "http", "method": "GET", "path": "/",
        "query_string": b"", "headers": headers,
    })


def _company(role: str) -> AsyncMock:
    return AsyncMock(return_value={"current_role": role, "settings": {}})


# ── Fail-closed: no token / rejected token ────────────────────────────────────

@pytest.mark.asyncio
async def test_no_token_redirects_to_login():
    r = await _check_permission(_req(None), "manage_integrations")
    assert isinstance(r, RedirectResponse)
    assert r.headers["location"] == "/login"


@pytest.mark.asyncio
async def test_rejected_token_401_converges_to_login():
    """A 401 from the API (dead/forged token) sends the caller to /login, even
    on a page-view gate."""
    err = AsyncMock(side_effect=APIError(401, "unauthorized"))
    with patch("ui.api_client.get_company", new=err):
        r = await _check_permission(_req(make_test_token(role="owner")), "manage_integrations")
        assert isinstance(r, RedirectResponse) and r.headers["location"] == "/login"
        r2 = await _check_permission(
            _req(make_test_token(role="owner")), "view_subscriptions", page_view=True
        )
    assert isinstance(r2, RedirectResponse) and r2.headers["location"] == "/login"


# ── Forged cookie role never grants ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_forged_cookie_role_ignored_authoritative_role_denies():
    """The cookie claims owner; the API says staff. The gate must use the API
    role and deny a privileged key."""
    forged = make_test_token(role="owner")
    with patch("ui.api_client.get_company", new=_company("staff")):
        r = await _check_permission(_req(forged), "manage_integrations")
    assert isinstance(r, RedirectResponse)
    assert r.headers["location"] == "/dashboard"


@pytest.mark.asyncio
async def test_authoritative_owner_role_grants():
    with patch("ui.api_client.get_company", new=_company("owner")):
        r = await _check_permission(_req(make_test_token(role="staff")), "manage_integrations")
    assert r is None


# ── Transient failure: mutation denies, page-view degrades ────────────────────

@pytest.mark.asyncio
async def test_privileged_gate_hard_denies_on_transient_failure():
    err = AsyncMock(side_effect=APIError(503, "service unavailable"))
    with patch("ui.api_client.get_company", new=err):
        r = await _check_permission(_req(make_test_token(role="owner")), "manage_integrations")
    assert isinstance(r, RedirectResponse)
    assert r.headers["location"] == "/dashboard"


@pytest.mark.asyncio
async def test_page_view_gate_degrades_on_transient_failure():
    """A page-view gate returns None on a transient API failure so the route
    renders its own neutral state rather than bouncing the user off the page."""
    err = AsyncMock(side_effect=APIError(503, "service unavailable"))
    with patch("ui.api_client.get_company", new=err):
        r = await _check_permission(
            _req(make_test_token(role="owner")), "view_subscriptions", page_view=True
        )
    assert r is None


# ── Account surface shares the fail-closed rule ───────────────────────────────

@pytest.mark.asyncio
async def test_account_allowed_true_for_admin_via_api():
    with patch("ui.api_client.get_company", new=_company("admin")):
        assert await _account_allowed(_req(make_test_token(role="admin"))) is True


@pytest.mark.asyncio
async def test_account_allowed_forged_cookie_denied():
    """Cookie claims admin; API says staff. Access is denied."""
    with patch("ui.api_client.get_company", new=_company("staff")):
        assert await _account_allowed(_req(make_test_token(role="admin"))) is False


@pytest.mark.asyncio
async def test_account_allowed_false_without_token():
    assert await _account_allowed(_req(None)) is False


@pytest.mark.asyncio
async def test_account_allowed_false_on_api_failure():
    err = AsyncMock(side_effect=APIError(503, "service unavailable"))
    with patch("ui.api_client.get_company", new=err):
        assert await _account_allowed(_req(make_test_token(role="admin"))) is False
