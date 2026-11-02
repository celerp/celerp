# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Settings > Taxes edits a rate in place: Enter saves it, a rate typed just before New Tax is
clicked is kept, and a 0% tax reads 0% (not the "--" of an empty cell)."""
from __future__ import annotations

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.browser

RATE = 'td[hx-get="/settings/taxes/{idx}/rate/edit"]'
EDITOR = 'input[hx-patch="/settings/taxes/{idx}/rate"]'


def _taxes(api) -> list[dict]:
    return api.get("/companies/me/taxes").json()


def _open_rate(page, ui_server, idx: int = 0):
    page.goto(f"{ui_server}/settings/sales?tab=taxes", wait_until="domcontentloaded")
    page.locator(RATE.format(idx=idx)).click()
    editor = page.locator(EDITOR.format(idx=idx))
    editor.wait_for(state="visible", timeout=4000)
    return editor


def test_enter_saves_a_tax_rate(page, ui_server, fresh_company):
    assert _taxes(fresh_company), "the new company starts with taxes"
    editor = _open_rate(page, ui_server)
    editor.fill("4.5")
    with page.expect_response(lambda r: r.request.method == "PATCH" and "/settings/taxes/0/rate" in r.url):
        editor.press("Enter")
    expect(page.locator(RATE.format(idx=0))).to_contain_text("4.5%")
    assert _taxes(fresh_company)[0]["rate"] == 4.5


def test_a_rate_typed_before_new_tax_is_kept(page, ui_server, fresh_company):
    before = len(_taxes(fresh_company))
    editor = _open_rate(page, ui_server)
    editor.fill("9")
    page.get_by_role("button", name="New Tax").click()
    page.locator(RATE.format(idx=before)).wait_for(state="visible", timeout=8000)
    taxes = _taxes(fresh_company)
    assert len(taxes) == before + 1
    assert taxes[0]["rate"] == 9
    # The new tax's 0% is a rate, not an empty cell.
    expect(page.locator(RATE.format(idx=before))).to_have_text("0%")
    expect(page.locator(RATE.format(idx=0))).to_have_text("9%")
