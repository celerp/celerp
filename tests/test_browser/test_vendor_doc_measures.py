# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Vendor document types must not show the inline Pieces/Weight sub-lines under the description.

Those secondary-measure sub-lines are an invoice-layout (sales) affordance; on a finalized vendor
document (purchase order / bill / consignment in) the editable view already omits them, but the
finalized view leaked them. This pins the finalized view to match.
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.browser

SHOTS = Path("context/reviews/documents")

# qty is in pieces, but the line carries a net weight -> a "- 5 gram" measure sub-line would render.
_LINE = {"name": "Widget", "quantity": 2, "unit_price": 10.0, "sell_by": "piece",
         "weight": 5, "weight_unit": "gram", "line_total": 20.0}


def _finalized(api, doc_type):
    doc_id = api.post("/docs", json={"doc_type": doc_type, "status": "draft",
                                     "line_items": [dict(_LINE)], "total": 20.0}).json()["id"]
    assert api.post(f"/docs/{doc_id}/finalize").status_code == 200
    return doc_id


def test_invoice_keeps_description_measures_but_vendor_doc_does_not(page, ui_server, api):
    # Control: a finalized invoice (sales) DOES show the measure sub-line.
    inv = _finalized(api, "invoice")
    page.goto(f"{ui_server}/docs/{inv}", wait_until="domcontentloaded")
    page.wait_for_selector("table.doc-lines", timeout=8000)
    assert page.locator("table.doc-lines .li-measure").count() >= 1, "invoice should keep its measure sub-line"
    SHOTS.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(SHOTS / "invoice-with-measures.png"), full_page=True)

    # Vendor doc: a finalized purchase order (converts to bill) must NOT show measure sub-lines.
    po = _finalized(api, "purchase_order")
    page.goto(f"{ui_server}/docs/{po}", wait_until="domcontentloaded")
    page.wait_for_selector("table.doc-lines", timeout=8000)
    assert page.locator("table.doc-lines .li-measure").count() == 0, \
        "vendor document must not show Pieces/Weight sub-lines under the description"
    page.screenshot(path=str(SHOTS / "vendor-doc-no-measures.png"), full_page=True)
