# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A draft List action that runs on the lines (Issue, Write off stock, Duplicate) saves the
edits still on the page first, as a document's Finalize does: an edit clicked straight past,
or one whose save is still on its way, is in the result, and a save that fails or is refused
stops the action with its own message."""
from __future__ import annotations

import json
import time
import uuid
from unittest.mock import AsyncMock

import pytest

from .conftest import answer_confirms

pytestmark = pytest.mark.browser

_BLOCKED = "Save blocked for this test"


def _sent(page) -> list:
    """Every non-GET request the page sends, as (method, path), in order."""
    log: list = []
    page.on("request", lambda q: q.method != "GET" and log.append((q.method, q.url.split("?")[0])))
    return log


def _wait(cond, timeout: float = 10.0, page=None) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        page.wait_for_timeout(100)
    return False


def _draft_list(api) -> str:
    r = api.post("/lists", json={"list_type": "quotation", "status": "draft", "line_items": [
        {"name": "Row", "description": "Row", "quantity": 1, "unit_price": 10, "line_total": 10}]})
    assert r.status_code in (200, 201), r.text
    return r.json().get("id") or r.json().get("entity_id")


def _draft_writeoff(api) -> tuple[str, str]:
    loc = str(api.post("/companies/me/locations", json={"name": f"Wo-{uuid.uuid4().hex[:6]}",
                                                           "type": "warehouse"}).json()["id"])
    r = api.post("/items", json={"status": "available", "sku": f"WO-{uuid.uuid4().hex[:8]}", "name": "Spoiled",
                                 "sell_by": "piece", "quantity": 5, "location_id": loc})
    assert r.status_code in (200, 201), r.text
    item_id = r.json().get("id") or r.json().get("entity_id")
    w = api.post("/lists/writeoff", json={"entity_ids": [item_id]})
    assert w.status_code in (200, 201), w.text
    return w.json()["id"], item_id


def _type_quantity(page, value: str) -> None:
    qty = page.locator('#line-body tr input[data-name="quantity"]').first
    qty.fill(value)


def _type_qty_out(page, value: str) -> None:
    page.locator("[hx-get$='/qty_out/edit']").first.dblclick()
    editor = page.locator("input[hx-patch$='/qty_out']").first
    editor.wait_for(timeout=5000)
    editor.fill(value)


def _click(page, label: str) -> None:
    page.get_by_role("button", name=label, exact=True).click()


def _delay(page, fragment: str, method: str, ms: int = 2000) -> None:
    """Hold the page's own requests to a matching url for `ms` before they leave, so a save is
    still on its way when the next click lands (a route handler would hold Playwright too)."""
    page.add_init_script("""(([fragment, method, ms]) => {
        const held = (m, u) => String(m).toUpperCase() === method && String(u).includes(fragment);
        const fetch0 = window.fetch;
        window.fetch = function (input, init) {
            const url = typeof input === 'string' ? input : input.url;
            if (!held((init && init.method) || 'GET', url)) return fetch0.call(this, input, init);
            return new Promise(r => setTimeout(r, ms)).then(() => fetch0.call(this, input, init));
        };
        const open0 = XMLHttpRequest.prototype.open, send0 = XMLHttpRequest.prototype.send, mark = Symbol();
        XMLHttpRequest.prototype.open = function (m, u) { this[mark] = held(m, u); return open0.apply(this, arguments); };
        XMLHttpRequest.prototype.send = function (body) {
            if (!this[mark]) return send0.call(this, body);
            setTimeout(() => send0.call(this, body), ms);
        };
    })(%s)""" % json.dumps([fragment, method, ms]))


def _fail(page, pattern: str, method: str, status: int, body: dict) -> None:
    page.route(pattern, lambda route: route.fulfill(status=status, content_type="application/json",
                                                    body=json.dumps(body))
               if route.request.method == method else route.continue_())


# Issue and Duplicate run on a list's lines; each case names how the result is read back.

def _issued_quantity(page, api, list_id) -> float | None:
    if not _wait(lambda: api.get(f"/lists/{list_id}").json().get("status") == "finalized", page=page):
        return None
    return api.get(f"/lists/{list_id}").json()["line_items"][0]["quantity"]


def _duplicate_quantity(page, api, list_id) -> float | None:
    if not _wait(lambda: list_id.split(":")[-1] not in page.url and "/lists/" in page.url, page=page):
        return None
    new_id = page.url.split("/lists/")[1].split("?")[0]
    return api.get(f"/lists/{new_id}").json()["line_items"][0]["quantity"]


_LIST_ACTIONS = {"issue": ("Issue", "/action/finalize", _issued_quantity),
                 "duplicate": ("Duplicate", "/action/duplicate", _duplicate_quantity)}


@pytest.mark.parametrize("action", sorted(_LIST_ACTIONS))
def test_an_edit_clicked_straight_past_is_in_the_result(page, ui_server, api, action):
    label, _, read = _LIST_ACTIONS[action]
    list_id = _draft_list(api)
    page.goto(f"{ui_server}/lists/{list_id}", wait_until="load")
    _type_quantity(page, "3")
    _click(page, label)
    assert read(page, api, list_id) == 3


@pytest.mark.parametrize("action", sorted(_LIST_ACTIONS))
def test_the_action_waits_for_a_slow_line_save(page, ui_server, api, action):
    label, _, read = _LIST_ACTIONS[action]
    list_id = _draft_list(api)
    _delay(page, "/lines", "POST")
    page.goto(f"{ui_server}/lists/{list_id}", wait_until="load")
    _type_quantity(page, "3")
    page.locator('#line-body tr input[data-name="quantity"]').first.evaluate("e => e.blur()")
    page.wait_for_timeout(600)  # past the blur debounce: the save is on its way
    _click(page, label)
    assert read(page, api, list_id) == 3


@pytest.mark.parametrize("action", sorted(_LIST_ACTIONS))
@pytest.mark.parametrize("status,body", [
    (500, {"error": _BLOCKED}),
    (409, {"code": "stale_version", "error": _BLOCKED})])
def test_a_failed_line_save_stops_the_action(page, ui_server, api, action, status, body):
    label, path, _ = _LIST_ACTIONS[action]
    list_id = _draft_list(api)
    _fail(page, "**/lines", "POST", status, body)
    page.goto(f"{ui_server}/lists/{list_id}", wait_until="load")
    log = _sent(page)
    _type_quantity(page, "3")
    _click(page, label)
    # Every refusal of the save shows in a toast as well as on the page's save status.
    page.locator(".toast--error", has_text=_BLOCKED).first.wait_for(timeout=8000)
    page.locator("#save-status", has_text=_BLOCKED).wait_for(timeout=8000)
    page.wait_for_timeout(800)
    assert not any(u.endswith(path) for _, u, in log), log
    stored = api.get(f"/lists/{list_id}").json()
    assert stored["status"] == "draft" and stored["line_items"][0]["quantity"] == 1


# Write off stock removes each line's typed quantity out from stock, after its confirmation.

def _on_hand(api, item_id) -> float:
    return float(api.get(f"/items/{item_id}").json().get("quantity") or 0)


def _written_off(page, api, list_id) -> list | None:
    if not _wait(lambda: api.get(f"/lists/{list_id}").json().get("status") == "closed", page=page):
        return None
    return [li.get("disposed_qty") for li in api.get(f"/lists/{list_id}").json()["line_items"]]


def test_write_off_takes_the_quantity_clicked_straight_past(page, ui_server, api):
    list_id, item_id = _draft_writeoff(api)
    answer_confirms(page)
    page.goto(f"{ui_server}/lists/{list_id}", wait_until="load")
    _type_qty_out(page, "2")
    _click(page, "Write off stock")
    assert _written_off(page, api, list_id) == [2]
    assert _on_hand(api, item_id) == 3


def test_write_off_waits_for_a_slow_quantity_save(page, ui_server, api):
    list_id, item_id = _draft_writeoff(api)
    _delay(page, "/writeoff-line/", "PATCH")
    answer_confirms(page)
    page.goto(f"{ui_server}/lists/{list_id}", wait_until="load")
    _type_qty_out(page, "2")
    _click(page, "Write off stock")
    assert _written_off(page, api, list_id) == [2]
    assert _on_hand(api, item_id) == 3


def test_a_failed_quantity_save_stops_the_write_off(page, ui_server, api):
    list_id, item_id = _draft_writeoff(api)
    _fail(page, "**/writeoff-line/**", "PATCH", 500, {"detail": _BLOCKED})
    answer_confirms(page)
    page.goto(f"{ui_server}/lists/{list_id}", wait_until="load")
    log = _sent(page)
    _type_qty_out(page, "2")
    _click(page, "Write off stock")
    # A cell's failed save shows in the page's error box, as every cell save does.
    page.locator("#global-ui-error", has_text=_BLOCKED).wait_for(timeout=8000)
    page.wait_for_timeout(800)
    assert not any(u.endswith("/action/write-off") for _, u in log), log
    assert api.get(f"/lists/{list_id}").json()["status"] == "draft"
    assert _on_hand(api, item_id) == 5


def test_a_refused_quantity_stops_the_write_off(page, ui_server, api):
    list_id, item_id = _draft_writeoff(api)
    answer_confirms(page)
    page.goto(f"{ui_server}/lists/{list_id}", wait_until="load")
    log = _sent(page)
    _type_qty_out(page, "9")  # more than the 5 on hand: the save answers with its reason in the cell
    _click(page, "Write off stock")
    page.locator("#line-body .cell-error").wait_for(timeout=8000)
    # The cell save's refusal says so as every save refusal does, in a toast too.
    reason = page.locator("#line-body [data-cell-unsaved]").inner_text()
    page.locator(".toast--error", has_text=reason).first.wait_for(timeout=8000)
    page.wait_for_timeout(800)
    assert not any(u.endswith("/action/write-off") for _, u in log), log
    assert api.get(f"/lists/{list_id}").json()["status"] == "draft"
    assert _on_hand(api, item_id) == 5


# Leaving the refused editor as it is keeps the refused entry and its reason: the stored value is
# not restored over it, so the next Write off stock is not sent on the stored quantity.
def test_leaving_a_refused_quantity_keeps_it_and_stops_the_write_off(page, ui_server, api):
    from ui.i18n import t
    list_id, item_id = _draft_writeoff(api)
    line_id = api.get(f"/lists/{list_id}").json()["line_items"][0]["line_id"]
    assert api.post(f"/lists/{list_id}/writeoff-line", json={"line_id": line_id, "qty_out": 4}).status_code == 200
    answer_confirms(page)
    page.goto(f"{ui_server}/lists/{list_id}", wait_until="load")
    log = _sent(page)
    _type_qty_out(page, "9")
    page.locator("input[hx-patch$='/qty_out']").first.evaluate("e => e.blur()")
    page.locator("#line-body .cell-error").wait_for(timeout=8000)
    editor = page.locator("input[hx-patch$='/qty_out']").first
    editor.focus()
    with page.expect_response(lambda r: "/qty_out" in r.url):  # the restore, or the save again
        editor.evaluate("e => e.blur()")
    page.wait_for_timeout(300)
    assert page.locator("#line-body .cell-error").count() == 1
    assert editor.input_value() == "9"
    _click(page, "Write off stock")
    page.locator("#save-status", has_text=t("documents.lines_unsaved", "en")).wait_for(timeout=8000)
    page.locator(".toast--error", has_text=t("documents.lines_unsaved", "en")).first.wait_for(timeout=8000)
    page.wait_for_timeout(800)
    assert not any(u.endswith("/action/write-off") for _, u in log), log
    assert _on_hand(api, item_id) == 5


def test_a_failed_line_save_stops_the_write_off(page, ui_server, api):
    list_id, item_id = _draft_writeoff(api)
    line_id = api.get(f"/lists/{list_id}").json()["line_items"][0]["line_id"]
    assert api.post(f"/lists/{list_id}/writeoff-line", json={"line_id": line_id, "qty_out": 2}).status_code == 200
    _fail(page, "**/lines", "POST", 500, {"error": _BLOCKED})
    answer_confirms(page)
    page.goto(f"{ui_server}/lists/{list_id}", wait_until="load")
    log = _sent(page)
    page.locator('#line-body tr input[data-name="description"]').first.fill("Spoiled in transit")
    _click(page, "Write off stock")
    page.locator(".toast--error", has_text=_BLOCKED).first.wait_for(timeout=8000)
    page.wait_for_timeout(800)
    assert not any(u.endswith("/action/write-off") for _, u in log), log
    assert _on_hand(api, item_id) == 5


def test_cancel_on_the_write_off_question_sends_nothing(page, ui_server, api):
    list_id, item_id = _draft_writeoff(api)
    asked: list = []
    page.goto(f"{ui_server}/lists/{list_id}", wait_until="load")
    log = _sent(page)
    # What the page had sent when the question showed: the action saves nothing before asking.
    answer_confirms(page, lambda q: asked.append(list(log)) and False)
    page.locator('#line-body tr input[data-name="description"]').first.fill("Spoiled in transit")
    _click(page, "Write off stock")
    assert _wait(lambda: asked, page=page)
    page.wait_for_timeout(1000)
    assert asked == [[]]
    # The edit still saves on its own, as any edit left on the page does; the action is never sent.
    assert not any(u.endswith("/action/write-off") for _, u in log), log
    assert api.get(f"/lists/{list_id}").json()["status"] == "draft"
    assert _on_hand(api, item_id) == 5


@pytest.mark.parametrize("action", sorted(_LIST_ACTIONS))
def test_a_line_save_on_its_way_that_fails_stops_the_action(page, ui_server, api, action):
    label, path, _ = _LIST_ACTIONS[action]
    list_id = _draft_list(api)
    _delay(page, "/lines", "POST")
    _fail(page, "**/lines", "POST", 500, {"error": _BLOCKED})
    page.goto(f"{ui_server}/lists/{list_id}", wait_until="load")
    log = _sent(page)
    _type_quantity(page, "3")
    page.locator('#line-body tr input[data-name="quantity"]').first.evaluate("e => e.blur()")
    page.wait_for_timeout(600)  # past the blur debounce: the save is on its way, and will fail
    _click(page, label)
    page.locator(".toast--error", has_text=_BLOCKED).first.wait_for(timeout=8000)
    page.wait_for_timeout(800)
    assert not any(u.endswith(path) for _, u, in log), log
    stored = api.get(f"/lists/{list_id}").json()
    assert stored["status"] == "draft" and stored["line_items"][0]["quantity"] == 1


# A row save rewrites every line through the editor: a write-off line's entry rides along as stored.

def _save_row(page, text: str) -> None:
    with page.expect_response(lambda r: r.url.split("?")[0].endswith("/lines") and r.request.method == "POST") as r:
        field = page.locator('#line-body tr input[data-name="description"]').first
        field.fill(text)
        field.evaluate("e => e.blur()")
    assert r.value.status == 200, r.value.text()


def test_a_row_save_keeps_the_stored_write_off_entry(page, ui_server, api):
    list_id, _ = _draft_writeoff(api)
    line_id = api.get(f"/lists/{list_id}").json()["line_items"][0]["line_id"]
    assert api.post(f"/lists/{list_id}/writeoff-line",
                    json={"line_id": line_id, "qty_out": 2, "comment": "Water damage"}).status_code == 200
    before = api.get(f"/lists/{list_id}").json()["line_items"][0]
    page.goto(f"{ui_server}/lists/{list_id}", wait_until="load")
    _save_row(page, "Spoiled in transit")
    li = api.get(f"/lists/{list_id}").json()["line_items"][0]
    assert (li["qty_out"], li["account"], li["comment"]) == (2, before["account"], "Water damage")


def test_a_row_save_after_a_quantity_save_keeps_it(page, ui_server, api):
    list_id, _ = _draft_writeoff(api)
    page.goto(f"{ui_server}/lists/{list_id}", wait_until="load")
    with page.expect_response(lambda r: "/writeoff-line/" in r.url and r.request.method == "PATCH"):
        _type_qty_out(page, "2")
        page.keyboard.press("Enter")
    _save_row(page, "Spoiled in transit")  # neither refused as someone else's change nor dropping the 2
    li = api.get(f"/lists/{list_id}").json()["line_items"][0]
    assert li["qty_out"] == 2 and li["description"] == "Spoiled in transit"


@pytest.mark.parametrize("action", sorted(_LIST_ACTIONS))
def test_a_failed_save_on_its_way_stops_the_action_even_when_the_retry_saves(page, ui_server, api, action):
    """The page already showed the save's failure, so the action is not sent on top of it, even
    though the action's own save of the same edit then goes through."""
    label, path, _ = _LIST_ACTIONS[action]
    list_id = _draft_list(api)
    _delay(page, "/lines", "POST")
    failed: list = []
    page.route("**/lists/*/lines", lambda route: (failed.append(1), route.fulfill(
        status=500, content_type="application/json", body=json.dumps({"error": _BLOCKED})))
        if route.request.method == "POST" and not failed else route.continue_())
    page.goto(f"{ui_server}/lists/{list_id}", wait_until="load")
    log = _sent(page)
    _type_quantity(page, "3")
    page.locator('#line-body tr input[data-name="quantity"]').first.evaluate("e => e.blur()")
    page.wait_for_timeout(600)  # past the blur debounce: the save is on its way, and will fail
    _click(page, label)
    page.locator(".toast--error", has_text=_BLOCKED).first.wait_for(timeout=8000)
    assert _wait(lambda: len([u for m, u in log if u.endswith("/lines")]) == 2, page=page), log
    page.wait_for_timeout(800)
    assert not any(u.endswith(path) for _, u, in log), log
    assert api.get(f"/lists/{list_id}").json()["status"] == "draft"


