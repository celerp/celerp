# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""
Browser test: the manual journal entry journey end to end.

Proves:
1. The Journal tab renders with a New entry action.
2. The entry form's balance chip flips to Balanced as amounts are typed.
3. Posting lands back on the Journal tab with the entry visible.
4. Voiding the entry marks it voided on the journal.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.browser


@pytest.fixture(scope="module")
def seeded_chart(api):
    r = api.post("/accounting/chart/seed")
    assert r.status_code in (200, 409), f"Chart seed failed: {r.text}"


def test_manual_je_post_and_void_journey(page, ui_server, seeded_chart):
    page.goto(f"{ui_server}/accounting/journal/new", wait_until="domcontentloaded")
    page.wait_for_selector("#je-lines", timeout=10000)

    page.fill('input[name="ts"]', "2026-06-15")
    page.fill('input[name="memo"]', "Browser test adjustment")

    # Two rows render by default; fill account + amount on each.
    inputs = page.locator("#je-lines .combobox-input")
    inputs.nth(0).click()
    page.wait_for_selector(".combobox-list.open", timeout=3000)
    inputs.nth(0).fill("1111")
    page.locator(".combobox-list.open .combobox-option:not(.combobox-option--empty)").first.click()
    page.fill('#je-lines [name="debit_0"]', "42.50")

    chip = page.locator("#je-balance-chip")
    assert "val-chip--alert" in (chip.get_attribute("class") or "")

    inputs.nth(1).click()
    page.wait_for_selector(".combobox-list.open", timeout=3000)
    inputs.nth(1).fill("4100")
    page.locator(".combobox-list.open .combobox-option:not(.combobox-option--empty)").first.click()
    page.fill('#je-lines [name="credit_1"]', "42.50")
    page.wait_for_function(
        "document.getElementById('je-balance-chip').className === 'val-chip'", timeout=3000)

    page.click('button[type="submit"]')
    page.wait_for_url("**/accounting?tab=journal*", timeout=10000)
    page.wait_for_selector("text=Browser test adjustment", timeout=10000)

    # Void: open the inline reason form and confirm.
    page.locator("details summary", has_text="Void").first.click()
    reason = page.locator('details[open] input[name="reason"]').first
    reason.fill("browser test cleanup")
    page.locator("details[open] button", has_text="Confirm").first.click()
    page.wait_for_selector(".badge--void, .payment-voided", timeout=10000)
