# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Browser test: Demand Planning flat-board interactions and Work In Progress ESC-cancel.

Covers (Phase 1 bulk_toolbar design):
- Page renders with title "Demand Planning" and intro banner.
- One row per demand-document-line (flat, not nested); columns include Product, Document,
  Type, For, Due, Ordered, Short, Status.
- A single document-Type filter bar (status_cards, one card per type) replaces the old
  two-bar .dp-filters / .filter-label design; it is NOT .dp-filters and has no .filter-label.
- Status and For column headers have Excel-style filter funnels: .colfilter[data-col] inside
  .colfilter-th. Clicking opens a .colfilter-pop with a search box, a "(Select all)" checkbox,
  and per-value checkboxes. Unchecking a value immediately hides matching rows (.dp-row-hidden)
  WITHOUT a page reload; the funnel gets .colfilter--active; hidden rows are deselected.
- All status values including Covered are visible by default (no server-side status pre-filter).
- The new shared bulk toolbar (.bulkbar) is present with a usable .bulk-action-select
  (readable before any selection, GDR 2e); .bulk-count shows "0 selected"; .bulk-select-all
  is present in the table header.
  Only VISIBLE rows count toward the selection total.

- Make: tick a row, the toolbar enables, choose "Make selected" from the Action dropdown ->
  a production run is created (verified via the API).
- Make & complete: tick a row, accept the confirm dialog, choose "Make & complete" -> the run
  is completed and the product's stock increases.
