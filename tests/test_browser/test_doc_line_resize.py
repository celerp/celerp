# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Browser tests for resizable invoice line-item columns.

The invoice line table (``table.doc-lines``) has drag-to-resize column headers
(the shared ``col_resize_script``). These assert the two properties that manual
testing kept getting wrong:

  RESIZE-01: dragging a column's handle widens THAT column only — the neighbour
             to its right is unchanged (one-sided; no redistribution).
  RESIZE-02: the new width survives a full page reload (localStorage persistence).
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.browser


@pytest.fixture(scope="module")
def resize_doc_id(api):
    r = api.post("/docs", json={
        "doc_type": "invoice",
        "ref_id": "RESIZE-001",
        "status": "draft",
        "line_items": [
            {"name": "Alpha", "quantity": 2, "unit_price": 10.0, "line_total": 20.0},
            {"name": "Beta", "quantity": 1, "unit_price": 5.0, "line_total": 5.0},
        ],
        "total": 25.0,
    })
    assert r.status_code in {200, 201}, f"create draft failed: {r.text}"
    return r.json()["id"]


def _col_width(page, sel):
    """Rendered border-box width of the first element matching `sel`."""
    return page.locator(sel).first.evaluate("el => el.getBoundingClientRect().width")


def _drag_handle(page, th_sel, dx):
    """Drag the resize handle inside `th_sel` horizontally by `dx` px."""
    handle = page.locator(f"{th_sel} .col-resize-handle").first
    # The line table sits well down a long page; bring the handle into the
    # viewport so mouse coordinates (viewport-relative) actually land on it.
    handle.scroll_into_view_if_needed()
    box = handle.bounding_box()
    assert box is not None, f"no .col-resize-handle inside {th_sel}"
    cx = box["x"] + box["width"] / 2
    cy = box["y"] + box["height"] / 2
    page.mouse.move(cx, cy)
    page.mouse.down()
    # move in steps so the document mousemove listener fires repeatedly
    page.mouse.move(cx + dx, cy, steps=12)
    page.mouse.up()


def test_invoice_column_resize_one_sided_and_persists(page, ui_server, resize_doc_id):
    page.goto(f"{ui_server}/docs/{resize_doc_id}", wait_until="domcontentloaded")
    # the resize script adds handles on load — wait for one to exist
    page.wait_for_selector("table.doc-lines thead th.col-desc .col-resize-handle", timeout=5000)

    desc = "table.doc-lines thead th.col-desc"
    qty = "table.doc-lines thead th.col-qty"

    desc_before = _col_width(page, desc)
    qty_before = _col_width(page, qty)

    # RESIZE-01: drag the description column's handle 140px to the right.
    _drag_handle(page, desc, 140)
    desc_after = _col_width(page, desc)
    qty_after = _col_width(page, qty)

    assert desc_after > desc_before + 80, (
        f"description column did not widen on drag: {desc_before:.0f} -> {desc_after:.0f}"
    )
    assert abs(qty_after - qty_before) < 3, (
        f"neighbour (qty) column changed — resize is not one-sided: "
        f"{qty_before:.0f} -> {qty_after:.0f}"
    )

    # RESIZE-02: the widened size must survive a full reload.
    page.reload(wait_until="domcontentloaded")
    page.wait_for_selector(desc, timeout=5000)
    page.wait_for_timeout(250)  # let the requestAnimationFrame restore run
    desc_reload = _col_width(page, desc)
    assert abs(desc_reload - desc_after) < 5, (
        f"resized width did not persist across reload: set {desc_after:.0f}, "
        f"after reload {desc_reload:.0f}"
    )


def test_invoice_column_resize_overflows_right_not_both_sides(page, ui_server, resize_doc_id):
    """Expanding a column past the section must overflow to the RIGHT (the wrap
    scrolls) — the table's left edge must NOT move (no stretch on both sides),
    like the inventory table."""
    page.goto(f"{ui_server}/docs/{resize_doc_id}", wait_until="domcontentloaded")
    page.wait_for_selector("table.doc-lines thead th.col-desc .col-resize-handle", timeout=5000)
    # start clean so a leftover persisted width doesn't skew the geometry
    page.evaluate("() => Object.keys(localStorage)"
                  ".filter(k => k.indexOf('celerp_dline_w_') === 0)"
                  ".forEach(k => localStorage.removeItem(k))")
    page.reload(wait_until="domcontentloaded")
    page.wait_for_selector("table.doc-lines thead th.col-desc .col-resize-handle", timeout=5000)

    desc = "table.doc-lines thead th.col-desc"
    table = "table.doc-lines"

    table_left_before = page.locator(table).first.evaluate("el => el.getBoundingClientRect().left")

    # drag the description column far wider than the section
    _drag_handle(page, desc, 500)

    table_left_after = page.locator(table).first.evaluate("el => el.getBoundingClientRect().left")
    assert abs(table_left_after - table_left_before) < 3, (
        f"table left edge moved on resize — it is stretching both sides: "
        f"{table_left_before:.0f} -> {table_left_after:.0f}"
    )

    # the scroll wrap now overflows horizontally (content wider than the viewport)
    overflow = page.evaluate(
        "() => { var w = document.querySelector('table.doc-lines').closest('.table-scroll-wrap');"
        " return w.scrollWidth - w.clientWidth; }"
    )
    assert overflow > 20, f"scroll wrap is not overflowing to the right: scrollWidth-clientWidth={overflow}"