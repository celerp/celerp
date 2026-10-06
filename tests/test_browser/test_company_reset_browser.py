# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Browser journeys for Reset this company: the dialog offers the company backup, asks
for the exact company name and shows a refusal inside itself; resetting a login's last
company lands on starting a new one, moving books in from another system or restoring a
backup, and an owner whose other company is still being moved in lands on that move."""

from __future__ import annotations

import os
import re
import uuid
from pathlib import Path

import pytest

from ui.i18n import t

from .test_company_backup_browser import _WAIT_MS, _add_user, _client, _db, _session_company, _token_for
from .test_migration_journeys_browser import _start_additional, held_runner  # noqa: F401 - fixture
from .test_migration_wizard_browser import _run_id, _through_review, _upload

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
    flash.locator(f"text={t('settings.reset_name_mismatch', 'en')}").wait_for(timeout=_WAIT_MS)
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


def _reset(page, name: str) -> None:
    _open_reset(page)
    page.locator('#company-reset-modal button:has-text("Skip - continue")').click()
    page.locator("#company-reset-confirm-input").fill(name)
    page.locator("#company-reset-confirm-btn").click()


def test_resetting_lands_on_the_owners_company_still_being_moved_in(playwright, ui_server, fresh_company,
                                                                    held_runner):
    """The owner's other company is still being moved in: the reset lands on that move."""
    source = fresh_company.get("/companies/me").json()
    user_id, _ = _add_user(fresh_company, "owner")
    moved = f"Moving In {uuid.uuid4().hex[:6]}"
    browser = playwright.chromium.launch(headless=True)
    try:
        ctx = browser.new_context(base_url=ui_server)
        ctx.add_cookies([{"name": "celerp_token", "value": _token_for(user_id, source["id"]),
                          "domain": "127.0.0.1", "path": "/"}])
        page = ctx.new_page()
        _start_additional(page, moved)
        page.click('button:has-text("Create company and migrate")')
        page.wait_for_url(re.compile(r"/migrations/[0-9a-f-]{36}$"), timeout=_WAIT_MS)
        run_id = _run_id(page)
        [(staged, is_staged)] = _db("SELECT id, is_migration_staged FROM companies WHERE name = %s", moved)
        assert is_staged and _session_company(ctx) == source["id"]

        _reset(page, source["name"])

        page.wait_for_url(re.compile(rf"/migrations/{run_id}$"), timeout=_WAIT_MS)
        assert _db("SELECT count(*) FROM companies WHERE id = %s", source["id"])[0][0] == 0
        assert _session_company(ctx) == str(staged)
        assert page.locator('button:has-text("Cancel")').count() == 1
        _shot(page, "6-reset-lands-on-the-move-in-progress")
        # Coming back later returns to the same move.
        page.goto("/")
        page.wait_for_url(re.compile(rf"/migrations/{run_id}$"), timeout=_WAIT_MS)
    finally:
        browser.close()


def _start_over(page, source: dict, user_id: str, email: str) -> None:
    _reset(page, source["name"])

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


def _no_sideways_scroll(page) -> bool:
    return page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


def test_a_login_with_no_company_moves_its_books_in(playwright, ui_server, fresh_company, held_runner):
    source = fresh_company.get("/companies/me").json()
    user_id, email = _add_user(fresh_company, "owner")
    moved = f"Moved In {uuid.uuid4().hex[:6]}"
    browser = playwright.chromium.launch(headless=True)
    errors: list[str] = []
    from celerp.gateway.state import get_session_token, set_session_token
    before = get_session_token()
    try:
        ctx = browser.new_context(base_url=ui_server, viewport={"width": 1440, "height": 900})
        ctx.add_cookies([{"name": "celerp_token", "value": _token_for(user_id, source["id"]),
                          "domain": "127.0.0.1", "path": "/"}])
        page = ctx.new_page()
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("response", lambda r: errors.append(f"{r.status} {r.url}") if r.status >= 500 else None)
        _reset(page, source["name"])
        page.wait_for_url(re.compile(r"/setup/start-company$"), timeout=_WAIT_MS)
        assert "still exists" in page.content()
        for width, height in ((390, 844), (1440, 900)):
            page.set_viewport_size({"width": width, "height": height})
            assert _no_sideways_scroll(page), width
        _shot(page, "7-start-company-ways-back-in")

        page.click('a:has-text("Move from another system")')
        page.wait_for_url(re.compile(r"/setup/start-company/migrate$"), timeout=_WAIT_MS)
        page.set_viewport_size({"width": 390, "height": 844})
        assert _no_sideways_scroll(page)
        page.set_viewport_size({"width": 1440, "height": 900})
        page.fill("#email", email)
        page.fill("#password", "TeamMember123!")
        _upload(page, "Example Bookkeeping")
        _through_review(page, moved)
        assert page.locator("#email").input_value() == email
        set_session_token("test-session-token-for-browser-tests")
        page.fill("#password", "TeamMember123!")
        page.click('button:has-text("Create company and migrate")')
        page.wait_for_url(re.compile(r"/migrations/[0-9a-f-]{36}$"), timeout=_WAIT_MS)
        _shot(page, "8-moving-in-with-no-company")

        [(staged, is_staged)] = _db("SELECT id, is_migration_staged FROM companies WHERE name = %s", moved)
        assert is_staged and _session_company(ctx) == str(staged)
        assert _db("SELECT count(*) FROM user_companies WHERE user_id = %s", user_id)[0][0] == 1
        # Back on start-company while signed in goes home, which is the move.
        page.goto("/setup/start-company/migrate")
        page.wait_for_url(re.compile(r"/migrations/[0-9a-f-]{36}$"), timeout=_WAIT_MS)
        assert errors == []
    finally:
        set_session_token(before)
        browser.close()