- Print component requirements: navigate to /manufacturing/requirements?ids=<id> and assert
  the page shows "Component Requirements", "Raw materials", and the expected component SKU.
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
    """Seed products short on stock (finalize invoices) so there are Needed lines;
    exercise the bulk_toolbar structure, column filters, and verify Make via API."""
    SHOTS.mkdir(parents=True, exist_ok=True)
    page.set_viewport_size({"width": 1440, "height": 1000})

    # ── Seed raw materials + finished goods ───────────────────────────────────

    # copper: a component used in wire and make-complete recipes
    copper = api.post("/items", json={"status": "available", 
        "sku": "DP-COPPER", "name": "Copper rod", "quantity": 100,
        "sell_by": "piece", "inventory_type": "component",
    }).json()["id"]

    # wire: stock=0, demand=4 -> Needed (short). Used for Make + requirements test.
    wire = api.post("/items", json={"status": "available", 
        "sku": "DP-WIRE", "name": "Copper wire", "quantity": 0, "sell_by": "piece",
    }).json()["id"]
    r = api.put(f"/manufacturing/items/{wire}/recipe",
                json={"output_qty": 1, "components": [{"item_id": copper, "quantity": 2}],
                      "labor": [], "overhead": []})
    assert r.status_code == 200, r.text

    # bead: stock=20 >= demand=3 -> Covered.
    bead = api.post("/items", json={"status": "available", 
        "sku": "DP-BEAD", "name": "Glass bead", "quantity": 20, "sell_by": "piece",
    }).json()["id"]
    api.put(f"/manufacturing/items/{bead}/recipe",
            json={"output_qty": 1, "components": [{"item_id": copper, "quantity": 1}],
                  "labor": [], "overhead": []})

    # pendant: stock=0, demand=2 -> Needed. Used for Make & complete.
    silver = api.post("/items", json={"status": "available", 
        "sku": "DP-SILVER", "name": "Silver rod", "quantity": 100,
        "sell_by": "piece", "inventory_type": "component",
    }).json()["id"]
    pendant = api.post("/items", json={"status": "available", 
        "sku": "DP-PENDANT", "name": "Silver pendant", "quantity": 0, "sell_by": "piece",
    }).json()["id"]
    r2 = api.put(f"/manufacturing/items/{pendant}/recipe",
                 json={"output_qty": 1, "components": [{"item_id": silver, "quantity": 1}],
                       "labor": [], "overhead": []})
    assert r2.status_code == 200, r2.text

    # Finalize invoices so items count as demand.
    doc_wire = api.post("/docs", json={"doc_type": "invoice", "line_items": [
        {"item_id": wire, "sku": "DP-WIRE", "name": "Copper wire", "quantity": 4, "unit_price": 5},
    ], "total": 20}).json()["id"]
    api.post(f"/docs/{doc_wire}/finalize")

    doc_bead = api.post("/docs", json={"doc_type": "invoice", "line_items": [
        {"item_id": bead, "sku": "DP-BEAD", "name": "Glass bead", "quantity": 3, "unit_price": 2},
    ], "total": 6}).json()["id"]
    api.post(f"/docs/{doc_bead}/finalize")

    doc_pendant = api.post("/docs", json={"doc_type": "invoice", "line_items": [
        {"item_id": pendant, "sku": "DP-PENDANT", "name": "Silver pendant",
         "quantity": 2, "unit_price": 10},
    ], "total": 20}).json()["id"]
    api.post(f"/docs/{doc_pendant}/finalize")

    # Navigate to Demand Planning.
    page.goto(f"{ui_server}/manufacturing", wait_until="domcontentloaded")
    page.wait_for_selector("#mfg-table", timeout=10000)

    # ── Basic page assertions ─────────────────────────────────────────────────

    body_text = page.locator("body").inner_text()
    assert "Demand Planning" in body_text, f"'Demand Planning' not found: {body_text[:200]}"

    assert page.locator(".info-banner").count() >= 1, ".info-banner missing from /manufacturing"

    # ── Flat-board structure ──────────────────────────────────────────────────

    page.wait_for_selector("#mfg-table:has-text('DP-WIRE')", timeout=8000)

    thead_text = page.locator("#mfg-table thead").inner_text().upper()
    assert "PRODUCT" in thead_text, f"'Product' column missing: {thead_text}"
    assert "DOCUMENT" in thead_text, f"'Document' column missing: {thead_text}"
    assert "STATUS" in thead_text, f"'Status' column missing: {thead_text}"

    rows = page.locator("#mfg-table tbody tr.data-row")
    assert rows.count() >= 1, "No data-row elements in #mfg-table"

    wire_row = page.locator("#mfg-table tr.data-row:has-text('DP-WIRE')")
    assert wire_row.count() >= 1, "DP-WIRE row not found on demand board"
    wire_row_first = wire_row.first
    wire_row_text = wire_row_first.inner_text()
    assert "invoice" in wire_row_text.lower(), (
        f"Type 'Invoice' not found in DP-WIRE row: {wire_row_text!r}"
    )
    needed_badge = wire_row_first.locator(".badge--peg-short")
    assert needed_badge.count() >= 1, (
        f"'.badge--peg-short' (Needed) not found in DP-WIRE row: {wire_row_text!r}"
    )
    # The status indicator carries an explanatory tooltip (title attribute).
    assert "Needed" in (needed_badge.first.get_attribute("title") or ""), (
        "coverage badge should have a 'Needed' tooltip"
    )

    # Covered row (bead, stock >= demand) should also be visible by default.
    assert page.locator("#mfg-table tr.data-row:has-text('DP-BEAD')").count() >= 1, (
        "DP-BEAD (Covered) row should be visible by default - all statuses shown"
    )

    # ── NEW: Single Type filter bar (status_cards), NO old .dp-filters ───────

    # The old double-bar (.dp-filters) is gone.
    assert page.locator(".dp-filters").count() == 0, (
        ".dp-filters still present - old double filter bar should be removed"
    )
    assert page.locator(".filter-label").count() == 0, (
        ".filter-label still present - old bar labels should be removed"
    )

    # A single .status-cards bar with Type cards is present.
    type_bar = page.locator(".status-cards").first
    assert type_bar.count() >= 1, ".status-cards type filter bar not found"

    # The bar has cards for the known document types (text is uppercased via CSS).
    type_texts = [t.strip().upper() for t in type_bar.locator(".status-card").all_inner_texts()]
    assert any("ALL" in t for t in type_texts), f"'All' type card not found; got: {type_texts}"
    assert any("INVOICE" in t for t in type_texts), f"'Invoices' card not found; got: {type_texts}"

    # ── NEW: bulk_toolbar structure assertions ────────────────────────────────

    # .bulkbar is present.
    bulkbar = page.locator(".bulkbar")
    assert bulkbar.count() >= 1, ".bulkbar not found on /manufacturing"

    # .bulk-action-select is present and usable before any selection, so its options can be read (GDR 2e).
    action_select = page.locator(".bulk-action-select")
    assert action_select.count() >= 1, ".bulk-action-select not found in bulk toolbar"
    assert not action_select.is_disabled(), ".bulk-action-select should stay usable so its options are discoverable before selecting"

    # .bulk-count shows "0 selected" initially.
    count_el = page.locator(".bulk-count")
    assert count_el.count() >= 1, ".bulk-count span not found in bulk toolbar"
    assert "0" in count_el.inner_text(), (
        f"Expected '0 selected' in .bulk-count, got: {count_el.inner_text()!r}"
    )

    # .bulk-select-all is the header select-all checkbox in the table thead.
    assert page.locator(".bulk-select-all").count() >= 1, ".bulk-select-all header checkbox not found"

    # Each data row has a .bulk-select checkbox (not .dp-select).
    wire_row_cb = wire_row_first.locator(".bulk-select")
    assert wire_row_cb.count() >= 1, (
        ".bulk-select checkbox not found on DP-WIRE row (old .dp-select selector gone)"
    )

    # The Action dropdown has the three expected options.
    opt_values = action_select.locator("option:not([disabled])").all_inner_texts()
    assert any("Make selected" in v for v in opt_values), (
        f"'Make selected' option not found; got: {opt_values}"
    )
    assert any("Make & complete" in v for v in opt_values), (
        f"'Make & complete' option not found; got: {opt_values}"
    )
    assert any("Print component requirements" in v for v in opt_values), (
        f"'Print component requirements' option not found; got: {opt_values}"
    )

    # Screenshot: demand board with Action dropdown visible (toolbar + seeded rows).
    page.screenshot(path=str(SHOTS / "demand-planning-type-filter-bar.png"), full_page=True)

    # ── NEW: Type card click reloads with ?type=... (server-side filter) ──────

    invoices_card = type_bar.locator(".status-card:has-text('Invoice')").first
    assert invoices_card.count() >= 1, "Invoices type card not found"
    invoices_card.click()
    page.wait_for_url("**/manufacturing?type=invoice", timeout=8000)
    page.wait_for_selector("#mfg-table", timeout=8000)
    # After clicking Invoices, the board still shows invoice-type rows.
    assert page.locator("#mfg-table tr.data-row:has-text('DP-WIRE')").count() >= 1, (
        "DP-WIRE row should still show under Invoices filter"
    )

    # Return to All.
    page.goto(f"{ui_server}/manufacturing", wait_until="domcontentloaded")
    page.wait_for_selector("#mfg-table:has-text('DP-WIRE')", timeout=8000)

    # ── NEW: Excel-style column filter funnels ────────────────────────────────

    # Both .colfilter buttons are present (one for "For", one for "Status").
    funnels = page.locator("#mfg-table .colfilter[data-col]")
    assert funnels.count() >= 2, (
        f"Expected at least 2 .colfilter funnel buttons in thead; found {funnels.count()}"
    )

    # .colfilter-th wrappers present.
    assert page.locator("#mfg-table .colfilter-th").count() >= 2, (
        ".colfilter-th wrappers not found"
    )

    # No popup visible yet.
    assert page.locator(".colfilter-pop").count() == 0, ".colfilter-pop should not be open initially"

    # Click the Status funnel to open its popup.
    status_funnel = page.locator("#mfg-table .colfilter[data-col='8']")
    assert status_funnel.count() >= 1, "Status colfilter (data-col=8) not found"
    status_funnel.click()

    # Popup should appear.
    popup = page.locator(".colfilter-pop")
    popup.wait_for(state="visible", timeout=5000)
    assert popup.count() >= 1, ".colfilter-pop did not open after clicking Status funnel"

    # Popup has a search box, a (Select all) checkbox, and per-value checkboxes.
    assert popup.locator(".colfilter-search").count() >= 1, "No search box in colfilter popup"
    assert popup.locator(".colfilter-all").count() >= 1, "No '(Select all)' item in popup"

    # There should be checkbox items for "Needed" and "Covered" (our two seeded statuses).
    popup_items = popup.locator(".colfilter-item:not(.colfilter-all)")
    item_texts = [t.strip() for t in popup_items.all_inner_texts()]
    assert any("Needed" in t for t in item_texts), f"'Needed' not in popup items: {item_texts}"
    assert any("Covered" in t for t in item_texts), f"'Covered' not in popup items: {item_texts}"

    # Screenshot: funnel popup open showing Needed + Covered checkboxes.
    page.screenshot(path=str(SHOTS / "demand-planning-funnel-popup-open.png"), full_page=True)

    # Uncheck "Covered" by clicking the checkbox next to it.
    covered_label = popup.locator(".colfilter-item:not(.colfilter-all)").filter(has_text="Covered")
    covered_cb = covered_label.locator("input[type=checkbox]")
    assert covered_cb.is_checked(), "Covered checkbox should be checked by default (all shown)"
    covered_cb.uncheck()

    # Client-side filter applies instantly (no page reload).
    # The Covered row (DP-BEAD) should now have .dp-row-hidden.
    page.wait_for_function(
        """() => {
          var rows = document.querySelectorAll('#mfg-table tbody tr.data-row');
          for (var i = 0; i < rows.length; i++) {
            if (rows[i].textContent.indexOf('DP-BEAD') >= 0)
              return rows[i].classList.contains('dp-row-hidden');
          }
          return false;
        }""",
        timeout=3000,
    )
    bead_row = page.locator("#mfg-table tr.data-row:has-text('DP-BEAD')")
    assert bead_row.count() >= 1, "DP-BEAD row should be in DOM (just hidden)"
    assert "dp-row-hidden" in (bead_row.first.get_attribute("class") or ""), (
        "DP-BEAD row does not have .dp-row-hidden after unchecking Covered"
    )

    # The Needed row (DP-WIRE) is still visible (not hidden).
    wire_hidden = page.locator("#mfg-table tr.data-row:has-text('DP-WIRE')")
    assert wire_hidden.count() >= 1
    assert "dp-row-hidden" not in (wire_hidden.first.get_attribute("class") or ""), (
        "DP-WIRE (Needed) should still be visible"
    )

    # The Status funnel should now have .colfilter--active.
    assert "colfilter--active" in (status_funnel.get_attribute("class") or ""), (
        "Status funnel should have .colfilter--active after applying a filter"
    )

    # Screenshot: board after unchecking Covered - bead row hidden.
    page.screenshot(path=str(SHOTS / "demand-planning-covered-hidden.png"), full_page=True)

    # Close the popup by pressing Escape.
    page.keyboard.press("Escape")
    page.wait_for_function(
        "document.querySelectorAll('.colfilter-pop').length === 0",
        timeout=3000,
    )

    # ── 2. Print component requirements page (BEFORE any Make runs) ──────────
    # Navigate directly to the requirements page for DP-WIRE (shortfall exists now).
    # Uses copper at 2/unit; DP-WIRE has demand=4, stock=0, so to_make=4, raw=8 copper.
    page.goto(f"{ui_server}/manufacturing/requirements?ids={wire}", wait_until="domcontentloaded")
    page.wait_for_selector("body", timeout=8000)

    req_body = page.locator("body").inner_text()
    assert "Component Requirements" in req_body, (
        f"'Component Requirements' heading not found: {req_body[:300]}"
    )
    assert "Raw materials" in req_body, (
        f"'Raw materials' section not found: {req_body[:400]}"
    )
    # DP-COPPER (the component) should be listed.
    assert "DP-COPPER" in req_body or "Copper rod" in req_body, (
        f"Expected DP-COPPER or 'Copper rod' in requirements page: {req_body[:500]}"
    )

    # Screenshot: the /manufacturing/requirements print page.
    page.screenshot(path=str(SHOTS / "manufacturing-requirements.png"), full_page=True)

    # ── 3. Make via the bulk toolbar -> verify a run is created ────────────────
    # Drive the real UI: tick the row, confirm the count updates, choose the action.
    page.goto(f"{ui_server}/manufacturing", wait_until="domcontentloaded")
    page.wait_for_selector("#mfg-table:has-text('DP-WIRE')", timeout=8000)
    assert not page.locator(".bulk-action-select").is_disabled(), "action select must stay usable so options are discoverable"
    page.locator("#mfg-table tr.data-row:has-text('DP-WIRE') .bulk-select").first.check()
    page.wait_for_function("/1 selected/.test((document.querySelector('.bulk-count')||{}).textContent||'')", timeout=3000)
    assert "1 selected" in page.locator(".bulk-count").inner_text()
    page.screenshot(path=str(SHOTS / "demand-planning-selected.png"), full_page=True)

    page.locator(".bulk-action-select").select_option(value="make")

    def _has(predicate) -> bool:
        for _ in range(24):
            if any(predicate(o) for o in api.get("/manufacturing").json()["items"]):
                return True
            time.sleep(0.25)
        return False

    assert _has(lambda o: o.get("output_item_id") == wire), (
        "No production run created for DP-WIRE via the 'Make selected' toolbar action"
    )
    page.wait_for_selector("#mfg-table", timeout=8000)
    page.screenshot(path=str(SHOTS / "demand-planning-after-make.png"), full_page=True)

    # ── 4. Make & complete via the toolbar (accept the confirm dialog) ─────────
    page.goto(f"{ui_server}/manufacturing", wait_until="domcontentloaded")
    page.wait_for_selector("#mfg-table:has-text('DP-PENDANT')", timeout=8000)
    page.on("dialog", lambda d: d.accept())
    page.locator("#mfg-table tr.data-row:has-text('DP-PENDANT') .bulk-select").first.check()
    page.wait_for_function("/1 selected/.test((document.querySelector('.bulk-count')||{}).textContent||'')", timeout=3000)
    page.locator(".bulk-action-select").select_option(value="make_complete")

    assert _has(lambda o: o.get("output_item_id") == pendant and o.get("status") == "completed"), (
        "No completed run for DP-PENDANT via the 'Make & complete' toolbar action"
    )
    assert float(api.get(f"/items/{pendant}").json().get("quantity", 0)) > 0, (
        "DP-PENDANT stock should be > 0 after Make & complete"
    )


