# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Info tooltips open on hover and keyboard focus, close on Escape, and always sit fully
inside the viewport: above the icon when there is room, below it when not, and pulled in from
the left and right edges, whatever card or container the icon lives in."""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.browser

_BUBBLE = "[role=tooltip]"


def _bubble_box(page) -> dict:
    bubble = page.locator(_BUBBLE)
    bubble.wait_for(state="visible", timeout=5000)
    return bubble.bounding_box()


def _inside_viewport(page, box: dict) -> bool:
    vp = page.viewport_size
    return box["x"] >= 0 and box["y"] >= 0 and box["x"] + box["width"] <= vp["width"] and box["y"] + box["height"] <= vp["height"]


def _visible_area(page, box: dict) -> float:
    """The bubble's area that is actually painted: what no ancestor clips away."""
    return page.evaluate("""() => {
        const b = document.querySelector('[role=tooltip]');
        let r = b.getBoundingClientRect(), x0 = r.left, y0 = r.top, x1 = r.right, y1 = r.bottom;
        for (let el = b.parentElement; el && el !== document.documentElement; el = el.parentElement) {
            const s = getComputedStyle(el);
            if (s.overflow !== 'visible') {
                const c = el.getBoundingClientRect();
                x0 = Math.max(x0, c.left); y0 = Math.max(y0, c.top); x1 = Math.min(x1, c.right); y1 = Math.min(y1, c.bottom);
            }
        }
        return Math.max(0, x1 - x0) * Math.max(0, y1 - y0);
    }""")


def test_company_details_tip_is_fully_visible(page, ui_server, api):
    page.set_viewport_size({"width": 1280, "height": 800})
    page.goto(f"{ui_server}/finance/company-details", wait_until="domcontentloaded")
    tip = page.locator(".info-tip").first
    tip.hover()
    box = _bubble_box(page)
    assert _inside_viewport(page, box), box
    assert _visible_area(page, box) == pytest.approx(box["width"] * box["height"], rel=0.01), "an ancestor clips the tooltip"
    assert tip.get_attribute("aria-describedby") == page.locator(_BUBBLE).get_attribute("id")


def test_manufacturing_settings_uses_the_same_tip(page, ui_server, api):
    page.set_viewport_size({"width": 1280, "height": 800})
    page.goto(f"{ui_server}/settings/manufacturing", wait_until="domcontentloaded")
    page.locator(".info-tip").first.hover()
    box = _bubble_box(page)
    assert _inside_viewport(page, box), box
    assert page.locator(_BUBBLE).count() == 1


def test_keyboard_focus_opens_and_escape_closes(page, ui_server, api):
    page.goto(f"{ui_server}/finance/company-details", wait_until="domcontentloaded")
    tip = page.locator(".info-tip").first
    tip.focus()
    _bubble_box(page)
    page.keyboard.press("Escape")
    page.locator(_BUBBLE).wait_for(state="hidden", timeout=3000)
    assert tip.get_attribute("aria-describedby") is None


def test_narrow_viewport_clamps_and_flips_below(page, ui_server, api):
    page.set_viewport_size({"width": 360, "height": 740})
    page.goto(f"{ui_server}/finance/company-details", wait_until="domcontentloaded")
    tip = page.locator(".info-tip").first
    # Put the icon hard against the top of the viewport: no room above, so it opens below.
    tip.evaluate("el => window.scrollBy(0, el.getBoundingClientRect().top - 2)")
    tip.focus()
    box = _bubble_box(page)
    assert _inside_viewport(page, box), box
    assert box["y"] >= tip.bounding_box()["y"], "with no room above, the tip opens below the icon"
