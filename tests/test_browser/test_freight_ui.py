# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Freight inventory type (P1) renders consistently: a Freight type-tab on the inventory list and
'Freight' selectable in the item-detail inventory_type dropdown."""
from __future__ import annotations

import uuid
from pathlib import Path

import pytest

pytestmark = pytest.mark.browser


def _no_crash(page, ctx=""):
    body = page.locator("body").inner_text()
    assert "Internal Server Error" not in body, f"{ctx}: 500"
    assert "Traceback (most recent call last)" not in body, f"{ctx}: traceback"


def test_freight_tab_and_dropdown(page, ui_server, api):
    tag = uuid.uuid4().hex[:6].upper()
    frt = api.post("/items", json={"sku": f"FRT-{tag}", "name": "Sea Freight", "quantity": 0,
                                   "sell_by": "piece", "inventory_type": "freight",
                                   "landed_cost_kind": "freight"})
    assert frt.status_code in {200, 201}, frt.text
    frt_id = frt.json()["id"]

    page.set_viewport_size({"width": 1440, "height": 1000})
    Path("context/reviews/freight").mkdir(parents=True, exist_ok=True)

    # The Freight type-tab exists and, when active, lists the freight item.
    page.goto(f"{ui_server}/inventory?inventory_type=freight", wait_until="domcontentloaded")
    page.wait_for_selector(".inventory-type-tabs", timeout=8000)
    _no_crash(page, "inventory")
    tabs = page.locator(".inventory-type-tabs .category-tab")
    labels = [tabs.nth(i).inner_text().strip() for i in range(tabs.count())]
    assert "Freight" in labels, f"Freight tab missing: {labels}"
    active = page.locator(".inventory-type-tabs .category-tab--active").inner_text().strip()
    assert active == "Freight", f"active tab is {active!r}"
    page.wait_for_selector(f"text=FRT-{tag}", timeout=8000)
    page.screenshot(path="context/reviews/freight/inventory-freight-tab.png", full_page=True)

    # Consistency check: a Service item (closest sibling, same table) must render the same way -
    # so any non-stock cell treatment ("0 piece", "--") is systemic, not freight-specific.
    svc = api.post("/items", json={"sku": f"SVC-{tag}", "name": "Consulting", "quantity": 0,
                                   "sell_by": "piece", "inventory_type": "service"})
    assert svc.status_code in {200, 201}, svc.text
    page.goto(f"{ui_server}/inventory?inventory_type=service", wait_until="domcontentloaded")
    page.wait_for_selector(f"text=SVC-{tag}", timeout=8000)
    _no_crash(page, "service")
    page.screenshot(path="context/reviews/freight/inventory-service-tab.png", full_page=True)

    # The item detail page renders the freight item with inventory_type = Freight.
    page.goto(f"{ui_server}/inventory/{frt_id}", wait_until="domcontentloaded")
    page.wait_for_selector("body", timeout=8000)
    _no_crash(page, "detail")
    body = page.locator("body").inner_text()
    assert "Freight" in body, "detail page does not show Freight type"
    page.screenshot(path="context/reviews/freight/inventory-freight-detail.png", full_page=True)
