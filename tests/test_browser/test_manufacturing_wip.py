# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Browser test: Work In Progress (/manufacturing/production) Phase-2 bulk toolbar + column funnels.

Drives the REAL UI (no API bypass for the action under test):
- The shared bulk toolbar (.bulkbar / .bulk-action-select / .bulk-count) is present; the Action
  select is disabled with nothing ticked.
- Ticking two run rows' .bulk-select enables the select and shows "2 selected".
- Choosing "Start" from the Action dropdown (select_option) dispatches the bulk action; both runs
  move to in_progress (verified by polling the backend list — effect verification, not the trigger).
- Re-ticking, accepting the confirm dialog, and choosing "Complete" closes both runs and the
  product's stock increases.
- The Excel column funnel on the Priority header (data-col 3) filters live: unchecking a value
  hides matching rows (.dp-row-hidden) without a reload and marks the funnel .colfilter--active.

Runs are SEEDED via the build API (setup, not the action under test).
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.browser

SHOTS = Path("context/reviews/manufacturing")


def _poll(api, predicate, tries: int = 80, delay: float = 0.25) -> bool:
    """Poll the backend run list until `predicate(items)` is true. The default window (20s) is
    generous on purpose: in the full suite the shared event store is large, so a bulk Complete
    (issue components -> receive output -> close run, each recomputing projections) settles more
    slowly than it does in isolation."""
    for _ in range(tries):
        items = api.get("/manufacturing").json()["items"]
        if predicate(items):
            return True
        time.sleep(delay)
    return False


