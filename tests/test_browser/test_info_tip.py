# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Info tooltips open on hover and keyboard focus, close on Escape, and always sit fully
inside the viewport: above the icon when there is room, below it when not, and pulled in from
the left and right edges, whatever card or container the icon lives in."""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.browser

_BUBBLE = "[role=tooltip]"


def _open_settled(page, url: str) -> None:
    """Open the page and let its system-health check answer before pointing at anything. On a
    busy machine that reply adds a banner above the page; arriving mid-hover, it moves the icon
    out from under the pointer, which correctly closes the tip."""
    with page.expect_response("**/health/system") as health:
        page.goto(url, wait_until="load")
    health.value.finished()
    page.evaluate("() => new Promise(r => requestAnimationFrame(() => r()))")


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
    _open_settled(page, f"{ui_server}/finance/company-details")
    tip = page.locator(".info-tip").first
    tip.hover()
    box = _bubble_box(page)
    assert _inside_viewport(page, box), box
    assert _visible_area(page, box) == pytest.approx(box["width"] * box["height"], rel=0.01), "an ancestor clips the tooltip"
    assert tip.get_attribute("aria-describedby") == page.locator(_BUBBLE).get_attribute("id")


def test_manufacturing_settings_uses_the_same_tip(page, ui_server, api):
    page.set_viewport_size({"width": 1280, "height": 800})
    _open_settled(page, f"{ui_server}/settings/manufacturing")
    page.locator(".info-tip").first.hover()
    box = _bubble_box(page)
    assert _inside_viewport(page, box), box
    assert page.locator(_BUBBLE).count() == 1


def test_keyboard_focus_opens_and_escape_closes(page, ui_server, api):
    page.goto(f"{ui_server}/finance/company-details", wait_until="load")
    tip = page.locator(".info-tip").first
    tip.focus()
    _bubble_box(page)
    page.keyboard.press("Escape")
    page.locator(_BUBBLE).wait_for(state="hidden", timeout=3000)
    assert tip.get_attribute("aria-describedby") is None


def test_narrow_viewport_clamps_and_flips_below(page, ui_server, api):
    page.set_viewport_size({"width": 360, "height": 740})
    page.goto(f"{ui_server}/finance/company-details", wait_until="load")
    tip = page.locator(".info-tip").first
    # Put the icon hard against the top of the viewport: no room above, so it opens below.
    tip.evaluate("el => window.scrollBy(0, el.getBoundingClientRect().top - 2)")
    tip.focus()
    box = _bubble_box(page)
    assert _inside_viewport(page, box), box
    assert box["y"] >= tip.bounding_box()["y"], "with no room above, the tip opens below the icon"


def _tab_to(page, index: int) -> None:
    """Move keyboard focus with Tab until it lands on the ``index``-th info icon."""
    for _ in range(80):
        page.keyboard.press("Tab")
        if page.evaluate("i => document.activeElement === document.querySelectorAll('.info-tip')[i]", index):
            return
    raise AssertionError("Tab never reached the info icon")


def _near_icon(tip_box: dict, bubble_box: dict) -> bool:
    """The bubble sits directly above or below the icon, not wherever it was first placed."""
    above = abs(tip_box["y"] - (bubble_box["y"] + bubble_box["height"])) <= 12
    below = abs(bubble_box["y"] - (tip_box["y"] + tip_box["height"])) <= 12
    return above or below


def test_tab_to_an_icon_below_the_fold_shows_its_tip(page, ui_server, api):
    """Focusing an icon below the fold scrolls it into view; the tip opens next to it."""
    page.set_viewport_size({"width": 1280, "height": 240})
    page.goto(f"{ui_server}/settings/manufacturing", wait_until="load")
    last = page.locator(".info-tip").count() - 1
    tip = page.locator(".info-tip").nth(last)
    assert tip.evaluate("el => el.getBoundingClientRect().top > window.innerHeight"), "icon must start below the fold"
    _tab_to(page, last)
    page.wait_for_timeout(300)
    box = _bubble_box(page)
    assert _inside_viewport(page, box), box
    assert _near_icon(tip.bounding_box(), box), (tip.bounding_box(), box)


def test_tip_follows_its_icon_on_scroll_and_closes_when_the_icon_leaves(page, ui_server, api):
    page.set_viewport_size({"width": 1280, "height": 600})
    page.goto(f"{ui_server}/settings/manufacturing", wait_until="load")
    page.evaluate("() => { document.body.style.paddingBottom = '1500px'; }")
    tip = page.locator(".info-tip").nth(1)
    tip.evaluate("el => window.scrollBy(0, el.getBoundingClientRect().top - 350)")
    tip.focus()
    first = _bubble_box(page)
    page.evaluate("() => window.scrollBy(0, 60)")
    page.wait_for_timeout(300)
    box = _bubble_box(page)
    assert box != first and _near_icon(tip.bounding_box(), box), (first, tip.bounding_box(), box)
    tip.evaluate("el => window.scrollBy(0, el.getBoundingClientRect().bottom + 20)")
    assert tip.evaluate("el => el.getBoundingClientRect().bottom <= 0"), "the icon must have scrolled out of view"
    page.locator(_BUBBLE).wait_for(state="hidden", timeout=3000)


def test_opening_a_second_tip_releases_the_first(page, ui_server, api):
    page.goto(f"{ui_server}/settings/manufacturing", wait_until="load")
    first, second = page.locator(".info-tip").nth(0), page.locator(".info-tip").nth(1)
    first.hover()
    _bubble_box(page)
    second.focus()
    bubble_id = page.locator(_BUBBLE).get_attribute("id")
    assert second.get_attribute("aria-describedby") == bubble_id
    assert first.get_attribute("aria-describedby") is None
    assert page.locator(_BUBBLE).inner_text() == second.get_attribute("data-tip")


def test_tip_reopens_after_its_bubble_is_removed_from_the_page(page, ui_server, api):
    page.goto(f"{ui_server}/settings/manufacturing", wait_until="load")
    tip = page.locator(".info-tip").first
    tip.focus()
    _bubble_box(page)
    page.keyboard.press("Escape")
    page.evaluate("() => document.querySelector('[role=tooltip]').remove()")
    tip.blur()
    tip.focus()
    _bubble_box(page)


@pytest.fixture
def touch_page(browser_context):
    """A signed-in phone-sized page that receives touch taps."""
    ctx = browser_context.browser.new_context(has_touch=True, is_mobile=True, viewport={"width": 390, "height": 800})
    ctx.add_cookies(browser_context.cookies())
    p = ctx.new_page()
    yield p
    ctx.close()


def test_tap_toggles_the_tip_and_a_tap_elsewhere_closes_it(touch_page, ui_server, api):
    page = touch_page
    page.goto(f"{ui_server}/settings/manufacturing", wait_until="load")
    tip = page.locator(".info-tip").first
    # Only the checkbox this icon's label wraps: other page checkboxes fill in from background fetches.
    checked = lambda: tip.evaluate("t => t.closest('label').querySelector('input[type=checkbox]').checked")
    before = checked()
    tip.tap()
    _bubble_box(page)
    assert checked() == before, "tapping the icon must not change the setting its label wraps"
    tip.tap()
    page.locator(_BUBBLE).wait_for(state="hidden", timeout=3000)
    tip.tap()
    _bubble_box(page)
    page.locator("h1, .page-title").first.tap()
    page.locator(_BUBBLE).wait_for(state="hidden", timeout=3000)
