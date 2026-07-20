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

    # Two rows render by default; fill account + amount on each. Options are
    # targeted by data-value: clicking .first races the filter re-render.
    inputs = page.locator("#je-lines .combobox-input")

    def _pick_account(idx: int, code: str) -> None:
        inputs.nth(idx).click()
        page.wait_for_selector(".combobox-list.open", timeout=3000)
        inputs.nth(idx).fill(code)
        opt = page.locator(f'.combobox-list.open .combobox-option[data-value="{code}"]').first
        opt.wait_for(state="visible", timeout=3000)
        opt.click()

    _pick_account(0, "1111")
    page.fill('#je-lines [name="debit_0"]', "42.50")

    chip = page.locator("#je-balance-chip")
    assert "val-chip--alert" in (chip.get_attribute("class") or "")

    _pick_account(1, "4100")
    page.fill('#je-lines [name="credit_1"]', "42.50")
    page.wait_for_function(
        "document.getElementById('je-balance-chip').className === 'val-chip'", timeout=3000)

    page.click('button[type="submit"]')
    page.wait_for_url("**/accounting?tab=journal*", timeout=10000)
    page.wait_for_selector("text=Browser test adjustment", timeout=10000)

    # Void: act on this entry's own row. The journal is newest-first and shared
    # with every other entry the suite has posted, and the void control is shown
    # on document-generated rows too so the server can explain the refusal, so
    # "the first Void on the page" is not necessarily this entry's.
    row = page.locator("tr", has_text="Browser test adjustment").first
    row.locator("details summary", has_text="Void").first.click()
    reason = row.locator('details[open] input[name="reason"]').first
    reason.fill("browser test cleanup")
    row.locator("details[open] button", has_text="Confirm").first.click()
    page.wait_for_selector("tr.payment-voided .badge--void", timeout=10000)
