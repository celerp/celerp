# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Browser test: a document's Manufacturing panel is product-centric — it links each
manufactured line item to its product Manufacturing tab and shows the JIT components summary.
Runs are not auto-created from the document."""
from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.browser

SHOTS = Path("context/reviews/manufacturing")


@pytest.fixture(scope="module")
def doc_with_mfg(api):
    """Gold + ring (recipe: 5 gold) + plain widget; an invoice with ring x2 and widget x1."""
    gold = api.post("/items", json={"sku": "D-GOLD", "name": "Gold 1g", "quantity": 100, "sell_by": "gram",
                                    "cost_total": 8000, "inventory_type": "component"}).json()["id"]
    ring = api.post("/items", json={"sku": "D-RING", "name": "Gold Ring", "quantity": 0, "sell_by": "piece"}).json()["id"]
    widget = api.post("/items", json={"sku": "D-WIDGET", "name": "Plain Widget", "quantity": 5, "sell_by": "piece"}).json()["id"]
    api.put(f"/manufacturing/items/{ring}/recipe",
            json={"output_qty": 1, "components": [{"item_id": gold, "quantity": 5}], "labor": [], "overhead": []})
    doc = api.post("/docs", json={"doc_type": "invoice", "line_items": [
        {"item_id": ring, "sku": "D-RING", "name": "Gold Ring", "quantity": 2, "unit_price": 500},
        {"item_id": widget, "sku": "D-WIDGET", "name": "Plain Widget", "quantity": 1, "unit_price": 20},
    ], "total": 1020})
    return doc.json()["id"], gold, ring


def test_doc_panel_is_product_centric(page, ui_server, api, doc_with_mfg):
    SHOTS.mkdir(parents=True, exist_ok=True)
    doc_id, gold, ring = doc_with_mfg
    page.set_viewport_size({"width": 1440, "height": 1100})

    page.goto(f"{ui_server}/docs/{doc_id}", wait_until="domcontentloaded")
    page.wait_for_selector("#doc-mfg-panel:has-text('Items to manufacture')", timeout=10000)
    panel = page.locator("#doc-mfg-panel").inner_text()
    # Product-centric: no auto-created orders, links to the product Manufacturing tab instead.
    assert "Create manufacturing order" not in panel and "Manufacturing orders" not in panel
    assert "Items to manufacture" in panel
    # JIT summary: 2 rings need 10 gold.
    assert "Raw materials required" in panel and "10" in panel
    # The manufactured item links to its product Manufacturing tab.
    assert page.locator(f"#doc-mfg-panel a[href*='{ring}'][href*='tab=manufacturing']").count() >= 1
    page.screenshot(path=str(SHOTS / "doc-panel.png"), full_page=True)

    # No runs are auto-created from the document.
    runs = [o for o in api.get("/manufacturing").json()["items"] if o.get("source_doc_id") == doc_id]
    assert runs == []
