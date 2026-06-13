# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The invoice line-table design (single-level header, PCS/WEIGHT inline in the
description cell, unit merged into the quantity cell) is shared by the outbound,
item-priced doc types: invoice, "Consignment Out" (memo) and "Lists" (list).

These assert memo + list drafts render with that layout, like invoices do.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.browser


def _make_draft(api, doc_type):
    r = api.post("/docs", json={
        "doc_type": doc_type,
        "status": "draft",
        "line_items": [{"name": "Widget", "quantity": 2, "unit_price": 5.0, "line_total": 10.0}],
        "total": 10.0,
    })
    assert r.status_code in {200, 201}, f"create {doc_type} failed: {r.text}"
    return r.json()["id"]


@pytest.mark.parametrize("doc_type", ["invoice", "memo", "list"])
def test_outbound_docs_use_invoice_line_layout(page, ui_server, api, doc_type):
    doc_id = _make_draft(api, doc_type)
    page.goto(f"{ui_server}/docs/{doc_id}", wait_until="domcontentloaded")
    page.wait_for_selector("table.doc-lines", timeout=5000)

    # the invoice-layout style hook is applied
    assert page.locator("table.doc-lines.doc-lines--invoice").count() == 1, (
        f"{doc_type}: line table is missing the doc-lines--invoice layout class"
    )
    # PCS/WEIGHT live inline in the description cell
    assert page.locator("table.doc-lines .desc-measures").count() >= 1, (
        f"{doc_type}: description cell has no inline .desc-measures"
    )
    # the unit is merged into the quantity cell, so there is no standalone UNIT column
    assert page.locator("table.doc-lines thead th.col-unit").count() == 0, (
        f"{doc_type}: a standalone UNIT column is present (unit should merge into qty)"
    )
    assert page.locator("table.doc-lines .qty-unit-wrap").count() >= 1, (
        f"{doc_type}: quantity cell is missing the merged .qty-unit-wrap"
    )


@pytest.mark.parametrize("doc_type", ["invoice", "memo"])
def test_print_view_preserves_weight_unit(page, ui_server, api, doc_type):
    """The /print view must show the weight WITH its unit (sourced from the parcel),
    for every invoice-layout doc type — previously only invoices were enriched, so a
    memo printed the weight value with no unit."""
    r = api.post("/items", json={
        "sku": f"RZ-WU-{doc_type}", "name": "Weighted Widget", "quantity": 5,
        "sell_by": "piece", "weight": 4.5, "weight_unit": "carat",
    })
    item_id = r.json()["id"]
    d = api.post("/docs", json={
        "doc_type": doc_type, "status": "draft",
        "line_items": [{"sku": f"RZ-WU-{doc_type}", "entity_id": item_id,
                        "quantity": 2, "unit_price": 1.0, "line_total": 2.0}],
        "total": 2.0,
    })
    doc_id = d.json()["id"]
    page.goto(f"{ui_server}/docs/{doc_id}/print", wait_until="domcontentloaded")
    body = page.content()
    assert "4.5 carat" in body, (
        f"{doc_type} print dropped the weight unit; expected '4.5 carat' in the print HTML"
    )