def test_wip_esc_cancel_inline_edit(page, ui_server, api):
    """ESC cancels the inline Due editor on /manufacturing/production without saving."""
    SHOTS.mkdir(parents=True, exist_ok=True)
    page.set_viewport_size({"width": 1440, "height": 1000})

    tin = api.post("/items", json={"status": "available", 
        "sku": "WIP-TIN", "name": "Tin", "quantity": 50,
        "sell_by": "piece", "inventory_type": "component",
    }).json()["id"]
    can = api.post("/items", json={"status": "available", 
        "sku": "WIP-CAN", "name": "Tin can", "quantity": 0, "sell_by": "piece",
    }).json()["id"]
    r = api.put(f"/manufacturing/items/{can}/recipe",
                json={"output_qty": 1, "components": [{"item_id": tin, "quantity": 1}],
                      "labor": [], "overhead": []})
    assert r.status_code == 200, r.text

    run_id = api.post(f"/manufacturing/items/{can}/build", json={"quantity": 1}).json()["id"]
    issue_r = api.post(f"/manufacturing/{run_id}/issue")
    assert issue_r.status_code in (200, 204), issue_r.text

    page.goto(f"{ui_server}/manufacturing/production", wait_until="domcontentloaded")
    page.wait_for_selector("#mfg-table", timeout=10000)

    assert "Work In Progress" in page.locator("body").inner_text()
    assert page.locator(".info-banner").count() >= 1
    assert page.locator(".status-cards").count() >= 1, "No status-cards found on WIP page"

    assert "WIP-CAN" in page.locator("#mfg-table").inner_text(), \
        "WIP-CAN run not found in the Work In Progress table"

    due_cell = page.locator(
        "#mfg-table .editable-cell[title='Double-click to set a due date']"
    ).first
    due_cell.dblclick()

    date_input = page.locator(
        "#mfg-table .editable-cell input[type='date'], #mfg-table .editable-cell input.cell-input--xs"
    )
    date_input.wait_for(state="visible", timeout=5000)
    assert date_input.count() >= 1, "Date input not rendered after double-click"

    date_input.press("Escape")

    page.wait_for_function(
        "document.querySelectorAll('#mfg-table input[type=\"date\"], #mfg-table input.cell-input--xs').length === 0",
        timeout=5000,
    )
    assert date_input.count() == 0, "Date input still visible after ESC"

    page.screenshot(path=str(SHOTS / "wip-esc-cancel.png"), full_page=True)