# A write-off line comes back from a row save exactly as stored, so the write-off after it removes
# the quantity typed for it.

def test_a_row_save_leaves_a_write_off_line_as_stored(page, ui_server, api):
    list_id, _ = _draft_writeoff(api)
    line_id = api.get(f"/lists/{list_id}").json()["line_items"][0]["line_id"]
    assert api.post(f"/lists/{list_id}/writeoff-line", json={"line_id": line_id, "qty_out": 2}).status_code == 200
    before = api.get(f"/lists/{list_id}").json()["line_items"][0]
    page.goto(f"{ui_server}/lists/{list_id}", wait_until="load")
    _save_row(page, "Spoiled in transit")
    after = api.get(f"/lists/{list_id}").json()["line_items"][0]
    assert after["description"] == "Spoiled in transit"
    stated = ("item_id", "qty_out", "account", "comment", "quantity", "pieces", "weight")
    assert {k: after.get(k) for k in stated} == {k: before.get(k) for k in stated}


def test_a_row_edit_then_write_off_takes_the_typed_quantity(page, ui_server, api):
    list_id, item_id = _draft_writeoff(api)
    answer_confirms(page)
    page.goto(f"{ui_server}/lists/{list_id}", wait_until="load")
    with page.expect_response(lambda r: "/writeoff-line/" in r.url and r.request.method == "PATCH"):
        _type_qty_out(page, "2")
        page.keyboard.press("Enter")
    page.locator('#line-body tr input[data-name="description"]').first.fill("Spoiled in transit")
    _click(page, "Write off stock")
    assert _written_off(page, api, list_id) == [2]
    assert _on_hand(api, item_id) == 3


