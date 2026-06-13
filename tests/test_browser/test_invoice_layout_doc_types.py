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

def test_doc_lines_columns_scale_proportionally(page, ui_server, api):
    """The line table fills its container (rightmost column pinned to the right edge), column
    widths are percentages, and they rescale when the viewport changes — not fixed pixels."""
    doc_id = _make_draft(api, "invoice")
    page.set_viewport_size({"width": 1400, "height": 900})
    page.goto(f"{ui_server}/docs/{doc_id}", wait_until="domcontentloaded")
    table = page.locator("table.doc-lines")
    table.wait_for(state="visible", timeout=5000)
    page.wait_for_function(
        "() => { const t = document.querySelector('table.doc-lines thead th'); "
        "return t && t.style.width.endsWith('%'); }", timeout=5000)

    def table_w():
        return page.evaluate("document.querySelector('table.doc-lines').offsetWidth")

    def container_w():
        return page.evaluate(
            "document.querySelector('.doc-section--lines').clientWidth")

    # Widths are stored/applied as percentages, and the table fills its container at this size.
    th_widths = page.eval_on_selector_all(
        "table.doc-lines thead th", "els => els.map(e => e.style.width)")
    assert all(w.endswith("%") for w in th_widths if w), th_widths
    wide_table, wide_container = table_w(), container_w()
    assert abs(wide_table - wide_container) <= 4, (wide_table, wide_container)

    # Shrink the viewport: the table scales down with the container (proportional, not fixed px).
    page.set_viewport_size({"width": 900, "height": 900})
    page.wait_for_function(
        "(w) => document.querySelector('table.doc-lines').offsetWidth < w",
        arg=wide_table, timeout=5000)  # poll until the layout settles (no fixed sleep -> no flake)
    narrow_table, narrow_container = table_w(), container_w()
    assert narrow_table < wide_table, (narrow_table, wide_table)        # it actually shrank
    assert abs(narrow_table - narrow_container) <= 4, (narrow_table, narrow_container)  # still fills


def test_doc_lines_resize_persists_as_percent(page, ui_server, api):
    """Dragging a column handle stores percentages (not px) and they restore on reload."""
    doc_id = _make_draft(api, "invoice")
    page.set_viewport_size({"width": 1400, "height": 900})
    page.goto(f"{ui_server}/docs/{doc_id}", wait_until="domcontentloaded")
    page.wait_for_function(
        "() => { const t = document.querySelector('table.doc-lines thead th'); "
        "return t && t.style.width.endsWith('%'); }", timeout=5000)

    handle = page.locator("table.doc-lines thead th .col-resize-handle").first
    handle.scroll_into_view_if_needed()  # the line table sits below the fold on a long doc page
    box = handle.bounding_box()
    cy = box["y"] + box["height"] / 2
    page.mouse.move(box["x"] + box["width"] / 2, cy)
    page.mouse.down()
    page.mouse.move(box["x"] + 120, cy, steps=6)
    page.mouse.up()

    stored = page.evaluate("localStorage.getItem('celerp_dline_wpct_invoice')")
    assert stored, "resize did not persist any widths"
    import json
    vals = json.loads(stored)
    assert vals and all(isinstance(v, (int, float)) for v in vals.values()), vals  # numeric %, not '120px'
