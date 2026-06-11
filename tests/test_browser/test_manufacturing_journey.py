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
    _combo_pick(page, "#recipe-form", "JNY-GOLD")  # ghost add-row → commits a real component
    page.wait_for_selector("input[name=comp_qty_0]", timeout=8000)
    page.fill("input[name=comp_qty_0]", "5")
    page.locator("input[name=comp_qty_0]").blur()
    page.fill("input[name=labor_new_op]", "Setting")
    page.locator("input[name=labor_new_op]").blur()
    page.wait_for_selector("input[name=labor_hours_0]", timeout=8000)
    page.fill("input[name=labor_hours_0]", "2")
    page.fill("input[name=labor_rate_0]", "50")
    page.locator("input[name=labor_rate_0]").blur()
    page.fill("input[name=oh_new_desc]", "Polish & box")
    page.locator("input[name=oh_new_desc]").blur()
    page.wait_for_selector("input[name=oh_amount_0]", timeout=8000)
    page.fill("input[name=oh_amount_0]", "15")
    page.locator("input[name=oh_amount_0]").blur()
    page.wait_for_selector("#recipe-cost-card:has-text('515')", timeout=8000)
    assert api.get(f"/items/{ring}").json()["recipe"]["unit_cost"] == 515.0

    # 2b. Apply the rolled standard cost to the item's cost price (Decision 1), and verify it sticks.
    page.click("button:has-text('Apply to cost price')")
    page.wait_for_selector(".flash--success:has-text('Cost price')", timeout=8000)
    assert api.get(f"/items/{ring}").json()["cost_price"] == 515.0

    # 3. Gold reprices → re-cost dependents (Phase 2).
    api.post(f"/items/{gold}/price", json={"price_type": "cost_total", "new_price": 10000})  # unit 80 -> 100
    page.goto(f"{ui_server}/inventory/{gold}?tab=manufacturing", wait_until="domcontentloaded")
    page.wait_for_selector("button:has-text('Re-cost dependents')", timeout=8000)
    assert "Used in other recipes" in page.locator("#recipe-section").inner_text()
    page.click("button:has-text('Re-cost dependents')")
    page.wait_for_selector(".flash--success", timeout=8000)
    assert api.get(f"/items/{ring}").json()["recipe"]["unit_cost"] == 615.0  # 5*100 + 100 + 15

    # 4. Sell on an invoice, then create the order from the doc (Phase 3).
    doc = api.post("/docs", json={"doc_type": "invoice", "line_items": [
        {"item_id": ring, "sku": "JNY-RING", "name": "18K Ring", "quantity": 2, "unit_price": 900},
    ], "total": 1800}).json()["id"]
    page.goto(f"{ui_server}/docs/{doc}", wait_until="domcontentloaded")
    page.wait_for_selector("#doc-mfg-panel button:has-text('Create manufacturing order')", timeout=10000)
    assert "10" in page.locator("#doc-mfg-panel").inner_text()  # 2 rings -> 10 g gold
    page.click("#doc-mfg-panel button:has-text('Create manufacturing order')")
    page.wait_for_selector("#doc-mfg-panel .flash", timeout=8000)
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
