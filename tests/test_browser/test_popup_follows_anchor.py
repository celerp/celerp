# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""
Browser test: a floating panel stays beside the control that opened it.

Pickers, row menus, column funnels and info tips are drawn with fixed positioning,
so no table or card can clip them. That makes them blind to the page moving under
them: a banner that loads in above the content, a scroll or a resize moves the
control and leaves the panel where it opened. A click meant for an option then
lands beside it.

Proves, for each kind of panel:
1. After content is inserted above the control by plain script (no htmx event to
   react to), the panel is still beside its control.
2. After the page scrolls, the panel is still beside its control.
3. For the account picker: an option clicked after the shift is ticked and the
   list stays open.
"""
from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.browser

# Insert a 60px block at the top of the page content, as a late banner would.
_SHIFT_JS = """() => {
    const d = document.createElement('div');
    d.style.height = '60px';
    document.getElementById('main-content').prepend(d);
}"""

# Make the page taller, then scroll whatever scrolls the anchor by 50px.
_SCROLL_JS = """(sel) => {
    const a = document.querySelector(sel);
    const spacer = document.createElement('div');
    spacer.style.height = '2000px';
    document.getElementById('main-content').append(spacer);
    let s = a.parentElement;
    while (s && !(s.scrollHeight > s.clientHeight && /(auto|scroll)/.test(getComputedStyle(s).overflowY))) {
        s = s.parentElement;
    }
    (s || document.scrollingElement).scrollBy(0, 50);
}"""

# The gap between the panel and its control, above or below it.
_GAP_JS = """([popSel, anchorSel]) => {
    const p = document.querySelector(popSel).getBoundingClientRect();
    const a = document.querySelector(anchorSel).getBoundingClientRect();
    return Math.min(Math.abs(p.top - a.bottom), Math.abs(p.bottom - a.top));
}"""


def _settle(page):
    page.wait_for_timeout(200)


def _assert_follows(page, pop_sel: str, anchor_sel: str):
    assert page.evaluate(_GAP_JS, [pop_sel, anchor_sel]) <= 8, "panel opened beside its control"
    page.evaluate(_SHIFT_JS)
    _settle(page)
    gap = page.evaluate(_GAP_JS, [pop_sel, anchor_sel])
    assert gap <= 8, f"panel left {gap:.0f}px from its control after content arrived above it"
    page.evaluate(_SCROLL_JS, anchor_sel)
    _settle(page)
    gap = page.evaluate(_GAP_JS, [pop_sel, anchor_sel])
    assert gap <= 8, f"panel left {gap:.0f}px from its control after a scroll"


def _open_loaded(page, url: str):
    """Open the page and let its system-health check answer first, so the only movement
    under the panel is the one the test makes."""
    with page.expect_response("**/health/system"):
        page.goto(url, wait_until="load")
    _settle(page)


@pytest.fixture(scope="module")
def statement_url(ui_server, api):
    r = api.post("/accounting/chart/seed")
    assert r.status_code in (200, 409), f"Chart seed failed: {r.text}"
    return f"{ui_server}/reports/statement"


def test_account_picker_follows_its_field_and_takes_the_click(page, statement_url):
    _open_loaded(page, statement_url)
    field = ".combobox-wrap[data-multiple] .combobox-input"
    page.locator(field).first.click()
    page.wait_for_selector(".combobox-list.open", timeout=3000)
    page.evaluate(_SHIFT_JS)
    _settle(page)
    assert page.evaluate(_GAP_JS, [".combobox-list.open", field]) <= 8

    first = page.locator(".combobox-list.open .combobox-option:not(.combobox-option--empty)").first
    value = first.get_attribute("data-value")
    first.click()
    page.wait_for_timeout(200)
    assert page.locator(".combobox-list.open").count() == 1, "the list closed on the click"
    ticked = page.locator(f'.combobox-list.open .combobox-option--selected[data-value="{value}"]')
    assert ticked.count() == 1, "the clicked option was not ticked"

    page.evaluate(_SCROLL_JS, field)
    _settle(page)
    assert page.evaluate(_GAP_JS, [".combobox-list.open", field]) <= 8


def test_row_menu_follows_its_button(page, ui_server, api):
    tag = uuid.uuid4().hex[:6]
    item_id = api.post("/items", json={"sku": f"PF-{tag}", "name": "Follow", "sell_by": "piece",
                                       "quantity": 1}).json()["id"]
    _open_loaded(page, f"{ui_server}/inventory?q={tag}")
    btn = f"#row-{item_id.replace(':', '-')} .row-menu-btn"
    page.locator(btn).click()
    page.wait_for_selector(".row-menu-dropdown.open", timeout=3000)
    _assert_follows(page, ".row-menu-dropdown.open", btn)


def test_column_funnel_follows_its_button(page, ui_server, api):
    tag = uuid.uuid4().hex[:6]
    part = api.post("/items", json={"status": "available", "sku": f"PP-{tag}", "name": "Part",
                                    "quantity": 5, "sell_by": "piece",
                                    "inventory_type": "component"}).json()["id"]
    product = api.post("/items", json={"status": "available", "sku": f"PM-{tag}", "name": "Made",
                                       "quantity": 0, "sell_by": "piece"}).json()["id"]
    r = api.put(f"/manufacturing/items/{product}/recipe",
                json={"output_qty": 1, "components": [{"item_id": part, "quantity": 1}],
                      "labor": [], "overhead": []})
    assert r.status_code == 200, r.text
    r = api.post(f"/manufacturing/items/{product}/build", json={"quantity": 1})
    assert r.status_code in (200, 201), r.text
    _open_loaded(page, f"{ui_server}/manufacturing/production")
    btn = "#mfg-table .colfilter[data-col]"
    page.wait_for_selector(btn, timeout=10000)
    page.locator(btn).first.click()
    page.wait_for_selector(".colfilter-pop", timeout=3000)
    _assert_follows(page, ".colfilter-pop", btn)


def test_server_column_funnel_follows_its_button(page, ui_server, api):
    tag = uuid.uuid4().hex[:6]
    api.post("/items", json={"sku": f"PL-{tag}", "name": "Funnel", "sell_by": "piece", "quantity": 1})
    _open_loaded(page, f"{ui_server}/inventory")
    btn = ".colfilter[data-param]"
    page.wait_for_selector(btn, timeout=10000)
    page.locator(btn).first.click()
    page.wait_for_selector(".colfilter-pop", timeout=3000)
    _assert_follows(page, ".colfilter-pop", btn)


def test_info_tip_follows_its_icon(page, ui_server):
    _open_loaded(page, f"{ui_server}/finance/company-details")
    page.locator(".info-tip").first.focus()
    page.locator("[role=tooltip]").wait_for(state="visible", timeout=5000)
    _assert_follows(page, "[role=tooltip]", ".info-tip:focus")


def test_document_item_list_follows_its_field(page, ui_server, api):
    sku = f"PD-{uuid.uuid4().hex[:6]}"
    r = api.post("/items", json={"status": "available", "sku": sku, "name": sku, "quantity": 1,
                                 "sell_by": "piece"})
    assert r.status_code in (200, 201), f"create item failed: {r.text}"
    r = api.post("/docs", json={"doc_type": "invoice", "status": "draft", "line_items": [],
                                "subtotal": 0, "total": 0})
    assert r.status_code in (200, 201), f"create draft failed: {r.text}"
    _open_loaded(page, f"{ui_server}/docs/{r.json()['id']}")
    page.wait_for_selector("#line-body", timeout=8000)
    page.evaluate("celerpAddLine()")
    field = '#line-body tr:last-child [data-name="sku"]'
    page.locator(field).focus()
    page.locator(field).fill(sku)
    page.locator(".catalog-ac-option", has_text=sku).first.wait_for(state="visible", timeout=8000)
    _assert_follows(page, ".catalog-ac-list[style*='block']", field)


def test_import_mapping_panel_follows_its_button(page, ui_server):
    _open_loaded(page, f"{ui_server}/inventory/import")
    page.locator("input[type='file']").first.set_input_files(
        {"name": "stock.csv", "mimeType": "text/csv",
         "buffer": b"Item Code,Item Name,Qty\nPF-1,Basket,4\n"})
    page.locator("form[action='/inventory/import/preview'] button[type='submit']").first.click()
    page.wait_for_selector(".mapping-dd-trigger", timeout=10000)
    page.locator(".mapping-dd-trigger").first.click()
    page.wait_for_selector(".mapping-dd-panel[style*='flex']", timeout=3000)
    _assert_follows(page, ".mapping-dd-panel[style*='flex']", ".mapping-dd-trigger")
