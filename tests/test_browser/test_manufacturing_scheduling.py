# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Browser test + screenshot: the In Production queue scheduling (Phase A).

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

    page.goto(f"{ui_server}/manufacturing?tab=in_production", wait_until="domcontentloaded")
    page.wait_for_selector("#mfg-table", timeout=10000)
    headers = page.locator("#mfg-table thead").inner_text().lower()
    assert "priority" in headers and "due" in headers

    # Sort: no-due-date first, then earliest due. The Due column reads: -- , 2020-01-01 , 2030-12-31.
    due_cells = page.locator("#mfg-table tbody tr td:nth-child(4)").all_inner_texts()
    assert due_cells[0].strip() in ("--", "")
    assert "2020-01-01" in due_cells[1] and "2030-12-31" in due_cells[2]

    # The overdue run (2020) is highlighted.
    assert page.locator("#mfg-table .cell--alert:has-text('2020-01-01')").count() == 1
    page.screenshot(path=str(SHOTS / "in-production-scheduling.png"), full_page=True)

    # Double-click the no-due run's Due chip -> date input -> set a date -> table refreshes.
    # Each row has two editable chips (priority then due); the due chip is the second.
    first_due = page.locator("#mfg-table tbody tr").first.locator(".editable-cell").nth(1)
    first_due.dblclick()
    inp = page.locator("#mfg-table input[name=due_date]")
    inp.wait_for(state="visible", timeout=5000)
    inp.fill("2027-03-15")
    inp.dispatch_event("change")
    page.wait_for_selector("#mfg-table:has-text('2027-03-15')", timeout=8000)
    assert any(r.get("due_date") == "2027-03-15" for r in api.get("/manufacturing").json()["items"])
