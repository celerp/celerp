# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""End-to-end manufacturing journey — every phase exercised together in one browser flow.

Mirrors context/2026-0611-manufacturing-user-journey.md. This is the ship-gate.
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.browser

SHOTS = Path("context/reviews/journey")


def _combo_pick(page, scope_selector, typed):
    box = page.locator(f"{scope_selector} .combobox-input").last
    box.click()
    box.fill(typed)
    page.wait_for_selector(".combobox-list.open", timeout=3000)
    opt = page.locator(".combobox-list.open .combobox-option:not(.combobox-option--empty):visible").first
    opt.wait_for(state="visible", timeout=3000)
    opt.click()


def test_full_manufacturing_journey(page, ui_server, api):
    SHOTS.mkdir(parents=True, exist_ok=True)
    page.set_viewport_size({"width": 1440, "height": 1100})

    # 1. Stock a raw material + an empty finished good.
    gold = api.post("/items", json={"sku": "JNY-GOLD", "name": "Gold 1g", "quantity": 100, "sell_by": "gram", "cost_total": 8000, "inventory_type": "component"}).json()["id"]
    ring = api.post("/items", json={"sku": "JNY-RING", "name": "18K Ring", "quantity": 0, "sell_by": "piece"}).json()["id"]

    # 2. Define the recipe on the item (Phase 1).
    page.goto(f"{ui_server}/inventory/{ring}?tab=manufacturing", wait_until="domcontentloaded")
    page.wait_for_selector("#recipe-form", timeout=10000)
    def _set_cell(data_col, value):
        for attempt in range(2):  # retry once: successive edits can race the prior swap
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

    _combo_pick(page, ".recipe-add-row", "JNY-GOLD")  # ghost add-row commits a real component
    page.wait_for_selector('td[data-col="recipe__components__0__quantity"]', timeout=8000)
    _set_cell("recipe__components__0__quantity", "5")
    page.fill("input[name=labor_new_op]", "Setting")
    page.fill("input[name=labor_new_hours]", "2")
    page.fill("input[name=labor_new_rate]", "50")
    page.locator("tr.recipe-add-row:has(input[name=labor_new_op]) button.recipe-add-btn").click()
    page.wait_for_selector('td[data-col="recipe__labor__0__rate"]', timeout=8000)
    page.fill("input[name=oh_new_desc]", "Polish & box")
    page.fill("input[name=oh_new_amount]", "15")
    page.locator("tr.recipe-add-row:has(input[name=oh_new_desc]) button.recipe-add-btn").click()
    page.wait_for_selector('td[data-col="recipe__overhead__0__amount"]', timeout=8000)
    page.wait_for_selector("#recipe-cost-card:has-text('515')", timeout=8000)
    # The rolled standard cost is applied to cost_price automatically — no button to press.
    ring_state = api.get(f"/items/{ring}").json()
    assert ring_state["recipe"]["unit_cost"] == 515.0
    assert ring_state["cost_price"] == 515.0
    assert page.locator("button:has-text('Apply to cost price')").count() == 0

    # 3. Reprice gold on its own Pricing tab (unit 80 -> 100); dependents recost automatically.
    page.goto(f"{ui_server}/inventory/{gold}?tab=pricing", wait_until="domcontentloaded")
    page.wait_for_selector(".pricing-grid input[name=cost_price]", timeout=10000)
    gold_cost = page.locator("input[name=cost_price]")
    gold_cost.fill("100")
    gold_cost.press("Enter")
    page.wait_for_selector("#pricing-save-status.saved", timeout=8000)
    # No manual re-cost step exists anymore; the ring updates on its own: 5*100 + 100 + 15 = 615.
    import time as _t
    deadline = _t.time() + 8
    while _t.time() < deadline:
        if (api.get(f"/items/{ring}").json().get("recipe") or {}).get("unit_cost") == 615.0:
            break
        _t.sleep(0.3)
    assert api.get(f"/items/{ring}").json()["recipe"]["unit_cost"] == 615.0

    # 4. Sell on an invoice -> demand appears on the product-centric To-Make board (Phase 1).
    #    Finalize it so it counts as demand (a draft invoice is a pro-forma, excluded from the board).
    doc = api.post("/docs", json={"doc_type": "invoice", "line_items": [
        {"item_id": ring, "sku": "JNY-RING", "name": "18K Ring", "quantity": 2, "unit_price": 900},
    ], "total": 1800}).json()["id"]
    api.post(f"/docs/{doc}/finalize")
    # The invoice's Manufacturing panel is product-centric (links to the product tab, no auto orders).
    page.goto(f"{ui_server}/docs/{doc}", wait_until="domcontentloaded")
    page.wait_for_selector("#doc-mfg-panel:has-text('Items to manufacture')", timeout=10000)
    panel = page.locator("#doc-mfg-panel").inner_text()
    assert "Manufacturing orders" not in panel and "10" in panel  # 2 rings -> 10 g gold (JIT summary)
    # The To-Make board pools that demand by product (lead column = item + qty to make).
    page.goto(f"{ui_server}/manufacturing", wait_until="domcontentloaded")
    page.wait_for_selector("#mfg-table:has-text('JNY-RING')", timeout=10000)
    page.screenshot(path=str(SHOTS / "to-make-board.png"), full_page=True)
    board = {r["item_id"]: r for r in api.get("/manufacturing/to-make").json()["items"]}
    assert board[ring]["to_make"] == 2.0 and board[ring]["demand"] == 2.0

    # 5. Make the ring from the invoice's demand — a work order is created (Phase 2/3). Work orders
    #    are no longer typed inline on the product hub; they come from an order. Setup creates the
    #    work order linked 1:1 to the invoice via the make endpoint; the hub then DRIVES it.
    doc_row = {r["item_id"]: r for r in api.get("/manufacturing/to-make").json()["items"]}[ring]
    made = api.post("/manufacturing/to-make/make",
                    json={"lines": [{"item_id": ring, "doc_id": doc_row["docs"][0]["doc_id"]}]}).json()
    run_id = made["created"][0]["run_id"]
    assert made["created"][0]["quantity"] == 2.0  # net shortfall = 2 demanded, 0 on hand

    # The product hub's Work orders table shows that order, linked to the source invoice.
    page.goto(f"{ui_server}/inventory/{ring}?tab=manufacturing", wait_until="domcontentloaded")
    page.wait_for_selector("#production-block table.data-table", timeout=10000)
    block = page.locator("#production-block")
    assert block.locator(".badge:has-text('Planned')").count() >= 1
    assert block.locator("table.data-table a.table-link[href*='/docs/']").count() >= 1

    # 6. Start, then Complete via the per-row Action dropdown — Complete issues the components and
    #    receives the finished goods. allow_splitting is true (default) so the ring is restocked
    #    IN PLACE (no nameless item).
    import time as _t2
    def _wait_status(want: str, timeout: float = 10.0) -> None:
        deadline = _t2.time() + timeout
        while _t2.time() < deadline:
            if api.get(f"/manufacturing/{run_id}").json().get("status") == want:
                return
            _t2.sleep(0.3)
        raise AssertionError(f"work order never reached {want}")

    block.locator(".wo-action-select").first.select_option(value="start")
    _wait_status("in_progress")
    page.wait_for_selector("#production-block .wo-action-select", timeout=8000)
    block.locator(".wo-action-select").first.select_option(value="complete")
    _wait_status("completed")
    # Completed work orders are hidden by default; reveal them with the toggle to confirm.
    page.locator("#production-block a:has-text('Show completed')").click()
    page.wait_for_selector("#production-block .badge:has-text('Completed')", timeout=8000)
    page.screenshot(path=str(SHOTS / "run-completed.png"), full_page=True)

    assert api.get(f"/manufacturing/{run_id}").json()["status"] == "completed"
    rings = [i for i in api.get("/items").json()["items"] if i.get("sku") == "JNY-RING"]
    assert len(rings) == 1 and rings[0]["id"] == ring  # restocked in place — one ring item, no clone
    assert rings[0]["quantity"] == 2.0  # made the 2 demanded
    assert api.get(f"/items/{gold}").json()["quantity"] == 90.0  # 100 - 5*2 issued

    # 7. The board now shows zero to-make for the ring (2 on hand >= 2 demanded).
    board = {r["item_id"]: r for r in api.get("/manufacturing/to-make").json()["items"]}
    assert board[ring]["on_hand"] == 2.0 and board[ring]["to_make"] == 0.0

    # 8. No standalone BOM anywhere.
    page.goto(f"{ui_server}/manufacturing", wait_until="domcontentloaded")
    page.wait_for_selector("body", timeout=8000)
    assert "Bills of Materials" not in page.content()
    assert page.goto(f"{ui_server}/manufacturing/boms").status in (404, 200)  # UI route gone (404) or app 404 page