# A quantity out whose save failed, was refused, or came back as a reason in place of its editor
# stays unsaved on the page: clicking Write off stock again sends nothing and says why, rather
# than writing off the stored quantity.

@pytest.mark.parametrize("typed,failure", [("2", "failed"), ("9", "refused"), ("2", "expired")])
def test_a_second_click_after_an_unsaved_quantity_sends_nothing(page, ui_server, api, typed, failure):
    list_id, item_id = _draft_writeoff(api)
    line_id = api.get(f"/lists/{list_id}").json()["line_items"][0]["line_id"]
    assert api.post(f"/lists/{list_id}/writeoff-line", json={"line_id": line_id, "qty_out": 4}).status_code == 200
    if failure == "failed":
        _fail(page, "**/writeoff-line/**", "PATCH", 500, {"detail": _BLOCKED})
    elif failure == "expired":
        # The cell save's answer to an expired session: its reason in place of the editor.
        from fasthtml.common import to_xml
        from ui.routes.documents import _cell_save_refusal
        expired = to_xml(_cell_save_refusal("Session expired"))
        page.route("**/writeoff-line/**", lambda route: route.fulfill(
            status=200, content_type="text/html", body=expired)
            if route.request.method == "PATCH" else route.continue_())
    answer_confirms(page)
    page.goto(f"{ui_server}/lists/{list_id}", wait_until="load")
    log = _sent(page)
    _type_qty_out(page, typed)
    _click(page, "Write off stock")
    page.wait_for_timeout(2000)
    page.evaluate("document.querySelectorAll('.toast, #global-ui-error').forEach(e => e.remove())")
    _click(page, "Write off stock")
    page.locator(".toast--error").first.wait_for(timeout=8000)
    page.wait_for_timeout(800)
    assert not any(u.endswith("/action/write-off") for _, u in log), log
    assert api.get(f"/lists/{list_id}").json()["status"] == "draft"
    assert _on_hand(api, item_id) == 5


