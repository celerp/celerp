# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The footer's star link keeps its star and its words on one line on a phone."""
from __future__ import annotations

import pytest
from playwright.sync_api import Page

pytestmark = pytest.mark.browser


@pytest.mark.parametrize("lang", ["en", "de"])
@pytest.mark.parametrize("width", [390, 1280])
def test_footer_star_link_stays_on_one_line(page: Page, fresh_company, ui_server, lang, width):
    page.context.add_cookies([{"name": "celerp_lang", "value": lang, "url": ui_server}])
    page.set_viewport_size({"width": width, "height": 800})
    page.goto("/inventory")
    star = page.locator("#star-cta")
    star.wait_for(state="visible")
    lines = star.evaluate("el => new Set(Array.from(el.getClientRects()).map(r => Math.round(r.top))).size")
    assert lines == 1, f"star link '{star.inner_text()}' breaks over {lines} lines"
