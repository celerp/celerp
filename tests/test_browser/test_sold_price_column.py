# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Browser check for the sold-inventory 'Sold' per-unit price column.

Seeds real sold items (item -> finalized invoice -> fulfill-lines), then loads
the sold inventory view and asserts the realized per-unit sale price is shown
in a read-only 'Sold' column beside the catalog price columns. A screenshot is
captured for visual review.
"""
from __future__ import annotations

import os
import uuid

import pytest

pytestmark = pytest.mark.browser

# A review screenshot is written only when SOLD_PRICE_SHOT_DIR is set; the DOM
# assertions below are the durable test value and always run.
_OUT = os.environ.get("SOLD_PRICE_SHOT_DIR")


def _sell_item(api, sku, unit_price, qty=1, sell_by="carat"):
    """Create an item and sell it via a finalized invoice; return its entity_id."""
    r = api.post("/items", json={"status": "available", "sku": sku, "name": sku,
                                 "quantity": qty, "sell_by": sell_by})
    assert r.status_code in {200, 201}, f"create item failed: {r.text}"
    item_id = r.json()["id"]
    r = api.post("/docs", json={
        "doc_type": "invoice",
        "ref_id": f"SOLD-{uuid.uuid4().hex[:6]}",
        "status": "draft",
        "line_items": [{"sku": sku, "name": sku, "quantity": qty, "unit_price": unit_price,
                        "line_total": unit_price * qty, "entity_id": item_id}],
        "total": unit_price * qty,
        "amount_outstanding": unit_price * qty,
    })
    assert r.status_code in {200, 201}, f"create doc failed: {r.text}"
    doc_id = r.json()["id"]
    assert api.post(f"/docs/{doc_id}/finalize").status_code in {200, 201}
    r = api.post(f"/docs/{doc_id}/fulfill-lines", json={"line_entity_ids": [item_id]})
    assert r.status_code in {200, 201}, f"fulfil failed: {r.text}"
    assert api.get(f"/items/{item_id}").json()["status"] == "sold"
    return item_id


def test_sold_view_shows_realized_price_column(page, ui_server, api):
    sku_a = f"SP-A-{uuid.uuid4().hex[:5]}"
    sku_b = f"SP-B-{uuid.uuid4().hex[:5]}"
    _sell_item(api, sku_a, unit_price=1250.0)
    _sell_item(api, sku_b, unit_price=89.96)

    # Wide enough that every column (including the trailing Sold column) is in view
    # for the review screenshot; the assertions below query the DOM regardless.
    page.set_viewport_size({"width": 2400, "height": 900})
    page.goto(f"{ui_server}/inventory?status=sold", wait_until="domcontentloaded")
    body = page.locator("body").inner_text()
    assert "Internal Server Error" not in body and "Traceback" not in body

    # The synthetic read-only money column is headed "Sold" (with the shared
    # "(Unit Price)" annotation the other money headers carry).
    header = page.locator('th[data-col="sold_price"], th:has-text("Sold")')
    assert header.count() > 0, "sold-price column header missing from sold view"

    # Realized per-unit prices render in read-only sold_price cells (never clickable).
    cells = page.locator('td[data-col="sold_price"]')
    assert cells.count() > 0, "no sold_price cells rendered"
    texts = " ".join((cells.nth(i).inner_text() or "") for i in range(cells.count()))
    assert "1,250" in texts, f"expected 1,250.00 in sold cells, got: {texts!r}"
    assert "89.96" in texts, f"expected 89.96 in sold cells, got: {texts!r}"
    # Read-only: no click-to-edit affordance on the sold price cell.
    assert page.locator('td[data-col="sold_price"].cell--clickable').count() == 0, \
        "sold_price cell must be read-only (no cell--clickable)"

    if _OUT:
        os.makedirs(_OUT, exist_ok=True)
        shot = os.path.join(_OUT, "sold-price-column.png")
        cells.first.scroll_into_view_if_needed()
        page.screenshot(path=shot, full_page=True)
        print(f"SOLD_PRICE_SCREENSHOT={shot}")


def test_item_detail_pricing_tab_shows_sold_price(page, ui_server, api):
    sku = f"SP-D-{uuid.uuid4().hex[:5]}"
    item_id = _sell_item(api, sku, unit_price=432.10)

    page.goto(f"{ui_server}/inventory/{item_id}?tab=pricing", wait_until="domcontentloaded")
    body = page.locator("body").inner_text()
    assert "Internal Server Error" not in body and "Traceback" not in body
    card = page.locator(".detail-card:has-text('Sold price')")
    assert card.count() > 0, "Sold price card missing from a sold item's pricing tab"
    assert "432.1" in card.inner_text()


def test_item_detail_pricing_tab_omits_sold_price_when_available(page, ui_server, api):
    sku = f"SP-U-{uuid.uuid4().hex[:5]}"
    r = api.post("/items", json={"status": "available", "sku": sku, "name": sku,
                                 "quantity": 1, "sell_by": "carat"})
    assert r.status_code in {200, 201}, r.text
    item_id = r.json()["id"]

    page.goto(f"{ui_server}/inventory/{item_id}?tab=pricing", wait_until="domcontentloaded")
    assert page.locator(".detail-card:has-text('Sold price')").count() == 0, \
        "Sold price card must not appear for an item that has not been sold"
