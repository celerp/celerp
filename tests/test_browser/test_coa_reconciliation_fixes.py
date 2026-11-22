# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Chart-of-accounts add/edit, end to end: accounts can be added, edited, and
deactivated without corrupting posted history."""
from __future__ import annotations

import uuid
from pathlib import Path

import pytest

pytestmark = pytest.mark.browser

OUT = Path("context/reviews/coa-reconciliation")


def _no_crash(page, ctx=""):
    body = page.locator("body").inner_text()
    assert "Internal Server Error" not in body, f"{ctx}: 500"
    assert "Traceback (most recent call last)" not in body, f"{ctx}: traceback"


def _shot(page, name):
    OUT.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(OUT / f"{name}.png"), full_page=True)


@pytest.fixture(scope="module")
def seeded_chart(api):
    r = api.post("/accounting/chart/seed")
    assert r.status_code in (200, 409), r.text


def test_chart_of_accounts_add_edit_deactivate(page, ui_server, api, seeded_chart):
    page.set_viewport_size({"width": 1440, "height": 1000})

    # Add: the tab's Add Account button leads to a working form.
    page.goto(f"{ui_server}/settings/accounting?tab=chart", wait_until="domcontentloaded")
    _no_crash(page, "chart-tab")
    page.click('a[href="/settings/accounting/chart/new"]')
    page.wait_for_url("**/settings/accounting/chart/new")
    _no_crash(page, "add-account-form")
    page.fill('input[name="code"]', "1999")
    page.fill('input[name="name"]', "Test Clearing")
    page.select_option('select[name="account_type"]', "asset")
    page.click('button[type="submit"]')
    page.wait_for_url("**/settings/accounting?tab=chart")
    _no_crash(page, "chart-after-add")
    row = page.locator('tr:has(a[href="/settings/accounting/chart/1999/edit"])')
    assert row.count() == 1
    assert "Test Clearing" in row.inner_text()
    _shot(page, "01-chart-account-added")

    # Edit: rename in place.
    page.click('a[href="/settings/accounting/chart/1999/edit"]')
    page.wait_for_url("**/settings/accounting/chart/1999/edit")
    _no_crash(page, "edit-account-form")
    page.fill('input[name="name"]', "Test Clearing Renamed")
    page.click('button[type="submit"]')
    page.wait_for_url("**/settings/accounting?tab=chart")
    row = page.locator('tr:has(a[href="/settings/accounting/chart/1999/edit"])')
    assert "Test Clearing Renamed" in row.inner_text()
    _shot(page, "02-chart-account-renamed")

    # Duplicate code: the 409 renders inline and no second 1999 row appears.
    page.goto(f"{ui_server}/settings/accounting/chart/new", wait_until="domcontentloaded")
    page.fill('input[name="code"]', "1999")
    page.fill('input[name="name"]', "Duplicate Attempt")
    page.click('button[type="submit"]')
    page.wait_for_selector("#account-form-error .error-banner")
    assert "1999" in page.locator("#account-form-error .error-banner").inner_text()
    assert "/settings/accounting/chart/new" in page.url
    _shot(page, "03-duplicate-code-inline-error")
    page.goto(f"{ui_server}/settings/accounting?tab=chart", wait_until="domcontentloaded")
    assert page.locator('a[href="/settings/accounting/chart/1999/edit"]').count() == 1

    # Post a journal entry crediting 1999, then deactivate it: the account
    # must stay visible (Inactive badge), never disappear from the chart.
    je = api.post("/accounting/journal-entries", json={
        "ts": "2026-03-10", "memo": "Clearing test",
        "entries": [
            {"account": "1111", "debit": 50.0, "credit": 0},
            {"account": "1999", "debit": 0, "credit": 50.0},
        ],
        "idempotency_token": uuid.uuid4().hex,
    })
    assert je.status_code == 200, je.text

    page.goto(f"{ui_server}/settings/accounting/chart/1999/edit", wait_until="domcontentloaded")
    page.select_option('select[name="is_active"]', "false")
    page.click('button[type="submit"]')
    page.wait_for_url("**/settings/accounting?tab=chart")
    _no_crash(page, "chart-after-deactivate")
    row = page.locator('tr:has(a[href="/settings/accounting/chart/1999/edit"])')
    assert row.count() == 1, "deactivated account must remain listed"
    assert "inactive" in row.inner_text().lower()
    _shot(page, "04-account-deactivated-still-listed")

