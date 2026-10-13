# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Playwright: the "?" match column of a searched inventory list stays fixed right after
the checkbox column when a saved column order is applied, so every row lines up with
its headers and the icon opens and closes its explanation."""
from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.browser

_LAYOUT_JS = """() => {
  const t = document.getElementById('data-table');
  const head = Array.from(t.querySelectorAll('thead tr:first-child > th')).map(th =>
    th.dataset.key || th.className);
  const rows = Array.from(t.querySelectorAll('tbody tr.data-row')).map(tr =>
    Array.from(tr.children).map(td => td.dataset.col || td.className));
  return {head, rows};
}"""


@pytest.fixture(scope="module")
def _tag(api):
    tag = f"mc{uuid.uuid4().hex[:6]}"
    for i in range(2):
        r = api.post("/items", json={
            "sku": f"MC-{uuid.uuid4().hex[:8]}", "name": f"Match {tag} {i}",
            "quantity": 3, "sell_by": "piece",
        })
        assert r.status_code in {200, 201}, r.text
    return tag


def _open(page, ui_server, tag: str) -> None:
    page.goto(f"{ui_server}/inventory?q={tag}", wait_until="domcontentloaded")
    page.wait_for_selector("#data-table tbody tr.data-row")


def _assert_aligned(layout: dict) -> None:
    head = layout["head"]
    assert head[0] == "col-checkbox" and head[1] == "col-match", head
    assert layout["rows"]
    for cells in layout["rows"]:
        assert cells[0] == "col-checkbox" and cells[1] == "col-match", cells
        data = [c for c in cells if c not in ("col-checkbox", "col-match", "col-actions")]
        assert data == [k for k in head if k not in ("col-checkbox", "col-match", "col-actions")]


def test_saved_column_order_keeps_match_column_first(page, ui_server, _tag):
    """Red statement: the saved-order scripts kept only the checkbox and actions cells
    in place, so a reordered searched row put its match cell out of line with the
    header (or before the checkbox)."""
    _open(page, ui_server, _tag)
    keys = page.evaluate(
        "() => Array.from(document.querySelectorAll('#data-table thead th[data-key]'))"
        ".map(th => th.dataset.key)")
    assert len(keys) > 2
    page.evaluate("(o) => localStorage.setItem('celerp_col_order_inventory', JSON.stringify(o))",
                  list(reversed(keys)))
    page.reload(wait_until="domcontentloaded")
    page.wait_for_selector("#data-table tbody tr.data-row")
    _assert_aligned(page.evaluate(_LAYOUT_JS))


def test_match_icon_opens_and_esc_closes(page, ui_server, _tag):
    _open(page, ui_server, _tag)
    icons = page.locator("#data-table tbody tr.data-row td.col-match .info-tip")
    assert icons.count() == 2
    icon = icons.first
    assert icon.inner_text() == "?"
    icon.hover()
    bubble = page.locator("#info-tip-bubble.info-tip-bubble--open")
    bubble.wait_for(timeout=5000)
    assert bubble.inner_text() == f"Name contains {_tag}"
    icon.focus()
    page.keyboard.press("Escape")
    assert page.locator("#info-tip-bubble.info-tip-bubble--open").count() == 0
