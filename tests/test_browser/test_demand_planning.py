# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Browser test: Demand Planning page interactions and Work In Progress ESC-cancel.

Covers:
- Checkbox enables the Make-selected button and updates the count badge.
- Expand arrow lazy-loads the pegging drill-down with a coverage badge.
- "Make selected" creates a production run; the row drops from the board.
- ESC cancels the inline Due editor on /manufacturing/production.
- Full-page screenshots for the visual review.
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.browser

SHOTS = Path("context/reviews/manufacturing")


def test_demand_planning_interactions(page, ui_server, api):
    """Seed a manufacturable product short on stock; exercise checkbox, expand, and Make."""
    SHOTS.mkdir(parents=True, exist_ok=True)
    page.set_viewport_size({"width": 1440, "height": 1000})

    # Seed: raw material + finished good with a recipe.
    copper = api.post("/items", json={
        "sku": "DP-COPPER", "name": "Copper rod", "quantity": 100,
        "sell_by": "piece", "inventory_type": "component",
    }).json()["id"]
    wire = api.post("/items", json={
        "sku": "DP-WIRE", "name": "Copper wire", "quantity": 0, "sell_by": "piece",
    }).json()["id"]
    r = api.put(f"/manufacturing/items/{wire}/recipe",
                json={"output_qty": 1, "components": [{"item_id": copper, "quantity": 2}],
                      "labor": [], "overhead": []})
    assert r.status_code == 200, r.text

    # Finalize an invoice so it counts as demand (draft invoices are excluded).
    doc = api.post("/docs", json={"doc_type": "invoice", "line_items": [
        {"item_id": wire, "sku": "DP-WIRE", "name": "Copper wire", "quantity": 4, "unit_price": 5},
    ], "total": 20}).json()["id"]
    fin = api.post(f"/docs/{doc}/finalize")
    assert fin.status_code in (200, 204), fin.text

    # Navigate to Demand Planning.
    page.goto(f"{ui_server}/manufacturing", wait_until="domcontentloaded")
    page.wait_for_selector("#mfg-table", timeout=10000)

    # The page title should be "Demand Planning".
    body_text = page.locator("body").inner_text()
    assert "Demand Planning" in body_text, f"'Demand Planning' not found in page: {body_text[:200]}"

    # Intro banner is present.
    assert page.locator(".info-banner").count() >= 1, ".info-banner missing from /manufacturing"

    # The wire row should be visible (4 demanded, 0 on hand -> shortfall).
    page.wait_for_selector("#mfg-table:has-text('DP-WIRE')", timeout=8000)

    # ── 1. Checkbox interaction ───────────────────────────────────────────────

    # Make button should be disabled initially (no selection).
    make_btn = page.locator("#dp-make-btn")
    assert make_btn.is_disabled(), "#dp-make-btn should be disabled with no selection"

    # Count badge shows "0 selected".
    count_el = page.locator("#dp-count")
    assert "0" in count_el.inner_text(), f"Expected '0 selected', got: {count_el.inner_text()}"

    # Tick the DP-WIRE row checkbox.
    wire_row = page.locator("#mfg-table .dp-select[value]").filter(has_text="").first
    # Find the checkbox for DP-WIRE by locating the row containing its SKU.
    wire_cb = page.locator(
        f"#mfg-table tr.data-row:has-text('DP-WIRE') .dp-select"
    ).first
    wire_cb.check()

    # The Make button should now be enabled and the count should show 1.
    page.wait_for_function(
        "!document.getElementById('dp-make-btn').disabled",
        timeout=5000,
    )
    assert not make_btn.is_disabled(), "#dp-make-btn should be enabled after ticking a row"
    count_text = count_el.inner_text()
    assert "1" in count_text, f"Expected '1 selected' in count badge, got: {count_text!r}"

    # Screenshot: demand planning with a row selected.
    page.screenshot(path=str(SHOTS / "demand-planning-selected.png"), full_page=True)

    # ── 2. Pegging drill-down (expand) ───────────────────────────────────────

    # The DP-WIRE row should have a .dp-expand button (docs exist because we created an invoice).
    expand_btn = page.locator(
        f"#mfg-table tr.data-row:has-text('DP-WIRE') .dp-expand"
    ).first
    assert expand_btn.count() == 1, ".dp-expand button not found on DP-WIRE row"

    expand_btn.click()

    # Wait for the nested drill-down table to appear inside the detail row.
    # The expand fills the #dp-docs-{safe_id} div in the sibling row.
    page.wait_for_selector(
        "#mfg-table .dp-docs-row:not([hidden]) .data-table--nested",
        timeout=8000,
    )

    # The nested table must have a coverage badge (Covered / Partial / Short) — case-insensitive.
    nested = page.locator("#mfg-table .data-table--nested")
    nested_text = nested.inner_text().upper()
    assert any(word in nested_text for word in ("COVERED", "PARTIAL", "SHORT")), (
        f"No coverage badge found in drill-down table: {nested.inner_text()[:300]}"
    )

    # Screenshot: demand planning with drill-down expanded.
    page.screenshot(path=str(SHOTS / "demand-planning-expanded.png"), full_page=True)

    # ── 3. Make selected ─────────────────────────────────────────────────────

    # The checkbox is still ticked; click "Make selected".
    make_btn.click()

    # Wait for the HTMX POST to complete. The response swaps outerHTML of #mfg-table,
    # and also fires a celerpToast HX-Trigger. Wait for the toast OR for the table to
    # reload (whichever is more reliable). We wait for a state that can only exist
    # post-swap: either DP-WIRE row is gone (now covered) or the table reloaded without
    # the selection state.
    # Give the POST up to 10s to complete.
    import time as _time
    deadline = _time.time() + 10
    while _time.time() < deadline:
        runs = api.get("/manufacturing").json()["items"]
        wire_runs = [o for o in runs if o.get("output_item_id") == wire]
        if wire_runs:
            break
        _time.sleep(0.5)

    assert len(wire_runs) >= 1, (
        f"No run found for DP-WIRE after Make selected. All runs: {runs}"
    )

    # The board should no longer show DP-WIRE in a short state (it is now covered by in-progress).
    # It may still appear if "show covered" is on, but the default view hides covered rows.
    # Check: either DP-WIRE is gone from the table, or the to_make API value is 0.
    # Poll briefly to allow the UI swap to complete.
    _time.sleep(0.5)
    api_rows = {r["item_id"]: r for r in api.get("/manufacturing/to-make").json()["items"]}
    if wire in api_rows:
        assert float(api_rows[wire].get("to_make", 0)) == 0.0, (
            f"DP-WIRE still shows non-zero to_make after Make selected: {api_rows[wire]}"
        )

    # Final screenshot: board after make.
    page.wait_for_selector("#mfg-table", timeout=5000)
    page.screenshot(path=str(SHOTS / "demand-planning-after-make.png"), full_page=True)


