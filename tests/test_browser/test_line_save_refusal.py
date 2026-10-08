# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A line save the server refuses puts the stored lines back in place, without a page
reload, and says why in a toast; deleting selected lines asks first."""
from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.browser

_REFUSAL = "Line 1 (W-1) holds reserved stock. Set it as available before removing it or changing its item."


@pytest.fixture()
def draft(api):
    r = api.post("/docs", json={
        "doc_type": "invoice", "status": "draft",
        "line_items": [
            {"name": "First", "quantity": 5, "unit_price": 10.0, "line_total": 50.0},
            {"name": "Second", "quantity": 2, "unit_price": 7.0, "line_total": 14.0},
        ],
    })
    assert r.status_code in {200, 201}, r.text
    return r.json()["id"]


def _open(page, ui_server, doc_id):
    page.goto(f"{ui_server}/docs/{doc_id}", wait_until="domcontentloaded")
    rows = page.locator("#line-body tr")
    rows.first.locator('[data-name="quantity"]').wait_for(timeout=8000)
    page.evaluate("window.__sameLoad = true")
    return rows


def test_a_refused_save_restores_the_stored_lines(page, ui_server, api, draft):
    rows = _open(page, ui_server, draft)
    page.route("**/docs/*/lines", lambda route: route.fulfill(
        status=400, content_type="application/json",
        body=json.dumps({"error": _REFUSAL, "restore": True})))

    qty = rows.first.locator('[data-name="quantity"]')
    qty.fill("9")
    qty.dispatch_event("input")
    with page.expect_response(lambda r: r.url.endswith("/lines") and r.request.method == "POST"):
        qty.dispatch_event("blur")

    page.locator(".toast__msg", has_text=_REFUSAL).wait_for(timeout=8000)
    page.wait_for_function(
        "parseFloat(document.querySelector('#line-body tr [data-name=\"quantity\"]').value) === 5", timeout=8000)
    assert page.evaluate("window.__sameLoad") is True, "the page must not reload"
    assert rows.count() == 2


def test_delete_selected_asks_first(page, ui_server, api, draft):
    rows = _open(page, ui_server, draft)
    rows.first.locator(".li-select").check()
    page.select_option("#li-bulk-select", "li-delete")

    asked: list[str] = []

    def answer(accept):
        def handler(dialog):
            asked.append(dialog.message)
            dialog.accept() if accept else dialog.dismiss()
        return handler

    page.once("dialog", answer(False))
    page.click("#li-bulk-delete-btn")
    assert asked == ["Delete 1 line?"]
    assert rows.count() == 2

    page.once("dialog", answer(True))
    with page.expect_response(lambda r: r.url.endswith("/lines") and r.request.method == "POST"):
        page.click("#li-bulk-delete-btn")
    assert rows.count() == 1
