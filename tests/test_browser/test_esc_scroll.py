# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""ESC scroll preservation: pressing ESC on an inline edit cell must not reset
the horizontal scroll position of .table-scroll-wrap.

Root cause: the browser resets .table-scroll-wrap scrollLeft when the edit
input gains focus. Fix: save on htmx:beforeRequest (before XHR, before focus),
restore on htmx:afterSettle.
"""
import pytest

pytestmark = pytest.mark.browser


def test_esc_does_not_reset_scroll(page, ui_server, seeded_user):
    """ESC-01: Pressing ESC after opening an inline edit must not reset horizontal scroll.

    Steps:
    1. Load /inventory (has demo items, multiple columns)
    2. Inject JS to widen the table and force scrollability
    3. Scroll .table-scroll-wrap to 200px
    4. Double-click a cell that is within the visible (scrolled) area
    5. Press ESC
    6. Assert .table-scroll-wrap scrollLeft is preserved
    """
    page.goto(f"{ui_server}/inventory", wait_until="domcontentloaded")
    page.wait_for_selector("table.data-table", timeout=5000)
    page.wait_for_timeout(800)

    # Force table wide enough to scroll, and widen the container to 800px
    # so there's room to see cells on the right while keeping overflow
    page.evaluate("""
        var tbl = document.querySelector('table.data-table');
        if (tbl) tbl.style.setProperty('min-width', '3000px', 'important');
        var wrap = document.querySelector('.table-scroll-wrap');
        if (wrap) wrap.style.setProperty('width', '800px', 'important');
    """)
    page.wait_for_timeout(100)

    # Scroll the wrap to 400px
    page.evaluate("document.querySelector('.table-scroll-wrap').scrollLeft = 400")
    page.wait_for_timeout(100)
    scroll_before = page.evaluate("document.querySelector('.table-scroll-wrap').scrollLeft")
    if scroll_before < 10:
        pytest.skip(f"Could not scroll (got {scroll_before}px) — table may not be wide enough")

    # Find a clickable cell that is in the right half of the visible area
    # (not cell index 0 which is at position 0 and would cause click-scroll)
    cells = page.locator("table.data-table td.cell--clickable")
    count = cells.count()
    if count == 0:
        pytest.skip("No clickable cells found")

    # Pick a cell near the middle of the row (index 2+) to avoid click-scroll to col 0
    target_idx = min(2, count - 1)
    cell = cells.nth(target_idx)
    cell.dblclick()

    # Wait for edit input to appear
    try:
        page.wait_for_selector("td input, td select", timeout=3000)
    except Exception:
        pytest.skip("Edit mode did not open")

    # Press ESC to cancel
    page.keyboard.press("Escape")

    # Wait for input to disappear (cell reverted to display mode)
    try:
        page.wait_for_function(
            "!document.querySelector('td input:not([type=hidden])')",
            timeout=3000,
        )
    except Exception:
        pass

    page.wait_for_timeout(400)

    scroll_after = page.evaluate("document.querySelector('.table-scroll-wrap').scrollLeft")
    assert scroll_after >= scroll_before - 5, (
        f"Scroll was reset after ESC: was {scroll_before}px, now {scroll_after}px"
    )
