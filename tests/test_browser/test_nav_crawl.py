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
    ("/accounting", "New entry"),  # landing page is the journal; the reports moved to /reports
    ("/accounting/pnl", "Profit"),
    ("/accounting/balance-sheet", None),
    ("/reports", "Reports"),
    ("/reports/pnl", "P&L"),
    ("/reports/balance-sheet", "Balance Sheet"),
    ("/reports/trial-balance", "Trial Balance"),
    ("/reports/general-ledger", "General Ledger"),
    ("/reports/cash-flow", "Cash Flow"),
    ("/reports/statement", "Statement of Account"),
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
    ("/manufacturing/production", None),
    ("/docs?type=production_order", None),
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


def test_active_nav_scrolled_into_view(page, ui_server):
    """NAV-23: on a short viewport, the active nav link is scrolled into view within the sidebar -
    a group near the bottom (Manufacturing) must not sit below the fold."""
    page.set_viewport_size({"width": 1280, "height": 600})
    page.goto(f"{ui_server}/manufacturing/production", wait_until="domcontentloaded")
    page.wait_for_selector(".sidebar .nav-link--active", timeout=8000)
    link = page.locator(".sidebar .nav-link--active").first
    assert link.inner_text().strip() != ""
    lb, sb = link.bounding_box(), page.locator(".sidebar").bounding_box()
    assert lb is not None and sb is not None
    # The active link sits within the sidebar's visible vertical bounds (allow 1px rounding).
    assert lb["y"] >= sb["y"] - 1, "active nav link is above the sidebar viewport"
    assert lb["y"] + lb["height"] <= sb["y"] + sb["height"] + 1, "active nav link is below the fold"


def test_wip_page_highlights_correct_nav_item(page, ui_server):
    """FIX-3: on /manufacturing/production the sidebar must highlight 'Work In Progress',
    NOT 'Demand Planning', and the active link must be within the sidebar's visible bounds."""
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(f"{ui_server}/manufacturing/production", wait_until="domcontentloaded")
    page.wait_for_selector(".sidebar .nav-link--active", timeout=8000)

    active_link = page.locator(".sidebar .nav-link--active").first
    active_text = active_link.inner_text().strip()
    assert active_text == "Work In Progress", (
        f"Sidebar active nav link on /manufacturing/production should be 'Work In Progress'; "
        f"got: {active_text!r}. Was 'Demand Planning' highlighted instead (FIX-3 regression)?"
    )

    # The active link must be within the sidebar's visible vertical bounds (allow 1px rounding).
    lb = active_link.bounding_box()
    sb = page.locator(".sidebar").bounding_box()
    assert lb is not None and sb is not None, "Could not get bounding boxes for active link / sidebar"
    assert lb["y"] >= sb["y"] - 1, (
        f"Active nav link y={lb['y']} is above the sidebar top y={sb['y']}"
    )
    assert lb["y"] + lb["height"] <= sb["y"] + sb["height"] + 1, (
        f"Active nav link bottom={lb['y']+lb['height']} is below sidebar bottom={sb['y']+sb['height']}"
    )


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
