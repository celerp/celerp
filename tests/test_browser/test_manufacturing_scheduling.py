# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Browser test + screenshot: the Work In Progress queue scheduling.

Covers the Due / Priority columns, overdue highlighting, the no-due-first then earliest-due sort,
and double-click-to-edit a run's due date.
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.browser

SHOTS = Path("context/reviews/manufacturing")


def test_in_production_scheduling(page, ui_server, api):
    SHOTS.mkdir(parents=True, exist_ok=True)
    page.set_viewport_size({"width": 1440, "height": 1000})

    gold = api.post("/items", json={"sku": "SCH-GOLD", "name": "Gold", "quantity": 100, "sell_by": "gram",
                                    "cost_total": 8000, "inventory_type": "component"}).json()["id"]
    ring = api.post("/items", json={"sku": "SCH-RING", "name": "Ring", "quantity": 0, "sell_by": "piece"}).json()["id"]
    api.put(f"/manufacturing/items/{ring}/recipe",
            json={"output_qty": 1, "components": [{"item_id": gold, "quantity": 1}], "labor": [], "overhead": []})

    runs = [api.post(f"/manufacturing/items/{ring}/build", json={"quantity": 1}).json()["id"] for _ in range(3)]
    api.post(f"/manufacturing/{runs[0]}/schedule", json={"due_date": "2020-01-01", "priority": "urgent"})  # overdue
    api.post(f"/manufacturing/{runs[1]}/schedule", json={"due_date": "2030-12-31"})                         # future
    # runs[2] left with no due date

    page.goto(f"{ui_server}/manufacturing/production", wait_until="domcontentloaded")
    page.wait_for_selector("#mfg-table", timeout=10000)
    headers = page.locator("#mfg-table thead").inner_text().lower()
    assert "priority" in headers and "due" in headers

    # Sort: no-due-date rows first, then earliest due. The three SCH-RING runs are:
    #   runs[2] -> no due date  (comes first among undated)
    #   runs[0] -> 2020-01-01  (overdue; earliest dated)
    #   runs[1] -> 2030-12-31  (future)
    # Other tests may also have undated planned runs; verify positional ordering only
    # among the dated cells by checking that 2020-01-01 appears before 2030-12-31.
    due_cells = page.locator("#mfg-table tbody tr td:nth-child(4)").all_inner_texts()
    date_cells = [c.strip() for c in due_cells if c.strip() not in ("--", "", EMPTY := "—")]
    idx_2020 = next((i for i, c in enumerate(due_cells) if "2020-01-01" in c), None)
    idx_2030 = next((i for i, c in enumerate(due_cells) if "2030-12-31" in c), None)
    assert idx_2020 is not None, "2020-01-01 due date not found in table"
    assert idx_2030 is not None, "2030-12-31 due date not found in table"
    assert idx_2020 < idx_2030, f"2020-01-01 should sort before 2030-12-31 but got positions {idx_2020}, {idx_2030}"
    # All rows before 2020-01-01 must be undated (no-due-first invariant).
    for c in due_cells[:idx_2020]:
        assert c.strip() in ("--", "", "—"), (
            f"Dated row appears before 2020-01-01, violating no-due-first sort: {c!r}"
        )

    # The overdue run (2020) is highlighted.
    assert page.locator("#mfg-table .cell--alert:has-text('2020-01-01')").count() == 1
    page.screenshot(path=str(SHOTS / "in-production-scheduling.png"), full_page=True)

    # Double-click a no-due run's Due chip -> date input -> set a date -> table refreshes.
    # Target the first row that has a -- due cell (runs[2] from this test, or any undated run).
    # Each row has two editable chips (priority then due); the due chip is the second (.nth(1)).
    undated_row = page.locator(
        "#mfg-table tbody tr:has(td:nth-child(4):has-text('--'))"
    ).first
    first_due = undated_row.locator(".editable-cell").nth(1)
    first_due.dblclick()
    inp = page.locator("#mfg-table input[name=due_date]")
    inp.wait_for(state="visible", timeout=5000)
    inp.fill("2027-03-15")
    inp.dispatch_event("change")
    page.wait_for_selector("#mfg-table:has-text('2027-03-15')", timeout=8000)
    assert any(r.get("due_date") == "2027-03-15" for r in api.get("/manufacturing").json()["items"])
