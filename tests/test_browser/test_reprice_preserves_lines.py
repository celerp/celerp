# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Regression: changing a document's price list must not drop line items.

Bug: switching the price list on a memo runs celerpReprice, which first persists the
line table by scraping the DOM (_celerpCollectLines). Its keep-guard was
`desc || sku || price`, so a scanned loose-stone line - barcode + item link but no
catalog sku, no description, and a zero price until it is priced - was silently
discarded before the reprice ran. These unpriced stones are exactly the lines a user
switches the price list to price, so a 72-stone memo lost every unpriced stone the
moment the list changed. The same collector backs auto-save and finalize, so the loss
was not unique to reprice.
"""
from __future__ import annotations

import time

import pytest

pytestmark = pytest.mark.browser


@pytest.fixture()
def memo(fresh_company):
    """An isolated company with a derived price list and a draft memo holding one
    ordinary catalog line plus one unpriced, barcode-only stone line."""
    api = fresh_company
    r = api.patch("/companies/me/price-lists", json={
        "price_lists": [{"name": "Retail"}, {"name": "Wholesale", "multiplier": 0.6}],
        "base_price_list": "Retail",
    })
    assert r.status_code in {200, 201}, r.text
    r = api.post("/docs", json={
        "doc_type": "memo",
        "status": "draft",
        "price_list": "Retail",
        "line_items": [
            {"sku": "WIDGET-1", "name": "Widget", "quantity": 1, "unit_price": 10.0, "line_total": 10.0},
            # A scanned loose stone: barcode only, no catalog sku or name, not yet priced.
            {"barcode": "STONE-1", "quantity": 1, "unit_price": 0.0, "line_total": 0.0},
        ],
        "total": 10.0,
    })
    assert r.status_code in {200, 201}, r.text
    return api, r.json()["id"]


def _line_items(api, doc_id):
    r = api.get(f"/docs/{doc_id}")
    assert r.status_code == 200, r.text
    return r.json()


def test_reprice_keeps_unpriced_barcode_lines(page, ui_server, memo):
    api, doc_id = memo
    assert len(_line_items(api, doc_id)["line_items"]) == 2

    page.goto(f"{ui_server}/docs/{doc_id}", wait_until="domcontentloaded")
    page.wait_for_selector("#line-body tr [data-name='unit_price']", timeout=8000)
    assert page.locator("#line-body tr").count() == 2

    # The reported action: switch the price list. celerpReprice persists the lines,
    # reprices against the new list, then reloads. Assert on stored state (the reprice
    # sets price_list=Wholesale) to avoid racing the reload.
    page.select_option("#doc-price-list", "Wholesale")

    deadline = time.time() + 10
    doc = _line_items(api, doc_id)
    while doc.get("price_list") != "Wholesale" and time.time() < deadline:
        time.sleep(0.2)
        doc = _line_items(api, doc_id)
    assert doc.get("price_list") == "Wholesale", "reprice did not run within the time budget"

    lines = doc["line_items"]
    barcodes = [li.get("barcode") for li in lines]
    assert len(lines) == 2, f"reprice dropped a line item: {lines!r}"
    assert "STONE-1" in barcodes, f"the unpriced barcode line was dropped: {barcodes!r}"
