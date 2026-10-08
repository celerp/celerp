# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A phone typed into a contact's phone cell is saved as typed, or refused with a reason,
whether or not the phone picker script loaded. It is never saved empty."""
from __future__ import annotations

import pytest
from playwright.sync_api import Page

pytestmark = pytest.mark.browser

_CELL = 'td[hx-get$="/field/phone/edit"]'


def _edit_phone(page: Page, contact_id: str, typed: str) -> None:
    page.goto(f"/contacts/{contact_id}")
    page.locator(_CELL).dblclick()
    box = page.locator("#contact-phone-visible")
    box.wait_for(state="visible")
    box.fill("")
    box.press_sequentially(typed)
    with page.expect_response(lambda r: r.request.method == "PATCH" and "/field/phone" in r.url):
        page.locator("td.cell--editing button.btn--primary").click()


def test_a_phone_typed_without_the_picker_is_saved(page: Page, fresh_company):
    page.route("**/vendor/intl-tel-input/**", lambda route: route.abort())
    cid = fresh_company.post("/crm/contacts", json={"name": "No Picker"}).json()["id"]
    _edit_phone(page, cid, "081 234 5678")
    assert fresh_company.get(f"/crm/contacts/{cid}").json()["phone"] == "081 234 5678"


def test_a_partial_phone_is_refused_and_the_saved_one_kept(page: Page, fresh_company):
    cid = fresh_company.post("/crm/contacts", json={"name": "Partial", "phone": "+66812345678"}).json()["id"]
    _edit_phone(page, cid, "12")
    assert fresh_company.get(f"/crm/contacts/{cid}").json()["phone"] == "+66812345678"
    assert page.locator(".cell-error").is_visible()
