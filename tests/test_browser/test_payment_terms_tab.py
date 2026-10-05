# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Payment terms live on the Contacts settings Payment Terms tab, and the settings gear
on the aging, sales and purchases reports opens that tab."""
from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.browser

_TAB = "/settings/contacts?tab=payment-terms"


@pytest.mark.parametrize("report", ["ar-aging", "ap-aging", "sales", "purchases"])
def test_report_settings_gear_opens_payment_terms(page: Page, fresh_company, report):
    page.goto(f"/reports/{report}")
    expect(page.locator("a.settings-gear")).to_have_attribute("href", _TAB)
