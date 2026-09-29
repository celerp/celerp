# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Browser test: choosing a List customer uses the document contact picker and saves."""
from __future__ import annotations

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.browser


@pytest.fixture(scope="module")
def list_url(ui_server, api):
    r = api.post("/crm/contacts", json={"name": "ListPickerCustomer", "contact_type": "customer",
                                        "company_name": "List Picker Ltd"})
    assert r.status_code in (200, 201), r.text
    r = api.post("/lists", json={"list_type": "quotation", "line_items": []})
    assert r.status_code in (200, 201), r.text
    return f"{ui_server}/lists/{r.json()['id']}"


def test_list_customer_is_chosen_with_the_contact_picker(page, list_url):
    page.goto(list_url, wait_until="domcontentloaded")
    page.locator("[hx-get*='field/contact_id/edit']").first.click()
    inp = page.locator(".combobox-wrap[data-search-url] .combobox-input").first
    expect(inp).to_be_visible(timeout=5000)
    inp.press_sequentially("ListPicker", delay=20)
    opt = page.locator(".combobox-list.open .combobox-option", has_text="ListPickerCustomer").first
    expect(opt).to_be_visible(timeout=10000)
    opt.click()
    # The save refreshes the whole List page with the customer snapshot applied.
    expect(page.locator("[hx-get*='field/contact_id/edit']").first).to_contain_text(
        "ListPickerCustomer", timeout=10000)
    page.reload(wait_until="domcontentloaded")
    expect(page.locator("[hx-get*='field/contact_id/edit']").first).to_contain_text(
        "ListPickerCustomer", timeout=10000)
    expect(page.get_by_text("List Picker Ltd").first).to_be_visible()
