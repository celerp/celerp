# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
# Copyright (c) 2026 Noah Severs. All rights reserved.
"""Group 2: Navigation crawl — all primary routes return 200, no error banners."""
import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.browser

_NAV_ROUTES = [
    ("/", None),
    ("/inventory", None),
    ("/inventory/new", "Add Item"),
    ("/docs", None),
    ("/crm", None),
    ("/crm/new", None),
    ("/lists", None),
    ("/lists/new", None),
    ("/accounting", "Profit"),  # landing page is P&L view
    ("/accounting/pnl", "Profit"),
    ("/accounting/balance-sheet", None),
    ("/reports", "Reports"),
    ("/reports/ar-aging", None),
    ("/reports/ap-aging", None),
    ("/reports/sales", None),
    ("/reports/purchases", None),
    ("/reports/expiring", None),
    ("/settings", "Deactivate"),
    ("/settings/users/new", None),
    ("/subscriptions", None),
    ("/subscriptions/new", None),
    ("/manufacturing", None),
    ("/manufacturing/new", None),
    ("/manufacturing/boms", None),
    ("/manufacturing/boms/new", None),
    ("/dashboard", None),
    ("/scanning", None),
    ("/labels", None),
]


@pytest.mark.parametrize("route,expected_text", _NAV_ROUTES, ids=[r for r, _ in _NAV_ROUTES])
def test_nav_route_loads(page, ui_server, route, expected_text):
    """NAV-01..21: Authenticated GET → page loads, no server error."""
    resp = page.goto(f"{ui_server}{route}", wait_until="domcontentloaded")
    # Should not redirect to login
    assert "/login" not in page.url, f"{route} redirected to login (auth cookie lost?)"
    # Should not 500
    assert resp.status != 500, f"{route} returned HTTP 500"
    # Should not show Python traceback or "Internal Server Error"
    body = page.locator("body").inner_text()
    assert "Internal Server Error" not in body, f"{route} shows Internal Server Error"
    assert "Traceback (most recent call last)" not in body, f"{route} shows traceback"
    # Optional text assertion
    if expected_text:
        assert expected_text in body, f"{route} body missing expected text: {expected_text!r}"


def test_search_loads_with_query(page, ui_server):
    """NAV-22: /search?q=test loads without 500."""
    resp = page.goto(f"{ui_server}/search?q=test", wait_until="domcontentloaded")
    assert resp.status != 500, "/search?q=test returned 500"
    assert "/login" not in page.url


def test_switch_company_loads(page, ui_server):
    """NAV-23: /switch-company renders without 500 or traceback.

    Route is an HTMX partial (company-picker panel), not a full page.
    With a single-company user it still returns a 200 with the panel HTML.
    """
    resp = page.goto(f"{ui_server}/switch-company", wait_until="domcontentloaded")
    assert resp.status != 500, "/switch-company returned HTTP 500"
    assert "/login" not in page.url, "/switch-company unexpectedly redirected to login"
    body = page.locator("body").inner_text()
    assert "Internal Server Error" not in body, "/switch-company shows Internal Server Error"
    assert "Traceback (most recent call last)" not in body, "/switch-company shows traceback"