# An edit made while the action's own save is on its way is not in what that save stored: the
# action is not sent and the page says why.

def test_a_row_edit_during_the_save_stops_the_action(page, ui_server, api):
    list_id = _draft_list(api)
    _delay(page, "/lines", "POST")
    page.goto(f"{ui_server}/lists/{list_id}", wait_until="load")
    log = _sent(page)
    _click(page, "Duplicate")
    field = page.locator('#line-body tr input[data-name="description"]').first
    field.fill("Typed during the save")
    field.evaluate("e => e.blur()")
    page.locator(".toast--error").first.wait_for(timeout=8000)
    assert not any(u.endswith("/action/duplicate") for _, u in log), log


def test_a_cell_save_started_during_the_save_stops_the_write_off(page, ui_server, api):
    list_id, item_id = _draft_writeoff(api)
    _delay(page, "/lines", "POST")
    _delay(page, "/writeoff-line/", "PATCH", 4000)
    answer_confirms(page)
    page.goto(f"{ui_server}/lists/{list_id}", wait_until="load")
    log = _sent(page)
    _click(page, "Write off stock")
    page.locator("[hx-get$='/account/edit']").first.dblclick()
    account = page.locator("input[type=hidden][hx-patch$='/account']").first
    account.wait_for(state="attached", timeout=5000)
    account.evaluate("e => e.dispatchEvent(new Event('change', {bubbles: true}))")
    page.locator(".toast--error").first.wait_for(timeout=8000)
    page.wait_for_timeout(4500)
    assert not any(u.endswith("/action/write-off") for _, u in log), log
    assert _on_hand(api, item_id) == 5


