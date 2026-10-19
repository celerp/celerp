# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A notice that names Try again, whether the server drew it, a toast shows it or a response
swaps it in, carries a Try again button, and clicking it reloads the page (never on its own)."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.browser

_NOTICE = "Celerp couldn't find this line. It may have been deleted. Select Try again."


def _mark_page(page):
    page.evaluate("window.__stillHere = true")


def _reloaded(page) -> bool:
    return page.evaluate("window.__stillHere !== true")


def test_a_flash_notice_gets_a_try_again_button_that_reloads_when_clicked(page, seeded_user):
    page.goto("/dashboard")
    page.evaluate(
        """msg => { const d = document.createElement('div'); d.className = 'flash flash--error';
                    d.id = 'probe'; d.textContent = msg; document.body.appendChild(d); }""", _NOTICE)
    button = page.locator("#probe a.try-again")
    button.wait_for()
    assert button.inner_text() == "Try again"
    assert page.locator("#probe a.try-again").count() == 1
    _mark_page(page)
    page.wait_for_timeout(500)
    assert not _reloaded(page), "the page reloaded on its own"
    with page.expect_navigation():
        button.click()
    assert _reloaded(page)


def test_a_toast_naming_try_again_gets_the_button(page, seeded_user):
    page.goto("/dashboard")
    page.evaluate("msg => celerpToast(msg, 'error', true)", _NOTICE)
    page.locator(".toast a.try-again").wait_for()
    assert page.locator(".toast a.try-again").count() == 1


def test_a_notice_that_does_not_name_try_again_gets_no_button(page, seeded_user):
    page.goto("/dashboard")
    page.evaluate(
        """() => { const d = document.createElement('div'); d.className = 'flash flash--error';
                   d.id = 'plain'; d.textContent = 'Not saved.'; document.body.appendChild(d); }""")
    page.wait_for_timeout(300)
    assert page.locator("#plain a.try-again").count() == 0
