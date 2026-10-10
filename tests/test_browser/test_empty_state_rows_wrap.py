"""An empty table's message stays readable on a phone: inside a table that scrolls
sideways in its own box, the empty-state sentence wraps within the width the box shows,
instead of running on under the part of the table that is scrolled out of view."""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.browser

_PAGES = ("/docs/received", "/payments", "/settings/general?tab=import-history")

_MEASURE = """() => [...document.querySelectorAll('.table-scroll-wrap .empty-state-msg')].map(el => {
    const box = el.closest('.table-scroll-wrap').getBoundingClientRect();
    const range = document.createRange(); range.selectNodeContents(el);
    const text = range.getBoundingClientRect();
    return {text: el.textContent.trim().slice(0, 40), left: Math.round(text.left), right: Math.round(text.right),
            boxLeft: Math.round(box.left), boxRight: Math.round(box.right)};
})"""


@pytest.mark.parametrize("width", [320, 390])
def test_an_empty_tables_message_wraps_within_the_visible_box(page, ui_server, fresh_company, width):
    page.set_viewport_size({"width": width, "height": 740})
    seen, cut = [], []
    for url in _PAGES:
        page.goto(f"{ui_server}{url}", wait_until="load")
        page.wait_for_timeout(300)
        for m in page.evaluate(_MEASURE):
            seen.append(url)
            if m["left"] < m["boxLeft"] or m["right"] > m["boxRight"]:
                cut.append(f"{url}: {m}")
    assert "/docs/received" in seen, seen
    assert not cut, f"at {width}px: {cut}"
