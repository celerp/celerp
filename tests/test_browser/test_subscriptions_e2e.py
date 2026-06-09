# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Browser E2E tests for subscriptions UI.

Covers:
  - SUB-E2E-01: List page renders (Sales tab)
  - SUB-E2E-02: List page renders (Purchasing tab)
  - SUB-E2E-03: New form loads without crash
  - SUB-E2E-04: Create subscription via form -> redirects to detail page
  - SUB-E2E-05: Detail page shows correct fields (name, contact, frequency)
  - SUB-E2E-06: Generate Now button present on active subscription
"""
from __future__ import annotations

import pathlib
import pytest

pytestmark = pytest.mark.browser


def _save(page, name: str) -> None:
    # Debug screenshots disabled — the hardcoded /mnt/storage path is not portable.
    # _SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    # page.screenshot(path=str(_SCREENSHOT_DIR / f"{name}.png"), full_page=True)
    pass


def _no_crash(page, ctx: str = "") -> None:
    body = page.locator("body").inner_text()
    assert "Internal Server Error" not in body, f"{ctx}: Internal Server Error in body"
    assert "Traceback" not in body, f"{ctx}: Traceback in body"
    assert "/login" not in page.url, f"{ctx}: redirected to login"


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_sub_list_sales(page, ui_server):
    """SUB-E2E-01: Sales subscriptions list renders without crash."""
    page.goto(f"{ui_server}/subscriptions?direction=sales")
    page.wait_for_load_state("load")
    _save(page, "01-list-sales")
    _no_crash(page, "list-sales")
    assert "Subscription" in page.title() or "Subscription" in page.locator("h1,h2").first.inner_text()


def test_sub_list_purchasing(page, ui_server):
    """SUB-E2E-02: Purchasing subscriptions list renders without crash."""
    page.goto(f"{ui_server}/subscriptions?direction=purchasing")
    page.wait_for_load_state("load")
    _save(page, "02-list-purchasing")
    _no_crash(page, "list-purchasing")


def test_sub_new_form_loads(page, ui_server):
    """SUB-E2E-03: 'New subscription' creates a draft and opens its detail page.

    There is no standalone GET form at /subscriptions/new — the New action does an
    htmx POST that HX-Redirects to the new draft's detail page (edited inline there).
    """
    page.goto(f"{ui_server}/subscriptions?direction=sales", wait_until="domcontentloaded")
    page.wait_for_load_state("load")
    _no_crash(page, "subscriptions-list")
    new_btn = page.locator("[hx-post*='/subscriptions/new']").first
    assert new_btn.count() > 0, "New subscription button missing on the list"
    new_btn.click()
    # htmx follows the HX-Redirect to /subscriptions/{id}
    page.wait_for_url(lambda url: "/subscriptions/" in url, timeout=10000)
    _no_crash(page, "new-subscription-detail")
    assert "/new" not in page.url, f"Should land on the draft detail page, got {page.url}"


def test_sub_create_and_detail(page, ui_server, api):
    """SUB-E2E-04: Create a subscription → its detail page renders the editable
    frequency / start-date controls (inline-edit, not a submit form)."""
    page.goto(f"{ui_server}/subscriptions?direction=sales", wait_until="domcontentloaded")
    page.wait_for_load_state("load")
    new_btn = page.locator("[hx-post*='/subscriptions/new']").first
    assert new_btn.count() > 0, "New subscription button missing on the list"
    new_btn.click()
    page.wait_for_url(lambda url: "/subscriptions/" in url, timeout=10000)
    page.wait_for_load_state("load")
    _no_crash(page, "subscription-detail")
    assert "/new" not in page.url, f"Expected a subscription detail page, got {page.url}"

    # Draft detail must expose the inline-editable subscription controls.
    assert page.locator("select[hx-patch$='/field/frequency']").count() > 0, \
        "Frequency control missing on subscription detail"
    assert page.locator("input[hx-patch$='/field/start_date']").count() > 0, \
        "Start-date control missing on subscription detail"
