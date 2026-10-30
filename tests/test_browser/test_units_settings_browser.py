# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The Units settings tab edits a unit in place.

Opening a unit's Decimals keeps its editor open, a refused change leaves the stored value
with the reason under the cell, a raised value saves, and adding a unit still lands on the
refreshed table (or says why it was refused).
"""
from __future__ import annotations

from urllib.parse import unquote_plus, urlsplit

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.browser

DEC = 'td[hx-get="/settings/units/carat/decimals/edit"]'
EDITOR = 'input[hx-patch="/settings/units/carat/decimals"]'


def _decimals(api, name="carat") -> int:
    return next(u["decimals"] for u in api.get("/companies/me/units").json() if u["name"] == name)


def _open_units(page, ui_server):
    page.goto(f"{ui_server}/settings/inventory?tab=units", wait_until="domcontentloaded")
    page.locator(DEC).wait_for(state="visible", timeout=8000)
    page.evaluate("window.__sameView = true")


def _save_decimals(page, value: str):
    page.locator(DEC).click()
    editor = page.locator(EDITOR)
    editor.wait_for(state="visible", timeout=4000)
    page.wait_for_timeout(800)
    assert page.evaluate("window.__sameView === true"), "the page reloaded when the editor opened"
    expect(editor).to_be_visible()
    editor.fill(value)
    with page.expect_response(lambda r: r.request.method == "PATCH" and "/settings/units/carat" in r.url):
        editor.press("Enter")
    page.wait_for_timeout(800)


def test_a_refused_decimals_change_stays_on_screen_with_the_reason(page, ui_server, fresh_company):
    lot = fresh_company.post("/items", json={"sku": "UNITS-FINE", "name": "Fine lot", "quantity": 0.27,
                                             "sell_by": "carat", "status": "available"})
    assert lot.status_code == 200, lot.text
    _open_units(page, ui_server)
    _save_decimals(page, "1")

    assert page.evaluate("window.__sameView === true"), "the page reloaded after the refusal"
    expect(page.locator(f"{DEC} .cell-error")).to_contain_text("UNITS-FINE")
    expect(page.locator(DEC)).to_contain_text("2")
    assert _decimals(fresh_company) == 2


def test_a_raised_decimals_value_saves_in_place(page, ui_server, fresh_company):
    _open_units(page, ui_server)
    _save_decimals(page, "3")

    assert page.evaluate("window.__sameView === true"), "the page reloaded after the save"
    expect(page.locator(DEC)).to_contain_text("3")
    expect(page.locator(f"{DEC} .cell-error")).to_have_count(0)
    assert _decimals(fresh_company) == 3


def test_adding_a_unit_shows_it_in_the_table(page, ui_server, fresh_company):
    _open_units(page, ui_server)
    page.fill('input[name="name"][placeholder]', "tola")
    page.fill('input[name="decimals"][form="units-add"]', "3")
    page.locator('button[type="submit"][form="units-add"]').click()
    page.locator('td[hx-get="/settings/units/tola/decimals/edit"]').wait_for(state="visible", timeout=8000)
    assert _decimals(fresh_company, "tola") == 3


def _watch_requests(page) -> list[tuple[str, str, str]]:
    sent: list[tuple[str, str, str]] = []
    page.on("request", lambda r: sent.append((r.method, r.url, r.post_data or ""))
            if "/settings/units" in r.url else None)
    page.evaluate("window.__invalid = []; "
                  "document.addEventListener('invalid', e => window.__invalid.push(e.target.name), true)")
    return sent


@pytest.mark.parametrize("field,value", [("decimals", "1"), ("decimals", "3"), ("label", "Carat weight")])
def test_enter_in_an_inline_editor_sends_only_its_change(page, ui_server, fresh_company, field, value):
    lot = fresh_company.post("/items", json={"sku": "UNITS-ENTER", "name": "Fine lot", "quantity": 0.27,
                                             "sell_by": "carat", "status": "available"})
    assert lot.status_code == 200, lot.text
    _open_units(page, ui_server)
    sent = _watch_requests(page)
    cell = page.locator(f'td[hx-get="/settings/units/carat/{field}/edit"]')
    cell.click()
    editor = page.locator(f'input[hx-patch="/settings/units/carat/{field}"]')
    editor.wait_for(state="visible", timeout=4000)
    page.wait_for_timeout(500)
    editor.fill(value)
    with page.expect_response(lambda r: r.request.method == "PATCH"):
        editor.press("Enter")
    page.wait_for_timeout(800)

    assert page.evaluate("window.__invalid") == [], "Enter ran the Add unit form's validation"
    expect(page.locator('[name="name"]:focus'), "focus jumped to the Add unit name box").to_have_count(0)
    assert [(m, urlsplit(u).path, unquote_plus(body)) for m, u, body in sent if m != "GET"] == [
        ("PATCH", f"/settings/units/carat/{field}", f"value={value}")]
    assert page.evaluate("window.__sameView === true"), "the page reloaded"
    if (field, value) == ("decimals", "1"):
        expect(page.locator(f"{DEC} .cell-error")).to_contain_text("UNITS-ENTER")
        assert _decimals(fresh_company) == 2
    else:
        expect(page.locator(f'td[hx-get="/settings/units/carat/{field}/edit"]')).to_contain_text(value)
        stored = next(u for u in fresh_company.get("/companies/me/units").json() if u["name"] == "carat")
        assert str(stored[field]) == value


def test_enter_in_the_add_unit_name_adds_it(page, ui_server, fresh_company):
    _open_units(page, ui_server)
    page.fill('input[name="name"][placeholder]', "tola")
    page.locator('input[name="name"][placeholder]').press("Enter")
    page.locator('td[hx-get="/settings/units/tola/decimals/edit"]').wait_for(state="visible", timeout=8000)
    assert _decimals(fresh_company, "tola") == 0
