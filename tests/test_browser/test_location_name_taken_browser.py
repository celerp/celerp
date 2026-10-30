# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Adding a location a second time, while the first new one still has its starting name,
tells the user the name is taken instead of failing without a word, and adds nothing."""
from __future__ import annotations

import pytest

from ui.i18n import t

from .test_company_backup_browser import _WAIT_MS
from .test_demo_items_link import _seed_demo_items

pytestmark = pytest.mark.browser

_ADD = 'button[hx-post="/settings/locations/new"]'


def _names(api) -> list[str]:
    return [loc["name"] for loc in api.get("/companies/me/locations").json()["items"]]


def test_adding_a_location_twice_says_the_name_is_taken(page, fresh_company):
    _seed_demo_items(fresh_company)
    page.goto("/settings/inventory?tab=locations")
    page.click(_ADD)
    # The add answers with HX-Redirect to this tab, which reloads it with the new row.
    page.locator("td", has_text="New Location").first.wait_for(timeout=_WAIT_MS)
    page.wait_for_load_state()
    before = _names(fresh_company)
    assert before.count("New Location") == 1

    page.click(_ADD)
    message = page.locator("#global-ui-error")
    message.wait_for(state="visible", timeout=_WAIT_MS)
    assert message.inner_text().strip() == t("company.err_location_name_taken", name="New Location")
    assert page.url.endswith("/settings/inventory?tab=locations")
    assert _names(fresh_company) == before
