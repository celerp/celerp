# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""On every list page the top bar fits a phone and a desktop window in English and German: every control
is on screen, none overlaps another, the search box keeps room to type, and the company
and language names are shown whole."""
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
@pytest.mark.parametrize("width", [390, 1280])
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
    if width == 390:
        assert "menu" in boxes

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
