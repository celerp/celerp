# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""An owner's inline password reset on Settings > Users keeps the page in shape: a refusal
wraps inside the table's card, and the notice of a done reset stands apart from the table."""

from __future__ import annotations

import pytest

from .test_company_backup_browser import _WAIT_MS, _add_user

pytestmark = pytest.mark.browser


def _reset(page, client, password: str) -> None:
    _, email = _add_user(client, "operator")
    page.set_viewport_size({"width": 1280, "height": 800})
    page.goto("/settings/general?tab=users")
    page.locator("tr", has_text=email).locator("button", has_text="Reset password").click()
    page.wait_for_selector("td.cell--editing input[name=password]", timeout=_WAIT_MS)
    page.fill("td.cell--editing input[name=password]", password)
    page.fill("td.cell--editing input[name=confirm]", password)
    page.click("td.cell--editing button[type=submit]")


def test_a_refused_reset_keeps_the_users_table_inside_its_card(page, fresh_company):
    _reset(page, fresh_company, "x")
    page.locator("td.cell--editing .cell-error").wait_for(timeout=_WAIT_MS)
    # The longest refusal still leaves the table within its card, and the page never scrolls sideways.
    table_over, page_over = page.evaluate("""() => {
        const table = document.querySelector('td.cell--editing').closest('table');
        return [table.getBoundingClientRect().right - table.parentElement.getBoundingClientRect().right,
                document.documentElement.scrollWidth - document.documentElement.clientWidth]; }""")
    assert table_over <= 1 and page_over == 0, (table_over, page_over)


def test_a_done_reset_notice_stands_apart_from_the_table(page, fresh_company):
    _reset(page, fresh_company, "a-new-pass-1")
    page.locator("#users-card .flash--success").wait_for(timeout=_WAIT_MS)
    gap = page.evaluate("""() => document.querySelector('#users-card table').getBoundingClientRect().top
        - document.querySelector('#users-card .flash--success').getBoundingClientRect().bottom""")
    assert gap >= 8, gap
