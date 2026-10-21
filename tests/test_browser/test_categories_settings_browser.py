# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The Categories settings tab says why a library category was refused, on the same page.

Adding a category that seeds back a deleted unit at fewer decimals than a lot holds is refused
with the reason under the library, without a reload; a category that fits still lands on the
refreshed list showing it applied.
"""
from __future__ import annotations

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.browser

ADD_BOOK = "form:has(input[name=name][value=book])"


def _applied(api) -> set[str]:
    return set(api.get("/companies/me/company-category-schemas").json())


def _open_categories(page, ui_server):
    page.goto(f"{ui_server}/settings/inventory?tab=categories", wait_until="domcontentloaded")
    page.locator(ADD_BOOK).first.wait_for(state="attached", timeout=8000)
    page.evaluate("document.querySelectorAll('details').forEach(d => d.open = true); window.__sameView = true")


def _add_book(page):
    form = page.locator(ADD_BOOK).first
    form.scroll_into_view_if_needed()
    with page.expect_response(lambda r: r.request.method == "POST" and "/settings/verticals/apply-category" in r.url):
        form.locator("button[type=submit]").click()


def test_a_refused_library_category_says_why_on_the_page(page, ui_server, fresh_company):
    units = fresh_company.get("/companies/me/units").json()
    finer = [{**u, "decimals": 3} if u["name"] == "gram" else u for u in units]
    if not any(u["name"] == "gram" for u in units):
        finer.append({"name": "gram", "label": "Gram (g)", "decimals": 3, "unit_type": "weight"})
    assert fresh_company.put("/companies/me/units", json={"units": finer}).status_code == 200
    lot = fresh_company.post("/items", json={"sku": "CAT-FINE", "name": "Gold dust", "quantity": 0.275,
                                             "sell_by": "gram", "status": "available"})
    assert lot.status_code == 200, lot.text
    without_gram = [u for u in finer if u["name"] != "gram"]
    assert fresh_company.put("/companies/me/units", json={"units": without_gram}).status_code == 200

    _open_categories(page, ui_server)
    _add_book(page)
    page.wait_for_timeout(800)

    assert page.evaluate("window.__sameView === true"), "the page reloaded after the refusal"
    expect(page.locator("#verticals-apply-result .cell-error")).to_contain_text("decimal places")
    expect(page.locator("#verticals-apply-result .cell-error")).to_contain_text("CAT-FINE")
    assert "book" not in _applied(fresh_company)
    assert not any(u["name"] == "gram" for u in fresh_company.get("/companies/me/units").json())


def test_adding_a_library_category_lands_on_the_refreshed_list(page, ui_server, fresh_company):
    assert "book" not in _applied(fresh_company)
    _open_categories(page, ui_server)
    _add_book(page)
    page.wait_for_url("**/settings/inventory?tab=categories", timeout=8000)
    page.wait_for_load_state("domcontentloaded")

    assert "book" in _applied(fresh_company)
    expect(page.locator(ADD_BOOK)).to_have_count(0)
    expect(page.locator("#verticals-apply-result .cell-error")).to_have_count(0)
