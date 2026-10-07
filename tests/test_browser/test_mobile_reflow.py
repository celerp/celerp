# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""On a phone the page never pans sideways.

A stock check runs on a phone from start to finish: pick the company, search,
choose the inventory type, select stock, read what the merge will do, confirm and
read the result. The page itself always fits the screen width. Only a part whose
content genuinely needs more width, such as the inventory table or the strip of
tabs, scrolls sideways inside its own box.
"""
from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.browser

# The parts that may scroll sideways inside their own box.
_OWN_SCROLL = ".table-scroll-wrap, .table-scroll-top, .category-tabs"

# Every element that reaches past either edge of the screen without sitting inside a part
# allowed to scroll, and every other box that scrolls sideways. The closed side menu waits
# off the left edge by design.
_OFFENDERS_JS = """(allowed) => {
  const W = window.innerWidth, out = [];
  const name = el => el.tagName.toLowerCase() + (el.id ? '#' + el.id : '')
    + (typeof el.className === 'string' && el.className ? '.' + el.className.trim().split(/\\s+/).join('.') : '');
  for (const el of document.querySelectorAll('body *')) {
    if ((el.closest(allowed) && !el.matches(allowed)) || el.closest('.sidebar:not(.sidebar--open)')) continue;
    const s = getComputedStyle(el);
    if (s.display === 'none' || s.visibility === 'hidden') continue;
    const r = el.getBoundingClientRect();
    if (r.width && (r.right > W + 0.5 || r.left < -0.5))
      out.push(`${name(el)} ${Math.round(r.left)}..${Math.round(r.right)}`);
    if (!el.matches(allowed) && ['auto', 'scroll'].includes(s.overflowX) && el.scrollWidth > el.clientWidth + 1)
      out.push(`${name(el)} scrolls sideways ${el.scrollWidth}>${el.clientWidth}`);
  }
  return out;
}"""


def _fits(page, step: str) -> None:
    width = page.evaluate("window.innerWidth")
    doc, body = page.evaluate("[document.documentElement.scrollWidth, document.body.scrollWidth]")
    offenders = page.evaluate(_OFFENDERS_JS, _OWN_SCROLL)
    assert doc <= width and body <= width and not offenders, (
        f"{step} at {width}px: page {doc}, body {body}; overflowing: {offenders[:25]}")


def _on_screen(page, selector: str, step: str) -> None:
    loc = page.locator(selector).first
    loc.scroll_into_view_if_needed()
    box = loc.bounding_box()
    width = page.evaluate("window.innerWidth")
    assert box and box["width"] > 0, f"{step}: {selector} is not shown"
    assert box["x"] >= 0 and box["x"] + box["width"] <= width + 0.5, (
        f"{step} at {width}px: {selector} spans {box['x']:.0f}..{box['x'] + box['width']:.0f}")


@pytest.mark.parametrize("width", [320, 390])
def test_the_stock_merge_journey_fits_a_phone_screen(page, ui_server, fresh_company, width):
    tag = uuid.uuid4().hex[:6].upper()
    api = fresh_company

    def lot(suffix: str) -> None:
        r = api.post("/items", json={
            "status": "available", "sku": f"MOB-{tag}-{suffix}", "name": f"Polished stock lot {suffix}",
            "quantity": 2, "sell_by": "piece", "cost_total": 20.0})
        assert r.status_code in (200, 201), r.text

    # The two lots sit in different inventory accounts, so the merge says what it moves
    # between them, naming an account whose name is one long unbroken word.
    lot("A")
    r = api.post("/accounting/accounts", json={
        "code": "1131", "name": "Consignment_stock_held_at_the_riverside_showroom_warehouse",
        "account_type": "asset", "parent_code": "1130"})
    assert r.status_code == 200, r.text
    r = api.put("/accounting/posting-accounts/inventory_opening", json={"code": "1131"})
    assert r.status_code == 200, r.text
    lot("B")

    page.set_viewport_size({"width": width, "height": 740})
    page.goto(f"{ui_server}/inventory?q=MOB-{tag}", wait_until="domcontentloaded")
    page.wait_for_selector("input.row-select", timeout=8000)
    # The company switcher arrives after the page; it is shown and can be used.
    page.wait_for_selector(".topbar select.company-switcher-select", state="attached",
                           timeout=8000)
    _fits(page, "inventory list")
    for selector in ("select.company-switcher-select", ".sidebar-toggle", ".global-search-input",
                     "#search-input", ".search-scope-label"):
        _on_screen(page, selector, "inventory list")
    label, box = (page.locator(s).first.bounding_box() for s in (".search-scope-label", "#search-input"))
    assert abs((label["x"] + label["width"] / 2) - (box["x"] + box["width"] / 2)) <= 2, (
        "the search label is not centred over its box")

    # The search box fills its row on a phone.
    assert page.locator("#search-input").bounding_box()["width"] >= 0.75 * width

    # A tab strip wider than the screen is a strip the user swipes, and every tab in it,
    # the current one included, can be brought into view.
    for strip in page.locator(".category-tabs").all():
        sw, cw, overflow = strip.evaluate("e => [e.scrollWidth, e.clientWidth, getComputedStyle(e).overflowX]")
        if sw > cw + 1:
            assert overflow in ("auto", "scroll"), f"tabs cut off: {sw}>{cw}, overflow-x {overflow}"
        edge = strip.bounding_box()
        for tab in [*strip.locator(".category-tab--active").all(), strip.locator(".category-tab").last]:
            tab.scroll_into_view_if_needed()
            box = tab.bounding_box()
            assert box["x"] >= edge["x"] - 0.5 and box["x"] + box["width"] <= edge["x"] + edge["width"] + 0.5
    _fits(page, "after reaching the tabs")

    # The inventory table keeps its own sideways scroll, however wide it gets.
    wide = page.evaluate("""() => {
      const wrap = document.querySelector('.table-scroll-wrap');
      wrap.querySelector('table').style.minWidth = '1600px';
      return [wrap.scrollWidth, wrap.clientWidth];
    }""")
    assert wide[0] > wide[1]
    _fits(page, "with a wide table")

    # Scrolling down never moves the page sideways, and it cannot be dragged sideways.
    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    page.mouse.wheel(0, 600)
    page.evaluate("window.scrollBy(200, 0)")
    assert page.evaluate("window.scrollX") == 0

    page.locator("input.row-select").nth(0).check()
    page.locator("input.row-select").nth(1).check()
    page.wait_for_selector("#bulk-toolbar.is-active", timeout=5000)
    page.select_option("#bulk-action-select", "merge")
    page.wait_for_selector("#merge-target-select", timeout=5000)
    page.select_option("#merge-target-select", index=1)
    page.wait_for_selector("#merge-confirm button:has-text('Confirm')", timeout=5000)
    page.wait_for_selector("#merge-reclass-note:visible", timeout=8000)
    _fits(page, "merge disclosure")
    for selector in ("#bulk-action-select", "#merge-target-select", "#merge-reclass-note",
                     "#merge-confirm button:has-text('Confirm')", "#merge-confirm button:has-text('Cancel')"):
        _on_screen(page, selector, "merge disclosure")

    page.click("#merge-confirm button:has-text('Confirm')")
    page.wait_for_selector(".toast-container .toast--visible", timeout=8000)
    page.wait_for_timeout(300)
    assert "riverside_showroom" in page.locator(".toast-container .toast").first.inner_text()
    _fits(page, "result toast")
    _on_screen(page, ".toast-container .toast", "result toast")


@pytest.mark.parametrize("width", [320, 390])
def test_the_dashboard_fits_a_phone_screen(page, ui_server, fresh_company, width):
    # Recent activity with a long unbroken word, so the activity table has content to fit.
    r = fresh_company.post("/items", json={
        "status": "available", "sku": f"DASH-{uuid.uuid4().hex[:6].upper()}",
        "name": "Consignment_stock_held_at_the_riverside_showroom_warehouse",
        "quantity": 2, "sell_by": "piece", "cost_total": 20.0})
    assert r.status_code in (200, 201), r.text

    page.set_viewport_size({"width": width, "height": 740})
    page.goto(f"{ui_server}/dashboard", wait_until="domcontentloaded")
    page.wait_for_selector(".chart-card", timeout=8000)
    page.wait_for_selector("table.activity-table", timeout=8000)
    page.wait_for_timeout(500)
    _fits(page, "dashboard")
    page.locator("table.activity-table").first.scroll_into_view_if_needed()
    _fits(page, "dashboard activity")
    for selector in (".chart-card", "table.activity-table"):
        assert page.locator(selector).first.evaluate(
            f"e => !!e.closest('{_OWN_SCROLL}') || e.getBoundingClientRect().right <= {width} + 0.5")


# Every main page, with each of its tabs. A tab list is read from the page itself, so a tab
# added later is swept too.
_MAIN_PAGES = ("/dashboard", "/inventory", "{item}", "/crm", "/contacts/{contact}", "/docs", "/docs/{doc}",
               "/lists", "/lists/{list}", "/manufacturing", "/manufacturing/production", "/manufacturing/{order}",
               "/accounting",
               "/settings/general", "/settings/inventory", "/settings/accounting", "/settings/contacts",
               "/settings/sales", "/settings/purchasing", "/settings/manufacturing", "/settings/payments",
               "/doctor")
_TABS = "a.category-tab[href*='tab='], .settings-tabs a[href]"


# Every table whose rows stop short of its own box: [width of the table, of its first row].
_SHORT_ROWS_JS = """() => [...document.querySelectorAll('.main-content .data-table')]
  .filter(t => t.offsetParent && t.querySelector('tr'))
  .map(t => [Math.round(t.getBoundingClientRect().width), Math.round(t.querySelector('tr').getBoundingClientRect().width)])
  .filter(([table, row]) => row < table - 2)"""


@pytest.mark.timeout(300)
@pytest.mark.parametrize("width", [320, 390, 820, 1280])
def test_every_main_page_fits_a_narrow_screen(page, ui_server, fresh_company, width):
    """No main page or tab is wider than a phone, tablet or laptop screen, and every table's rows
    span the table, as they do on a desktop."""
    api = fresh_company
    tag = uuid.uuid4().hex[:6].upper()

    def made(r) -> str:
        assert r.status_code in (200, 201), r.text
        return r.json()["id"]

    part = made(api.post("/items", json={"status": "available", "sku": f"PH-{tag}-P", "sell_by": "piece",
                                         "name": "Consignment_stock_held_at_the_riverside_showroom_warehouse",
                                         "quantity": 5, "cost_total": 50.0}))
    item = made(api.post("/items", json={"status": "available", "sku": f"PH-{tag}-I", "sell_by": "piece",
                                         "name": "Finished ring", "quantity": 1, "cost_total": 10.0}))
    contact = made(api.post("/crm/contacts", json={"name": "Riverside showroom", "contact_type": "customer"}))
    doc = made(api.post("/docs", json={"doc_type": "invoice", "contact_id": contact, "line_items": [
        {"description": "Polished stock lot", "quantity": 1, "unit_price": 100.0, "line_total": 100.0}]}))
    lst = made(api.post("/lists", json={"list_type": "quotation"}))
    order = made(api.post("/manufacturing", json={
        "description": "Ring run", "order_type": "assembly", "inputs": [{"item_id": part, "quantity": 1}],
        "output_item_id": item, "quantity": 1}))
    ids = {"item": f"/inventory/{item}", "contact": contact, "doc": doc, "list": lst, "order": order}

    page.set_viewport_size({"width": width, "height": 740})
    urls = [u.format(**ids) for u in _MAIN_PAGES]
    seen, wide = set(), []
    while urls:
        url = urls.pop(0)
        if url in seen:
            continue
        seen.add(url)
        page.goto(f"{ui_server}{url}", wait_until="load")
        page.wait_for_timeout(600)
        doc_w, client_w = page.evaluate(
            "[document.documentElement.scrollWidth, document.documentElement.clientWidth]")
        if doc_w != client_w:
            wide.append(f"{url}: {doc_w} > {client_w}")
        short = page.evaluate(_SHORT_ROWS_JS)
        if short:
            wide.append(f"{url}: table rows short of the table {short}")
        if url.startswith(("/inventory/", "/settings/")):
            for href in page.eval_on_selector_all(_TABS, "els => els.map(e => e.getAttribute('href'))"):
                if href and href.startswith("/") and href not in seen:
                    urls.append(href)
    assert not wide, f"at {width}px: {wide}"
    assert len(seen) > len(_MAIN_PAGES), "no tab was swept"


@pytest.mark.parametrize("lang", ["en", "de"])
@pytest.mark.parametrize("width", [320, 390])
def test_the_alerts_panel_opens_inside_a_phone_screen(page, ui_server, fresh_company, width, lang):
    # With a second company the top bar carries the company switcher, and the top bar's
    # first row wraps, so the bell sits anywhere along it (further left in German).
    host = ui_server.split("//", 1)[1].split(":", 1)[0]
    page.context.add_cookies([{"name": "celerp_lang", "value": lang, "domain": host, "path": "/"}])
    try:
        page.set_viewport_size({"width": width, "height": 740})
        page.goto(f"{ui_server}/dashboard", wait_until="load")
        page.wait_for_selector(".topbar select.company-switcher-select", state="attached", timeout=8000)
        page.click(".notif-bell-btn")
        page.wait_for_selector("#notif-panel", state="visible", timeout=5000)
        box = page.locator("#notif-panel").bounding_box()
    finally:
        page.context.clear_cookies(name="celerp_lang")
    assert box["x"] >= 0 and box["x"] + box["width"] <= width + 0.5, (
        f"the alerts panel spans {box['x']:.0f}..{box['x'] + box['width']:.0f} on a {width}px screen")
