# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""On every list page the top bar fits a phone, a tablet and a desktop window in English and German:
every control is on screen, none overlaps another, the search box keeps room to type, and the
company and language names are shown whole. Up to tablet width the search takes a full row of
its own, whatever the company is called."""
from __future__ import annotations

import pytest
from playwright.sync_api import Page

pytestmark = pytest.mark.browser

_CONTROLS = {
    "menu": ".topbar .sidebar-toggle",
    "search": ".topbar .global-search-input",
    "search help": ".topbar .global-search-help",
    "company": ".topbar .company-switcher-select",
    "globe": ".topbar .lang-switcher__globe",
    "language": ".topbar .lang-switcher-wrap .combobox-input",
    "bell": ".topbar .notif-bell-btn",
    "user": ".topbar .user-menu__trigger",
}

# Width the text of a control needs, against the width it is given (both in px).
_TEXT_FIT_JS = """(el) => {
  const c = document.createElement('canvas').getContext('2d');
  const cs = getComputedStyle(el);
  c.font = cs.fontStyle + ' ' + cs.fontWeight + ' ' + cs.fontSize + ' ' + cs.fontFamily;
  const text = el.tagName === 'SELECT' ? el.options[el.selectedIndex].text : el.value;
  const pad = parseFloat(cs.paddingLeft) + parseFloat(cs.paddingRight)
    + parseFloat(cs.borderLeftWidth) + parseFloat(cs.borderRightWidth)
    + (el.tagName === 'SELECT' ? 16 : 0);
  return {need: Math.ceil(c.measureText(text).width + pad), have: el.getBoundingClientRect().width, text};
}"""


def _overlap(a: dict, b: dict) -> bool:
    return (a["x"] < b["x"] + b["width"] - 0.5 and b["x"] < a["x"] + a["width"] - 0.5
            and a["y"] < b["y"] + b["height"] - 0.5 and b["y"] < a["y"] + a["height"] - 0.5)


@pytest.mark.parametrize("path", ["/inventory", "/docs", "/lists"])
@pytest.mark.parametrize("lang", ["en", "de"])
@pytest.mark.parametrize("width", [320, 390, 480, 768, 1280])
def test_topbar_controls_fit_without_overlap(page: Page, fresh_company, ui_server, lang, width, path):
    page.context.add_cookies([{"name": "celerp_lang", "value": lang, "url": ui_server}])
    page.set_viewport_size({"width": width, "height": 800})
    page.goto(path)
    page.wait_for_selector(_CONTROLS["company"], state="visible")
    page.wait_for_load_state("load")

    boxes = {}
    for name, sel in _CONTROLS.items():
        loc = page.locator(sel).first
        if loc.is_visible():
            boxes[name] = loc.bounding_box()
    assert {"search", "company", "language", "bell", "user"} <= boxes.keys(), boxes.keys()
    if width <= 768:
        assert "menu" in boxes
        _assert_search_has_its_own_row(boxes, width)

    for name, b in boxes.items():
        assert b["x"] >= 0 and b["x"] + b["width"] <= width + 0.5, f"{name} off screen: {b}"
    names = sorted(boxes)
    for i, a in enumerate(names):
        for other in names[i + 1:]:
            if {a, other} == {"search", "search help"}:
                continue  # the help button sits inside the search box by design
            assert not _overlap(boxes[a], boxes[other]), f"{a} overlaps {other}: {boxes[a]} {boxes[other]}"

    assert boxes["search"]["width"] >= 200, f"search box too narrow to type in: {boxes['search']}"
    # A placeholder longer than the box ends in a visible ellipsis, never a hard cut.
    ph = page.locator(_CONTROLS["search"]).evaluate("""(el) => {
      const c = document.createElement('canvas').getContext('2d');
      const cs = getComputedStyle(el);
      c.font = cs.fontStyle + ' ' + cs.fontWeight + ' ' + cs.fontSize + ' ' + cs.fontFamily;
      return {need: Math.ceil(c.measureText(el.placeholder).width), overflow: cs.textOverflow,
              have: el.clientWidth - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight)};
    }""")
    assert ph["need"] <= ph["have"] + 1 or ph["overflow"] == "ellipsis", f"search placeholder is cut: {ph}"
    for name in ("company", "language"):
        fit = page.locator(_CONTROLS[name]).first.evaluate(_TEXT_FIT_JS)
        assert fit["need"] <= fit["have"] + 1, f"{name} '{fit['text']}' is cut: {fit}"


def _assert_search_has_its_own_row(boxes: dict, width: int) -> None:
    """The search spans the bar's width (less its 12px padding) on a row no other control shares."""
    search = boxes["search"]
    assert search["width"] >= width - 24 - 40, f"search does not span its row: {search}"
    for name, b in boxes.items():
        if name not in ("search", "search help"):
            assert b["y"] + b["height"] <= search["y"] + 0.5, f"{name} shares the search row: {b} {search}"


@pytest.mark.parametrize("lang", ["en", "de"])
@pytest.mark.parametrize("width", [320, 390, 480, 768])
@pytest.mark.parametrize("chars", [0, 12, 40])
def test_phone_search_keeps_its_row_whatever_the_company_name(page: Page, fresh_company, ui_server,
                                                              lang, width, chars):
    """A long company name wraps onto a row of its own and shows whole; the search keeps its
    full row below the controls and every control stays on screen."""
    page.context.add_cookies([{"name": "celerp_lang", "value": lang, "url": ui_server}])
    page.set_viewport_size({"width": width, "height": 800})
    page.goto("/inventory")
    page.wait_for_selector(_CONTROLS["company"], state="visible")
    page.wait_for_load_state("load")
    page.locator(_CONTROLS["company"]).evaluate(
        "(el, name) => { el.options[el.selectedIndex].text = name; }", "Company " + "x" * chars)
    boxes = {name: page.locator(sel).first.bounding_box() for name, sel in _CONTROLS.items()
             if page.locator(sel).first.is_visible()}
    for name, b in boxes.items():
        assert b["x"] >= 0 and b["x"] + b["width"] <= width + 0.5, f"{name} off screen: {b}"
    _assert_search_has_its_own_row(boxes, width)
    fit = page.locator(_CONTROLS["company"]).evaluate(_TEXT_FIT_JS)
    if fit["need"] <= width - 24:
        assert fit["need"] <= fit["have"] + 1, f"company '{fit['text']}' is cut: {fit}"
    else:  # a name wider than the screen takes the whole of its row
        assert fit["have"] >= width - 24 - 1, f"company '{fit['text']}' does not take its row: {fit}"
