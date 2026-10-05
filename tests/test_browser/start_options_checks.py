# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Layout checks for the restore / move option boxes (ui/components/start_options.py),
shared by the setup page and the dashboard card tests so both pages are held to the
same rule."""
from __future__ import annotations

from playwright.sync_api import Page

_BOXES_JS = """root => Array.from(document.querySelectorAll(root + ' .start-option')).map(box => {
  const r = box.getBoundingClientRect();
  const icon = box.querySelector('.start-option-icon').getBoundingClientRect();
  const desc = box.querySelector('.start-option-desc').getBoundingClientRect();
  const title = box.querySelector('.start-option-title');
  return {title: title.textContent, x: r.x, y: r.y, w: r.width, h: r.height,
          iconX: icon.x, descX: desc.x, padLeft: parseFloat(getComputedStyle(box).paddingLeft)};
})"""


def option_boxes(page: Page, root: str) -> list[dict]:
    return page.evaluate(_BOXES_JS, root)


def assert_start_options_layout(page: Page, root: str, restore_title: str, move_title: str) -> None:
    """Restore and Move your books sit side by side when the viewport is wide, Restore
    on the left; at phone width they stack, Restore first. In each box the text starts
    at the box's own left edge, the same x as the icon (no hanging indent)."""
    boxes = option_boxes(page, root)
    titles = [b["title"] for b in boxes]
    assert titles == [restore_title, move_title], titles
    left, right = boxes
    if page.viewport_size["width"] >= 768:
        assert abs(left["y"] - right["y"]) <= 1, f"not on one row: {boxes}"
        assert left["x"] + left["w"] <= right["x"], f"Restore is not left of Move: {boxes}"
    else:
        assert left["y"] + left["h"] <= right["y"], f"not stacked at phone width: {boxes}"
    for b in boxes:
        assert abs(b["descX"] - b["iconX"]) <= 1, f"{b['title']}: text indented past the icon: {b}"
        assert abs(b["descX"] - (b["x"] + b["padLeft"] + 1)) <= 1, f"{b['title']}: text not flush left: {b}"
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
