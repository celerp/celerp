# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Browser test + screenshot: the product Manufacturing tab production hub - the Work orders table
(each row linked to the order it fulfils) and the per-row Action dropdown that advances a run."""
from __future__ import annotations

import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.browser

SHOTS = Path("context/reviews/manufacturing")


def _poll_run_status(api, run_id: str, want: str, timeout: float = 45.0) -> dict:
    """Poll the /manufacturing board until the named work order reaches `want`. Returns its row."""
    deadline = time.time() + timeout
    last: dict = {}
    while time.time() < deadline:
        items = {o["id"]: o for o in api.get("/manufacturing").json()["items"]}
        last = items.get(run_id, {})
        if str(last.get("status") or "").lower() == want:
            return last
        time.sleep(0.3)
    raise AssertionError(f"work order {run_id} never reached {want!r}; last status={last.get('status')!r}")


def test_product_hub_work_orders_and_action_dropdown(page, ui_server, api):
    SHOTS.mkdir(parents=True, exist_ok=True)
    gold = api.post("/items", json={"sku": "HUB-GOLD", "name": "Gold 1g", "quantity": 100, "sell_by": "gram",
                                    "cost_total": 8000, "inventory_type": "component"}).json()["id"]
    ring = api.post("/items", json={"sku": "HUB-RING", "name": "18K Ring", "quantity": 0, "sell_by": "piece"}).json()["id"]
    api.put(f"/manufacturing/items/{ring}/recipe",
            json={"output_qty": 1, "components": [{"item_id": gold, "quantity": 5}], "labor": [], "overhead": []})
    # Finalize the invoice so it counts as demand (a draft invoice is a pro-forma, excluded).
    inv = api.post("/docs", json={"doc_type": "invoice", "line_items": [
        {"item_id": ring, "sku": "HUB-RING", "name": "18K Ring", "quantity": 2, "unit_price": 900}], "total": 1800}).json()
    api.post(f"/docs/{inv['id']}/finalize")

    # Setup only: create the work order linked 1:1 to the invoice via the make endpoint. The action
    # under test (advancing the run) is driven through the real UI dropdown below.
    board = {r["item_id"]: r for r in api.get("/manufacturing/to-make").json()["items"]}
    doc_id = board[ring]["docs"][0]["doc_id"]
    made = api.post("/manufacturing/to-make/make",
                    json={"lines": [{"item_id": ring, "doc_id": doc_id}]}).json()
    run_id = made["created"][0]["run_id"]
    inv_no = api.get(f"/manufacturing/{run_id}").json()["source_doc_number"]
    assert inv_no.startswith("INV-"), f"work order not linked to a finalized invoice: {inv_no!r}"

    page.set_viewport_size({"width": 1440, "height": 1000})
    page.goto(f"{ui_server}/inventory/{ring}?tab=manufacturing", wait_until="domcontentloaded")
    page.wait_for_selector("#production-block table.data-table", timeout=10000)

    block = page.locator("#production-block")
    # Open demand block still shows the invoice (the document that wants this product).
    assert "Open demand" in block.inner_text()

    # The Work orders table shows a row with the source-order link (the INV- number) + a status badge.
    # The hub has two data-tables (Open demand + Work orders); the WO table is the one with .wo-ref.
    assert "Work orders" in block.inner_text()
    wo_table = block.locator("table.data-table:has(.wo-ref)")
    assert wo_table.count() == 1, "Work orders table not found"
    src_link = wo_table.locator(f"a.table-link:has-text('{inv_no}')")
    assert src_link.count() == 1, f"Source order link to {inv_no} not found in Work orders table"
    assert wo_table.locator(".badge:has-text('Planned')").count() >= 1, "status badge not rendered"

    page.screenshot(path=str(SHOTS / "hub-work-orders.png"), full_page=True)

    # Drive the Action dropdown through the real UI: Start -> the run goes in_progress.
    select = block.locator(".wo-action-select").first
    select.select_option(value="start")
    in_prog = _poll_run_status(api, run_id, "in_progress")
    assert in_prog["status"] == "in_progress"

    # The refreshed block now offers Complete in the same dropdown; choosing it closes the run,
    # issues components and receives the finished good.
    page.wait_for_selector("#production-block .wo-action-select", timeout=8000)
    block.locator(".wo-action-select").first.select_option(value="complete")
    done = _poll_run_status(api, run_id, "completed")
    assert done["status"] == "completed"

    # Finished stock went up; the gold component was consumed (5 per ring * 2 = 10 g).
    assert api.get(f"/items/{ring}").json()["quantity"] == 2.0
    assert api.get(f"/items/{gold}").json()["quantity"] == 90.0


def test_hub_work_orders_sort_filter_coverage(page, ui_server, api):
    """The hub Work orders + Open demand tables: sortable headers, Excel funnels, and a Coverage
    column on demand."""
    gold = api.post("/items", json={"sku": "HF-GOLD", "name": "Gold", "quantity": 100, "sell_by": "gram",
                                    "cost_total": 8000, "inventory_type": "component"}).json()["id"]
    ring = api.post("/items", json={"sku": "HF-RING", "name": "Ring", "quantity": 0, "sell_by": "piece"}).json()["id"]
    api.put(f"/manufacturing/items/{ring}/recipe",
            json={"output_qty": 1, "components": [{"item_id": gold, "quantity": 5}], "labor": [], "overhead": []})
    for q in (1, 2):  # two orders -> two demand lines -> two work orders (qty 1 and 2)
        inv = api.post("/docs", json={"doc_type": "invoice", "total": q, "line_items": [
            {"item_id": ring, "sku": "HF-RING", "name": "Ring", "quantity": q, "unit_price": 1}]}).json()
        api.post(f"/docs/{inv['id']}/finalize")
    board = {r["item_id"]: r for r in api.get("/manufacturing/to-make").json()["items"]}
    lines = [{"item_id": ring, "doc_id": d["doc_id"]} for d in board[ring]["docs"]]
    api.post("/manufacturing/to-make/make", json={"lines": lines})

    page.set_viewport_size({"width": 1440, "height": 1000})
    page.goto(f"{ui_server}/inventory/{ring}?tab=manufacturing", wait_until="domcontentloaded")
    page.wait_for_selector("#wo-orders-table", timeout=10000)

    # Two active work orders; Status funnel + sortable headers present.
    assert page.locator("#wo-orders-table tbody tr.data-row").count() == 2
    assert page.locator("#wo-orders-table .colfilter[data-col='5']").count() == 1   # Status funnel
    assert page.locator("#wo-orders-table thead th[data-sort='3']").count() == 1    # Qty sortable

    # Open demand has a Coverage column with coverage badges.
    assert "COVERAGE" in page.locator("#wo-demand-table thead").inner_text().upper()
    assert page.locator("#wo-demand-table .badge[class*='peg-']").count() >= 1

    # Sort the work orders by Qty ascending -> first visible row's qty cell is the smaller (1).
    page.locator("#wo-orders-table thead th[data-sort='3']").click()
    first_qty = page.locator("#wo-orders-table tbody tr.data-row:not(.enh-page-hidden) td:nth-child(4)").first.inner_text().strip()
    assert first_qty == "1", f"qty-ascending sort failed; first row qty={first_qty!r}"
