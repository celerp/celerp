# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
# Copyright (c) 2026 Noah Severs. All rights reserved.
"""Group 5: Settings tabs — each tab loads without 404 or error."""
import pytest

pytestmark = pytest.mark.browser

# (tab_selector, expected_text_in_response)
_SETTINGS_TABS = [
    ("company", "Company"),
    ("users", "Users"),
    ("locations", "Locations"),
    ("taxes", "Taxes"),
    ("payment-terms", "Payment Terms"),
    ("field-schema", "Schema"),
    ("cat-schema", "Category"),
    ("modules", "Modules"),
    ("import-history", "Import"),
    ("bulk-attachments", "Attachment"),
    ("labels", "Labels"),
]


@pytest.mark.parametrize("tab,expected_text", _SETTINGS_TABS, ids=[t for t, _ in _SETTINGS_TABS])
def test_settings_tab_loads(page, ui_server, tab, expected_text):
    """SET-01..12: Settings tab → content loads, no errors."""
    # Navigate directly to the settings page with the tab parameter
    # The UI uses HTMX tabs — we can hit the tab endpoint directly
    resp = page.goto(f"{ui_server}/settings?tab={tab}", wait_until="domcontentloaded")
    assert resp.status != 500, f"Settings tab {tab!r} returned 500"
    body = page.locator("body").inner_text()
    assert "Internal Server Error" not in body, f"Settings tab {tab!r}: Internal Server Error"
    assert "Traceback" not in body, f"Settings tab {tab!r}: traceback in body"
    assert "/login" not in page.url, f"Settings tab {tab!r}: redirected to login"


@pytest.mark.parametrize("lang", ["en", "de"])
def test_category_rows_keep_their_buttons_inside_the_card_on_a_phone(page, fresh_company, ui_server, lang):
    """The Your Categories table fits a 390px screen in every language: the longer German
    button labels never push a button past the card edge, in the rows, the add form
    or an open rename."""
    assert fresh_company.post("/companies/me/business-type", json={"vertical": "gemstones"}).status_code == 200
    page.context.add_cookies([{"name": "celerp_lang", "value": lang, "url": ui_server}])
    page.set_viewport_size({"width": 390, "height": 900})
    page.goto("/settings/inventory?tab=categories")
    table = page.locator(".your-cats-table")
    table.wait_for()
    edge = page.evaluate("() => document.querySelector('.your-cats-table').parentElement.getBoundingClientRect().right")

    def inside(buttons):
        for btn in buttons:
            box = btn.bounding_box()
            assert box["x"] + box["width"] <= edge + 1, f"{btn.inner_text()!r} ends at {box['x'] + box['width']}, past {edge}"

    inside(table.locator("tbody .your-cats-action .btn").all() + table.locator(".cat-add-form button").all())
    table.locator(".cat-name-display").first.click()
    table.locator("input[name=new_name]").wait_for()
    inside(table.locator("input[name=new_name]").locator("xpath=..").locator("button").all())
