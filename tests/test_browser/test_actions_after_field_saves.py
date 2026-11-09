# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A field save and an action clicked after it are independent requests: a refused field never
holds back another field's save, and a refusal never holds back the action it names."""
from __future__ import annotations

import time

import pytest

from .conftest import answer_confirms
from .test_subscriptions_e2e import _draft

pytestmark = pytest.mark.browser


def _sent(page) -> list:
    """Every non-GET request the page sends, as (method, path), in order."""
    log: list = []
    page.on("request", lambda q: q.method != "GET" and log.append((q.method, q.url.split("?")[0])))
    return log


def _wait_sent(page, log: list, method: str, suffix: str, timeout: float = 8.0) -> bool:
    # The page's waits, not time.sleep: the request listener runs only while Playwright is called.
    deadline = time.time() + timeout
    while time.time() < deadline:
        if any(m == method and u.endswith(suffix) for m, u in log):
            return True
        page.wait_for_timeout(100)
    return False


def _edit_header_date(page, field: str, value: str) -> None:
    page.locator(f"[hx-get$='/field/{field}/edit']").first.click()
    editor = page.locator(f"input[hx-patch$='/field/{field}']").first
    editor.wait_for(timeout=5000)
    editor.fill(value)
    editor.evaluate("e => e.blur()")


def test_a_field_saved_while_another_shows_a_refusal_is_stored(page, ui_server, api):
    r = api.post("/docs", json={"doc_type": "invoice", "status": "draft", "issue_date": "2026-11-01",
                                "due_date": "2026-12-01", "line_items": [
                                    {"name": "W", "description": "W", "quantity": 1, "unit_price": 100.0,
                                     "line_total": 100.0}]})
    assert r.status_code in (200, 201), r.text
    path = f"/docs/{r.json()['id']}"
    answer_confirms(page)
    page.goto(f"{ui_server}{path}", wait_until="load")
    log = _sent(page)
    # A due date before the issue date is refused, and its editor stays open with the refusal.
    _edit_header_date(page, "due_date", "2026-10-01")
    page.locator(".toast--error .toast__msg").first.wait_for(timeout=8000)
    _edit_header_date(page, "issue_date", "2026-11-05")
    assert _wait_sent(page, log, "PATCH", "/field/issue_date"), log
    deadline = time.time() + 8
    while time.time() < deadline and api.get(path).json().get("issue_date", "")[:10] != "2026-11-05":
        page.wait_for_timeout(100)
    assert api.get(path).json()["issue_date"][:10] == "2026-11-05"


def test_activate_is_sent_after_a_refused_schedule_change(page, ui_server, api):
    eid = _draft(api, {"unit_price": 100.0, "line_total": 100.0, "taxes": []})
    answer_confirms(page)
    page.goto(f"{ui_server}/subscriptions/{eid}", wait_until="load")
    log = _sent(page)
    # A draft refuses to stop at the end of its period; the refusal names Activate.
    page.locator("select[hx-patch$='/schedule/cancel_at_period_end']").select_option("true")
    page.locator(".toast--error .toast__msg").first.wait_for(timeout=8000)
    page.get_by_role("button", name="Activate", exact=True).click()
    assert _wait_sent(page, log, "POST", f"/subscriptions/{eid}/activate"), log
