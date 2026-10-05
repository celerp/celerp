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
