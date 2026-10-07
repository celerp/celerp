# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The dashboard KPI cards fill their rows evenly: no card alone on a row under a full
one, both KPI groups share the same columns, and a full row spans the whole width."""
from __future__ import annotations

import pytest
from playwright.sync_api import Page

pytestmark = pytest.mark.browser


@pytest.mark.parametrize("width", [390, 1280])
def test_kpi_rows_fill_evenly(page: Page, width):
    page.set_viewport_size({"width": width, "height": 900})
    page.goto("/dashboard")
    page.wait_for_load_state("load")
    grids = page.locator(".kpi-grid")
    assert grids.count() >= 1

    widths = set()
    for g in range(grids.count()):
        grid = grids.nth(g)
        gbox = grid.bounding_box()
        rows: dict[int, list[dict]] = {}
        for box in (c.bounding_box() for c in grid.locator(":scope > *").all()):
            rows.setdefault(round(box["y"]), []).append(box)
            widths.add(round(box["width"]))
        counts = [len(r) for _, r in sorted(rows.items())]
        assert max(counts) - min(counts) <= 1, f"group {g} rows {counts} at {width}px"
        for r in rows.values():
            if len(r) == max(counts):
                right = max(b["x"] + b["width"] for b in r)
                assert abs(right - (gbox["x"] + gbox["width"])) <= 1, f"group {g} row {len(r)} cards stops short"
    assert len(widths) == 1, f"KPI groups use different columns: {widths}"


@pytest.mark.parametrize("width", [390, 1280])
def test_chart_cards_fit_the_screen(page: Page, fresh_company, width):
    """A chart's canvas never pushes its card past the screen edge on a phone."""
    for i in range(3):
        r = fresh_company.post("/items", json={"sku": f"CH-{i}", "name": "Chart stone", "quantity": 1,
                                               "sell_by": "piece", "category": "colored_stone"})
        assert r.status_code in (200, 201), r.text
    page.set_viewport_size({"width": width, "height": 900})
    page.goto("/dashboard")
    page.wait_for_selector("#chart-inventory-cat")
    grid = page.locator(".charts-grid").bounding_box()
    for card in page.locator(".charts-grid > .chart-card").all():
        box = card.bounding_box()
        assert box["x"] + box["width"] <= grid["x"] + grid["width"] + 1, \
            f"a chart card runs past the screen at {width}px: {box} vs {grid}"
