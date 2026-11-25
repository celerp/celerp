# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Browser test: one user's column setup is the same on every device. Two browser contexts
stand for two devices signed in as the same user; a column hidden or shown on either is what
the other shows on its next visit, even when that device has opened the list before."""
from __future__ import annotations

import uuid

import pytest

from .conftest import saved_column_layout, session_cookies

pytestmark = pytest.mark.browser

_SHOWN = ("(k) => { var th = document.querySelector('#data-table thead th[data-key=\"' + k + '\"]');"
          " return !!th && th.offsetParent !== null; }")


@pytest.fixture
def other_device(browser_context, ui_server, seeded_user):
    ctx = browser_context.browser.new_context(base_url=ui_server)
    ctx.add_cookies(session_cookies(seeded_user))
    ctx.route("**/health/system", lambda route: route.fulfill(json={"overall": "ok"}))
    yield ctx.new_page()
    ctx.close()


def _open(page, ui_server):
    page.goto(f"{ui_server}/inventory", wait_until="domcontentloaded")
    page.wait_for_selector("#data-table thead th[data-key]", timeout=10000)


def _toggle(page, key):
    page.locator("#col-mgr-btn").click()
    page.locator(f"#col-chk-{key}").click()
    page.keyboard.press("Escape")


def test_a_column_change_on_one_device_shows_on_the_other(page, other_device, ui_server, api):
    for _ in range(2):
        api.post("/items", json={"sku": f"DEV-{uuid.uuid4().hex[:8]}", "name": "Device Item",
                                 "quantity": 1, "sell_by": "piece"}).raise_for_status()
    _open(other_device, ui_server)  # the second device has opened the list before
    _open(page, ui_server)
    key = page.locator("#col-mgr-menu input[name=cols]:checked").first.get_attribute("value")
    assert page.evaluate(_SHOWN, key)

    _toggle(page, key)
    saved_column_layout(api, "inventory", lambda l: (l.get("vis") or {}).get(key) is False)
    _open(other_device, ui_server)
    assert not other_device.evaluate(_SHOWN, key), f"column {key} hidden on one device still shows on the other"

    _toggle(other_device, key)
    saved_column_layout(api, "inventory", lambda l: (l.get("vis") or {}).get(key) is True)
    _open(page, ui_server)
    assert page.evaluate(_SHOWN, key), f"column {key} shown again on one device stays hidden on the other"
