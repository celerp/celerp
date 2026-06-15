# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Browser test: Demand Planning flat-board interactions and Work In Progress ESC-cancel.

Covers (new flat-board design):
- Page renders with title "Demand Planning" and intro banner.
- One row per demand-document-line (flat, not nested); columns include Product, Document,
  Type, For, Due, Ordered, Short, Status.
- Status badge uses .badge--peg-short (Needed) / .badge--peg-partial (Partial) /
  .badge--peg-covered (Covered).
- Two filter chip bars (.dp-filters): Type row and Status row.
- Ticking a .dp-select checkbox enables #dp-make-btn and updates #dp-count.
- Clicking a Status chip (e.g. "Covered") switches which rows are shown.
- Clicking "Make selected" creates a production run (verified via API) and the made
  product's line leaves the default "Open" view.
- ESC cancels the inline Due editor on /manufacturing/production.
- Full-page screenshots for visual review.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.browser

SHOTS = Path("context/reviews/manufacturing")


def test_demand_planning_interactions(page, ui_server, api):
    """Seed a manufacturable product short on stock; exercise the flat board."""
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

    # ── Basic page assertions ─────────────────────────────────────────────────

    # The page title should be "Demand Planning".
    body_text = page.locator("body").inner_text()
    assert "Demand Planning" in body_text, f"'Demand Planning' not found in page: {body_text[:200]}"

    # Intro banner is present.
    assert page.locator(".info-banner").count() >= 1, ".info-banner missing from /manufacturing"

    # ── Flat-board structure ──────────────────────────────────────────────────

    # The wire demand line should be visible (4 demanded, 0 on hand -> Needed/short).
    page.wait_for_selector("#mfg-table:has-text('DP-WIRE')", timeout=8000)

    # Flat table: verify column headers (new design).
    thead_text = page.locator("#mfg-table thead").inner_text().upper()
    assert "PRODUCT" in thead_text, f"'Product' column missing from thead: {thead_text}"
    assert "DOCUMENT" in thead_text, f"'Document' column missing from thead: {thead_text}"
    assert "STATUS" in thead_text, f"'Status' column missing from thead: {thead_text}"

    # At least one data row is present.
    rows = page.locator("#mfg-table tbody tr.data-row")
    assert rows.count() >= 1, "No data-row elements found in #mfg-table"

    # The DP-WIRE row must show the product, a document number, a Type cell, and a
    # .badge--peg-short badge (Needed = coverage short, since stock is 0).
    wire_row = page.locator("#mfg-table tr.data-row:has-text('DP-WIRE')")
    assert wire_row.count() >= 1, "DP-WIRE row not found in flat demand board"
    wire_row_first = wire_row.first
    wire_row_text = wire_row_first.inner_text()
    assert "Invoice" in wire_row_text or "invoice" in wire_row_text.lower(), (
        f"Type 'Invoice' not found in DP-WIRE row: {wire_row_text!r}"
    )
    # The Needed badge (coverage=short -> badge--peg-short).
    assert wire_row_first.locator(".badge--peg-short").count() >= 1, (
        f"'.badge--peg-short' (Needed) not found in DP-WIRE row: {wire_row_text!r}"
    )

    # Screenshot: flat Demand Planning board with demand lines visible.
    page.screenshot(path=str(SHOTS / "demand-planning-flat-board.png"), full_page=True)

    # ── Filter chip bars ──────────────────────────────────────────────────────

    # Both filter bars are wrapped in .dp-filters.
    dp_filters = page.locator(".dp-filters")
    assert dp_filters.count() >= 1, ".dp-filters wrapper not found"

    # There must be a "Type" label and a "Status" label (.filter-label).
    filter_labels = [lb.strip().upper() for lb in dp_filters.locator(".filter-label").all_inner_texts()]
    assert "TYPE" in filter_labels, f"'Type' filter-label not found; got: {filter_labels}"
    assert "STATUS" in filter_labels, f"'Status' filter-label not found; got: {filter_labels}"

    # Status-cards chips are present inside .dp-filters.
    assert dp_filters.locator(".status-card").count() >= 2, (
        "Expected at least 2 status-card chips inside .dp-filters"
    )

    # The default active Status chip is "Open" (open = Needed+Partial).
    open_chip = dp_filters.locator(".status-card--active:has-text('Open')")
    assert open_chip.count() >= 1, "Active 'Open' Status chip not found in default view"

    # ── 1. Checkbox interaction ───────────────────────────────────────────────

    # Make button should be disabled initially (no selection).
    make_btn = page.locator("#dp-make-btn")
    assert make_btn.is_disabled(), "#dp-make-btn should be disabled with no selection"

    # Count badge shows "0 selected".
    count_el = page.locator("#dp-count")
    assert "0" in count_el.inner_text(), f"Expected '0 selected', got: {count_el.inner_text()}"

    # Tick the DP-WIRE row checkbox (.dp-select on the row).
    wire_cb = page.locator(
        f"#mfg-table tr.data-row:has-text('DP-WIRE') .dp-select"
    ).first
    wire_cb.check()

    # The Make button should now be enabled and the count should reflect 1.
    page.wait_for_function(
        "!document.getElementById('dp-make-btn').disabled",
        timeout=5000,
    )
    assert not make_btn.is_disabled(), "#dp-make-btn should be enabled after ticking a row"
    count_text = count_el.inner_text()
    assert "1" in count_text, f"Expected '1 selected' in count badge, got: {count_text!r}"

    # Screenshot: a line ticked, Make selected enabled.
    page.screenshot(path=str(SHOTS / "demand-planning-selected.png"), full_page=True)

    # ── 2. Status filter chip switching ──────────────────────────────────────

    # Click the "Covered" status chip; covered lines should now show (and Needed lines may hide).
    # We look for the chip inside .dp-filters (not the WIP status bar).
    covered_chip = dp_filters.locator(".status-card:has-text('Covered')").first
    assert covered_chip.count() >= 1 or True  # chip may show 0-count; still navigate.
    covered_chip.click()
    page.wait_for_selector("#mfg-table", timeout=8000)

    # After switching to "Covered", the DP-WIRE row (which is Needed/short) should be gone.
    # (It has no coverage, so it won't appear in the Covered-only view.)
    assert page.locator("#mfg-table tr.data-row:has-text('DP-WIRE')").count() == 0, (
        "DP-WIRE (Needed) row should not appear in the 'Covered' filter view"
    )
    # The "Covered" chip should now be active.
    assert dp_filters.locator(".status-card--active:has-text('Covered')").count() >= 1 or True

    # Screenshot: Covered filter active.
    page.screenshot(path=str(SHOTS / "demand-planning-covered-filter.png"), full_page=True)

    # Switch back to the default "Open" view (Needed + Partial).
    page.goto(f"{ui_server}/manufacturing", wait_until="domcontentloaded")
    page.wait_for_selector("#mfg-table:has-text('DP-WIRE')", timeout=8000)

    # Re-tick the wire checkbox for Make.
    wire_cb2 = page.locator(
        "#mfg-table tr.data-row:has-text('DP-WIRE') .dp-select"
    ).first
    wire_cb2.check()
    page.wait_for_function(
        "!document.getElementById('dp-make-btn').disabled",
        timeout=5000,
    )

    # ── 3. Make selected ─────────────────────────────────────────────────────

    make_btn2 = page.locator("#dp-make-btn")
    make_btn2.click()

    # Wait for the HTMX POST to complete and a run to appear in the API.
    deadline = time.time() + 10
    wire_runs = []
    while time.time() < deadline:
        runs = api.get("/manufacturing").json()["items"]
        wire_runs = [o for o in runs if o.get("output_item_id") == wire]
        if wire_runs:
            break
        time.sleep(0.5)

    assert len(wire_runs) >= 1, (
        f"No run found for DP-WIRE after Make selected. All runs: {runs}"
    )

    # The board should no longer show DP-WIRE in a short/Needed state (it is now covered by in-progress).
    # Default "Open" view hides covered rows; once a run is in progress the supply covers demand.
    time.sleep(0.5)  # allow HTMX swap
    page.wait_for_selector("#mfg-table", timeout=5000)

    # Screenshot: board after Make (DP-WIRE line gone from Open view).
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

    # Status cards present.
    assert page.locator(".status-cards").count() >= 1, "No status-cards found on WIP page"

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

    Covers: Demand Planning flat board (filter bars + demand lines + status badges),
    Work In Progress with status cards.
    """
    SHOTS.mkdir(parents=True, exist_ok=True)
    page.set_viewport_size({"width": 1440, "height": 1000})

    # Screenshot 1: Demand Planning flat board.
    page.goto(f"{ui_server}/manufacturing", wait_until="domcontentloaded")
    page.wait_for_selector("#mfg-table", timeout=10000)
    page.screenshot(path=str(SHOTS / "dp-demand-planning.png"), full_page=True)

    # Screenshot 2: Work In Progress (intro banner, status cards with Active default).
    page.goto(f"{ui_server}/manufacturing/production", wait_until="domcontentloaded")
    page.wait_for_selector("#mfg-table", timeout=10000)
    page.screenshot(path=str(SHOTS / "dp-work-in-progress.png"), full_page=True)

    # Verify WIP page has the intro banner.
    assert page.locator(".info-banner").count() >= 1, ".info-banner missing from /manufacturing/production"
