"""A company reset the server refuses leaves the owner on the reset modal reading why,
instead of moving on as if the company had been reset."""
from __future__ import annotations

import re

import pytest

pytestmark = pytest.mark.browser

_REFUSAL = "This company cannot be reset because records in ext_links that belong to another company refer to its data."


def test_a_refused_reset_shows_its_reason_in_the_modal(page, ui_server, fresh_company):
    page.route("**/settings/company/reset", lambda route: route.fulfill(
        status=200, content_type="text/html", body=f'<div class="flash flash--error">{_REFUSAL}</div>'))
    name = fresh_company.get("/companies/me").json()["name"]
    page.goto(f"{ui_server}/settings/general?tab=company")
    page.locator(".btn--danger.btn--outline").first.click()
    page.locator("#company-reset-step1 .btn--secondary").click()
    page.locator("#company-reset-confirm-input").fill(name)
    with page.expect_response("**/settings/company/reset"):
        page.locator("#company-reset-confirm-btn").click()
    page.wait_for_timeout(1000)

    assert "/settings/general" in page.url
    message = page.locator("#company-reset-modal").get_by_text(_REFUSAL)
    assert message.is_visible()


def test_an_accepted_reset_leaves_the_settings_page(page, ui_server, fresh_company):
    name = fresh_company.get("/companies/me").json()["name"]
    page.goto(f"{ui_server}/settings/general?tab=company")
    page.locator(".btn--danger.btn--outline").first.click()
    page.locator("#company-reset-step1 .btn--secondary").click()
    page.locator("#company-reset-confirm-input").fill(name)
    page.locator("#company-reset-confirm-btn").click()
    # The owner has other companies, so the reset signs them into one of those.
    page.wait_for_url(re.compile(r"/(\?.*)?$|/dashboard"))