# A tab whose record was changed elsewhere since it loaded: the action's save carries the page's
# version, so the server refuses it with its own message and nothing acts, as Finalize does.

def _draft_doc(api, doc_type: str) -> str:
    r = api.post("/docs", json={"doc_type": doc_type, "status": "draft", "line_items": [
        {"name": "W", "description": "W", "quantity": 1, "unit_price": 100.0, "line_total": 100.0, "taxes": []}],
        "total": 100.0, **({"frequency": "monthly", "start_date": "2027-03-01"}
                           if doc_type == "subscription_invoice" else {})})
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


def _change_lines_elsewhere(page, path: str) -> None:
    other = page.context.new_page()
    other.goto(path, wait_until="load")
    with other.expect_response(lambda r: r.url.split("?")[0].endswith("/lines") and r.request.method == "POST") as r:
        qty = other.locator('#line-body tr input[data-name="quantity"]').first
        qty.fill("7")
        qty.evaluate("e => e.blur()")
    assert r.value.status == 200, r.value.text()
    other.close()


_STALE = {"invoice": ("/docs/", "Issue Invoice", "/action/finalize"),
          "quotation": ("/docs/", "Finalize", "/action/finalize"),
          "purchase_order": ("/docs/", "Convert to Bill", "/action/finalize"),
          "subscription_invoice": ("/subscriptions/", "Activate", "/activate"),
          "issue": ("/lists/", "Issue", "/action/finalize"),
          "duplicate": ("/lists/", "Duplicate", "/action/duplicate"),
          "writeoff": ("/lists/", "Write off stock", "/action/write-off")}


