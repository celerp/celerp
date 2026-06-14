# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Browser test + screenshots: the internal Production Order document interface.

Verifies the production-order editor hides every money column (it is invoice-to-self demand, not a
sale) and that the list page renders under the Manufacturing section.
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.browser

SHOTS = Path("context/reviews/manufacturing")


def test_production_order_interface_hides_money(page, ui_server, api):
    SHOTS.mkdir(parents=True, exist_ok=True)
    page.set_viewport_size({"width": 1440, "height": 1000})

    gold = api.post("/items", json={"sku": "POB-GOLD", "name": "Gold 1g", "quantity": 100, "sell_by": "gram",
                                    "cost_total": 8000, "inventory_type": "component"}).json()["id"]
    ring = api.post("/items", json={"sku": "POB-RING", "name": "18K Ring", "quantity": 0, "sell_by": "piece"}).json()["id"]
    api.put(f"/manufacturing/items/{ring}/recipe",
            json={"output_qty": 1, "components": [{"item_id": gold, "quantity": 5}], "labor": [], "overhead": []})
    po = api.post("/docs", json={"doc_type": "production_order", "line_items": [
        {"item_id": ring, "sku": "POB-RING", "name": "18K Ring", "quantity": 4}]}).json()
    po_id = po["id"]
    assert po_id.startswith("doc:PRD-")

    # Editor: the line table must not show any money columns.
    page.goto(f"{ui_server}/docs/{po_id}", wait_until="domcontentloaded")
    page.wait_for_selector(".doc-lines", timeout=10000)
    page.screenshot(path=str(SHOTS / "production-order-editor.png"), full_page=True)
    for money_col in (".doc-lines th.col-unit-price", ".doc-lines th.col-disc",
                      ".doc-lines th.col-tax", ".doc-lines th.col-total"):
        assert not page.locator(money_col).is_visible(), f"{money_col} should be hidden on a production order"
    # The product line is still present (SKU lives in a line input, so check rendered HTML).
    assert "POB-RING" in page.content()
    # No financial totals panel.
    assert page.locator(".total-panel").count() == 0

    # List page renders under its own type.
    page.goto(f"{ui_server}/docs?type=production_order", wait_until="domcontentloaded")
    page.wait_for_selector("body", timeout=8000)
    body = page.locator("body").inner_text()
    assert "Production Orders" in body
    page.screenshot(path=str(SHOTS / "production-order-list.png"), full_page=True)

    # Finalising releases it as demand on the To-Make board.
    assert api.post(f"/docs/{po_id}/finalize").status_code == 200
    board = {r["item_id"]: r for r in api.get("/manufacturing/to-make").json()["items"]}
    assert ring in board and board[ring]["demand"] == 4.0
