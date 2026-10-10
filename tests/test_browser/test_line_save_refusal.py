# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A line save the server refuses puts the stored lines back in place, without a page
reload, and says why in one toast after one request, however the edit was committed (Tab,
Enter or a click elsewhere); Escape cancels an edit without saving; deleting selected lines
asks first."""
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


def _refuse_saves(page) -> list[str]:
    """Refuse every line save the way the server does, and record each request sent."""
    posts: list[str] = []

    def handle(route):
        posts.append(route.request.method)
        route.fulfill(status=400, content_type="application/json",
                      body=json.dumps({"error": _REFUSAL, "restore": True}))

    page.route("**/docs/*/lines", handle)
    return posts


def _settle(page) -> None:
    # Longer than the autosave delay plus a round trip, so a second save would have been sent.
    page.wait_for_timeout(1500)


def _toasts(page) -> int:
    return page.locator(".toast__msg", has_text=_REFUSAL).count()


def _type_quantity(rows, value: str):
    qty = rows.first.locator('[data-name="quantity"]')
    qty.click()
    qty.press("Control+a")
    qty.type(value)
    return qty


def _commit_by(page, qty, how: str) -> None:
    if how == "tab":
        qty.press("Tab")
    elif how == "enter":
        qty.press("Enter")
    else:
        page.locator("h1").first.click()


@pytest.mark.parametrize("how", ["tab", "enter", "click-away"])
def test_a_refused_edit_sends_one_save_and_one_message(page, ui_server, api, draft, how):
    """Tab, Enter and a click elsewhere each commit the edit once. The refused save puts the
    stored lines back without a reload and says why once; swapping the rows in never sends
    a second save."""
    rows = _open(page, ui_server, draft)
    posts = _refuse_saves(page)

    qty = _type_quantity(rows, "9")
    _commit_by(page, qty, how)

    page.locator(".toast__msg", has_text=_REFUSAL).first.wait_for(timeout=8000)
    page.wait_for_function(
        "parseFloat(document.querySelector('#line-body tr [data-name=\"quantity\"]').value) === 5", timeout=8000)
    _settle(page)
    assert len(posts) == 1, f"{how}: one refused edit must send one save, sent {len(posts)}"
    assert _toasts(page) == 1, f"{how}: one refused edit must show one message, showed {_toasts(page)}"
    assert page.evaluate("window.__sameLoad") is True, "the page must not reload"
    assert rows.count() == 2


def test_escape_cancels_the_edit_without_saving(page, ui_server, api, draft):
    rows = _open(page, ui_server, draft)
    posts = _refuse_saves(page)

    qty = _type_quantity(rows, "9")
    qty.press("Escape")

    _settle(page)
    assert float(qty.input_value()) == 5, "Escape must put the field back to its value before the edit"
    assert posts == [], "Escape must not save"
    assert _toasts(page) == 0
    assert page.evaluate("document.activeElement === document.body"), "Escape must leave the field"


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


def test_any_refused_save_restores_the_stored_lines(page, ui_server, api, draft):
    """A save refused for a reason other than a protected line (here a date check) puts the
    deleted row back too, with the reason in a toast."""
    rows = _open(page, ui_server, draft)
    reason = "The due date cannot be before the issue date."
    page.route("**/docs/*/lines", lambda route: route.fulfill(
        status=400, content_type="application/json", body=json.dumps({"error": reason})))
    rows.first.locator(".li-select").check()
    page.select_option("#li-bulk-select", "li-delete")
    page.once("dialog", lambda d: d.accept())
    with page.expect_response(lambda r: r.url.endswith("/lines") and r.request.method == "POST"):
        page.click("#li-bulk-delete-btn")

    page.locator(".toast__msg", has_text=reason).wait_for(timeout=8000)
    page.wait_for_function("document.querySelectorAll('#line-body tr').length === 2", timeout=8000)
    assert page.evaluate("window.__sameLoad") is True, "the page must not reload"
