# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Printable production worksheet: the header print icon opens one page with everything."""
from __future__ import annotations

import base64
from pathlib import Path

import pytest

pytestmark = pytest.mark.browser

SHOTS = Path("context/reviews/worksheet")

_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


def test_print_icon_opens_full_worksheet(page, ui_server, api):
    SHOTS.mkdir(parents=True, exist_ok=True)
    raw = api.post("/items", json={"sku": "WS-GOLD", "name": "Gold", "quantity": 100, "sell_by": "gram",
                                   "cost_total": 8000, "inventory_type": "component"}).json()["id"]
    ring = api.post("/items", json={"sku": "WS-RING", "name": "Print Ring", "quantity": 0, "sell_by": "piece"}).json()["id"]
    api.put(f"/manufacturing/items/{ring}/recipe", json={
        "output_qty": 1,
        "components": [{"item_id": raw, "sku": "WS-GOLD", "quantity": 5, "unit": "gram"}],
        "labor": [{"operation": "Setting", "kind": "hourly", "hours": 1, "rate": 50}],
        "overhead": [{"description": "Polish & box", "amount": 15}],
    })
    api.put(f"/manufacturing/items/{ring}/workflow", json={"steps": [
        {"name": "Cast", "instructions": "Pour and cool", "station": "Casting", "time_value": 12, "time_unit": "hr", "wait": True},
        {"name": "Set", "instructions": "Set the stone", "station": "Bench", "time_value": 25, "time_unit": "min"},
    ]})
    api.post(f"/items/{ring}/files?document_tag=product_images", files={"file": ("ring.png", _PNG, "image/png")})

    page.goto(f"{ui_server}/inventory/{ring}?tab=manufacturing", wait_until="domcontentloaded")
    icon = page.locator("a[title='Print production worksheet']")
    icon.wait_for(state="visible", timeout=10000)
    assert icon.get_attribute("href").endswith(f"/inventory/{ring}/worksheet/print")

    # Click the icon -> the worksheet opens in a new tab (auto window.print()).
    with page.expect_popup() as popup_info:
        icon.click()
    ws = popup_info.value
    ws.wait_for_load_state("domcontentloaded")
    body = ws.locator("body").inner_text()
    # One page carries product info + recipe + workflow.
    assert "Production Worksheet" in body
    assert "Print Ring" in body and "WS-RING" in body
    assert "WS-GOLD" in body            # a materials component
    assert "Setting" in body            # a labor operation
    assert "Cast" in body and "Set" in body          # workflow steps
    assert "unattended" in body         # the wait step badge
    assert ws.locator(".ws-images img").count() >= 1  # product image
    assert ws.locator(".ws-tbl").count() >= 2         # recipe + workflow tables
    ws.screenshot(path=str(SHOTS / "print.png"), full_page=True)
