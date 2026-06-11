# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Browser test + screenshot capture for the item Manufacturing (recipe) tab.

Proves the Phase-1 slice end to end in a real browser:
1. The Manufacturing tab renders the costing sheet (Materials / Labor / Overhead + Cost summary).
2. Components/labor/overhead can be added on-page (HTMX, no reload) with a searchable SKU picker.
3. Saving rolls the cost up and shows the unit cost.

Screenshots are written to context/reviews/phase1/ for the professionalism review.
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.browser

SHOTS = Path("context/reviews/phase1")


@pytest.fixture(scope="module")
def recipe_item(api):
    """Create a priced raw material and a finished good; return (ui_path, raw_sku)."""
    raw = api.post("/items", json={"sku": "GOLD-1G", "name": "Gold 1g", "quantity": 100, "sell_by": "gram", "cost_total": 8000})
    assert raw.status_code == 200, raw.text
    fg = api.post("/items", json={"sku": "RING-18K", "name": "18K Ring", "quantity": 0, "sell_by": "piece"})
    assert fg.status_code == 200, fg.text
    return fg.json()["id"], "GOLD-1G"


def _open_tab(page, ui_server, item_id):
    page.goto(f"{ui_server}/inventory/{item_id}?tab=manufacturing", wait_until="domcontentloaded")
    page.wait_for_selector("#recipe-form", timeout=10000)


def test_recipe_tab_flow_with_screenshots(page, ui_server, recipe_item):
    SHOTS.mkdir(parents=True, exist_ok=True)
    item_id, raw_sku = recipe_item
    page.set_viewport_size({"width": 1440, "height": 1000})

    _open_tab(page, ui_server, item_id)
    page.screenshot(path=str(SHOTS / "01-empty.png"), full_page=True)

    # Add a component and pick the raw via the searchable SKU picker.
    page.click("button:has-text('+ Add component')")
    page.wait_for_selector("input[name=comp_qty_0]", timeout=5000)
    box = page.locator("#recipe-form .combobox-input").last
    box.click()
    box.fill(raw_sku)
    page.wait_for_selector(".combobox-list.open", timeout=3000)
    opt = page.locator(".combobox-list.open .combobox-option:not(.combobox-option--empty):visible").first
    opt.wait_for(state="visible", timeout=3000)
    opt.click()
    page.fill("input[name=comp_qty_0]", "5")

    # Add a labor operation.
    page.click("button:has-text('+ Add operation')")
    page.wait_for_selector("input[name=labor_op_0]", timeout=5000)
    page.fill("input[name=labor_op_0]", "Setting")
    page.fill("input[name=labor_hours_0]", "2")
    page.fill("input[name=labor_rate_0]", "50")

    # Add an overhead line.
    page.click("button:has-text('+ Add cost')")
    page.wait_for_selector("input[name=oh_desc_0]", timeout=5000)
    page.fill("input[name=oh_desc_0]", "Polishing & box")
    page.fill("input[name=oh_amount_0]", "15")

    page.screenshot(path=str(SHOTS / "02-filled.png"), full_page=True)

    # Save → cost roll-up: 5*80 materials + 100 labor + 15 overhead = 515
    page.click("button:has-text('Save recipe')")
    page.wait_for_selector(".flash--success", timeout=8000)
    page.screenshot(path=str(SHOTS / "03-saved.png"), full_page=True)

    summary = page.locator(".recipe-cost-summary").inner_text()
    assert "515" in summary, f"Expected unit cost 515 in summary, got: {summary}"


def test_fixed_labor_and_derived_unit(page, ui_server, api):
    """Fixed (flat) labor + component unit derived from the component's sell unit."""
    gold = api.post("/items", json={"sku": "FL-GOLD", "name": "Gold", "quantity": 100, "sell_by": "gram", "cost_total": 1000}).json()["id"]  # unit 10
    fg = api.post("/items", json={"sku": "FL-FG", "name": "FG", "quantity": 0, "sell_by": "piece"}).json()["id"]
    page.set_viewport_size({"width": 1440, "height": 1000})
    page.goto(f"{ui_server}/inventory/{fg}?tab=manufacturing", wait_until="domcontentloaded")
    page.wait_for_selector("#recipe-form", timeout=10000)

    page.click("button:has-text('+ Add component')")
    page.wait_for_selector("input[name=comp_qty_0]", timeout=5000)
    box = page.locator("#recipe-form .combobox-input").last
    box.click(); box.fill("FL-GOLD")
    page.wait_for_selector(".combobox-list.open", timeout=3000)
    page.locator(".combobox-list.open .combobox-option:not(.combobox-option--empty):visible").first.click()
    page.fill("input[name=comp_qty_0]", "2")
    # The unit cell shows the component's sell unit (gram), live, with no unit input.
    page.wait_for_selector('.comp-unit[data-for="comp_item_0"]:has-text("gram")', timeout=3000)
    assert page.locator("input[name=comp_unit_0]").count() == 0

    page.click("button:has-text('+ Add operation')")
    page.fill("input[name=labor_op_0]", "Bench fee")
    page.select_option("select[name=labor_kind_0]", "fixed")
    # Switching to Fixed disables hours/rate and enables the amount field.
    assert page.locator("input[name=labor_hours_0]").is_disabled()
    assert not page.locator("input[name=labor_amount_0]").is_disabled()
    page.fill("input[name=labor_amount_0]", "30")

    page.click("button:has-text('Save recipe')")
    page.wait_for_selector(".flash--success", timeout=8000)
    # 2 * 10 materials + 30 fixed labor = 50
    assert api.get(f"/items/{fg}").json()["recipe"]["unit_cost"] == 50.0
    assert api.get(f"/items/{fg}").json()["recipe"]["components"][0]["unit"] == "gram"

    # After a save the button must read "Update recipe" and a "Clear recipe" action must exist (GDR §2a).
    assert page.locator("button:has-text('Update recipe')").count() == 1
    assert page.locator("button:has-text('Clear recipe')").count() == 1

    # Cost summary must not be clipped: it sits in the right column, fully on-screen.
    box = page.locator(".recipe-cost-summary").bounding_box()
    vw = page.viewport_size["width"]
    assert box is not None and box["x"] + box["width"] <= vw + 1, f"Cost summary overflows viewport: {box}, vw={vw}"
