# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The Categories settings tab says why a category, library category or preset was refused, where
it was clicked.

Adding a category (or the preset holding it) that seeds back a deleted unit at fewer decimals than
a lot holds is refused with the reason beside the button, in view and without a reload; a category
that fits still lands on the refreshed list showing it applied. A new category whose name is taken
is refused beside the add form, which keeps what was typed, and the list stays as it was.
"""
from __future__ import annotations

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.browser

ADD_BOOK = "form:has(input[name=name][value=book])"
APPLY_BOOKS = "form:has(input[name=vertical][value=books_media])"
# The refusal in the row or heading of the button that was clicked, on screen.
SHOWN_BY = """(form, text) => { const slot = form.closest('tr, summary').querySelector('.vert-apply-result .cell-error');
  if (!slot || !slot.innerText.includes(text)) return false;
  const r = slot.getBoundingClientRect(); return r.bottom > 0 && r.top < innerHeight; }"""


def _applied(api) -> set[str]:
    return set(api.get("/companies/me/company-category-schemas").json())


def _open_categories(page, ui_server):
    page.goto(f"{ui_server}/settings/inventory?tab=categories", wait_until="domcontentloaded")
    page.locator(ADD_BOOK).first.wait_for(state="attached", timeout=8000)
    page.evaluate("document.querySelectorAll('details').forEach(d => d.open = true); window.__sameView = true")


def _submit(page, selector: str, path: str):
    form = page.locator(selector).first
    form.scroll_into_view_if_needed()
    with page.expect_response(lambda r: r.request.method == "POST" and path in r.url):
        form.locator("button[type=submit]").click()
    return form


def _add_book(page):
    return _submit(page, ADD_BOOK, "/settings/verticals/apply-category")


def _gram_lot_then_delete_gram(api) -> None:
    units = api.get("/companies/me/units").json()
    finer = [{**u, "decimals": 3} if u["name"] == "gram" else u for u in units]
    if not any(u["name"] == "gram" for u in units):
        finer.append({"name": "gram", "label": "Gram (g)", "decimals": 3, "unit_type": "weight"})
    assert api.put("/companies/me/units", json={"units": finer}).status_code == 200
    lot = api.post("/items", json={"sku": "CAT-FINE", "name": "Gold dust", "quantity": 0.275,
                                   "sell_by": "gram", "status": "available"})
    assert lot.status_code == 200, lot.text
    without_gram = [u for u in finer if u["name"] != "gram"]
    assert api.put("/companies/me/units", json={"units": without_gram}).status_code == 200


@pytest.mark.parametrize("selector, path", [(ADD_BOOK, "/settings/verticals/apply-category"),
                                            (APPLY_BOOKS, "/settings/verticals/apply-preset")])
def test_a_refused_library_category_or_preset_says_why_where_it_was_clicked(page, ui_server, fresh_company,
                                                                            selector, path):
    api = fresh_company
    _gram_lot_then_delete_gram(api)

    _open_categories(page, ui_server)
    form = _submit(page, selector, path)
    page.wait_for_function(f"({SHOWN_BY})(document.querySelector('{selector}'), 'CAT-FINE')", timeout=8000)

    assert page.evaluate("window.__sameView === true"), "the page reloaded after the refusal"
    assert form.evaluate(SHOWN_BY, "decimal places")
    # Only the clicked button answers.
    expect(page.locator(".vert-apply-result .cell-error")).to_have_count(1)
    assert "book" not in _applied(api)
    assert not any(u["name"] == "gram" for u in api.get("/companies/me/units").json())


def test_adding_a_library_category_lands_on_the_refreshed_list(page, ui_server, fresh_company):
    assert "book" not in _applied(fresh_company)
    _open_categories(page, ui_server)
    _add_book(page)
    page.wait_for_url("**/settings/inventory?tab=categories", timeout=8000)
    page.wait_for_load_state("domcontentloaded")

    assert "book" in _applied(fresh_company)
    expect(page.locator(ADD_BOOK)).to_have_count(0)
    expect(page.locator(".vert-apply-result .cell-error")).to_have_count(0)


def test_a_new_category_with_a_taken_name_is_refused_beside_the_form(page, ui_server, fresh_company):
    api = fresh_company
    assert api.post("/companies/me/categories", json={"name": "Colored Stone"}).status_code == 200
    _open_categories(page, ui_server)
    rows = page.locator("#your-cats-section tbody tr")
    before = rows.count()
    typed = page.locator("input[name=new_category_name]")
    typed.fill("colored stone")
    _submit(page, "form.cat-add-form", "/settings/categories")

    refusal = page.locator("form.cat-add-form .cat-add-result .cell-error")
    expect(refusal).to_have_text("A category called colored stone already exists. Choose a different name.")
    expect(refusal).to_be_in_viewport()
    assert page.evaluate("window.__sameView === true"), "the page reloaded after the refusal"
    expect(rows).to_have_count(before)
    expect(typed).to_have_value("colored stone")

    # A name not taken lands on the refreshed list showing it.
    typed.fill("Gemstone")
    form = page.locator("form.cat-add-form")
    with page.expect_navigation(url="**/settings/inventory?tab=categories", timeout=8000):
        form.locator("button[type=submit]").click()
    expect(page.locator("#your-cats-section tbody tr")).to_have_count(before + 1)
    expect(page.locator(".cat-add-result .cell-error")).to_have_count(0)


def test_a_blank_new_category_says_to_enter_a_name_beside_the_form(page, ui_server, fresh_company):
    _open_categories(page, ui_server)
    rows = page.locator("#your-cats-section tbody tr")
    before = rows.count()
    page.locator("input[name=new_category_name]").fill("")
    _submit(page, "form.cat-add-form", "/settings/categories")

    refusal = page.locator("form.cat-add-form .cat-add-result .cell-error")
    expect(refusal).to_have_text("Enter a category name.")
    expect(refusal).to_be_in_viewport()
    assert page.evaluate("window.__sameView === true"), "the page reloaded after the refusal"
    expect(rows).to_have_count(before)
