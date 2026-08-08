# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Browser tests: reorder-digest upsell nudge for a non-paid account.

A fresh company runs on a local instance with no live paid relay tunnel, so it
is non-paid. Turning the low-stock digest on for the first time saves the
setting and opens a nudge toward hands-off Connect delivery. The nudge is
dismissable and never unsets the saved digest.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.browser

_REORDER = "/settings/inventory?tab=reorder"
_SAVE = "form[action='/settings/inventory/reorder'] button[type=submit]"


def _enable_digest(page, ui_server) -> None:
    page.goto(f"{ui_server}{_REORDER}", wait_until="domcontentloaded")
    page.wait_for_selector("input[name=reorder_alert_email]", timeout=10000)
    box = page.locator("input[name=reorder_alert_email]")
    if box.is_checked():
        box.uncheck()
    box.check()
    page.click(_SAVE)


def test_reorder_digest_free_checked_shows_upsell(page, ui_server, fresh_company):
    """Non-paid off->on save: the box stays checked (saved) and the upsell modal is visible."""
    _enable_digest(page, ui_server)
    page.wait_for_selector("#digest-upsell-modal", state="visible", timeout=10000)
    assert page.locator("#digest-upsell-modal").is_visible()
    assert page.locator("input[name=reorder_alert_email]").is_checked()


def test_reorder_upsell_cancel_dismisses(page, ui_server, fresh_company):
    """The explicit Cancel closes the nudge, keeps the reorder tab, and the digest stays saved."""
    _enable_digest(page, ui_server)
    page.wait_for_selector("#digest-upsell-modal", state="visible", timeout=10000)
    page.click("#digest-upsell-modal button:has-text('Continue on your own plan')")
    page.wait_for_selector("#digest-upsell-modal", state="detached", timeout=10000)
    assert page.locator("#digest-upsell-modal").count() == 0
    assert "tab=reorder" in page.url
    assert page.locator("input[name=reorder_alert_email]").is_checked()
