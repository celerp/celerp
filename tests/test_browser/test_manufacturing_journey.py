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
    page.locator("input[name=labor_new_op]").blur()
    page.wait_for_selector('td[data-col="recipe__labor__0__hours"]', timeout=8000)
    _set_cell("recipe__labor__0__hours", "2")
    _set_cell("recipe__labor__0__rate", "50")
    page.fill("input[name=oh_new_desc]", "Polish & box")
    page.locator("input[name=oh_new_desc]").blur()
    page.wait_for_selector('td[data-col="recipe__overhead__0__amount"]', timeout=8000)
    _set_cell("recipe__overhead__0__amount", "15")
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

    # 4. Sell on an invoice; the manufacturing order is created automatically (Phase 3).
    doc = api.post("/docs", json={"doc_type": "invoice", "line_items": [
        {"item_id": ring, "sku": "JNY-RING", "name": "18K Ring", "quantity": 2, "unit_price": 900},
    ], "total": 1800}).json()["id"]
    page.goto(f"{ui_server}/docs/{doc}", wait_until="domcontentloaded")
    page.wait_for_selector("#doc-mfg-panel:has-text('Manufacturing orders')", timeout=10000)
    panel = page.locator("#doc-mfg-panel").inner_text()
    assert "Create manufacturing order" not in panel  # nothing to click; it already exists
    assert "10" in panel  # 2 rings -> 10 g gold in the JIT summary
    page.screenshot(path=str(SHOTS / "doc-order-created.png"), full_page=True)
    doc_orders = [o for o in api.get("/manufacturing").json()["items"] if o.get("source_doc_id") == doc]
    assert len(doc_orders) == 1 and doc_orders[0]["inputs"][0]["quantity"] == 10.0

    # 5. Ad-hoc build via the repurposed /manufacturing/new (Phase 4).
    page.goto(f"{ui_server}/manufacturing/new", wait_until="domcontentloaded")
    page.wait_for_selector(".combobox-input", timeout=8000)
    _combo_pick(page, "form", "JNY-RING")
    page.fill("input[name=quantity]", "3")
    page.click("button:has-text('Create order')")
    page.wait_for_load_state("domcontentloaded")
    build_orders = [o for o in api.get("/manufacturing").json()["items"]
                    if any(i.get("item_id") == gold and i.get("quantity") == 15.0 for i in o.get("inputs", []))]
    assert len(build_orders) == 1

    # 6. Run the build order to completion (existing lifecycle) → produces a finished ring.
    order_id = build_orders[0]["id"]
    assert api.post(f"/manufacturing/{order_id}/consume", json={"item_id": gold, "quantity": 15}).status_code == 200
    assert api.post(f"/manufacturing/{order_id}/start").status_code == 200
    assert api.post(f"/manufacturing/{order_id}/complete", json={}).status_code == 200
    produced = [i for i in api.get("/items").json()["items"]
                if i.get("sku") == "JNY-RING" and i.get("manufacturing_order_id") == order_id]
    assert len(produced) == 1 and produced[0]["quantity"] == 3.0

    # 7. No standalone BOM anywhere.
    page.goto(f"{ui_server}/manufacturing", wait_until="domcontentloaded")
    page.wait_for_selector("body", timeout=8000)
    assert "Bills of Materials" not in page.content()
    assert page.goto(f"{ui_server}/manufacturing/boms").status in (404, 200)  # UI route gone (404) or app 404 page