@pytest.mark.parametrize("case", sorted(_STALE))
def test_a_stale_tab_acts_on_nothing(page, ui_server, api, case):
    from ui.i18n import t

    base, label, path = _STALE[case]
    if case == "writeoff":
        list_id, _ = _draft_writeoff(api)
        eid = list_id
    else:
        eid = _draft_list(api) if base == "/lists/" else _draft_doc(api, case)
    answer_confirms(page)
    page.goto(f"{ui_server}{base}{eid}", wait_until="load")
    if case == "writeoff":
        line_id = api.get(f"/lists/{eid}").json()["line_items"][0]["line_id"]
        assert api.post(f"/lists/{eid}/writeoff-line", json={"line_id": line_id, "qty_out": 3}).status_code == 200
    else:
        _change_lines_elsewhere(page, f"{ui_server}{base}{eid}")
    log = _sent(page)
    _click(page, label)
    message = t("documents.err_list_changed" if base == "/lists/" else "documents.err_doc_changed", "en")
    page.locator("#save-status", has_text=message).wait_for(timeout=8000)
    page.locator(".toast--error", has_text=message).first.wait_for(timeout=8000)
    page.wait_for_timeout(800)
    assert not any(u.endswith(path) for _, u in log), log
    stored = api.get(f"{'/lists/' if base == '/lists/' else '/docs/'}{eid}").json()
    assert stored["status"] == "draft"


