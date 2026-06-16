# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Browser test + screenshot: the product Manufacturing tab production hub - open demand block,
Make control, and production runs with inline status actions."""
from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.browser

SHOTS = Path("context/reviews/manufacturing")


def test_product_hub_demand_make_and_run_actions(page, ui_server, api):
    SHOTS.mkdir(parents=True, exist_ok=True)
    gold = api.post("/items", json={"sku": "HUB-GOLD", "name": "Gold 1g", "quantity": 100, "sell_by": "gram",
                                    "cost_total": 8000, "inventory_type": "component"}).json()["id"]
    ring = api.post("/items", json={"sku": "HUB-RING", "name": "18K Ring", "quantity": 0, "sell_by": "piece"}).json()["id"]
    api.put(f"/manufacturing/items/{ring}/recipe",
            json={"output_qty": 1, "components": [{"item_id": gold, "quantity": 5}], "labor": [], "overhead": []})
    # Finalize the invoice so it counts as demand (a draft invoice is a pro-forma, excluded).
    inv = api.post("/docs", json={"doc_type": "invoice", "line_items": [
        {"item_id": ring, "sku": "HUB-RING", "name": "18K Ring", "quantity": 2, "unit_price": 900}], "total": 1800}).json()
    api.post(f"/docs/{inv['id']}/finalize")

    page.set_viewport_size({"width": 1440, "height": 1000})
    page.goto(f"{ui_server}/inventory/{ring}?tab=manufacturing", wait_until="domcontentloaded")
    page.wait_for_selector("#production-block", timeout=10000)

    block = page.locator("#production-block")
    # Open demand block shows the invoice (the document that wants this product).
    assert "Open demand" in block.inner_text()
    assert block.locator("a:has-text('INV'), a[href*='/docs/']").count() >= 1

    # Make 2 -> a Planned run appears.
    page.fill("#production-block .make-form input[name=quantity]", "2")
    page.locator("#production-block .make-form button:has-text('Make')").click()
    page.wait_for_selector("#production-block:has-text('Planned')", timeout=8000)
    page.screenshot(path=str(SHOTS / "product-hub.png"), full_page=True)
    runs = [r for r in api.get(f"/manufacturing/items/{ring}/hub").json()["runs"]]
    assert len(runs) == 1 and runs[0]["status"] == "planned" and runs[0]["output_item_id"] == ring

    # Start the run from the hub -> the pipeline advances and the Complete action appears.
    page.locator("#production-block button:has-text('Start')").click()
    # The Complete button only renders once the run is in progress (wait on the affordance, not on
    # the always-present 'In Progress' pipeline-step label).
    page.wait_for_selector("#production-block button:has-text('Complete')", timeout=8000)
    assert api.get(f"/manufacturing/items/{ring}/hub").json()["runs"][0]["status"] == "in_progress"
