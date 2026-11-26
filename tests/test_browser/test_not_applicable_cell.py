# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""In the inventory's mixed-category All view, the cell of an attribute the row's category
does not define shows a not-applicable mark and stays clickable: opening it shows the
refusal a value there would get, with the link to the page that adds the attribute to the
category, never an editor."""
from __future__ import annotations

import pytest

from .test_company_backup_browser import _WAIT_MS
from .test_demo_items_link import _seed_demo_items

pytestmark = pytest.mark.browser


def test_a_cell_outside_the_rows_category_explains_instead_of_editing(page, fresh_company):
    assert fresh_company.post("/companies/me/business-type", json={"vertical": "gemstones"}).status_code == 200
    _seed_demo_items(fresh_company, "gemstones")
    # Shown here, a stone's origin type column reaches rows of categories without it.
    assert fresh_company.patch("/companies/me/column-prefs", json={
        "prefs": {"inventory": {"vis": {"origin_type": True}}}}).status_code == 200
    page.goto("/inventory")
    cell = page.locator("td.cell--na[data-col=origin_type]:visible").first
    cell.wait_for(timeout=_WAIT_MS)
    assert cell.locator("span.cell-na").inner_text().strip()
    cell_id, column = cell.get_attribute("id"), cell.get_attribute("data-col")
    cell.dblclick()
    opened = page.locator(f"#{cell_id} .cell-error")
    opened.wait_for(timeout=_WAIT_MS)
    assert f"no {column} attribute" in opened.inner_text()
    assert opened.locator("a[href='/settings/inventory?tab=categories']").count() == 1
    assert page.locator(f"#{cell_id} input, #{cell_id} select, #{cell_id} textarea").count() == 0
