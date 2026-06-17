# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Browser tests for the PROPORTIONAL drag-to-resize invoice line columns.

The invoice line table (``table.doc-lines``) fills its container (``width:100%; table-layout:fixed``)
and holds each column width as a PERCENTAGE of the table (see the shared ``col_resize_script``).
Dragging a column's handle widens THAT column and narrows its right-hand neighbour by the same amount,
so the columns always sum to 100% — the table stays the container width with NO horizontal overflow.
Widths persist across reload (localStorage, keyed ``celerp_dline_wpct_*``).

  RESIZE-01: dragging a handle widens the column and narrows its right neighbour (proportional);
             the table width is unchanged (no overflow). The new width survives a reload.
  RESIZE-02: dragging a column far wider does NOT overflow the wrap and does NOT move the table's
             left edge — the neighbour absorbs the change down to its floor.
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


def _right_neighbour_width(page):
    """Width of the column immediately to the right of col-desc (the one the drag narrows)."""
    return page.evaluate(
        "() => { const ths=[...document.querySelectorAll('table.doc-lines thead th')];"
        " const i=ths.findIndex(h => h.classList.contains('col-desc'));"
        " return ths[i+1].getBoundingClientRect().width; }"
    )


def _clear_saved_widths(page):
    page.evaluate("() => Object.keys(localStorage)"
                  ".filter(k => k.indexOf('celerp_dline_w') === 0)"
                  ".forEach(k => localStorage.removeItem(k))")


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


def test_invoice_column_resize_proportional_and_persists(page, ui_server, resize_doc_id):
    page.goto(f"{ui_server}/docs/{resize_doc_id}", wait_until="domcontentloaded")
    page.wait_for_selector("table.doc-lines thead th.col-desc .col-resize-handle", timeout=5000)
    _clear_saved_widths(page)
    page.reload(wait_until="domcontentloaded")
    page.wait_for_selector("table.doc-lines thead th.col-desc .col-resize-handle", timeout=5000)
    page.wait_for_timeout(250)  # let the requestAnimationFrame baseline run

    desc = "table.doc-lines thead th.col-desc"
    table = "table.doc-lines"

    desc_before = _col_width(page, desc)
    nbr_before = _right_neighbour_width(page)
    table_before = _col_width(page, table)

    # RESIZE-01: drag the description column's handle 140px to the right.
    _drag_handle(page, desc, 140)
    desc_after = _col_width(page, desc)
    nbr_after = _right_neighbour_width(page)
    table_after = _col_width(page, table)

    assert desc_after > desc_before + 60, (
        f"description column did not widen on drag: {desc_before:.0f} -> {desc_after:.0f}"
    )
    # proportional: the right neighbour narrows (it is NOT left unchanged)
    assert nbr_after < nbr_before - 40, (
        f"right neighbour did not narrow — resize is not proportional: {nbr_before:.0f} -> {nbr_after:.0f}"
    )
    # the table still fills its container (no overflow, no stretch)
    assert abs(table_after - table_before) < 6, (
        f"table width changed on resize (should stay container width): {table_before:.0f} -> {table_after:.0f}"
    )

    # RESIZE persistence: the widened size must survive a full reload (saved as %).
    page.reload(wait_until="domcontentloaded")
    page.wait_for_selector(desc, timeout=5000)
    page.wait_for_timeout(250)  # let the requestAnimationFrame restore run
    desc_reload = _col_width(page, desc)
    assert abs(desc_reload - desc_after) < 8, (
        f"resized width did not persist across reload: set {desc_after:.0f}, after reload {desc_reload:.0f}"
    )


def test_invoice_column_resize_does_not_overflow(page, ui_server, resize_doc_id):
    """Proportional resize keeps the table at the container width: dragging a column far wider must
    NOT overflow the scroll wrap and must NOT move the table's left edge — the right neighbour
    absorbs the change down to its minimum."""
    page.goto(f"{ui_server}/docs/{resize_doc_id}", wait_until="domcontentloaded")
    page.wait_for_selector("table.doc-lines thead th.col-desc .col-resize-handle", timeout=5000)
    _clear_saved_widths(page)
    page.reload(wait_until="domcontentloaded")
    page.wait_for_selector("table.doc-lines thead th.col-desc .col-resize-handle", timeout=5000)
    page.wait_for_timeout(250)

    desc = "table.doc-lines thead th.col-desc"
    table = "table.doc-lines"

    table_left_before = page.locator(table).first.evaluate("el => el.getBoundingClientRect().left")
    nbr_before = _right_neighbour_width(page)

    # drag the description column far wider than its neighbour
    _drag_handle(page, desc, 500)

    table_left_after = page.locator(table).first.evaluate("el => el.getBoundingClientRect().left")
    assert abs(table_left_after - table_left_before) < 3, (
        f"table left edge moved on resize: {table_left_before:.0f} -> {table_left_after:.0f}"
    )
    # the neighbour narrowed (proportional give-and-take)
    assert _right_neighbour_width(page) < nbr_before - 40, "right neighbour did not narrow"
    # no horizontal overflow — the table fills the wrap, it does not scroll
    overflow = page.evaluate(
        "() => { var w = document.querySelector('table.doc-lines').closest('.table-scroll-wrap');"
        " return w.scrollWidth - w.clientWidth; }"
    )
    assert overflow < 6, f"proportional resize must not overflow the wrap: scrollWidth-clientWidth={overflow}"
