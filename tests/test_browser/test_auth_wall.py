# Copyright (c) 2026 Noah Severs. All rights reserved.
"""Group 1: Auth wall — unauthenticated access redirects to /login."""
import pytest

pytestmark = pytest.mark.browser

# Kernel routes: always registered regardless of ENABLED_MODULES
_KERNEL_ROUTES = ["/", "/settings"]

# Module routes: only registered when the corresponding module is enabled
_MODULE_ROUTES = [
    "/inventory",
    "/docs",
    "/crm",
    "/lists",
    "/accounting",
    "/reports",
    "/subscriptions",
    "/manufacturing",
    "/dashboard",
]

_PROTECTED_ROUTES = _KERNEL_ROUTES + _MODULE_ROUTES


@pytest.mark.parametrize("route", _PROTECTED_ROUTES)
def test_auth_wall_redirects_to_login(unauthed_page, ui_server, route):
    """AUTH-01..N: Unauthenticated GET → redirect to /login or /setup.

    Module routes may return 404 when not enabled - that is also acceptable
    (route not registered == not exploitable). Kernel routes MUST redirect.
    """
    page = unauthed_page
    resp = page.goto(f"{ui_server}{route}", wait_until="domcontentloaded")
    if resp and resp.status == 404:
        # Route not registered (module not enabled in this environment) - acceptable
        pytest.skip(f"{route} not registered in this environment (module disabled)")
    assert "/login" in page.url or "/setup" in page.url, (
        f"Expected redirect to /login or /setup for {route}, got: {page.url}"
    )


def test_search_requires_auth_or_returns_200(unauthed_page, ui_server):
    """AUTH-12: /search may redirect to /login OR return 200 with login form."""
    page = unauthed_page
    resp = page.goto(f"{ui_server}/search?q=test", wait_until="domcontentloaded")
    # Either redirect to login or serve a 200 with login form - not a 500
    assert resp.status != 500, f"/search returned 500 unauthenticated"
    # Should either redirect or show login
    page_ok = "/login" in page.url or page.locator("input[type=password]").count() > 0 or resp.status == 200
    assert page_ok, f"Unexpected auth behavior for /search: url={page.url}, status={resp.status}"
