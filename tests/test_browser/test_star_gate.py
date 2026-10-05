# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The GitHub star card waits for 10 distinct days of use, counted in the browser, and
never shares the dashboard with the "Bring in your data" card. The clock is faked, and
the relay's star copy is served by the test.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from urllib.parse import urlsplit

import psycopg2
import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.browser

_DAY1 = datetime(2026, 3, 2, 10, 0, 0)
_CTA = {"mode": "live", "headline": "Star Celerp on GitHub", "body": "It helps.", "url": "https://github.com/celerp/celerp"}


def _serve_star_copy(page: Page) -> list[str]:
    asked: list[str] = []

    def cta(route):
        if "medium=dashboard" in route.request.url:
            asked.append(route.request.url)
        route.fulfill(status=200, content_type="application/json", body=json.dumps(_CTA))

    page.route("**/stars/cta*", cta)
    page.route("**/stars/badge*", lambda r: r.fulfill(status=200, content_type="application/json", body="{}"))
    return asked


def _open(page: Page, when: datetime) -> None:
    page.clock.set_fixed_time(when)
    page.goto("/dashboard")
    page.wait_for_load_state("load")


def _dismiss_import_card(company) -> None:
    r = company.patch("/companies/me", json={"settings": {"getting_started_dismissed": True}})
    assert r.status_code == 200, r.text


def test_star_hidden_until_tenth_day_of_use(page: Page, fresh_company):
    _dismiss_import_card(fresh_company)
    asked = _serve_star_copy(page)
    for day in range(9):
        _open(page, _DAY1 + timedelta(days=day))
        page.wait_for_timeout(300)
        expect(page.locator("#star-supporter-card")).to_be_hidden()
    assert asked == [], "no star request before the tenth day"
    _open(page, _DAY1 + timedelta(days=9))
    expect(page.locator("#star-supporter-card")).to_be_visible()
    assert len(asked) == 1


def test_repeat_opens_same_day_count_once(page: Page, fresh_company):
    _dismiss_import_card(fresh_company)
    asked = _serve_star_copy(page)
    for hour in range(5):
        _open(page, _DAY1 + timedelta(hours=hour))
    for day in range(1, 9):
        _open(page, _DAY1 + timedelta(days=day))
    page.wait_for_timeout(300)
    expect(page.locator("#star-supporter-card")).to_be_hidden()
    assert asked == []


def test_star_waits_for_import_card_to_go(page: Page, fresh_company):
    asked = _serve_star_copy(page)
    for day in range(12):
        _open(page, _DAY1 + timedelta(days=day))
    expect(page.locator("#getting-started-card")).to_be_visible()
    page.wait_for_timeout(300)
    expect(page.locator("#star-supporter-card")).to_have_count(0)
    assert asked == []
    page.locator("#getting-started-dismiss").click()
    expect(page.locator("#getting-started-card")).to_have_count(0)
    expect(page.locator("#star-supporter-card")).to_be_visible()


def test_long_gap_does_not_trigger_star(page: Page, fresh_company):
    _dismiss_import_card(fresh_company)
    asked = _serve_star_copy(page)
    _open(page, _DAY1)
    _open(page, _DAY1 + timedelta(days=1))
    _open(page, _DAY1 + timedelta(days=61))
    page.wait_for_timeout(300)
    expect(page.locator("#star-supporter-card")).to_be_hidden()
    assert asked == []


def _serve_star_copy_keeping_dismissed(page: Page) -> list[str]:
    """The relay's copy is served by the test, but the install's own dismissed flag
    comes from the real server, so the card's hide decision is the app's."""
    asked: list[str] = []

    def cta(route):
        real = route.fetch().json()
        if "medium=dashboard" in route.request.url:
            asked.append(route.request.url)
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps({**_CTA, "dismissed": real.get("dismissed", False)}))

    page.route("**/stars/cta*", cta)
    page.route("**/stars/badge*", lambda r: r.fulfill(status=200, content_type="application/json", body="{}"))
    return asked


@pytest.fixture
def star_dismissal_cleared():
    """The dismissed flag is for the whole install; clear it after the test so the rest
    of the session's server is as it found it."""
    yield
    parts = urlsplit(os.environ["DATABASE_URL"].replace("+asyncpg", ""))
    conn = psycopg2.connect(host=parts.hostname, port=parts.port, user=parts.username,
                            password=parts.password, dbname=parts.path.lstrip("/"))
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE system_runtime_state SET value = "
                        "(value::jsonb - 'star_prompt_dismissed')::json")
    finally:
        conn.close()


def test_hidden_star_card_never_returns(page: Page, fresh_company, star_dismissal_cleared):
    """Once the install has hidden the star card it stays hidden, even with 10+ days of
    use and the import card gone, which would otherwise show it."""
    _dismiss_import_card(fresh_company)
    assert fresh_company.post("/stars/dismiss").status_code == 200
    asked = _serve_star_copy_keeping_dismissed(page)
    for day in range(12):
        _open(page, _DAY1 + timedelta(days=day))
    page.wait_for_timeout(300)
    expect(page.locator("#getting-started-card")).to_have_count(0)
    assert page.evaluate("window.celerpUseDays(false)") >= 10
    assert len(asked) >= 1, "the card asked the server, and the server said dismissed"
    expect(page.locator("#star-supporter-card")).to_be_hidden()


def test_closing_the_star_card_keeps_it_closed(page: Page, fresh_company, star_dismissal_cleared):
    _dismiss_import_card(fresh_company)
    _serve_star_copy_keeping_dismissed(page)
    for day in range(10):
        _open(page, _DAY1 + timedelta(days=day))
    expect(page.locator("#star-supporter-card")).to_be_visible()
    with page.expect_response("**/stars/dismiss"):
        page.locator("#star-card-dismiss").click()
    expect(page.locator("#star-supporter-card")).to_be_hidden()
    for day in range(10, 13):
        _open(page, _DAY1 + timedelta(days=day))
        page.wait_for_timeout(300)
        expect(page.locator("#star-supporter-card")).to_be_hidden()
