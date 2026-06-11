# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Browser test: contact picker server-side search on a document page.

Proves:
1. The contact field on an invoice renders a combobox with data-search-url set.
2. Typing in the combobox triggers a server-side search (network request with q=).
3. The dropdown updates with results returned by /contacts/search-options.
4. Selecting a result commits the value to the hidden input.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.browser


@pytest.fixture(scope="module")
def contact_search_page(ui_server, api):
    """Seed two customers and a draft invoice; return the invoice URL."""
    for name in ("SearchableAlpha", "SearchableBeta"):
        r = api.post("/crm/contacts", json={"name": name, "contact_type": "customer"})
        assert r.status_code in (200, 201), f"Create contact failed: {r.text}"

    r = api.post("/docs", json={
        "doc_type": "invoice",
        "ref_id": "INV-SEARCH-TEST",
        "status": "draft",
        "line_items": [],
        "total": 0.0,
    })
    assert r.status_code in (200, 201), f"Create invoice failed: {r.text}"
    doc_id = r.json()["id"]
    return f"{ui_server}/docs/{doc_id}"


def _load(page, url):
    page.goto(url, wait_until="domcontentloaded")
    # Wait for at least one editable cell to confirm the doc detail rendered
    page.wait_for_selector(".editable-cell", timeout=10000)


def test_contact_field_has_server_search_url(page, contact_search_page):
    """After clicking the contact cell, the combobox wrap must carry data-search-url."""
    _load(page, contact_search_page)
    # Click the contact display cell to open the edit combobox. Target the
    # contact field specifically — a bare has_text="Select" can match another
    # editable cell (e.g. a line-item picker) that appears first in the DOM.
    contact_cell = page.locator("[hx-get*='field/contact_id/edit']").first
    contact_cell.click()
    page.wait_for_selector(".combobox-wrap[data-search-url]", timeout=5000)
    wrap = page.locator(".combobox-wrap[data-search-url]").first
    assert wrap.get_attribute("data-search-url") == "1"
    assert "search-options" in (wrap.get_attribute("hx-get") or "")


def test_contact_search_sends_q_param(page, contact_search_page):
    """Typing in the contact combobox must fire a request with q= in the URL."""
    _load(page, contact_search_page)
    contact_cell = page.locator("[hx-get*='field/contact_id/edit']").first
    contact_cell.click()
    page.wait_for_selector(".combobox-wrap[data-search-url] .combobox-input", timeout=5000)

    requests: list[str] = []
    page.on("request", lambda req: requests.append(req.url) if "search-options" in req.url else None)

    inp = page.locator(".combobox-wrap[data-search-url] .combobox-input").first
    inp.fill("Searchable")
    # Wait for the debounced HTMX request (300ms delay + network round-trip)
    page.wait_for_timeout(700)

    matching = [u for u in requests if "q=" in u and "search-options" in u]
    assert matching, f"No search-options request with q= fired. All requests: {requests}"
    assert "Searchable" in matching[0] or "searchable" in matching[0].lower()


def test_contact_search_shows_results(page, contact_search_page):
    """After typing, the dropdown must show contacts matching the query."""
    _load(page, contact_search_page)
    contact_cell = page.locator("[hx-get*='field/contact_id/edit']").first
    contact_cell.click()
    page.wait_for_selector(".combobox-wrap[data-search-url] .combobox-input", timeout=5000)

    inp = page.locator(".combobox-wrap[data-search-url] .combobox-input").first
    inp.fill("Alpha")
    # Wait for HTMX to fire (300ms debounce) and the response to render
    page.wait_for_timeout(700)
    page.wait_for_selector(".combobox-list.open .combobox-option:not(.combobox-option--empty)", timeout=3000)

    opts = page.locator(".combobox-list.open .combobox-option:not(.combobox-option--empty)")
    assert opts.count() >= 1, "Expected at least one search result for 'Alpha'"
    texts = [opts.nth(i).inner_text() for i in range(opts.count())]
    assert any("Alpha" in t or "alpha" in t.lower() for t in texts), f"'Alpha' not in results: {texts}"


def test_contact_search_select_commits_value(page, contact_search_page):
    """Clicking a server-search result must update the hidden input value."""
    _load(page, contact_search_page)
    contact_cell = page.locator("[hx-get*='field/contact_id/edit']").first
    contact_cell.click()
    page.wait_for_selector(".combobox-wrap[data-search-url] .combobox-input", timeout=5000)

    inp = page.locator(".combobox-wrap[data-search-url] .combobox-input").first
    hidden = page.locator(".combobox-wrap[data-search-url] input[type=hidden]").first
    inp.fill("Alpha")
    page.wait_for_timeout(700)
    page.wait_for_selector(".combobox-list.open .combobox-option:not(.combobox-option--empty)", timeout=3000)

    first_opt = page.locator(".combobox-list.open .combobox-option:not(.combobox-option--empty)").first
    opt_value = first_opt.get_attribute("data-value")
    first_opt.click()
    page.wait_for_timeout(200)

    assert hidden.input_value() == opt_value, (
        f"Hidden input not updated after selecting search result: "
        f"expected {opt_value!r}, got {hidden.input_value()!r}"
    )


def test_clearing_search_restores_static_options(page, contact_search_page):
    """Clearing the search input must restore the original static options."""
    _load(page, contact_search_page)
    contact_cell = page.locator("[hx-get*='field/contact_id/edit']").first
    contact_cell.click()
    page.wait_for_selector(".combobox-wrap[data-search-url] .combobox-input", timeout=5000)

    inp = page.locator(".combobox-wrap[data-search-url] .combobox-input").first
    # Type to trigger server search
    inp.fill("Alpha")
    page.wait_for_timeout(700)
    # Now clear
    inp.fill("")
    page.wait_for_timeout(200)
    # Static options (loaded at page render time) should be back
    opts = page.locator(".combobox-list.open .combobox-option:not(.combobox-option--empty)")
    assert opts.count() > 0, "Static options not restored after clearing search"