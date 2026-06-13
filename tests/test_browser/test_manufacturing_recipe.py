# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Browser test + screenshot capture for the item Manufacturing (recipe) tab.

The recipe behaves like every other celerp table: a ghost add-row commits real rows,
values are double-click-to-edit cells, and every change persists immediately.
Screenshots go to context/reviews/phase1/ for the professionalism review.
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.browser

SHOTS = Path("context/reviews/phase1")


@pytest.fixture(scope="module")
def recipe_item(api):
    """A priced raw component and a finished good; returns (fg_id, raw_sku)."""
    raw = api.post("/items", json={"sku": "GOLD-1G", "name": "Gold 1g", "quantity": 100, "sell_by": "gram",
                                   "cost_total": 8000, "inventory_type": "component"})
    assert raw.status_code == 200, raw.text
    fg = api.post("/items", json={"sku": "RING-18K", "name": "18K Ring", "quantity": 0, "sell_by": "piece"})
    assert fg.status_code == 200, fg.text
    return fg.json()["id"], "GOLD-1G"


def _open_tab(page, ui_server, item_id):
    page.goto(f"{ui_server}/inventory/{item_id}?tab=manufacturing", wait_until="domcontentloaded")
    page.wait_for_selector("#recipe-form", timeout=10000)


def _ghost_pick(page, sku):
    """Pick a SKU in the ghost add-row combobox; commits a real component row."""
    box = page.locator(".recipe-add-row .combobox-input").first
    box.click()
    box.fill(sku)
    page.wait_for_selector(".combobox-list.open", timeout=3000)
    opt = page.locator(".combobox-list.open .combobox-option:not(.combobox-option--empty):visible").first
    opt.wait_for(state="visible", timeout=3000)
    opt.click()


def _set_cell(page, data_col, value):
    """Edit a recipe value the system-standard way: double-click, type, Enter.

    Retries once: rapid successive cell edits can race the previous cell's swap settling.
    """
    for attempt in range(2):
        try:
            page.dblclick(f'td[data-col="{data_col}"]')
            inp = page.locator("input[name=value]")
            inp.wait_for(state="visible", timeout=4000)
            inp.fill(str(value))
            inp.press("Enter")
            cell = page.locator(f'td[data-col="{data_col}"]')
            cell.wait_for(state="visible", timeout=6000)
            if str(value) in cell.inner_text():
                return
        except Exception:
            if attempt:
                raise
    raise AssertionError(f"cell {data_col} did not save {value}")


def test_recipe_tab_flow_with_screenshots(page, ui_server, api, recipe_item):
    SHOTS.mkdir(parents=True, exist_ok=True)
    item_id, raw_sku = recipe_item
    page.set_viewport_size({"width": 1440, "height": 1000})

    _open_tab(page, ui_server, item_id)
    page.screenshot(path=str(SHOTS / "01-empty.png"), full_page=True)

    # Ghost add-row commits a real, persisted component (qty defaults to 1).
    _ghost_pick(page, raw_sku)
    page.wait_for_selector('td[data-col="recipe__components__0__quantity"]', timeout=8000)
    _set_cell(page, "recipe__components__0__quantity", "5")
    page.wait_for_selector("#recipe-cost-card:has-text('400')", timeout=8000)

    # Materials row shows the catalog unit cost (80) and extended cost (400), both read-only,
    # and the Materials section header carries the section total (400). (Items #2 + #4.)
    mat_row = page.locator('td[data-col="recipe__components__0__quantity"]').locator("xpath=..")
    row_text = mat_row.inner_text()
    assert "80" in row_text and "400" in row_text
    mat_head = page.locator(".recipe-block:has-text('Materials') .recipe-section-total").first
    assert "400" in mat_head.inner_text()
    # The unit cost is not editable here (no edit_url / dblclick handler on those cells).
    assert page.locator('td[data-col="recipe__components__0__unit_cost"]').count() == 0

    # Labor: fill the whole add-row (operation + hours + rate), then "+ Add" commits it at once.
    page.fill("input[name=labor_new_op]", "Setting")
    page.fill("input[name=labor_new_hours]", "2")
    page.fill("input[name=labor_new_rate]", "50")
    page.locator("tr.recipe-add-row:has(input[name=labor_new_op]) button.recipe-add-btn").click()
    page.wait_for_selector('td[data-col="recipe__labor__0__rate"]', timeout=8000)

    # Overhead: same one-step add.
    page.fill("input[name=oh_new_desc]", "Polishing & box")
    page.fill("input[name=oh_new_amount]", "15")
    page.locator("tr.recipe-add-row:has(input[name=oh_new_desc]) button.recipe-add-btn").click()
    page.wait_for_selector('td[data-col="recipe__overhead__0__amount"]', timeout=8000)
    page.screenshot(path=str(SHOTS / "02-filled.png"), full_page=True)

    # 5*80 materials + 100 labor + 15 overhead = 515, all persisted automatically.
    page.wait_for_selector("#recipe-cost-card:has-text('515')", timeout=8000)
    page.screenshot(path=str(SHOTS / "03-saved.png"), full_page=True)
    assert api.get(f"/items/{item_id}").json()["recipe"]["unit_cost"] == 515.0

    # No Save button anywhere; Clear remains (GDR 2a).
    assert page.locator("button:has-text('Save recipe')").count() == 0
    assert page.locator("button:has-text('Clear recipe')").count() == 1

    # Progress survives navigation: leave the tab and come back.
    page.goto(f"{ui_server}/inventory/{item_id}?tab=details", wait_until="domcontentloaded")
    _open_tab(page, ui_server, item_id)
    page.wait_for_selector("#recipe-cost-card:has-text('515')", timeout=8000)
    assert page.locator('td[data-col="recipe__components__0__quantity"]').inner_text().strip() == "5"


