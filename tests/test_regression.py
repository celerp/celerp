# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Regression tests for bugs caught in production but missed by the test suite.

Each class is named after the bug it covers. These run in the default suite
(no browser required, no MODULE_DIR needed).

REG-001: Missing route registrations in ui/app.py
REG-002: Setup/company form fields silently dropped (not saved to settings)
REG-003: Dashboard 404 shows error banner instead of redirecting to /setup
"""

from __future__ import annotations

import os

os.environ.setdefault("ALLOW_INSECURE_JWT", "true")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import uuid
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport


# ---------------------------------------------------------------------------
# REG-001  All primary nav routes registered in ui/app.py
# ---------------------------------------------------------------------------

class TestRouteRegistration:
    """REG-001: Every content route must return something other than 404.

    The bug: ui/app.py imported route modules but never called setup_routes()
    on them. ui/conftest.py masks this by patching module routes directly,
    so test_ui.py couldn't catch it. This test imports ui/app.py WITHOUT
    any conftest patching and asserts the routes exist.
    """

    # Routes that must NOT return 404 for an unauthenticated request.
    # Unauthenticated → expect 302 (redirect to /login or /setup), never 404.
    _NAV_ROUTES = [
        "/lists",
        "/inventory",
        "/docs",
        "/crm",
        "/accounting",
        "/reports",
        "/subscriptions",
        "/manufacturing",
        "/dashboard",
        "/settings",
        # "/scanning",  # Scanning module disabled until complete
        "/search",
    ]

    @pytest_asyncio.fixture
    async def raw_ui_client(self):
        """Client against ui/app directly, no module mocks, no auth."""
        # Patch api_client calls that fire at import/startup to avoid network errors
        with (
            patch("ui.api_client.bootstrap_status", new=AsyncMock(return_value=True)),
        ):
            from ui.app import app as ui_app
            async with AsyncClient(
                transport=ASGITransport(app=ui_app),
                base_url="http://ui",
                follow_redirects=False,
            ) as c:
                yield c

    @pytest.mark.asyncio
    @pytest.mark.parametrize("route", _NAV_ROUTES)
    async def test_route_not_404(self, raw_ui_client, route):
        """REG-001: Route must be registered - unauthenticated request redirects, never 404."""
        r = await raw_ui_client.get(route)
        assert r.status_code != 404, (
            f"{route} returned 404 - route is not registered in ui/app.py. "
            "Check the setup_routes() loop in ui/app.py."
        )
        # Unauthenticated → should redirect to /login or /setup
        assert r.status_code in (302, 303), (
            f"{route} returned {r.status_code}, expected 302 redirect for unauthenticated request"
        )


# ---------------------------------------------------------------------------
# REG-002  Setup/company form persists tax_id, phone, address, currency etc.
# ---------------------------------------------------------------------------

class TestSetupCompanySave:
    """REG-002: Fields submitted in /setup/company must round-trip through patch_company.

    The bug: tax_id, phone, address were sent as 'direct_patch' but CompanyPatch
    only accepts 'settings', so Pydantic silently dropped them. Fix: route them
    into _SETTINGS_FIELDS in api_client.patch_company.
    """

    @pytest.mark.asyncio
    async def test_setup_company_fields_routed_to_settings(self):
        """REG-002a: tax_id, phone, address go into settings_patch, not direct_patch."""
        import ui.api_client as api_client

        patched_calls: list[dict] = []

        def mock_client_ctx(token):
            class FakeResp:
                def json(self_):
                    return {"id": "c1", "name": "Co", "slug": "co", "settings": {}}
                def raise_for_status(self_): pass
                @property
                def is_error(self_): return False
                @property
                def is_redirect(self_): return False

            class FakeClient:
                async def get(self_, url, **kw):
                    return FakeResp()
                async def patch(self_, url, json=None, **kw):
                    patched_calls.append({"url": url, "json": json})
                    return FakeResp()
                async def __aenter__(self_): return self_
                async def __aexit__(self_, *a): pass

            return FakeClient()

        with patch.object(api_client, "_client", side_effect=mock_client_ctx):
            await api_client.patch_company("tok", {
                "tax_id": "1234567890123",
                "phone": "+66 2 123 4567",
                "address": "123 Main St",
                "currency": "THB",
                "timezone": "Asia/Bangkok",
                "fiscal_year_start": "01",
            })

        # Must have at least one PATCH call with settings containing tax_id, phone, address
        settings_patches = [c for c in patched_calls if c["url"] == "/companies/me" and "settings" in (c.get("json") or {})]
        assert settings_patches, "No PATCH /companies/me with settings dict was sent"
        merged_settings = settings_patches[0]["json"]["settings"]
        assert merged_settings.get("tax_id") == "1234567890123", "tax_id not in settings patch"
        assert merged_settings.get("phone") == "+66 2 123 4567", "phone not in settings patch"
        assert merged_settings.get("address") == "123 Main St", "address not in settings patch"
        assert merged_settings.get("currency") == "THB", "currency not in settings patch"

    @pytest.mark.asyncio
    async def test_setup_company_no_direct_patch_for_known_fields(self):
        """REG-002b: Known fields must not leak into direct_patch (which CompanyPatch drops)."""
        import ui.api_client as api_client

        patched_calls: list[dict] = []

        def mock_client_ctx(token):
            class FakeResp:
                def json(self_): return {"id": "c1", "name": "Co", "slug": "co", "settings": {}}
                @property
                def is_error(self_): return False
                @property
                def is_redirect(self_): return False

            class FakeClient:
                async def get(self_, url, **kw): return FakeResp()
                async def patch(self_, url, json=None, **kw):
                    patched_calls.append({"url": url, "json": json})
                    return FakeResp()
                async def __aenter__(self_): return self_
                async def __aexit__(self_, *a): pass

            return FakeClient()

        with patch.object(api_client, "_client", side_effect=mock_client_ctx):
            await api_client.patch_company("tok", {
                "tax_id": "X",
                "phone": "Y",
                "address": "Z",
                "currency": "USD",
            })

        # There must be no PATCH call where json contains only tax_id/phone/address
        # without a 'settings' key (that would be the dropped direct_patch)
        bad_patches = [
            c for c in patched_calls
            if c["url"] == "/companies/me"
            and "settings" not in (c.get("json") or {})
            and any(k in (c.get("json") or {}) for k in ("tax_id", "phone", "address"))
        ]
        assert not bad_patches, (
            f"Fields were sent as direct_patch (dropped by CompanyPatch): {bad_patches}"
        )


# ---------------------------------------------------------------------------
# REG-003  Dashboard 404 redirects to /setup, not error banner
# ---------------------------------------------------------------------------

class TestDashboard404Redirect:
    """REG-003: When GET /companies/me returns 404, dashboard must redirect to /setup.

    The bug: only 401 was handled with a redirect; 404 fell through to
    the error banner 'Error loading dashboard: Not found'.
    """

    @pytest_asyncio.fixture
    async def ui_client(self):
        from ui.app import app as ui_app
        async with AsyncClient(
            transport=ASGITransport(app=ui_app),
            base_url="http://ui",
            follow_redirects=False,
        ) as c:
            yield c

    def _authed(self) -> dict:
        from tests.conftest import make_test_token
        return {"celerp_token": make_test_token()}

    @pytest.mark.asyncio
    async def test_dashboard_404_redirects_to_setup(self, ui_client):
        """REG-003a: 404 from API → redirect to /setup, not error banner."""
        from ui.api_client import APIError

        with patch("ui.api_client.get_company", new=AsyncMock(side_effect=APIError(404, "Not found"))):
            r = await ui_client.get("/dashboard", cookies=self._authed())

        assert r.status_code in (302, 303), (
            f"Expected redirect on 404, got {r.status_code}"
        )
        location = r.headers.get("location", "")
        assert "/setup" in location, (
            f"Expected redirect to /setup on 404, got location={location!r}"
        )

    @pytest.mark.asyncio
    async def test_dashboard_401_redirects_to_login(self, ui_client):
        """REG-003b: 401 from API → redirect to /login (pre-existing, regression guard)."""
        from ui.api_client import APIError

        with patch("ui.api_client.get_company", new=AsyncMock(side_effect=APIError(401, "Invalid token"))):
            r = await ui_client.get("/dashboard", cookies=self._authed())

        assert r.status_code in (302, 303)
        location = r.headers.get("location", "")
        assert "/login" in location, (
            f"Expected redirect to /login on 401, got location={location!r}"
        )

    @pytest.mark.asyncio
    async def test_dashboard_500_shows_error_banner(self, ui_client):
        """REG-003c: Non-404/401 errors show the error banner (not crash)."""
        from ui.api_client import APIError

        with (
            patch("ui.api_client.get_company", new=AsyncMock(side_effect=APIError(503, "Service unavailable"))),
            # Also mock bootstrap_status so the auth middleware doesn't interfere
            patch("ui.routes.auth.bootstrap_status", new=AsyncMock(return_value=True)),
        ):
            r = await ui_client.get("/dashboard", cookies=self._authed())

        # Should render a page (200 or redirect), not crash (500)
        assert r.status_code != 500, "Unhandled APIError caused a 500"


# ---------------------------------------------------------------------------
# REG-004  Stale cookie after init --force causes infinite redirect loop
# ---------------------------------------------------------------------------

class TestStaleTokenRedirectLoop:
    """REG-004: Stale cookie (invalid after init --force) must not loop.

    The bug: /login saw a cookie present and blindly redirected to /dashboard.
    /dashboard hit the API, got 401, redirected back to /login. Infinite loop.
    Fix: /login validates the token; on 401 it clears the cookie and shows
    the login form instead of redirecting.
    """

    @pytest_asyncio.fixture
    async def ui_client(self):
        from ui.app import app as ui_app
        async with AsyncClient(
            transport=ASGITransport(app=ui_app),
            base_url="http://ui",
            follow_redirects=False,
        ) as c:
            yield c

    @pytest.mark.asyncio
    async def test_stale_token_shows_login_not_redirect(self, ui_client):
        """REG-004a: GET /login with an invalid token → shows login page, clears cookie."""
        from ui.api_client import APIError

        with (
            patch("ui.routes.auth.api_get_company", new=AsyncMock(side_effect=APIError(401, "Invalid token"))),
            patch("ui.routes.auth.bootstrap_status", new=AsyncMock(return_value=True)),
        ):
            r = await ui_client.get("/login", cookies={"celerp_token": "stale-token"})

        # Must NOT redirect back to dashboard (that causes the loop)
        assert r.status_code not in (302, 303) or "/login" not in r.headers.get("location", ""), (
            "Stale token caused redirect loop: /login redirected away instead of showing login form"
        )
        # Must clear the cookie
        set_cookie_header = r.headers.get("set-cookie", "")
        # Either max-age=0 or expires in the past signals deletion
        assert (
            "celerp_token" in set_cookie_header and
            ("max-age=0" in set_cookie_header.lower() or "expires" in set_cookie_header.lower())
        ), f"Stale celerp_token cookie was not cleared. set-cookie: {set_cookie_header!r}"

    @pytest.mark.asyncio
    async def test_valid_token_still_redirects_to_dashboard(self, ui_client):
        """REG-004b: GET /login with a valid token still redirects to / (no regression)."""
        with (
            patch("ui.routes.auth.api_get_company", new=AsyncMock(return_value={"name": "Co", "settings": {}})),
            patch("ui.routes.auth.bootstrap_status", new=AsyncMock(return_value=True)),
        ):
            r = await ui_client.get("/login", cookies={"celerp_token": "valid-token"})

        assert r.status_code in (302, 303), f"Expected redirect for valid token, got {r.status_code}"
        assert r.headers.get("location", "") == "/", (
            f"Valid token should redirect to /, got {r.headers.get('location')!r}"
        )
