# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Verify the review-pass polish: Components inventory filter (item 3) + split box one-line (item 4)."""
from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.browser

SHOTS = Path("context/reviews/polish")


def test_components_filter_tab_and_split_box_layout(page, ui_server, api):
    SHOTS.mkdir(parents=True, exist_ok=True)
    page.set_viewport_size({"width": 1440, "height": 1000})
    item = api.post("/items", json={"sku": "PL-RAW", "name": "Raw", "quantity": 10, "sell_by": "piece"}).json()["id"]
    # A user can classify an item as a component (item 3).
    comp = api.post("/items", json={"sku": "PL-COMP", "name": "Brass rod", "quantity": 50, "sell_by": "piece", "inventory_type": "component"})
    assert comp.status_code == 200, comp.text

    # Item 3: a "Components" filter tab exists, between Stocked and Services.
    page.goto(f"{ui_server}/inventory", wait_until="domcontentloaded")
    page.wait_for_selector(".inventory-type-tabs", timeout=10000)
    tabs = [t.strip() for t in page.locator(".inventory-type-tabs .category-tab").all_inner_texts()]
    assert "Components" in tabs, tabs
    assert tabs.index("Components") == tabs.index("Stocked") + 1, tabs
    page.screenshot(path=str(SHOTS / "01-components-tab.png"))
    # The filter actually filters (URL carries the var per GDR §2m): component item shown, stocked hidden.
    page.goto(f"{ui_server}/inventory?inventory_type=component", wait_until="domcontentloaded")
    page.wait_for_selector("#inventory-content", timeout=8000)
    body = page.locator("#inventory-content").inner_text()
    assert "PL-COMP" in body and "PL-RAW" not in body, body[:300]

    # Item 4: on a splittable item, the +, inputs and Go sit on one line (.split-line).
    page.goto(f"{ui_server}/inventory/{item}?tab=details", wait_until="domcontentloaded")
    page.wait_for_selector(".split-line", timeout=10000)
    line = page.locator(".split-line").first
    add = line.locator(".split-add-btn").bounding_box()
    go = line.locator("button[type=submit]").first.bounding_box()
    inp = line.locator("input[name=split_qty]").first.bounding_box()
    # + first, input in the middle, Go last — all roughly on the same row.
    assert add["x"] < inp["x"] < go["x"], (add, inp, go)
    assert abs(add["y"] - go["y"]) < 40, (add["y"], go["y"])
    page.screenshot(path=str(SHOTS / "02-split-line.png"))
