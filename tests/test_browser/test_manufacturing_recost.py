# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Browser test + screenshots: automatic mark-to-market re-costing.

Proves:
1. A manufactured item's cost summary shows live rolled totals (no recalculate/apply buttons,
   no "Used in other recipes" panel — re-costing is automatic).
2. Editing a component's cost on its own Pricing tab propagates automatically to every recipe
   that uses it: the dependent's rolled unit cost and its cost_price both update, with no
   manual step.
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.browser

SHOTS = Path("context/reviews/phase2")


@pytest.fixture(scope="module")
def recost_items(api):
    """Gold (raw, priced: unit 80) + a ring whose recipe uses 5 gold. Returns ids."""
    gold = api.post("/items", json={"sku": "MM-GOLD", "name": "Gold 1g", "quantity": 100,
                                    "sell_by": "gram", "cost_total": 8000, "inventory_type": "component"})
    assert gold.status_code == 200, gold.text
    gold_id = gold.json()["id"]
    ring = api.post("/items", json={"sku": "MM-RING", "name": "Gold Ring", "quantity": 0, "sell_by": "piece"})
    assert ring.status_code == 200, ring.text
    ring_id = ring.json()["id"]
    r = api.put(f"/manufacturing/items/{ring_id}/recipe",
                json={"output_qty": 1, "components": [{"item_id": gold_id, "quantity": 5}], "labor": [], "overhead": []})
    assert r.status_code == 200, r.text
    return gold_id, ring_id


def _mfg_tab(page, ui_server, item_id):
    page.goto(f"{ui_server}/inventory/{item_id}?tab=manufacturing", wait_until="domcontentloaded")
    page.wait_for_selector("#recipe-section .recipe-cost-summary", timeout=10000)


def test_auto_recost_from_component_pricing(page, ui_server, api, recost_items):
    SHOTS.mkdir(parents=True, exist_ok=True)
    gold_id, ring_id = recost_items
    page.set_viewport_size({"width": 1440, "height": 1000})

    # Manufactured item: live cost summary, recipe-set already applied cost_price, and NO manual
    # re-cost controls anywhere (propagation is automatic).
    _mfg_tab(page, ui_server, ring_id)
    summary = page.locator("#recipe-cost-card").inner_text()
    assert "Unit cost" in summary and "400" in summary
    assert page.locator("button:has-text('Recalculate cost')").count() == 0
    assert page.locator("button:has-text('Apply to cost price')").count() == 0
    assert page.locator("text=Used in other recipes").count() == 0
    assert page.locator("button:has-text('Re-cost dependents')").count() == 0
    assert api.get(f"/items/{ring_id}").json()["cost_price"] == 400.0
    page.screenshot(path=str(SHOTS / "01-manufactured-item.png"), full_page=True)

    # Edit gold's cost on its OWN pricing tab (unit 80 -> 100). Autosave triggers propagation.
    page.goto(f"{ui_server}/inventory/{gold_id}?tab=pricing", wait_until="domcontentloaded")
    page.wait_for_selector(".pricing-grid input[name=cost_price]", timeout=10000)
    cost = page.locator("input[name=cost_price]")
    cost.fill("100")
    cost.press("Enter")  # Enter commits
    page.wait_for_selector("#pricing-save-status.saved", timeout=8000)
    page.screenshot(path=str(SHOTS / "02-component-reprice.png"), full_page=True)

    # The ring's rolled cost AND cost_price update automatically: 5 * 100 = 500.
    import time as _t
    deadline = _t.time() + 8
    ring = {}
    while _t.time() < deadline:
        ring = api.get(f"/items/{ring_id}").json()
        if (ring.get("recipe") or {}).get("unit_cost") == 500.0:
            break
        _t.sleep(0.3)
    assert ring["recipe"]["unit_cost"] == 500.0, ring.get("recipe")
    assert ring["cost_price"] == 500.0

    # And the ring's Manufacturing tab reflects it without any manual action.
    _mfg_tab(page, ui_server, ring_id)
    page.wait_for_selector("#recipe-cost-card:has-text('500')", timeout=8000)
    page.screenshot(path=str(SHOTS / "03-dependent-updated.png"), full_page=True)