# Send emails the stored document: a line edit still on the page is saved first, and the dialog's
# fields go with the send. The page renders Send only for an instance bound to the relay.

def test_send_saves_the_lines_first_and_posts_its_fields(page, ui_server, api, monkeypatch):
    import ui.api_client
    monkeypatch.setattr(ui.api_client, "get_relay_status", AsyncMock(return_value={"gateway_token_set": True}))
    doc_id = _draft_doc(api, "invoice")
    page.route("**/action/send", lambda route: route.fulfill(status=200, content_type="application/json", body="{}")
               if route.request.method == "POST" else route.continue_())
    page.goto(f"{ui_server}/docs/{doc_id}", wait_until="load")
    log = _sent(page)
    page.locator('#line-body tr input[data-name="description"]').first.fill("Sent as typed")
    _click(page, "Send")
    dialog = page.locator("dialog[open]")
    dialog.locator('input[name="sent_to"]').fill("buyer@example.com")
    with page.expect_request(lambda q: q.url.endswith("/action/send") and q.method == "POST") as sent:
        dialog.get_by_role("button", name="Send Document", exact=True).click()
    body = sent.value.post_data or ""
    assert 'name="sent_to"\r\n\r\nbuyer@example.com\r\n' in body, body
    urls = [u for _, u in log]
    assert any(u.endswith("/lines") for u in urls[:urls.index(sent.value.url)]), log
    page.locator("dialog[open]").wait_for(state="detached", timeout=5000)
    assert api.get(f"/docs/{doc_id}").json()["line_items"][0]["description"] == "Sent as typed"


