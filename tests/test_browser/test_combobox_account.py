# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""
Browser test: account combobox on bill line items is functional.

Proves:
1. The account column renders a combobox (not a plain text input).
2. Focusing the input opens the dropdown list.
3. Typing filters options.
4. Selecting an option commits the value to the hidden input.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.browser


@pytest.fixture(scope="module")
def bill_page_url(ui_server, api):
    """Seed chart of accounts and create a draft bill; return its URL."""
    r = api.post("/accounting/chart/seed")
    assert r.status_code in (200, 409), f"Chart seed failed: {r.text}"

    r = api.post("/docs", json={
        "doc_type": "bill",
        "ref_id": "BILL-COMBOBOX-TEST",
        "status": "draft",
        "line_items": [{"name": "Test Item", "quantity": 1, "unit_price": 100.0, "line_total": 100.0}],
        "total": 100.0,
    })
    assert r.status_code in (200, 201), f"Create bill failed: {r.text}"
    doc_id = r.json()["id"]
    return f"{ui_server}/docs/{doc_id}"


def _load(page, url):
    page.goto(url, wait_until="domcontentloaded")
    page.wait_for_selector(".col-account", timeout=10000)


def test_account_column_has_combobox_wrap(page, bill_page_url):
    """The account cell must contain a .combobox-wrap, not a bare input."""
    _load(page, bill_page_url)
    assert page.locator(".col-account .combobox-wrap").count() >= 1, "Expected .combobox-wrap inside .col-account"


def test_account_combobox_opens_on_focus(page, bill_page_url):
    """Focusing the combobox input must open the dropdown list."""
    _load(page, bill_page_url)
    inp = page.locator(".col-account .combobox-input").first
    inp.click()
    page.wait_for_selector(".combobox-list.open", timeout=3000)
    assert page.locator(".combobox-list.open").is_visible()


def test_account_combobox_has_options(page, bill_page_url):
    """After opening, must show at least one non-empty option."""
    _load(page, bill_page_url)
    page.locator(".col-account .combobox-input").first.click()
    page.wait_for_selector(".combobox-list.open", timeout=3000)
    opts = page.locator(".combobox-list.open .combobox-option:not(.combobox-option--empty)")
    assert opts.count() > 0, "No options visible in account combobox dropdown"


def test_account_combobox_filter(page, bill_page_url):
    """Typing in the input filters options."""
    _load(page, bill_page_url)
    inp = page.locator(".col-account .combobox-input").first
    inp.click()
    page.wait_for_selector(".combobox-list.open", timeout=3000)
    inp.fill("1130")
    page.wait_for_timeout(300)
    opts = page.locator(".combobox-list.open .combobox-option:not(.combobox-option--empty)").filter(visible=True)
    assert opts.count() >= 1, "Expected at least one option matching '1130'"


def test_account_combobox_select_updates_hidden(page, bill_page_url):
    """Clicking an option must update the hidden input value."""
    _load(page, bill_page_url)
    inp = page.locator(".col-account .combobox-input").first
    hidden = page.locator(".col-account input[type=hidden]").first
    inp.click()
    page.wait_for_selector(".combobox-list.open", timeout=3000)
    first_opt = page.locator(".combobox-list.open .combobox-option:not(.combobox-option--empty)").first
    opt_value = first_opt.get_attribute("data-value")
    first_opt.click()
    page.wait_for_timeout(200)
    assert hidden.input_value() == opt_value, f"Hidden input not updated: expected {opt_value}, got {hidden.input_value()}"