def test_fixed_labor_derived_unit_and_add_new_option(page, ui_server, api):
    """Fixed labor, unit derived from the component's sell unit, and the add-new option."""
    api.post("/items", json={"sku": "FL-GOLD", "name": "Gold", "quantity": 100, "sell_by": "gram",
                             "cost_total": 1000, "inventory_type": "component"})  # unit 10
    fg = api.post("/items", json={"sku": "FL-FG", "name": "FG", "quantity": 0, "sell_by": "piece"}).json()["id"]
    page.set_viewport_size({"width": 1440, "height": 1000})
    _open_tab(page, ui_server, fg)

    # Pinned action options live inside THIS picker: scope toggle + add-new. They survive typing.
    box = page.locator(".recipe-add-row .combobox-input").first
    box.click()
    page.wait_for_selector(".combobox-list.open", timeout=3000)
    assert page.locator(".combobox-list.open .combobox-option--new:has-text('Add new component')").count() == 1
    assert page.locator(".combobox-list.open .combobox-option--pinned:has-text('Search all items')").count() == 1
    box.fill("zzz-no-match")
    assert page.locator(".combobox-list.open .combobox-option--pinned:has-text('Search all items')").is_visible()
    box.fill("")

    _ghost_pick(page, "FL-GOLD")
    page.wait_for_selector('td[data-col="recipe__components__0__quantity"]', timeout=8000)
    _set_cell(page, "recipe__components__0__quantity", "2")
    # Unit comes from the component's sell unit; there is no unit input anywhere.
    assert "gram" in page.locator(".comp-unit-cell").first.inner_text()
    assert page.locator("input[name=comp_unit_0]").count() == 0

    # Fixed labor: choose Fixed + amount right in the add-row, then + Add — all in one step.
    page.fill("input[name=labor_new_op]", "Bench fee")
    page.select_option("select[name=labor_new_kind]", "fixed")
    page.fill("input[name=labor_new_amount]", "30")
    page.locator("tr.recipe-add-row:has(input[name=labor_new_op]) button.recipe-add-btn").click()
    page.wait_for_selector('td[data-col="recipe__labor__0__kind"]:has-text("Fixed")', timeout=8000)

    # 2 * 10 materials + 30 fixed labor = 50, persisted automatically.
    page.wait_for_selector("#recipe-cost-card:has-text('50')", timeout=8000)
    got = api.get(f"/items/{fg}").json()["recipe"]
    assert got["unit_cost"] == 50.0
    assert got["components"][0]["unit"] == "gram"
    assert got["labor"][0]["kind"] == "fixed"