def test_demand_planning_screenshots(page, ui_server, api):
    """Capture full-page screenshots for visual review.

    Covers: Demand Planning flat board (Type filter bar + funnel icons + demand lines),
    Work In Progress with status cards, and /manufacturing/production sidebar nav.
    """
    SHOTS.mkdir(parents=True, exist_ok=True)
    page.set_viewport_size({"width": 1440, "height": 1000})

    # Screenshot 1: Demand Planning flat board.
    page.goto(f"{ui_server}/manufacturing", wait_until="domcontentloaded")
    page.wait_for_selector("#mfg-table", timeout=10000)
    page.screenshot(path=str(SHOTS / "dp-demand-planning.png"), full_page=True)

    # Screenshot 2: Work In Progress (banner, status cards with Active default).
    page.goto(f"{ui_server}/manufacturing/production", wait_until="domcontentloaded")
    page.wait_for_selector("#mfg-table", timeout=10000)
    page.screenshot(path=str(SHOTS / "dp-work-in-progress.png"), full_page=True)

    assert page.locator(".info-banner").count() >= 1, ".info-banner missing from /manufacturing/production"

    # Screenshot 3: WIP page sidebar - verify correct nav item is active.
    sidebar_active = page.locator(".sidebar .nav-link--active").first
    active_text = sidebar_active.inner_text().strip()
    assert active_text == "Work In Progress", (
        f"Sidebar active nav item should be 'Work In Progress' on /manufacturing/production; "
        f"got: {active_text!r}"
    )
    page.screenshot(path=str(SHOTS / "wip-sidebar-active-nav.png"), full_page=True)