# Send runs from inside its dialog, which covers the page's save status: a stale tab's refusal shows
# in a toast above it, and nothing is sent.
def test_send_from_a_stale_tab_says_why_above_the_dialog(page, ui_server, api, monkeypatch):
    import ui.api_client
    from ui.i18n import t
    monkeypatch.setattr(ui.api_client, "get_relay_status", AsyncMock(return_value={"gateway_token_set": True}))
    doc_id = _draft_doc(api, "invoice")
    page.route("**/action/send", lambda route: route.fulfill(status=200, content_type="application/json", body="{}")
               if route.request.method == "POST" else route.continue_())
    page.goto(f"{ui_server}/docs/{doc_id}", wait_until="load")
    _change_lines_elsewhere(page, f"{ui_server}/docs/{doc_id}")
    log = _sent(page)
    _click(page, "Send")
    dialog = page.locator("dialog[open]")
    dialog.locator('input[name="sent_to"]').fill("buyer@example.com")
    dialog.get_by_role("button", name="Send Document", exact=True).click()
    toast = page.locator(".toast--error", has_text=t("documents.err_doc_changed", "en")).first
    toast.wait_for(timeout=8000)
    assert toast.is_visible()
    page.wait_for_timeout(800)
    assert not any(u.endswith("/action/send") for _, u in log), log


# A purchase line's account box shows a pick hint after a typed name it does not know; putting the
# box back with Escape leaves nothing unsaved, so the document's action runs.
@pytest.mark.parametrize("doc_type,label", [("purchase_order", "Convert to Bill"), ("bill", "Finalize")])
def test_an_account_box_put_back_does_not_stop_the_action(page, ui_server, api, doc_type, label):
    doc_id = _draft_doc(api, doc_type)
    page.goto(f"{ui_server}/docs/{doc_id}", wait_until="load")
    box = page.locator("#line-body td.col-account input:not([type=hidden])").first
    box.click()
    box.press_sequentially("a", delay=50)
    box.press("Tab")
    page.locator("#line-body .combobox-note").wait_for(timeout=5000)
    box.click()
    box.press("Escape")
    page.locator("h1").first.click()
    assert box.input_value() == ""
    _click(page, label)
    assert _wait(lambda: api.get(f"/docs/{doc_id}").json()["status"] != "draft", page=page)
    assert not page.locator(".toast--error").count()


# A write-off entry editor swapped out during its blur delay never sends its save, so it holds no
# later action: Write off stock goes through on the stored entry.
def test_an_entry_editor_swapped_out_before_its_save_holds_nothing(page, ui_server, api):
    list_id, item_id = _draft_writeoff(api)
    line_id = api.get(f"/lists/{list_id}").json()["line_items"][0]["line_id"]
    assert api.post(f"/lists/{list_id}/writeoff-line", json={"line_id": line_id, "qty_out": 2}).status_code == 200
    answer_confirms(page)
    page.goto(f"{ui_server}/lists/{list_id}", wait_until="load")
    _type_qty_out(page, "4")
    held = page.locator("input[hx-patch$='/qty_out']").first.evaluate(
        "e => { e.blur(); const n = window._celerpLineCellSaves.size;"
        " htmx.swap(e, '<span>--</span>', {swapStyle: 'outerHTML'}); return n; }")
    assert held == 1
    _click(page, "Write off stock")
    assert _written_off(page, api, list_id) == [2]
    assert _on_hand(api, item_id) == 3
