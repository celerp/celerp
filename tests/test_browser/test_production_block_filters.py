# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Production block filters: due-date from/to ranges (open demand + work orders) and the
Status filter hiding completed/cancelled work orders by default (no toggle button)."""
from __future__ import annotations

import time as _t

import pytest

pytestmark = pytest.mark.browser


def _poll(fn, timeout=6.0):
    deadline = _t.time() + timeout
    while _t.time() < deadline:
        if fn():
            return True
        _t.sleep(0.2)
    return False


def test_production_block_date_filters_and_no_toggle(page, ui_server, api):
    gold = api.post("/items", json={"status": "available", "sku": "PBF-GOLD", "name": "Gold", "quantity": 100, "sell_by": "gram",
                                    "cost_total": 8000, "inventory_type": "component"}).json()["id"]
    ring = api.post("/items", json={"status": "available", "sku": "PBF-RING", "name": "Ring", "quantity": 0, "sell_by": "piece"}).json()["id"]
    api.put(f"/manufacturing/items/{ring}/recipe",
            json={"output_qty": 1, "components": [{"item_id": gold, "quantity": 5}], "labor": [], "overhead": []})
    inv = api.post("/docs", json={"doc_type": "invoice", "due_date": "2030-06-15", "line_items": [
        {"item_id": ring, "sku": "PBF-RING", "name": "Ring", "quantity": 2, "unit_price": 900}], "total": 1800}).json()
    api.post(f"/docs/{inv['id']}/finalize")
    # Make a work order from that demand so the Work orders table renders.
    board = {r["item_id"]: r for r in api.get("/manufacturing/to-make").json()["items"]}
    api.post("/manufacturing/to-make/make", json={"lines": [{"item_id": ring, "doc_id": board[ring]["docs"][0]["doc_id"]}]})

    page.set_viewport_size({"width": 1440, "height": 1100})
    page.goto(f"{ui_server}/inventory/{ring}?tab=manufacturing", wait_until="domcontentloaded")
    page.wait_for_selector("#wo-demand-table tbody tr.data-row", timeout=10000)

    # No 'show/hide completed' toggle remains.
    assert page.locator("#production-block a:has-text('Show completed')").count() == 0
    assert page.locator("#production-block a:has-text('Hide completed')").count() == 0

    # Open demand: the row carries the invoice due date and is visible.
    demand_row = page.locator("#wo-demand-table tbody tr.data-row").first
    assert "2030-06-15" in demand_row.inner_text()
    assert demand_row.is_visible()

    # Due-date 'to' before the due hides the row; widening past it shows it again.
    to_inp = page.locator('.daterange[data-daterange-table="wo-demand-table"] [data-bound=to]')
    to_inp.fill("2020-12-31")
    assert _poll(lambda: not demand_row.is_visible()), "demand row should hide when filtered out by due date"
    to_inp.fill("2031-12-31")
    assert _poll(lambda: demand_row.is_visible()), "demand row should reappear when range widens"

    # The Work orders table has its own due-date range control + a Status funnel.
    assert page.locator('.daterange[data-daterange-table="wo-orders-table"]').count() == 1
    assert page.locator('#wo-orders-table .colfilter[data-col="5"]').count() == 1
