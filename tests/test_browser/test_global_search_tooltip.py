# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Global search help tooltip: the (?) affordance beside the top search bar reveals
the query syntax on click/focus, lists the operators with a worked example, and
dismisses on ESC.
"""
import pytest

pytestmark = pytest.mark.browser


def test_global_search_tooltip_present(page, ui_server, seeded_user):
    """J4: the (?) help affordance renders on the global bar, opens to show the
    syntax, and closes on ESC.

    Red at merge-base: no help affordance exists on the global bar, so the
    .global-search-help selector never appears and wait_for_selector times out.
    """
    page.goto(f"{ui_server}/dashboard", wait_until="domcontentloaded")

    help_btn = page.wait_for_selector(".global-search-help", timeout=5000)
    panel = page.locator(".global-search-help-panel")

    # Opens on click and shows the syntax with a worked example.
    help_btn.click()
    panel.wait_for(state="visible", timeout=3000)
    text = panel.inner_text()
    assert "&" in text
    assert "5-10" in text

    # ESC dismisses it (GDR 2j).
    page.keyboard.press("Escape")
    panel.wait_for(state="hidden", timeout=3000)


def test_inventory_page_search_scoped_example(page, ui_server, seeded_user):
    """The rendered help panel documents the field-scoped syntax with a per-piece
    worked example (the ct_each alias), so a first-time user sees how to scope a
    field before typing.

    Red at merge-base: the panel shows only the plain, OR, AND, and range rows,
    with no scoped example, so the ct_each text never appears.
    """
    page.goto(f"{ui_server}/inventory", wait_until="domcontentloaded")

    help_btn = page.wait_for_selector(".page-search-wrap .global-search-help", timeout=5000)
    panel = page.locator("#page-search-help-panel")

    help_btn.click()
    panel.wait_for(state="visible", timeout=3000)
    assert "ct_each" in panel.inner_text()


def test_inventory_page_search_tooltip_present(page, ui_server, seeded_user):
    """The in-page inventory search box carries its own (?) affordance with the
    same syntax panel, alongside the global bar's instance.

    Red at merge-base: only the top bar renders a help affordance, so the
    .page-search-wrap .global-search-help selector never appears.
    """
    page.goto(f"{ui_server}/inventory", wait_until="domcontentloaded")

    help_btn = page.wait_for_selector(".page-search-wrap .global-search-help", timeout=5000)
    panel = page.locator("#page-search-help-panel")

    help_btn.click()
    panel.wait_for(state="visible", timeout=3000)
    assert "5-10" in panel.inner_text()

    # ESC dismisses it (GDR 2j).
    page.keyboard.press("Escape")
    panel.wait_for(state="hidden", timeout=3000)
