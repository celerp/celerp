# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Browser test + screenshots for Phase 2 — mark-to-market re-costing.

Proves:
1. A manufactured item's Manufacturing tab shows a "Recalculate cost" button.
2. A raw component used elsewhere shows a "Used in other recipes" panel + "Re-cost dependents".
3. Clicking "Re-cost dependents" after a price change propagates the new cost (unmissable flash).
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.browser

SHOTS = Path("context/reviews/phase2")


@pytest.fixture(scope="module")
def recost_items(api):
    """Gold (raw, priced) + a ring whose recipe uses 5 gold. Returns ids."""
    gold = api.post("/items", json={"sku": "MM-GOLD", "name": "Gold 1g", "quantity": 100, "sell_by": "gram", "cost_total": 8000})
    assert gold.status_code == 200, gold.text
    gold_id = gold.json()["id"]
    ring = api.post("/items", json={"sku": "MM-RING", "name": "Gold Ring", "quantity": 0, "sell_by": "piece"})
    assert ring.status_code == 200, ring.text
    ring_id = ring.json()["id"]
    r = api.put(f"/manufacturing/items/{ring_id}/recipe",
                json={"output_qty": 1, "components": [{"item_id": gold_id, "quantity": 5}], "labor": [], "overhead": []})
    assert r.status_code == 200, r.text
    return gold_id, ring_id


def _tab(page, ui_server, item_id):
    page.goto(f"{ui_server}/inventory/{item_id}?tab=manufacturing", wait_until="domcontentloaded")
    page.wait_for_selector("#recipe-section .recipe-cost-summary", timeout=10000)


def test_recost_flow_with_screenshots(page, ui_server, api, recost_items):
    SHOTS.mkdir(parents=True, exist_ok=True)
    gold_id, ring_id = recost_items
    page.set_viewport_size({"width": 1440, "height": 1000})

    # Manufactured item shows "Recalculate cost".
    _tab(page, ui_server, ring_id)
    assert page.locator("button:has-text('Recalculate cost')").count() == 1
    page.screenshot(path=str(SHOTS / "01-manufactured-item.png"), full_page=True)

    # Raw component shows the "Used in other recipes" panel + Re-cost dependents.
    _tab(page, ui_server, gold_id)
    page.wait_for_selector("button:has-text('Re-cost dependents')", timeout=5000)
    assert "Used in other recipes" in page.locator("#recipe-section").inner_text()
    page.screenshot(path=str(SHOTS / "02-component-used-in.png"), full_page=True)

    # Reprice gold, then re-cost dependents from gold's tab.
    api.post(f"/items/{gold_id}/price", json={"price_type": "cost_total", "new_price": 10000})  # unit 80 -> 100
    page.click("button:has-text('Re-cost dependents')")
    page.wait_for_selector(".flash--success", timeout=8000)
    page.screenshot(path=str(SHOTS / "03-recosted.png"), full_page=True)
    assert "Re-costed 1 dependent" in page.locator(".flash--success").inner_text()

    # Ring's unit cost must now reflect the new gold price: 5 * 100 = 500.
    ring = api.get(f"/items/{ring_id}").json()
    assert ring["recipe"]["unit_cost"] == 500.0, ring["recipe"]
