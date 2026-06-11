# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Browser test + screenshots for Phase 3 — manufacture from an invoice + JIT summary."""
from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.browser

SHOTS = Path("context/reviews/phase3")


@pytest.fixture(scope="module")
def doc_with_mfg(api):
    """Gold + ring (recipe: 5 gold) + plain widget; an invoice with ring x2 and widget x1."""
    gold = api.post("/items", json={"sku": "D-GOLD", "name": "Gold 1g", "quantity": 100, "sell_by": "gram", "cost_total": 8000}).json()["id"]
    ring = api.post("/items", json={"sku": "D-RING", "name": "Gold Ring", "quantity": 0, "sell_by": "piece"}).json()["id"]
    widget = api.post("/items", json={"sku": "D-WIDGET", "name": "Plain Widget", "quantity": 5, "sell_by": "piece"}).json()["id"]
    api.put(f"/manufacturing/items/{ring}/recipe",
            json={"output_qty": 1, "components": [{"item_id": gold, "quantity": 5}], "labor": [], "overhead": []})
    doc = api.post("/docs", json={"doc_type": "invoice", "line_items": [
        {"item_id": ring, "sku": "D-RING", "name": "Gold Ring", "quantity": 2, "unit_price": 500},
        {"item_id": widget, "sku": "D-WIDGET", "name": "Plain Widget", "quantity": 1, "unit_price": 20},
    ], "total": 1020})
    return doc.json()["id"]


def test_doc_manufacture_flow_with_screenshots(page, ui_server, api, doc_with_mfg):
    SHOTS.mkdir(parents=True, exist_ok=True)
    doc_id = doc_with_mfg
    page.set_viewport_size({"width": 1440, "height": 1100})

    page.goto(f"{ui_server}/docs/{doc_id}", wait_until="domcontentloaded")
    page.wait_for_selector("#doc-mfg-panel button:has-text('Create manufacturing order')", timeout=10000)
    panel = page.locator("#doc-mfg-panel").inner_text()
    assert "Sub-assemblies to build" in panel and "Raw materials required" in panel
    # JIT summary: 2 rings need 10 gold.
    assert "10" in panel
    page.screenshot(path=str(SHOTS / "01-doc-panel.png"), full_page=True)

    page.click("#doc-mfg-panel button:has-text('Create manufacturing order')")
    page.wait_for_selector("#doc-mfg-panel .flash", timeout=8000)
    flash = page.locator("#doc-mfg-panel .flash").inner_text()
    assert "Created 1 manufacturing order" in flash
    assert "Skipped 1" in flash and "no recipe" in flash
    page.screenshot(path=str(SHOTS / "02-orders-created.png"), full_page=True)

    # Exactly one order, with the right inputs (10 gold), linked back to the doc.
    orders = [o for o in api.get("/manufacturing").json()["items"] if o.get("source_doc_id") == doc_id]
    assert len(orders) == 1
    assert orders[0]["inputs"][0]["quantity"] == 10.0