def test_wip_bulk_actions_and_priority_funnel(page, ui_server, api):
    SHOTS.mkdir(parents=True, exist_ok=True)
    page.set_viewport_size({"width": 1440, "height": 1000})

    # ── Seed a manufacturable product with a recipe, then build two PLANNED runs ──
    steel = api.post("/items", json={
        "sku": "WIP-STEEL", "name": "Steel bar", "quantity": 1000,
        "sell_by": "piece", "inventory_type": "component",
    }).json()["id"]
    widget = api.post("/items", json={
        "sku": "WIP-WIDGET", "name": "Steel widget", "quantity": 0, "sell_by": "piece",
    }).json()["id"]
    r = api.put(f"/manufacturing/items/{widget}/recipe",
                json={"output_qty": 1, "components": [{"item_id": steel, "quantity": 2}],
                      "labor": [], "overhead": []})
    assert r.status_code == 200, r.text

    run_a = api.post(f"/manufacturing/items/{widget}/build", json={"quantity": 3}).json()["id"]
    run_b = api.post(f"/manufacturing/items/{widget}/build", json={"quantity": 5}).json()["id"]
    # Give the two runs distinct priorities so the Priority funnel has >1 value to filter on
    # (this is data setup; the funnel itself is exercised through the UI below).
    assert api.post(f"/manufacturing/{run_a}/schedule", json={"priority": "high"}).status_code in (200, 204)
    assert api.post(f"/manufacturing/{run_b}/schedule", json={"priority": "low"}).status_code in (200, 204)

    both = {run_a, run_b}

    # Both runs are PLANNED to start with.
    assert _poll(api, lambda items: all(
        o.get("status") == "planned" for o in items if o.get("id") in both
    ) and len([o for o in items if o.get("id") in both]) == 2), "both runs should start PLANNED"

    # ── WIP page: bulk toolbar present, Action select disabled with no selection ──
    page.goto(f"{ui_server}/manufacturing/production", wait_until="domcontentloaded")
    page.wait_for_selector("#mfg-table", timeout=10000)
    assert "Work In Progress" in page.locator("body").inner_text()
    page.wait_for_selector("#mfg-table:has-text('WIP-WIDGET')", timeout=8000)

    bulkbar = page.locator(".bulkbar")
    assert bulkbar.count() >= 1, ".bulkbar not found on /manufacturing/production"
    action_select = page.locator(".bulk-action-select")
    assert action_select.count() >= 1, ".bulk-action-select not found in WIP bulk toolbar"
    assert action_select.is_disabled(), ".bulk-action-select should be disabled with nothing selected"
    count_el = page.locator(".bulk-count")
    assert "0" in count_el.inner_text(), f"expected '0 selected'; got {count_el.inner_text()!r}"
    assert page.locator("#mfg-table thead .bulk-select-all").count() >= 1, ".bulk-select-all not in header"

    # The Action dropdown exposes the six Phase-2 lifecycle actions.
    opt_values = [v.strip() for v in action_select.locator("option:not([disabled])").all_inner_texts()]
    for label in ("Start", "Issue components", "Complete", "Put on hold", "Resume", "Cancel"):
        assert any(label == v for v in opt_values), f"'{label}' option missing; got {opt_values}"

    page.screenshot(path=str(SHOTS / "wip-bulk-toolbar.png"), full_page=True)

    # ── Tick both runs -> select enables, "2 selected" ──
    for rid in (run_a, run_b):
        page.locator(f"#mfg-table tr.data-row:has(input.bulk-select[value='{rid}']) .bulk-select").first.check()
    page.wait_for_function("!document.querySelector('.bulk-action-select').disabled", timeout=3000)
    assert "2 selected" in count_el.inner_text(), f"expected '2 selected'; got {count_el.inner_text()!r}"
    page.screenshot(path=str(SHOTS / "wip-two-selected.png"), full_page=True)

    # ── Bulk Start via the Action dropdown (real UI) -> both runs go in_progress ──
    page.locator(".bulk-action-select").select_option(value="start")
    assert _poll(api, lambda items: all(
        o.get("status") == "in_progress" for o in items if o.get("id") in both
    )), "both runs should be in_progress after the bulk Start UI action"
    page.wait_for_selector("#mfg-table", timeout=8000)
    page.screenshot(path=str(SHOTS / "wip-after-start.png"), full_page=True)

    # ── Bulk Complete via the Action dropdown (accept the confirm) -> completed + stock up ──
    stock_before = float(api.get(f"/items/{widget}").json().get("quantity", 0))
    page.on("dialog", lambda d: d.accept())
    page.wait_for_selector("#mfg-table:has-text('WIP-WIDGET')", timeout=8000)
    for rid in (run_a, run_b):
        page.locator(f"#mfg-table tr.data-row:has(input.bulk-select[value='{rid}']) .bulk-select").first.check()
    page.wait_for_function("!document.querySelector('.bulk-action-select').disabled", timeout=3000)
    page.locator(".bulk-action-select").select_option(value="complete")

    assert _poll(api, lambda items: all(
        o.get("status") == "completed" for o in items if o.get("id") in both
    )), "both runs should be completed after the bulk Complete UI action"
    # 3 + 5 = 8 widgets produced.
    assert _poll(api, lambda _items: float(api.get(f"/items/{widget}").json().get("quantity", 0)) > stock_before), \
        "WIP-WIDGET stock should increase after Complete"
    assert float(api.get(f"/items/{widget}").json()["quantity"]) >= stock_before + 8, \
        "WIP-WIDGET stock should rise by the produced quantity (8)"

    # ── Priority Excel funnel filters live (data-col 3) ──
    page.goto(f"{ui_server}/manufacturing/production?status=completed", wait_until="domcontentloaded")
    page.wait_for_selector("#mfg-table:has-text('WIP-WIDGET')", timeout=8000)

    # The Priority header has a .colfilter funnel at data-col=3 (checkbox col is 0, Product 1).
    prio_funnel = page.locator("#mfg-table .colfilter[data-col='3']")
    assert prio_funnel.count() >= 1, "Priority colfilter (data-col=3) not found in WIP header"
    # And the Product header has one too (data-col=1).
    assert page.locator("#mfg-table .colfilter[data-col='1']").count() >= 1, \
        "Product colfilter (data-col=1) not found in WIP header"

    assert page.locator(".colfilter-pop").count() == 0, "no funnel popup should be open initially"
    prio_funnel.click()
    popup = page.locator(".colfilter-pop")
    popup.wait_for(state="visible", timeout=5000)
    assert popup.locator(".colfilter-search").count() >= 1, "no search box in Priority funnel popup"
    assert popup.locator(".colfilter-all").count() >= 1, "no '(Select all)' in Priority funnel popup"

    item_texts = [t.strip() for t in popup.locator(".colfilter-item:not(.colfilter-all)").all_inner_texts()]
    assert any("High" in t for t in item_texts), f"'High' not in Priority funnel: {item_texts}"
    assert any("Low" in t for t in item_texts), f"'Low' not in Priority funnel: {item_texts}"
    page.screenshot(path=str(SHOTS / "wip-priority-funnel-popup.png"), full_page=True)

    # Uncheck "High" -> the High-priority run row (run_a) hides without a reload.
    high_label = popup.locator(".colfilter-item:not(.colfilter-all)").filter(has_text="High")
    high_cb = high_label.locator("input[type=checkbox]")
    assert high_cb.is_checked(), "High should be checked by default"
    high_cb.uncheck()

    page.wait_for_function(
        """(rid) => {
          var r = document.querySelector('#mfg-table tr.data-row:has(input.bulk-select[value="'+rid+'"])');
          return r && r.classList.contains('dp-row-hidden');
        }""",
        arg=run_a,
        timeout=3000,
    )
    high_row = page.locator(f"#mfg-table tr.data-row:has(input.bulk-select[value='{run_a}'])")
    assert "dp-row-hidden" in (high_row.first.get_attribute("class") or ""), \
        "High-priority run row should be hidden after unchecking High"
    low_row = page.locator(f"#mfg-table tr.data-row:has(input.bulk-select[value='{run_b}'])")
    assert "dp-row-hidden" not in (low_row.first.get_attribute("class") or ""), \
        "Low-priority run row should still be visible"
    assert "colfilter--active" in (prio_funnel.get_attribute("class") or ""), \
        "Priority funnel should be .colfilter--active after filtering"
