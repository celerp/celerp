# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Verify the review-pass polish: Components inventory filter (item 3) + split box one-line (item 4).
Also covers: ESC-to-cancel on the Work In Progress inline editor + manufacturing UI screenshots."""
from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.browser

SHOTS = Path("context/reviews/polish")
MFG_SHOTS = Path("context/reviews/manufacturing")


def test_components_filter_tab_and_split_box_layout(page, ui_server, api):
    SHOTS.mkdir(parents=True, exist_ok=True)
    page.set_viewport_size({"width": 1440, "height": 1000})
    item = api.post("/items", json={"sku": "PL-RAW", "name": "Raw", "quantity": 10, "sell_by": "piece"}).json()["id"]
    # A user can classify an item as a component (item 3).
    comp = api.post("/items", json={"sku": "PL-COMP", "name": "Brass rod", "quantity": 50, "sell_by": "piece", "inventory_type": "component"})
    assert comp.status_code == 200, comp.text

    # Item 3: a "Component" filter tab exists, between Stocked Inventory and Service.
    # Stocked/Non-Stocked spell out "Inventory" so they aren't confused with the Component type.
    page.goto(f"{ui_server}/inventory", wait_until="domcontentloaded")
    page.wait_for_selector(".inventory-type-tabs", timeout=10000)
    tabs = [t.strip() for t in page.locator(".inventory-type-tabs .category-tab").all_inner_texts()]
    assert "Stocked Inventory" in tabs and "Non-Stocked Inventory" in tabs, tabs
    assert "Stocked" not in tabs and "Non-Stocked" not in tabs, tabs  # old bare labels gone
    assert tabs.index("Component") == tabs.index("Stocked Inventory") + 1, tabs
    page.screenshot(path=str(SHOTS / "01-components-tab.png"))
    # The filter actually filters (URL carries the var per GDR §2m): component item shown, stocked hidden.
    page.goto(f"{ui_server}/inventory?inventory_type=component", wait_until="domcontentloaded")
    page.wait_for_selector("#inventory-content", timeout=8000)
    body = page.locator("#inventory-content").inner_text()
    assert "PL-COMP" in body and "PL-RAW" not in body, body[:300]
    # Component rows are visually distinct (left accent class).
    assert page.locator("tr.data-row--component").count() >= 1

    # /manufacturing: the Demand Planning board (lead column = item + qty to make).
    gold = api.post("/items", json={"sku": "PL-MGOLD", "name": "Gold", "quantity": 50, "sell_by": "gram",
                                    "cost_total": 500, "inventory_type": "component"}).json()["id"]
    fg = api.post("/items", json={"sku": "PL-MRING", "name": "Ring", "quantity": 0, "sell_by": "piece"}).json()["id"]
    api.put(f"/manufacturing/items/{fg}/recipe",
            json={"output_qty": 1, "components": [{"item_id": gold, "quantity": 1}], "labor": [], "overhead": []})
    # Finalize the invoice so it counts as committed demand (draft/pro-forma invoices are excluded
    # from the To-Make board per the manufacturing rules).
    doc_id = api.post("/docs", json={"doc_type": "invoice", "line_items": [
        {"item_id": fg, "sku": "PL-MRING", "name": "Ring", "quantity": 3, "unit_price": 10}], "total": 30}).json()["id"]
    api.post(f"/docs/{doc_id}/finalize")
    page.goto(f"{ui_server}/manufacturing", wait_until="domcontentloaded")
    page.wait_for_selector("#mfg-table", timeout=10000)
    # New flat board: columns are Product, Document, Type, For, Due, Ordered, Short, Status.
    thead_text = page.locator("table.data-table thead").inner_text().upper()
    assert "PRODUCT" in thead_text, f"'Product' column missing from thead: {thead_text}"
    assert "STATUS" in thead_text, f"'Status' column missing from thead: {thead_text}"
    assert "PL-MRING" in page.locator("#mfg-table").inner_text()
    # No category-tabs on the restructured page; it is now two separate pages.
    assert page.locator(".category-tabs").count() == 0, "category-tabs should be gone after restructure"
    # Bulk-action toolbar and select-all are present on the Demand Planning page.
    assert page.locator(".bulkbar").count() >= 1, ".bulkbar not found on /manufacturing"
    assert page.locator(".bulk-select-all").count() >= 1, ".bulk-select-all header checkbox not found"
    # Search narrows the board by product / SKU (new placeholder text).
    box = page.get_by_placeholder("Search product / document...")
    box.fill("PL-MRING")
    page.wait_for_selector("#mfg-table:has-text('PL-MRING')", timeout=8000)
    box.fill("NO-SUCH-ITEM-123")
    page.wait_for_selector("#mfg-table:has-text('Nothing to make')", timeout=8000)

    # /lists: the same date-filter bar is present.
    page.goto(f"{ui_server}/lists", wait_until="domcontentloaded")
    page.wait_for_selector(".preset-btn", timeout=10000)

    # /docs?view=drafts&type=invoice: no redundant chip at all, and the date bar is present.
    api.post("/docs", json={"doc_type": "invoice", "line_items": [], "total": 0})  # ensure a draft exists
    page.goto(f"{ui_server}/docs?view=drafts&type=invoice&preset=all", wait_until="domcontentloaded")
    page.wait_for_selector("#doc-table, .data-table", timeout=10000)
    assert page.locator(".drafts-tab").count() == 0
    assert page.locator(".preset-btn").count() >= 7  # full date-filter bar on the drafts view too

    # Item 4: on a splittable item, the +, inputs and Go sit on one line (.split-line).
    page.goto(f"{ui_server}/inventory/{item}?tab=details", wait_until="domcontentloaded")
    page.wait_for_selector(".split-line", timeout=10000)
    line = page.locator(".split-line").first
    add = line.locator(".split-add-btn").bounding_box()
    go = line.locator("button[type=submit]").first.bounding_box()
    inp = line.locator("input[name=split_qty]").first.bounding_box()
    # + first, input in the middle, Go last — all roughly on the same row.
    assert add["x"] < inp["x"] < go["x"], (add, inp, go)
    assert abs(add["y"] - go["y"]) < 40, (add["y"], go["y"])
    page.screenshot(path=str(SHOTS / "02-split-line.png"))


def test_in_production_esc_cancels_inline_edit(page, ui_server, api):
    """Pressing ESC in the inline editor on the Work In Progress page restores the
    display chip without saving.

    Flow: create item + recipe -> build (planned) -> issue (in_progress) -> navigate to
    /manufacturing/production?status=all -> double-click Due cell -> verify editor
    appears -> press ESC -> verify editor is gone and display chip is restored.
    """
    MFG_SHOTS.mkdir(parents=True, exist_ok=True)
    page.set_viewport_size({"width": 1440, "height": 1000})

    # Seed: gold component + ring product with a recipe.
    gold = api.post("/items", json={
        "sku": "ESC-GOLD", "name": "Gold", "quantity": 100, "sell_by": "gram",
        "cost_total": 800, "inventory_type": "component",
    }).json()["id"]
    ring = api.post("/items", json={
        "sku": "ESC-RING", "name": "Ring (ESC test)", "quantity": 0, "sell_by": "piece",
    }).json()["id"]
    r = api.put(f"/manufacturing/items/{ring}/recipe",
                json={"output_qty": 1, "components": [{"item_id": gold, "quantity": 5}],
                      "labor": [], "overhead": []})
    assert r.status_code == 200, r.text

    # Build a planned run and issue it -> status becomes in_progress.
    run_id = api.post(f"/manufacturing/items/{ring}/build", json={"quantity": 2}).json()["id"]
    issue_r = api.post(f"/manufacturing/{run_id}/issue")
    assert issue_r.status_code == 200, issue_r.text
    state = api.get(f"/manufacturing/{run_id}").json()
    assert state["status"] == "in_progress", f"Expected in_progress, got: {state['status']}"

    # Navigate to Work In Progress (status=all so our run is visible).
    page.goto(f"{ui_server}/manufacturing/production?status=all",
              wait_until="domcontentloaded")
    page.wait_for_selector("#mfg-table", timeout=10000)

    # The run row should appear.
    assert page.locator("#mfg-table").inner_text().find("ESC-RING") >= 0, (
        "ESC-RING not found in the In Production table"
    )

    # Double-click the Due cell in our run row to open the inline date editor.
    due_cell = page.locator("#mfg-table .editable-cell[title='Double-click to set a due date']").first
    due_cell.dblclick()

    # The date input should now be visible inside the editable-cell.
    date_input = page.locator("#mfg-table .editable-cell input[type='date'], #mfg-table .editable-cell input.cell-input--xs")
    date_input.wait_for(state="visible", timeout=5000)
    assert date_input.count() >= 1, "Date input not rendered after double-click"

    # Press ESC - the editor should be replaced by the display chip without a POST.
    date_input.press("Escape")

    # After ESC: the date input must be gone (the display chip was swapped back in).
    page.wait_for_function(
        "document.querySelectorAll('#mfg-table input[type=\"date\"], #mfg-table input.cell-input--xs').length === 0",
        timeout=5000,
    )
    assert date_input.count() == 0, "Date input still visible after ESC - ESC cancel did not work"

    # The editable-cell div is still there (restored to its display state).
    assert due_cell.count() >= 1, "editable-cell div disappeared after ESC"

    page.screenshot(path=str(MFG_SHOTS / "in-production-esc-cancel.png"), full_page=True)


def test_manufacturing_ui_screenshots(page, ui_server, api):
    """Capture screenshots of all the changed manufacturing UIs for visual review.

    Covers: sidebar nav (Manufacturing under Inventory), /settings/manufacturing (layout with
    tooltips + narrow hours input), work centers section, /docs?type=production_order (info
    banner), /manufacturing To-Make board (units in qty columns), in_production tab.
    """
    MFG_SHOTS.mkdir(parents=True, exist_ok=True)
    page.set_viewport_size({"width": 1440, "height": 1000})

    # 1. Sidebar nav: Manufacturing group should be visible under Inventory.
    page.goto(f"{ui_server}/manufacturing", wait_until="domcontentloaded")
    page.wait_for_selector("#mfg-table", timeout=10000)
    page.screenshot(path=str(MFG_SHOTS / "sidebar-manufacturing-nav.png"), full_page=True)

    # 2. /settings/manufacturing with production rules card + narrow hours input + work centers.
    page.goto(f"{ui_server}/settings/manufacturing", wait_until="domcontentloaded")
    page.wait_for_selector("input[name=hours_per_day]", timeout=10000)
    page.screenshot(path=str(MFG_SHOTS / "settings-manufacturing-layout.png"), full_page=True)

    # 3. /docs?type=production_order - should show the info banner.
    page.goto(f"{ui_server}/docs?type=production_order", wait_until="domcontentloaded")
    page.wait_for_selector(".info-banner, #doc-table, .data-table", timeout=10000)
    page.screenshot(path=str(MFG_SHOTS / "production-order-info-banner.png"), full_page=True)
    # Verify the info banner is actually present.
    assert page.locator(".info-banner").count() >= 1, (
        ".info-banner not found on /docs?type=production_order"
    )

    # 4. To-Make board with units visible (need a finalized invoice with a manufacturable item).
    unit_gold = api.post("/items", json={
        "sku": "SS-GOLD", "name": "Silver", "quantity": 100, "sell_by": "gram",
        "cost_total": 200, "inventory_type": "component",
    }).json()["id"]
    unit_fg = api.post("/items", json={
        "sku": "SS-BANGLE", "name": "Bangle", "quantity": 0, "sell_by": "piece",
    }).json()["id"]
    api.put(f"/manufacturing/items/{unit_fg}/recipe",
            json={"output_qty": 1, "components": [{"item_id": unit_gold, "quantity": 2}],
                  "labor": [], "overhead": []})
    doc_r = api.post("/docs", json={"doc_type": "invoice", "line_items": [
        {"item_id": unit_fg, "sku": "SS-BANGLE", "name": "Bangle", "quantity": 5, "unit_price": 20},
    ], "total": 100})
    api.post(f"/docs/{doc_r.json()['id']}/finalize")

    page.goto(f"{ui_server}/manufacturing", wait_until="domcontentloaded")
    page.wait_for_selector("#mfg-table", timeout=10000)
    page.screenshot(path=str(MFG_SHOTS / "to-make-with-units.png"), full_page=True)
    # Verify units show in the To Make qty columns.
    table_text = page.locator("#mfg-table").inner_text()
    assert "SS-BANGLE" in table_text, "SS-BANGLE not in To Make board"
    # The _qty helper formats as "N unit" e.g. "5 piece"; "piece" should appear in the qty cells.
    assert "piece" in table_text.lower(), f"'piece' unit not visible in table: {table_text[:300]}"

    # 5. Work In Progress page screenshot (canonical URL).
    page.goto(f"{ui_server}/manufacturing/production?status=all",
              wait_until="domcontentloaded")
    page.wait_for_selector("#mfg-table", timeout=10000)
    page.screenshot(path=str(MFG_SHOTS / "in-production-tab.png"), full_page=True)
