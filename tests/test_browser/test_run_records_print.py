# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Print views for production runs: the worksheet names its components, and a run has its own
run sheet of the calculated (scaled) input quantities."""
from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.browser

SHOTS = Path("context/reviews/run-records")


def test_worksheet_print_names_components(page, ui_server, api):
    """The worksheet resolves each material's name from the item list, so an imported recipe whose
    component rows carry only a SKU still prints the readable name next to it."""
    SHOTS.mkdir(parents=True, exist_ok=True)
    gold = api.post("/items", json={"sku": "WN-GOLD", "name": "Rose Gold Alloy", "quantity": 100,
                                    "sell_by": "gram", "cost_total": 8000, "inventory_type": "component"}).json()["id"]
    ring = api.post("/items", json={"sku": "WN-RING", "name": "Name Ring", "quantity": 0, "sell_by": "piece"}).json()["id"]
    # The stored recipe row carries sku + unit but no denormalized name (imported-recipe shape).
    api.put(f"/manufacturing/items/{ring}/recipe", json={
        "output_qty": 1,
        "components": [{"item_id": gold, "sku": "WN-GOLD", "quantity": 5, "unit": "gram"}],
        "labor": [], "overhead": [],
    })

    page.goto(f"{ui_server}/inventory/{ring}/worksheet/print", wait_until="domcontentloaded")
    body = page.locator("body").inner_text()
    assert "WN-GOLD" in body                 # the SKU
    assert "Rose Gold Alloy" in body         # name resolved from the item list, not the stored recipe row
    page.screenshot(path=str(SHOTS / "worksheet-names.png"), full_page=True)


def test_run_sheet_print_renders_scaled_inputs(page, ui_server, api):
    """A production run's run sheet prints the calculated input quantities for that run's build size,
    each row showing SKU and name, under a 'Run - build qty N' header."""
    SHOTS.mkdir(parents=True, exist_ok=True)
    gold = api.post("/items", json={"sku": "RS-GOLD", "name": "Yellow Gold", "quantity": 100, "sell_by": "gram",
                                    "cost_total": 8000, "inventory_type": "component", "status": "available"}).json()["id"]
    ring = api.post("/items", json={"sku": "RS-RING", "name": "Sheet Ring", "quantity": 0,
                                    "sell_by": "piece", "status": "available"}).json()["id"]
    api.put(f"/manufacturing/items/{ring}/recipe", json={
        "output_qty": 1,
        "components": [{"item_id": gold, "sku": "RS-GOLD", "quantity": 5, "unit": "gram"}],
        "labor": [], "overhead": [],
    })
    run = api.post(f"/manufacturing/items/{ring}/build", json={"quantity": 3}).json()["id"]  # planned 15 gold

    page.goto(f"{ui_server}/manufacturing/{run}/run-sheet/print", wait_until="domcontentloaded")
    body = page.locator("body").inner_text()
    assert "Run - build qty 3" in body
    assert "RS-GOLD" in body and "Yellow Gold" in body
    assert "15" in body                      # 5 per unit scaled by the build of 3
    page.screenshot(path=str(SHOTS / "run-sheet.png"), full_page=True)
