# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The held-back notice, per cause, as the user reads it in the notification bell, and
the Doctor page showing the failed step with its error."""
from __future__ import annotations

import json
import os
from urllib.parse import urlsplit

import psycopg2
import pytest

from celerp.held_back import TITLE, UNOWNED_STEP, UPDATE_STEP, Failure, HeldBack, module_start_step, unowned_error
from test_helpers import sidebar_label
from ui import i18n
from ui.i18n import refusal_text

pytestmark = pytest.mark.browser

_CAUSES = {
    "update_failed": HeldBack((Failure(UPDATE_STEP, "OperationalError: could not serialize access"),)),
    "module_off": HeldBack(disabled=("Manufacturing",)),
    "module_not_installed": HeldBack((Failure(UNOWNED_STEP, unowned_error(["zz.widget.made"])),)),
    "module_start_failed": HeldBack((Failure(module_start_step("Manufacturing"),
                                             "ZeroDivisionError: division by zero"),)),
}


def _db(sql: str, params: tuple = ()) -> None:
    parts = urlsplit(os.environ["DATABASE_URL"].replace("+asyncpg", ""))
    conn = psycopg2.connect(host=parts.hostname, port=parts.port, user=parts.username,
                            password=parts.password, dbname=parts.path.lstrip("/"))
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
    finally:
        conn.close()


@pytest.fixture
def held(api_server):
    """Holds the running API back with a given cause and its notice; restores it after."""
    from celerp.main import app

    def hold(cause: HeldBack) -> None:
        app.state.data_current, app.state.held_back = False, cause
        _db("UPDATE notifications SET read = true WHERE title = %s", (TITLE,))
        _db("INSERT INTO notifications (id, company_id, category, title, body, action_url, i18n,"
            " priority, read, created_at) SELECT gen_random_uuid(), id, 'system', %s, %s, %s, %s,"
            " 'high', false, now() FROM companies",
            (TITLE, cause.notice()["message"], cause.action_url, json.dumps(cause.notice_keys())))

    yield hold
    app.state.data_current, app.state.held_back = True, None
    _db("UPDATE notifications SET read = true WHERE title = %s", (TITLE,))


@pytest.mark.parametrize("cause", list(_CAUSES))
def test_the_held_back_notice_and_doctor_show_the_cause(page, held, cause, tmp_path):
    held(_CAUSES[cause])
    page.goto("/")
    page.click(".notif-bell-btn")
    item = page.locator("#notif-panel .notif-item", has_text=TITLE).first
    item.wait_for()
    body = item.locator(".notif-item__body").inner_text()
    assert "You can still view all your records" in body
    assert ("Enable Manufacturing in Modules" in body) is (cause == "module_off")
    panel = page.locator("#notif-panel")
    panel.screenshot(path=str(tmp_path / f"notice-{cause}.png"))
    page.eval_on_selector("#notif-list", "el => { el.scrollTop = el.scrollHeight; }")
    panel.screenshot(path=str(tmp_path / f"notice-{cause}-scrolled.png"))

    assert item.locator("a").get_attribute("href") == _CAUSES[cause].action_url

    page.goto("/doctor")
    start = page.locator("#doctor-start")
    for entry in _CAUSES[cause].report()["failures"]:
        assert entry["step"]["message"] in start.inner_text()
        assert refusal_text(entry["error"]) in start.locator(".doctor-error").all_inner_texts()
    page.screenshot(path=str(tmp_path / f"doctor-{cause}.png"), full_page=True)


def test_doctor_reports_a_clean_start(page, tmp_path):
    page.goto("/doctor")
    page.locator("#doctor-start").wait_for()
    assert page.locator("#doctor-start .doctor-failures").count() == 0
    page.screenshot(path=str(tmp_path / "doctor-clean.png"), full_page=True)


def test_a_german_reader_sees_the_notice_and_doctor_in_german(page, held, ui_server, tmp_path):
    cause = _CAUSES["module_start_failed"]
    held(cause)
    host = ui_server.split("//", 1)[1].split(":", 1)[0]
    page.context.add_cookies([{"name": "celerp_lang", "value": "de", "domain": host, "path": "/"}])
    try:
        page.goto("/")
        page.click(".notif-bell-btn")
        item = page.locator("#notif-panel .notif-item", has_text=i18n.t("held_back.title", "de")).first
        item.wait_for()
        body = item.locator(".notif-item__body").inner_text()
        assert sidebar_label("nav.doctor", "de") in body
        assert "Restart Celerp" not in body and "ask an admin" not in body
        page.locator("#notif-panel").screenshot(path=str(tmp_path / "notice-de.png"))

        page.goto("/doctor")
        start = page.locator("#doctor-start").inner_text()
        assert i18n.t("held_back.title", "de") in start
        assert sidebar_label("nav.report_bug", "de") in start
        assert "Starting the Manufacturing module" not in start
        assert "ZeroDivisionError: division by zero" in start
        page.screenshot(path=str(tmp_path / "doctor-de.png"), full_page=True)
    finally:
        page.context.clear_cookies(name="celerp_lang")
