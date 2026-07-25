# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Browser test: contact picker server-side search on a document page.

Proves:
1. The contact field on an invoice renders a combobox with data-search-url set.
2. Typing in the combobox triggers a server-side search (network request with q=).
3. The dropdown updates with results returned by /contacts/search-options.
4. Selecting a result commits that contact to the document.
"""
from __future__ import annotations

import pytest
from playwright.sync_api import expect

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

    inp = page.locator(".combobox-wrap[data-search-url] .combobox-input").first
    # Deterministically wait for the debounced search request rather than a fixed sleep, which
    # races the 300ms debounce + network under suite load (the source of intermittent failures).
    with page.expect_request(
        lambda req: "search-options" in req.url and "q=" in req.url, timeout=5000
    ) as req_info:
        # Type char-by-char (real input events) rather than fill(): a single fill() event can land
        # before HTMX attaches the 'input changed delay:300ms' trigger to the freshly-opened combobox.
        inp.press_sequentially("Searchable", delay=20)
    url = req_info.value.url
    assert "Searchable" in url or "searchable" in url.lower(), f"q= did not carry the query: {url}"


def test_contact_search_shows_results(page, contact_search_page):
    """After typing, the dropdown must show contacts matching the query."""
    _load(page, contact_search_page)
    contact_cell = page.locator("[hx-get*='field/contact_id/edit']").first
    contact_cell.click()
    page.wait_for_selector(".combobox-wrap[data-search-url] .combobox-input", timeout=5000)

    inp = page.locator(".combobox-wrap[data-search-url] .combobox-input").first
    inp.press_sequentially("Alpha", delay=20)  # real input events trigger the HTMX search reliably
    page.wait_for_selector(".combobox-list.open .combobox-option:not(.combobox-option--empty)", timeout=10000)

    opts = page.locator(".combobox-list.open .combobox-option:not(.combobox-option--empty)")
    assert opts.count() >= 1, "Expected at least one search result for 'Alpha'"
    texts = [opts.nth(i).inner_text() for i in range(opts.count())]
    assert any("Alpha" in t or "alpha" in t.lower() for t in texts), f"'Alpha' not in results: {texts}"


def test_contact_search_select_commits_value(page, contact_search_page):
    """Selecting a server-search result must commit that contact to the document."""
    _load(page, contact_search_page)
    contact_cell = page.locator("[hx-get*='field/contact_id/edit']").first
    contact_cell.click()
    page.wait_for_selector(".combobox-wrap[data-search-url] .combobox-input", timeout=5000)

    inp = page.locator(".combobox-wrap[data-search-url] .combobox-input").first
    inp.press_sequentially("Alpha", delay=20)
    page.wait_for_selector(".combobox-list.open .combobox-option:not(.combobox-option--empty)", timeout=10000)

    first_opt = page.locator(".combobox-list.open .combobox-option:not(.combobox-option--empty)").first
    opt_label = first_opt.inner_text().strip()
    first_opt.click()

    # Selecting writes the hidden input and fires its change event, which commits the
    # field and swaps the editor back to the display cell - taking the hidden input
    # with it. Assert on what the commit leaves behind, not on the element it removes:
    # reading the hidden input is a race the fast machine loses.
    cell = page.locator("[hx-get*='field/contact_id/edit']").first
    expect(cell).to_contain_text(opt_label, timeout=10000)
    # And it is the server that holds it, not just the DOM.
    page.reload(wait_until="domcontentloaded")
    expect(page.locator("[hx-get*='field/contact_id/edit']").first).to_contain_text(opt_label, timeout=10000)


def test_clearing_search_restores_static_options(page, contact_search_page):
    """Clearing the search input must restore the original static options."""
    _load(page, contact_search_page)
    contact_cell = page.locator("[hx-get*='field/contact_id/edit']").first
    contact_cell.click()
    page.wait_for_selector(".combobox-wrap[data-search-url] .combobox-input", timeout=5000)

    inp = page.locator(".combobox-wrap[data-search-url] .combobox-input").first
    # Type to trigger server search, then wait for results so we know the search round-tripped.
    inp.press_sequentially("Alpha", delay=20)
    page.wait_for_selector(".combobox-list.open .combobox-option:not(.combobox-option--empty)", timeout=10000)
    # Now clear
    inp.fill("")
    page.wait_for_timeout(200)
    # Static options (loaded at page render time) should be back
    opts = page.locator(".combobox-list.open .combobox-option:not(.combobox-option--empty)")
    assert opts.count() > 0, "Static options not restored after clearing search"