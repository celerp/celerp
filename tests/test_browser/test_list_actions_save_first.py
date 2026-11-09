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
        const open0 = XMLHttpRequest.prototype.open, send0 = XMLHttpRequest.prototype.send;
        XMLHttpRequest.prototype.open = function (m, u) { this._held = held(m, u); return open0.apply(this, arguments); };
        XMLHttpRequest.prototype.send = function (body) {
            if (!this._held) return send0.call(this, body);
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
@pytest.mark.parametrize("status,body,shown_in", [
    (500, {"error": _BLOCKED}, ".toast--error"),
    (409, {"code": "stale_version", "error": _BLOCKED}, "#save-status")])
def test_a_failed_line_save_stops_the_action(page, ui_server, api, action, status, body, shown_in):
    label, path, _ = _LIST_ACTIONS[action]
    list_id = _draft_list(api)
    _fail(page, "**/lines", "POST", status, body)
    page.goto(f"{ui_server}/lists/{list_id}", wait_until="load")
    log = _sent(page)
    _type_quantity(page, "3")
    _click(page, label)
    page.locator(shown_in, has_text=_BLOCKED).first.wait_for(timeout=8000)
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
    page.wait_for_timeout(800)
    assert not any(u.endswith("/action/write-off") for _, u in log), log
    assert api.get(f"/lists/{list_id}").json()["status"] == "draft"
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
