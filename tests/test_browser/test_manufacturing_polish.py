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
    # Component rows are visually distinct (left accent class).
    assert page.locator("tr.data-row--component").count() >= 1

    # /manufacturing: header search box + the standard date-filter bar with all presets.
    gold = api.post("/items", json={"sku": "PL-MGOLD", "name": "Gold", "quantity": 50, "sell_by": "gram",
                                    "cost_total": 500, "inventory_type": "component"}).json()["id"]
    fg = api.post("/items", json={"sku": "PL-MRING", "name": "Ring", "quantity": 0, "sell_by": "piece"}).json()["id"]
    api.put(f"/manufacturing/items/{fg}/recipe",
            json={"output_qty": 1, "components": [{"item_id": gold, "quantity": 1}], "labor": [], "overhead": []})
    api.post(f"/manufacturing/items/{fg}/build", json={"quantity": 1})
    page.goto(f"{ui_server}/manufacturing", wait_until="domcontentloaded")
    page.wait_for_selector("#mfg-table", timeout=10000)
    bar = page.locator(".preset-btn").all_inner_texts()
    for label in ("This Month", "Last 3 Months", "Last 6 Months", "Last 12 Months", "This Fiscal Year", "Last Fiscal Year", "All Time"):
        assert any(label.lower() in b.lower() for b in bar), (label, bar)
    assert page.locator("button:has-text('Apply')").count() >= 1
    # Search narrows the order table (by output SKU / doc number / description).
    box = page.get_by_placeholder("Search order, doc number, SKU...")
    box.fill("PL-MRING")
    page.wait_for_selector("#mfg-table:has-text('assembly'), #mfg-table:has-text('Build')", timeout=8000)
    box.fill("NO-SUCH-DOC-123")
    page.wait_for_selector("#mfg-table:has-text('No production orders')", timeout=8000)

    # /lists: the same date-filter bar is present.
    page.goto(f"{ui_server}/lists", wait_until="domcontentloaded")
    page.wait_for_selector(".preset-btn", timeout=10000)

    # /docs?view=drafts&type=invoice: no redundant chip at all, and the date bar is present.
    api.post("/docs", json={"doc_type": "invoice", "line_items": [], "total": 0})  # ensure a draft exists
    page.goto(f"{ui_server}/docs?view=drafts&type=invoice&preset=all", wait_until="domcontentloaded")
    page.wait_for_selector("#doc-table, .data-table", timeout=10000)
    assert page.locator(".drafts-tab").count() == 0
    assert page.locator(".preset-btn").count() >= 7  # full date-filter bar on the drafts view too

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
