# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The Units settings tab edits a unit in place.

Opening a unit's Decimals keeps its editor open, a refused change leaves the stored value
with the reason under the cell, a raised value saves, and adding a unit still lands on the
refreshed table (or says why it was refused).
"""
from __future__ import annotations

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
    page.fill('tfoot input[name="decimals"]', "3")
    page.locator('tfoot button[type="submit"]').click()
    page.locator('td[hx-get="/settings/units/tola/decimals/edit"]').wait_for(state="visible", timeout=8000)
    assert _decimals(fresh_company, "tola") == 3
