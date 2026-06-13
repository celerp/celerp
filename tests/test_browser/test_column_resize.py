# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Playwright: resizable columns on the inventory and contacts data tables.

Both pages render the shared ``data_table`` (``#data-table``, ``thead
th[data-key]``, drag-to-resize handles persisted to
``localStorage[celerp_col_widths_<entity>]``). These assert that dragging a
column's handle actually widens that column and that the width survives a full
reload — the same properties exercised for the invoice line table in
``test_doc_line_resize.py``.
"""
from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.browser


@pytest.fixture(scope="module")
def _seed_inventory(api):
    for _ in range(2):
        r = api.post("/items", json={
            "sku": f"RZ-{uuid.uuid4().hex[:8]}", "name": "Resize Item",
            "quantity": 5, "sell_by": "piece",
        })
        assert r.status_code in {200, 201}, r.text


@pytest.fixture(scope="module")
def _seed_contacts(api):
    for _ in range(2):
        r = api.post("/crm/contacts", json={
            "name": f"Resize Contact {uuid.uuid4().hex[:6]}", "type": "customer",
        })
        assert r.status_code in {200, 201}, r.text


def _col_width(page, sel):
    return page.locator(sel).first.evaluate("el => el.getBoundingClientRect().width")


def _visible_data_keys(page):
    """data-key of every currently-visible resizable header column, in order."""
    return page.evaluate(
        "() => Array.from(document.querySelectorAll('#data-table thead th[data-key]'))"
        ".filter(th => th.offsetParent !== null).map(th => th.dataset.key)"
    )


def _drag_handle(page, th_sel, dx):
    handle = page.locator(f"{th_sel} .col-resize-handle").first
    handle.scroll_into_view_if_needed()
    box = handle.bounding_box()
    assert box is not None, f"no .col-resize-handle inside {th_sel}"
    cx = box["x"] + box["width"] / 2
    cy = box["y"] + box["height"] / 2
    page.mouse.move(cx, cy)
    page.mouse.down()
    page.mouse.move(cx + dx, cy, steps=12)
    page.mouse.up()


def _run_resize_check(page, ui_server, path, storage_key):
    page.goto(f"{ui_server}{path}", wait_until="domcontentloaded")
    page.wait_for_selector("#data-table thead th[data-key] .col-resize-handle", timeout=5000)
    # drop any persisted widths so we measure from the schema defaults
    page.evaluate("(k) => localStorage.removeItem(k)", storage_key)
    page.reload(wait_until="domcontentloaded")
    page.wait_for_selector("#data-table thead th[data-key] .col-resize-handle", timeout=5000)

    keys = _visible_data_keys(page)
    assert len(keys) >= 2, f"{path}: need >=2 visible resizable columns, got {keys}"
    col = f'#data-table thead th[data-key="{keys[0]}"]'
    neighbor = f'#data-table thead th[data-key="{keys[1]}"]'

    col_before = _col_width(page, col)
    neighbor_before = _col_width(page, neighbor)

    # drag the first column's handle well to the right
    _drag_handle(page, col, 250)
    col_after = _col_width(page, col)
    neighbor_after = _col_width(page, neighbor)

    assert col_after > col_before + 120, (
        f"{path}: column '{keys[0]}' did not widen on drag: {col_before:.0f} -> {col_after:.0f}"
    )
    assert abs(neighbor_after - neighbor_before) < 3, (
        f"{path}: neighbour '{keys[1]}' changed — resize is not one-sided: "
        f"{neighbor_before:.0f} -> {neighbor_after:.0f}"
    )

    # the width must survive a full reload
    page.reload(wait_until="domcontentloaded")
    page.wait_for_selector(col, timeout=5000)
    page.wait_for_timeout(250)  # let the requestAnimationFrame width-restore run
    col_reload = _col_width(page, col)
    assert abs(col_reload - col_after) < 6, (
        f"{path}: width did not persist across reload: set {col_after:.0f}, "
        f"after reload {col_reload:.0f}"
    )


def test_inventory_columns_resize_and_persist(page, ui_server, _seed_inventory):
    _run_resize_check(page, ui_server, "/inventory", "celerp_col_widths_inventory")


def test_contacts_columns_resize_and_persist(page, ui_server, _seed_contacts):
    _run_resize_check(page, ui_server, "/contacts/customers", "celerp_col_widths_customers")


def test_files_table_columns_resize(page, ui_server, api):
    """The contact files table (`#files-table-<sid>`) has its own drag-to-resize
    (a third, simpler implementation — keys by index, no persistence). Assert a
    column actually widens on drag; there is no reload-persistence to check."""
    r = api.post("/crm/contacts", json={
        "name": f"Files Resize {uuid.uuid4().hex[:6]}", "type": "customer",
    })
    cid = r.json().get("entity_id") or r.json().get("id")
    page.goto(f"{ui_server}/contacts/{cid}", wait_until="domcontentloaded")
    # the files section sits at the bottom of the contact page
    page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
    handle_sel = 'table[id^="files-table-"] thead th .col-resize-handle'
    page.wait_for_selector(handle_sel, timeout=8000)

    th = page.locator('table[id^="files-table-"] thead th:has(.col-resize-handle)').first
    th.scroll_into_view_if_needed()
    w_before = th.evaluate("el => el.getBoundingClientRect().width")

    handle = th.locator(".col-resize-handle").first
    box = handle.bounding_box()
    assert box is not None, "no resize handle on the files table header"
    cx = box["x"] + box["width"] / 2
    cy = box["y"] + box["height"] / 2
    page.mouse.move(cx, cy)
    page.mouse.down()
    page.mouse.move(cx + 200, cy, steps=12)
    page.mouse.up()

    w_after = th.evaluate("el => el.getBoundingClientRect().width")
    assert w_after > w_before + 100, (
        f"files-table column did not widen on drag: {w_before:.0f} -> {w_after:.0f}"
    )