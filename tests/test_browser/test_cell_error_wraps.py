# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A refusal shown inside a table cell wraps inside the table, so the whole message,
including what to do next, can be read without scrolling sideways."""

from __future__ import annotations

import pytest

from .conftest import _set_auth_cookie
from .test_company_backup_browser import _WAIT_MS, _add_user, _session_company, _token_for

pytestmark = pytest.mark.browser


def test_refusal_in_a_users_cell_wraps_inside_the_table(page, browser_context, fresh_company, seeded_user):
    company = _session_company(browser_context)
    owner_id = next(u["id"] for u in fresh_company.get("/companies/me/users").json()["items"]
                    if u.get("is_install_owner"))
    uid, _ = _add_user(fresh_company, "owner")
    _set_auth_cookie(browser_context, _token_for(uid, company))
    page.set_viewport_size({"width": 1280, "height": 800})
    page.goto("/settings/general?tab=users")
    page.click(f'td[hx-get="/settings/users/{owner_id}/is_active/edit"]')
    page.wait_for_selector("td.cell--editing select", timeout=_WAIT_MS)
    page.select_option("td.cell--editing select", "false")
    error = page.locator(".data-table .cell-error")
    error.wait_for(timeout=_WAIT_MS)
    assert "Global Config" in error.inner_text()
    # The message's right edge stays inside the card that holds the table.
    overflow = error.evaluate(
        "el => el.getBoundingClientRect().right - el.closest('table').parentElement.getBoundingClientRect().right")
    assert overflow <= 1
