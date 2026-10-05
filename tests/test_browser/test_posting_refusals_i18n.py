# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A posting account the role cannot use is refused in the user's language."""
import pytest

pytestmark = pytest.mark.browser


def test_a_refused_posting_account_is_explained_in_german(page, ui_server):
    from playwright.sync_api import expect

    host = ui_server.split("//", 1)[1].split(":", 1)[0]
    page.context.add_cookies([{"name": "celerp_lang", "value": "de", "domain": host, "path": "/"}])
    try:
        page.goto(f"{ui_server}/settings/accounting?tab=posting-accounts", wait_until="domcontentloaded")
        row = page.locator("#posting-sales_revenue")
        row.locator("td").nth(1).click()
        picker = row.locator("[hx-patch]")
        expect(picker).to_have_count(1)
        # 1110 (Cash) is an asset header, never a revenue account: the API refuses it.
        picker.evaluate("""el => {
            if (el.tagName === 'SELECT') el.add(new Option('1110', '1110'));
            el.value = '1110';
            el.dispatchEvent(new Event('change', {bubbles: true}));
        }""")
        row = page.locator("#posting-sales_revenue")
        expect(row.locator(".cell-error")).to_contain_text("1110")
        text = row.inner_text()
        assert "ist auf Konto 1110" in text, text
        assert "is set to account" not in text, text
    finally:
        page.context.clear_cookies(name="celerp_lang")
