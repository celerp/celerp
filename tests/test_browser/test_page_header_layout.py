# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The inventory, documents and lists page headers read cleanly on a phone and a desktop window, in English
and German (900 is a window narrow enough to wrap the header beside the sidebar):
no control overlaps another, the search caption sits directly above its
box, the box keeps room to type (and on a phone shows its placeholder whole), and every type tab
can be brought fully into view."""
from __future__ import annotations

import pytest
from playwright.sync_api import Page

pytestmark = pytest.mark.browser

# Width the placeholder needs, against the room the input gives it (both in px).
_PLACEHOLDER_FIT_JS = """(el) => {
  const c = document.createElement('canvas').getContext('2d');
  const cs = getComputedStyle(el);
  c.font = cs.fontStyle + ' ' + cs.fontWeight + ' ' + cs.fontSize + ' ' + cs.fontFamily;
  return {need: Math.ceil(c.measureText(el.placeholder).width), have: el.clientWidth
    - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight), text: el.placeholder, overflow: cs.textOverflow};
}"""


def _overlap(a: dict, b: dict) -> bool:
    return (a["x"] < b["x"] + b["width"] - 0.5 and b["x"] < a["x"] + a["width"] - 0.5
            and a["y"] < b["y"] + b["height"] - 0.5 and b["y"] < a["y"] + a["height"] - 0.5)


def _open(page: Page, ui_server: str, lang: str, width: int, path: str = "/inventory") -> None:
    page.context.add_cookies([{"name": "celerp_lang", "value": lang, "url": ui_server}])
    page.set_viewport_size({"width": width, "height": 800})
    page.goto(path)
    page.wait_for_selector(".page-header #search-input", state="visible")
    page.wait_for_load_state("load")
    # The company switcher loads after the page; on a phone it can add a top bar row.
    page.wait_for_selector("#topbar-company-switcher", state="detached")


@pytest.mark.parametrize("path", ["/inventory", "/docs", "/lists"])
@pytest.mark.parametrize("lang", ["en", "de"])
@pytest.mark.parametrize("width", [390, 900, 1280])
def test_list_page_header_stacks_without_overlap(page: Page, fresh_company, ui_server, lang, width, path):
    _open(page, ui_server, lang, width, path)
    header = page.locator(".page-header")
    boxes = {"title": header.locator(".page-title").bounding_box(),
             "caption": header.locator(".search-scope-label").bounding_box(),
             "search": header.locator("#search-input").bounding_box()}
    actions = header.locator(".page-actions > .btn")
    for i in range(actions.count()):
        boxes[f"button {actions.nth(i).inner_text()}"] = actions.nth(i).bounding_box()
    assert len(boxes) >= 6, boxes.keys()

    for name, b in boxes.items():
        assert b["x"] >= 0 and b["x"] + b["width"] <= width + 0.5, f"{name} off screen: {b}"
    names = sorted(boxes)
    for i, a in enumerate(names):
        for other in names[i + 1:]:
            assert not _overlap(boxes[a], boxes[other]), f"{a} overlaps {other}: {boxes[a]} {boxes[other]}"

    cap, search, title = boxes["caption"], boxes["search"], boxes["title"]
    assert search["width"] >= 200, f"search box too narrow to type in: {search}"
    assert 0 <= search["y"] - (cap["y"] + cap["height"]) <= 6, f"caption not directly above the box: {cap} {search}"
    assert cap["x"] < search["x"] + search["width"] and search["x"] < cap["x"] + cap["width"]
    fit = header.locator("#search-input").evaluate(_PLACEHOLDER_FIT_JS)
    if width == 390:
        assert fit["need"] <= fit["have"] + 1, f"placeholder '{fit['text']}' is cut: {fit}"
    else:
        # The desktop box keeps its width; a longer placeholder ends in a visible ellipsis.
        assert fit["need"] <= fit["have"] + 1 or fit["overflow"] == "ellipsis", fit
    if width == 390:
        # Phone: title, then caption over a full-width search, then the buttons.
        assert cap["y"] >= title["y"] + title["height"], f"caption crowds the title: {title} {cap}"
        content = page.locator(".main-content").evaluate(
            "el => { const cs = getComputedStyle(el); return el.clientWidth"
            " - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight); }")
        assert search["width"] >= content - 1, f"search not full width: {search['width']} of {content}"
        for name, b in boxes.items():
            if name.startswith("button"):
                assert b["y"] >= search["y"] + search["height"], f"{name} not below the search: {b}"


@pytest.mark.parametrize("lang", ["en", "de"])
def test_every_type_tab_can_be_scrolled_into_view(page: Page, fresh_company, ui_server, lang):
    _open(page, ui_server, lang, 390)
    strip = page.locator("#inventory-type-tabs")
    tabs = strip.locator(".category-tab")
    assert tabs.count() >= 3
    clipped = strip.evaluate("el => el.scrollWidth > el.clientWidth")
    if clipped:
        overflow = strip.evaluate("el => getComputedStyle(el).overflowX")
        assert overflow in ("auto", "scroll"), f"tabs are cut off and the strip does not scroll ({overflow})"
    for i in range(tabs.count()):
        tabs.nth(i).scroll_into_view_if_needed()
        s, b = strip.bounding_box(), tabs.nth(i).bounding_box()
        name = tabs.nth(i).inner_text()
        assert b["x"] >= s["x"] - 0.5 and b["x"] + b["width"] <= s["x"] + s["width"] + 0.5, \
            f"tab {name!r} cannot be brought fully into view: tab {b}, strip {s}"
        assert b["x"] >= 0 and b["x"] + b["width"] <= 390.5, f"tab {name!r} off screen: {b}"
