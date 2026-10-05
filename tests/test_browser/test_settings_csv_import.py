# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The settings CSV imports reached from their tabs create what the file holds and lead
back to the tab that lists it: taxes on Sales settings, payment terms on Contacts."""
from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.browser

_CASES = [
    ("/settings/sales?tab=taxes", "taxes", b"name,rate,tax_type,is_default,description\nCSV Tax 7,7,both,,From a file\n",
     "CSV Tax 7"),
    ("/settings/contacts?tab=payment-terms", "payment-terms", b"name,days,description\nNet 45,45,Due in 45 days\n",
     "Net 45"),
]


@pytest.mark.parametrize("tab, kind, csv, name", _CASES, ids=[c[1] for c in _CASES])
def test_settings_import_creates_the_row_and_returns_to_its_tab(page: Page, fresh_company, tab, kind, csv, name):
    page.set_viewport_size({"width": 1280, "height": 900})
    page.goto(tab)
    page.locator(f"a[href='/settings/import/{kind}']").click()
    expect(page).to_have_url(re.compile(rf"/settings/import/{kind}$"))
    page.locator("input[type='file']").first.set_input_files({"name": f"{kind}.csv", "mimeType": "text/csv", "buffer": csv})
    page.locator("#csv-preview-btn").click()
    page.get_by_role("button", name="Continue to Preview").click()
    page.locator("button", has_text="Import All 1 Rows").click()
    back = page.locator("#import-preview a.btn--primary")
    expect(back).to_have_attribute("href", tab)
    # The result: one created, nothing failed.
    expect(page.locator(".import-summary-cards > *").first).to_contain_text("1")
    expect(page.locator("#import-preview details")).to_have_count(0)
    back.click()
    expect(page.locator("td", has_text=name).first).to_be_visible()
