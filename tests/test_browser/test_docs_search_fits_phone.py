# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The documents search in the filters panel fits a phone in English and German: the placeholder
shows whole (not cut, not ellipsed), the box, Apply and Reset sit on screen and the page does not
scroll sideways. The box is named for what it searches, so a screen reader hears the whole meaning
however short the placeholder is."""
from __future__ import annotations

import pytest
from playwright.sync_api import Page

from .conftest import wait_for_topbar

pytestmark = pytest.mark.browser

_FIT_JS = """async (main) => {
  await document.fonts.ready;
  await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
  const box = el => { const r = el.getBoundingClientRect(); return {x: r.x, width: r.width}; };
  const input = main.querySelector('.list-filters #search-input');
  const cs = getComputedStyle(input);
  const c = document.createElement('canvas').getContext('2d');
  c.font = cs.fontStyle + ' ' + cs.fontWeight + ' ' + cs.fontSize + ' ' + cs.fontFamily;
  return {
    need: Math.ceil(c.measureText(input.placeholder).width),
    have: input.clientWidth - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight),
    placeholder: input.placeholder,
    name: input.getAttribute('aria-label') || '',
    caption: main.querySelector('label[for="search-input"]').innerText.trim(),
    boxes: {input: box(input),
            submit: box(main.querySelector('.list-filters button[type="submit"]')),
            clear: box(main.querySelector('.list-filters .filter-reset'))},
    scroll: document.documentElement.scrollWidth,
  };
}"""


@pytest.mark.parametrize("lang", ["en", "de"])
@pytest.mark.parametrize("width", [320, 375, 390])
def test_docs_search_fits_a_phone(page: Page, fresh_company, ui_server, lang, width):
    page.context.add_cookies([{"name": "celerp_lang", "value": lang, "url": ui_server}])
    page.set_viewport_size({"width": width, "height": 800})
    # A custom period shows Apply beside Reset, the panel at its widest.
    page.goto("/docs?preset=custom&from=2026-01-01&to=2026-12-31")
    page.wait_for_selector(".list-filters #search-input", state="visible")
    page.wait_for_load_state("load")
    wait_for_topbar(page)
    got = page.locator(".main-content").evaluate(_FIT_JS)

    assert got["need"] <= got["have"] + 1, f"placeholder does not fit: {got}"
    for name, b in got["boxes"].items():
        assert b["width"] > 0 and b["x"] >= 0 and b["x"] + b["width"] <= width + 0.5, f"{name} off screen: {got}"
    assert got["scroll"] <= width, f"page scrolls sideways: {got}"
    assert got["name"].startswith(got["caption"]), got
    assert got["placeholder"].rstrip(".") in got["name"], got
