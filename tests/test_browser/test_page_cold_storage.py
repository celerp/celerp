# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Browser test: every test's page starts with none of the app's localStorage, however the
test before it left its page (here on about:blank, where a page can no longer reach the app's
storage, the way the help capture leaves every step)."""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.browser

_KEY = "celerp_sidebar_groups"


def test_a_page_starts_without_the_storage_a_page_left_on_about_blank_wrote(browser_context, ui_server, request):
    earlier = browser_context.new_page()
    earlier.goto(f"{ui_server}/inventory", wait_until="domcontentloaded")
    earlier.evaluate("(k) => localStorage.setItem(k, JSON.stringify({inventory: false}))", _KEY)
    earlier.goto("about:blank")
    earlier.close()

    page = request.getfixturevalue("page")
    page.goto(f"{ui_server}/help", wait_until="domcontentloaded")
    assert page.evaluate("(k) => localStorage.getItem(k)", _KEY) is None
