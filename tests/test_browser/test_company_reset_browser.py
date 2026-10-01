# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Browser journeys for Reset this company: the dialog offers the company backup, asks
for the exact company name and shows a refusal inside itself; resetting a login's last
company lands on starting a new one."""

from __future__ import annotations

import os
import re
import uuid
from pathlib import Path

import pytest

from .test_company_backup_browser import _WAIT_MS, _add_user, _client, _db, _session_company, _token_for

pytestmark = pytest.mark.browser

_COMPANY_TAB = "/settings/general?tab=company"
_SHOTS = os.environ.get("COMPANY_RESET_SCREENSHOTS")


def _shot(page, name: str) -> None:
    """Save a screenshot when a directory is configured for them."""
    if _SHOTS:
        page.screenshot(path=str(Path(_SHOTS) / f"{name}.png"), full_page=True)


def _companies_named(name: str) -> int:
    return _db("SELECT count(*) FROM companies WHERE name = %s", name)[0][0]


def _open_reset(page) -> None:
    page.goto(_COMPANY_TAB)
    page.click('button:has-text("Reset this company")')
    page.wait_for_selector("#company-reset-modal[open]", timeout=_WAIT_MS)


def test_reset_dialog_needs_the_exact_name_and_shows_refusals_inside(page, fresh_company, api_server):
    source = fresh_company.get("/companies/me").json()
    name = source["name"]
    _open_reset(page)
    dialog = page.locator("#company-reset-modal")
    assert dialog.locator('a:has-text("Download company backup first")').get_attribute("href") \
        == "/company-backup/download"
    assert dialog.locator(f'strong:has-text("{name}")').count() >= 1
    _shot(page, "1-reset-dialog")

    dialog.locator('button:has-text("Skip - continue")').click()
    confirm = page.locator("#company-reset-confirm-btn")
    field = page.locator("#company-reset-confirm-input")
    field.fill(name.lower())
    assert confirm.is_disabled()
    field.fill(name)
    assert confirm.is_enabled()

    # The company is renamed elsewhere while the dialog is open: the server refuses the
    # old name and the reason shows inside the dialog, which stays open.
    renamed = f"Renamed {uuid.uuid4().hex[:6]}"
    with _client(api_server, fresh_company.headers["Authorization"].split(" ", 1)[1]) as c:
        assert c.patch("/companies/me", json={"name": renamed}).status_code == 200
    confirm.click()
    flash = page.locator("#company-reset-flash")
    flash.locator("text=does not match this company's name").wait_for(timeout=_WAIT_MS)
    assert "Nothing was deleted." in flash.inner_text()
    assert page.locator("#company-reset-modal[open]").count() == 1
    _shot(page, "2-reset-refused-inside-dialog")
    assert _companies_named(renamed) == 1

    # The page now shows the new name; resetting under it goes through.
    _open_reset(page)
    page.locator('#company-reset-modal button:has-text("Skip - continue")').click()
    page.locator("#company-reset-confirm-input").fill(renamed)
    page.locator("#company-reset-confirm-btn").click()
    page.wait_for_url(re.compile(r"/(\?.*)?$|/dashboard"), timeout=_WAIT_MS)
    assert _companies_named(renamed) == 0
    assert _session_company(page.context) != source["id"]


def _start_over(page, source: dict, user_id: str, email: str) -> None:
    _open_reset(page)
    page.locator('#company-reset-modal button:has-text("Skip - continue")').click()
    page.locator("#company-reset-confirm-input").fill(source["name"])
    page.locator("#company-reset-confirm-btn").click()

    page.wait_for_url(re.compile(r"/setup/start-company$"), timeout=_WAIT_MS)
    assert _db("SELECT count(*) FROM companies WHERE id = %s", source["id"])[0][0] == 0
    assert _db("SELECT count(*) FROM users WHERE id = %s", user_id)[0][0] == 1
    _shot(page, "3-start-a-new-company")

    fresh_name = f"Fresh Start {uuid.uuid4().hex[:6]}"

    def submit():
        page.fill("#email", email)
        page.fill("#password", "TeamMember123!")
        page.fill("#company_name", fresh_name)
        page.click('button:has-text("Start a new company")')

    # Another user is still signed in and there is no relay: the page says why it cannot go on.
    submit()
    page.locator("text=Direct connections can only serve one authenticated user at a time").wait_for(
        timeout=_WAIT_MS)
    _shot(page, "4-start-refused-while-someone-else-is-signed-in")

    from celerp.gateway.state import get_session_token, set_session_token
    before = get_session_token()
    set_session_token("test-session-token-for-browser-tests")
    try:
        submit()
        page.wait_for_url(re.compile(r"/setup/company"), timeout=_WAIT_MS)
    finally:
        set_session_token(before)
    _shot(page, "5-new-company-created")
    assert _db("SELECT count(*) FROM companies c JOIN user_companies uc ON uc.company_id = c.id "
               "WHERE c.name = %s AND uc.user_id = %s AND uc.role = 'owner'", fresh_name, user_id)[0][0] == 1


def test_resetting_the_last_company_lands_on_starting_a_new_one(playwright, ui_server, fresh_company):
    source = fresh_company.get("/companies/me").json()
    user_id, email = _add_user(fresh_company, "owner")
    browser = playwright.chromium.launch(headless=True)
    try:
        ctx = browser.new_context(base_url=ui_server)
        ctx.add_cookies([{"name": "celerp_token", "value": _token_for(user_id, source["id"]),
                          "domain": "127.0.0.1", "path": "/"}])
        page = ctx.new_page()
        _start_over(page, source, user_id, email)
    finally:
        browser.close()