def test_wip_esc_cancel_inline_edit(page, ui_server, api):
    """ESC cancels the inline Due editor on /manufacturing/production without saving."""
    SHOTS.mkdir(parents=True, exist_ok=True)
    page.set_viewport_size({"width": 1440, "height": 1000})

    # Seed a run that will appear on the WIP page.
    tin = api.post("/items", json={
        "sku": "WIP-TIN", "name": "Tin", "quantity": 50,
        "sell_by": "piece", "inventory_type": "component",
    }).json()["id"]
    can = api.post("/items", json={
        "sku": "WIP-CAN", "name": "Tin can", "quantity": 0, "sell_by": "piece",
    }).json()["id"]
    r = api.put(f"/manufacturing/items/{can}/recipe",
                json={"output_qty": 1, "components": [{"item_id": tin, "quantity": 1}],
                      "labor": [], "overhead": []})
    assert r.status_code == 200, r.text

    run_id = api.post(f"/manufacturing/items/{can}/build", json={"quantity": 1}).json()["id"]
    issue_r = api.post(f"/manufacturing/{run_id}/issue")
    assert issue_r.status_code in (200, 204), issue_r.text

    # Navigate to Work In Progress (status=all to ensure the run is visible).
    page.goto(f"{ui_server}/manufacturing/production?status=all", wait_until="domcontentloaded")
    page.wait_for_selector("#mfg-table", timeout=10000)

    # Page title is "Work In Progress".
    assert "Work In Progress" in page.locator("body").inner_text()

    # Intro banner present.
    assert page.locator(".info-banner").count() >= 1

    # Status cards present; "Active" should be the default active card.
    assert page.locator(".status-cards, .filter-cards").count() >= 1 or \
        page.locator("[class*='card']").count() >= 1, "No status cards found"

    # WIP-CAN should appear.
    assert "WIP-CAN" in page.locator("#mfg-table").inner_text(), \
        "WIP-CAN run not found in the Work In Progress table"

    # Double-click the Due cell to open the inline editor.
    due_cell = page.locator(
        "#mfg-table .editable-cell[title='Double-click to set a due date']"
    ).first
    due_cell.dblclick()

    date_input = page.locator(
        "#mfg-table .editable-cell input[type='date'], #mfg-table .editable-cell input.cell-input--xs"
    )
    date_input.wait_for(state="visible", timeout=5000)
    assert date_input.count() >= 1, "Date input not rendered after double-click"

    # Press ESC - editor should close without saving.
    date_input.press("Escape")

    page.wait_for_function(
        "document.querySelectorAll('#mfg-table input[type=\"date\"], #mfg-table input.cell-input--xs').length === 0",
        timeout=5000,
    )
    assert date_input.count() == 0, "Date input still visible after ESC"

    page.screenshot(path=str(SHOTS / "wip-esc-cancel.png"), full_page=True)


def test_demand_planning_screenshots(page, ui_server, api):
    """Capture full-page screenshots for visual review.

    Covers: sidebar Manufacturing group, Demand Planning intro/checkboxes/bulk-bar,
    Work In Progress with status cards.
    """
    SHOTS.mkdir(parents=True, exist_ok=True)
    page.set_viewport_size({"width": 1440, "height": 1000})

    # Screenshot 1: sidebar Manufacturing nav (Demand Planning / Work In Progress / Production Orders).
    page.goto(f"{ui_server}/manufacturing", wait_until="domcontentloaded")
    page.wait_for_selector("#mfg-table", timeout=10000)
    page.screenshot(path=str(SHOTS / "dp-sidebar-nav.png"), full_page=True)

    # Screenshot 2: Demand Planning page (intro banner, checkboxes, bulk-action bar).
    # The page is already loaded; take the shot.
    page.screenshot(path=str(SHOTS / "dp-demand-planning.png"), full_page=True)

    # Screenshot 3: Work In Progress (intro banner, status cards with Active default).
    page.goto(f"{ui_server}/manufacturing/production", wait_until="domcontentloaded")
    page.wait_for_selector("#mfg-table", timeout=10000)
    page.screenshot(path=str(SHOTS / "dp-work-in-progress.png"), full_page=True)

    # Verify WIP page has the intro banner.
    assert page.locator(".info-banner").count() >= 1, ".info-banner missing from /manufacturing/production"
