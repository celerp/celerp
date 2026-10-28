# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Escape leaves any click-to-edit cell as it was, including the cells that carry no Escape
handler of their own: the display comes back and the half-typed value is never saved, even
when the user then clicks elsewhere."""
from __future__ import annotations

import pytest

from .test_company_backup_browser import _WAIT_MS

pytestmark = pytest.mark.browser


def _location(api) -> dict:
    return next(loc for loc in api.get("/companies/me/locations").json()["items"] if loc["is_default"])


def _price_list(api) -> dict:
    return api.get("/companies/me/price-lists").json()[0]


def _escape_leaves_cell(page, cell_sel: str, typed: str | None):
    cell = page.locator(cell_sel)
    before = cell.inner_text().strip()
    cell.click()
    field = page.locator(".cell--editing input:not([type=hidden]), .cell--editing select").first
    field.wait_for(timeout=_WAIT_MS)
    if typed is not None:
        field.fill(typed)
    field.press("Escape")
    page.locator(".cell--editing").wait_for(state="detached", timeout=_WAIT_MS)
    assert page.locator(cell_sel).inner_text().strip() == before
    page.locator("h1, h2, .page-title").first.click()
    page.wait_for_timeout(600)
    return before


@pytest.mark.parametrize("field", ["name", "type"])
def test_escape_leaves_a_location_cell_unchanged(page, fresh_company, field):
    loc = _location(fresh_company)
    page.goto("/settings/inventory?tab=locations")
    _escape_leaves_cell(page, f'[hx-get="/settings/locations/{loc["id"]}/{field}/edit"]',
                        "Abandoned edit" if field == "name" else None)
    assert _location(fresh_company)[field] == loc[field]


def test_escape_leaves_a_price_list_cell_unchanged(page, fresh_company):
    pl = _price_list(fresh_company)
    page.goto("/settings/contacts?tab=price-lists")
    _escape_leaves_cell(page, '[hx-get="/settings/price-lists/0/name/edit"]', "Abandoned edit")
    assert _price_list(fresh_company)["name"] == pl["name"]
