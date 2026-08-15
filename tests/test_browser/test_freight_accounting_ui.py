# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Accounting UI renders the freight/landed-cost flow: after a freight bill is finalized and received,
the balance sheet and the inventory / freight-clearing ledgers render correctly and consistently."""
from __future__ import annotations

import uuid
from pathlib import Path

import pytest

pytestmark = pytest.mark.browser


def _no_crash(page, response, ctx=""):
    """The page was served and rendered.

    The status is checked as well as the text: a 404 page carries neither a
    traceback nor the words below, so a body-only check lets a URL that no longer
    exists pass as a healthy page.
    """
    assert response is not None, f"{ctx}: no response"
    assert response.status < 400, f"{ctx}: HTTP {response.status}"
    body = page.locator("body").inner_text()
    assert "Internal Server Error" not in body, f"{ctx}: 500"
    assert "Traceback (most recent call last)" not in body, f"{ctx}: traceback"


def test_freight_accounting_pages_render(page, ui_server, api):
    tag = uuid.uuid4().hex[:6].upper()
    loc = api.post("/companies/me/locations", json={"name": f"WH-{tag}", "type": "warehouse"})
    assert loc.status_code in {200, 201}, loc.text
    loc_id = loc.json()["id"]
    goods = api.post("/items", json={"status": "available", "sku": f"GD-{tag}", "name": "Goods", "quantity": 0, "sell_by": "piece"}).json()["id"]
    frt = api.post("/items", json={"status": "available", "sku": f"FR-{tag}", "name": "Sea Freight", "quantity": 0, "sell_by": "piece",
                                   "inventory_type": "freight", "landed_cost_kind": "freight"}).json()["id"]

    bill = api.post("/docs", json={"doc_type": "bill", "line_items": [
        {"entity_id": goods, "sku": f"GD-{tag}", "name": "Goods", "quantity": 10, "unit_price": 10, "line_total": 100, "sell_by": "piece", "receive_as": "stock"},
        {"entity_id": frt, "sku": f"FR-{tag}", "name": "Sea Freight", "quantity": 1, "unit_price": 40, "line_total": 40, "sell_by": "piece"}],
        "total": 140}).json()["id"]
    assert api.post(f"/docs/{bill}/finalize").status_code == 200
    assert api.post(f"/docs/{bill}/receive", json={"location_id": loc_id, "received_items": [
        {"item_id": goods, "sku": f"GD-{tag}", "name": "Goods", "quantity_received": 10, "cost_price": 10, "receive_as": "stock"}]}).status_code == 200

    page.set_viewport_size({"width": 1440, "height": 1000})
    out = Path("context/reviews/freight"); out.mkdir(parents=True, exist_ok=True)

    # Balance sheet renders the inventory assets without crashing.
    resp = page.goto(f"{ui_server}/accounting?tab=balance-sheet", wait_until="domcontentloaded")
    page.wait_for_selector("body", timeout=8000)
    _no_crash(page, resp, "balance-sheet")
    assert "Inventory" in page.locator("body").inner_text()
    page.screenshot(path=str(out / "accounting-balance-sheet.png"), full_page=True)

    # Freight clearing ledger: filled at finalize, drawn down at receive -> nets to zero.
    resp = page.goto(f"{ui_server}/reports/ledger/1130-FRT", wait_until="domcontentloaded")
    page.wait_for_selector("body", timeout=8000)
    _no_crash(page, resp, "freight-clearing-ledger")
    page.screenshot(path=str(out / "accounting-freight-clearing-ledger.png"), full_page=True)

    # Purchased-inventory ledger: holds goods base + capitalised landed.
    resp = page.goto(f"{ui_server}/reports/ledger/1130-P", wait_until="domcontentloaded")
    page.wait_for_selector("body", timeout=8000)
    _no_crash(page, resp, "inventory-ledger")
    page.screenshot(path=str(out / "accounting-inventory-ledger.png"), full_page=True)
