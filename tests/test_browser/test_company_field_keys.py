# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Keys in a company field's edit cell on Company Details (GDR 2j).

Initial conditions: the seeded company, no opening balance date set, fiscal year starting
in January. Escape in an open edit cell puts the display cell back without saving; Enter in
it saves, as the cell's Save button does. Both for the opening balance date (a text-like
input) and the fiscal year start (a select).
"""
from __future__ import annotations

import re

import pytest

pytestmark = pytest.mark.browser

_DATE = "#company-opening_balance_date-input"
_FISCAL = "#company-fiscal_year_start-input"


def _cell(page, label: str):
    return page.locator("tr", has_text=re.compile(label)).first.locator("td").nth(1)


def _opening_date(api) -> str | None:
    company = api.get("/companies/me").json()
    return company.get("opening_balance_date") or (company.get("settings") or {}).get("opening_balance_date")


def test_escape_cancels_and_enter_saves_the_opening_balance_date(page, ui_server, api):
    page.goto(f"{ui_server}/finance/company-details", wait_until="domcontentloaded")
    before = _opening_date(api)
    cell = _cell(page, "Opening balances as at")

    cell.click()
    page.wait_for_selector(_DATE, timeout=5000)
    page.fill(_DATE, "2026-01-31")
    page.locator(_DATE).press("Escape")
    page.wait_for_selector(_DATE, state="detached", timeout=5000)
    assert _opening_date(api) == before

    _cell(page, "Opening balances as at").click()
    page.wait_for_selector(_DATE, timeout=5000)
    page.fill(_DATE, "2026-01-31")
    page.locator(_DATE).press("Enter")
    page.wait_for_selector(_DATE, state="detached", timeout=5000)
    assert "2026-01-31" in _cell(page, "Opening balances as at").inner_text()
    assert _opening_date(api) == "2026-01-31"

    r = page.request.patch(f"{ui_server}/settings/company/opening_balance_date", form={"value": ""})
    assert r.ok, r.text()


def test_escape_closes_the_fiscal_year_start_select(page, ui_server, api):
    page.goto(f"{ui_server}/finance/company-details", wait_until="domcontentloaded")
    _cell(page, "Fiscal year start").click()
    page.wait_for_selector(_FISCAL, timeout=5000)
    page.locator(_FISCAL).press("Escape")
    page.wait_for_selector(_FISCAL, state="detached", timeout=5000)
