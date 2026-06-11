# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Browser test + screenshots for Phase 4 — the repurposed /manufacturing/new build flow."""
from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.browser

SHOTS = Path("context/reviews/phase4")


@pytest.fixture(scope="module")
def buildable(api):
    gold = api.post("/items", json={"sku": "N-GOLD", "name": "Gold 1g", "quantity": 100, "sell_by": "gram", "cost_total": 8000}).json()["id"]
    ring = api.post("/items", json={"sku": "N-RING", "name": "Gold Ring", "quantity": 0, "sell_by": "piece"}).json()["id"]
    api.put(f"/manufacturing/items/{ring}/recipe",
            json={"output_qty": 1, "components": [{"item_id": gold, "quantity": 5}], "labor": [], "overhead": []})
    return gold, ring


def test_new_order_build_flow(page, ui_server, api, buildable):
    SHOTS.mkdir(parents=True, exist_ok=True)
    gold, ring = buildable
    page.set_viewport_size({"width": 1440, "height": 900})

    page.goto(f"{ui_server}/manufacturing/new", wait_until="domcontentloaded")
    page.wait_for_selector("select[name=finished_item_id], .combobox-input", timeout=10000)
    page.screenshot(path=str(SHOTS / "01-new-order.png"), full_page=True)

    # Pick the manufacturable SKU via the searchable picker, set qty 3, create.
    box = page.locator(".combobox-input").first
    box.click()
    box.fill("N-RING")
    page.wait_for_selector(".combobox-list.open", timeout=3000)
    page.locator(".combobox-list.open .combobox-option:not(.combobox-option--empty):visible").first.click()
    page.fill("input[name=quantity]", "3")
    page.click("button:has-text('Create order')")

    # Redirected to the order detail; inputs expanded to 15 gold.
    page.wait_for_load_state("domcontentloaded")
    page.wait_for_selector("#mfg-detail, .detail-panel, body", timeout=8000)
    page.screenshot(path=str(SHOTS / "02-order-created.png"), full_page=True)

    orders = api.get("/manufacturing").json()["items"]
    ring_orders = [o for o in orders if any(i.get("item_id") == gold and i.get("quantity") == 15.0 for i in o.get("inputs", []))]
    assert len(ring_orders) == 1, f"expected one build order with 15 gold, got {[o.get('inputs') for o in orders]}"
