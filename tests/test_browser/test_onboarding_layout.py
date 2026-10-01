# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Getting started and the product import read cleanly on a desktop and a phone.

Each screen loads with no page or console errors, never scrolls the whole page
sideways (a wide table scrolls inside its own box), and on a 390 px phone the
step indicator and the menu control stay on screen."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.browser

VIEWPORTS = {"desktop": (1440, 900), "phone": (390, 844)}

# Twelve columns, wider than either viewport once laid out as a mapping table.
_WIDE_CSV = (
    "Item Code,Item Name,Category,Qty,Unit,Selling Price,Cost Price,Barcode,Supplier,Date Added,Notes,Colour\n"
    "STRAW-10,Straw basket,Baskets,4,pcs,250,120,885000000001,Village craft,2026-08-01,Hand made,Natural\n"
    "TOTE-N,Tote bag,Bags,120,pcs,85,40,885000000002,Village craft,2026-08-02,Cotton,Navy\n"
).encode()


def _watch(page) -> list[str]:
    problems: list[str] = []
    page.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))
    page.on("console", lambda m: problems.append(f"console: {m.text}") if m.type == "error" else None)
    return problems


def _page_overflow(page) -> int:
    return page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")


def _right_edge(page, selector: str) -> float:
    return page.evaluate(
        "s => Math.max(...[...document.querySelectorAll(s)].map(e => e.getBoundingClientRect().right))", selector)


@pytest.fixture(params=list(VIEWPORTS), ids=list(VIEWPORTS))
def sized(page, request):
    width, height = VIEWPORTS[request.param]
    page.set_viewport_size({"width": width, "height": height})
    return page, width


def _check(page, width: int, problems: list[str]) -> None:
    assert problems == [], problems
    assert _page_overflow(page) <= 0, f"page scrolls sideways by {_page_overflow(page)} px"
    if width < 500 and page.locator(".topbar").count():
        menu = page.locator(".sidebar-toggle")
        assert menu.is_visible()
        box = menu.bounding_box()
        assert box and box["x"] >= 0 and box["x"] + box["width"] <= width


@pytest.mark.parametrize("path", ["/onboarding", "/dashboard", "/setup/new-company", "/setup/new-company/migrate"])
def test_getting_started_pages(sized, ui_server, path):
    page, width = sized
    problems = _watch(page)
    page.goto(f"{ui_server}{path}", wait_until="load")
    _check(page, width, problems)


def test_product_import_upload_mapping_and_review(sized, ui_server):
    page, width = sized
    problems = _watch(page)
    page.goto(f"{ui_server}/inventory/import?from_onboarding=1", wait_until="load")
    _check(page, width, problems)

    page.locator("input[type='file']").first.set_input_files(
        {"name": "shop-stock-list.csv", "mimeType": "text/csv", "buffer": _WIDE_CSV})
    page.locator("form[action='/inventory/import/preview'] button[type='submit']").first.click()
    page.wait_for_selector(".column-mapping-table")
    _check(page, width, problems)
    # The mapping table scrolls inside its own box, never past the viewport.
    assert _right_edge(page, ".mapping-scroll-wrapper") <= width
    assert _right_edge(page, ".import-steps .import-step") <= width, "a step of the indicator is off screen"

    with page.expect_navigation():
        page.locator("form[action='/inventory/import/mapped'] button.btn--primary").click()
    page.wait_for_selector(".csv-fix-table")
    _check(page, width, problems)
    assert _right_edge(page, ".import-steps .import-step") <= width, "a step of the indicator is off screen"
    # The review grid scrolls inside its own box, so every price cell can be reached.
    assert _right_edge(page, ".table-scroll") <= width
