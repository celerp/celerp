# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A line with no inventory item is ticked, unticked and sent like any other line.

Select all is tri-state and every row's box can be unticked. Reserve and Set as available
send the item-less rows to the server, which leaves them as they are and names them in a
notice that stays. Each row is named by its line id, an empty one when the row has none.
Set as scanned and Print labels act on the stocked rows and name the rows they leave out."""
from __future__ import annotations

import re
import uuid

import pytest

from .conftest import answer_confirms

pytestmark = pytest.mark.browser

_GIA = {"description": "GIA Certificate", "name": "GIA Certificate", "quantity": 1, "unit_price": 150.0}


def _stone(api, loc_id: str | None = None) -> dict:
    sku = f"ILL-{uuid.uuid4().hex[:8]}"
    r = api.post("/items", json={"status": "available", "sku": sku, "name": sku, "quantity": 1, "sell_by": "piece",
                                 **({"location_id": loc_id} if loc_id else {})})
    assert r.status_code in (200, 201), r.text
    return {"entity_id": r.json()["id"], "sku": sku}


def _stone_line(stone: dict) -> dict:
    return {"sku": stone["sku"], "name": stone["sku"], "entity_id": stone["entity_id"], "item_id": stone["entity_id"],
            "quantity": 1, "unit_price": 100.0, "sell_by": "piece"}


def _invoice(api, lines: list[dict], *, finalize: bool = True) -> str:
    r = api.post("/docs", json={"doc_type": "invoice", "status": "draft", "line_items": lines})
    assert r.status_code in (200, 201), r.text
    doc_id = r.json()["id"]
    if finalize:
        assert api.post(f"/docs/{doc_id}/finalize").status_code in (200, 201)
    return doc_id


def _quotation(api, lines: list[dict]) -> str:
    r = api.post("/lists", json={"list_type": "quotation", "status": "draft", "line_items": lines})
    assert r.status_code in (200, 201), r.text
    return r.json().get("entity_id") or r.json()["id"]


def _line_ids(api, path: str) -> list[str]:
    return [li.get("line_id") for li in api.get(path).json()["line_items"]]


def _status(api, stone: dict) -> str:
    return api.get(f"/items/{stone['entity_id']}").json()["status"]


def _posted(req, name: str) -> list[str]:
    """The values a form request sent under ``name``, multipart or urlencoded."""
    body = req.post_data or ""
    multipart = re.findall(r'name="%s"\r\n\r\n([^\r]*)' % re.escape(name), body)
    if multipart or "Content-Disposition" in body:
        return multipart
    from urllib.parse import parse_qsl
    return [v for k, v in parse_qsl(body, keep_blank_values=True) if k == name]


def _sent(path: str):
    """Matches the request that runs the line action at ``path``, not its preview (dry_run)."""
    return lambda q: q.method == "POST" and q.url.endswith(path) and _posted(q, "dry_run") != ["1"]


def _bound(path: str):
    """Matches the request that runs the stock line action at ``path`` bound to the confirm its
    preview asked (``binding``). An action sent unbound never matches, so a test waiting for it
    fails."""
    return lambda q: _sent(path)(q) and _posted(q, "binding") not in ([], [""])


def _persistent_toast(page, text: str) -> str:
    toast = page.locator(".toast-container .toast--persist", has_text=text).first
    toast.wait_for(state="visible", timeout=10000)
    return toast.inner_text()


def _row_number(page, text: str) -> int:
    """The 1-based position of the line row showing ``text``."""
    rows = page.locator("tbody tr:has(.li-select)")
    for i in range(rows.count()):
        row = rows.nth(i)
        if text in (row.inner_text() or "") or row.locator(f"input[value='{text}']").count():
            return i + 1
    raise AssertionError(f"no row shows {text!r}")


def _select_all_state(page) -> dict:
    return page.evaluate("() => { const sa = document.getElementById('li-select-all');"
                         " return {checked: sa.checked, indeterminate: sa.indeterminate}; }")


def _record_label_submits(page) -> None:
    page.evaluate("""() => { window.__labelSubmits = [];
      HTMLFormElement.prototype.submit = function() {
        window.__labelSubmits.push(Array.from(this.querySelectorAll('input[name=selected]')).map(i => i.value)); }; }""")


@pytest.mark.parametrize("finalized", [True, False], ids=["finalized", "draft"])
def test_select_all_is_tri_state_and_untick_works(page, ui_server, api, finalized):
    doc_id = _invoice(api, [_stone_line(_stone(api)), dict(_GIA)], finalize=finalized)
    page.goto(f"{ui_server}/docs/{doc_id}", wait_until="domcontentloaded")
    page.wait_for_selector("#li-select-all", timeout=8000)
    boxes = page.locator("tbody .li-select")
    assert boxes.count() == 2
    page.check("#li-select-all")
    assert boxes.nth(0).is_checked() and boxes.nth(1).is_checked()
    boxes.nth(1).uncheck(timeout=3000)
    assert not boxes.nth(1).is_checked() and boxes.nth(0).is_checked()
    assert _select_all_state(page) == {"checked": False, "indeterminate": True}
    boxes.nth(0).uncheck(timeout=3000)
    assert _select_all_state(page) == {"checked": False, "indeterminate": False}
    boxes.nth(0).check(timeout=3000)
    boxes.nth(1).check(timeout=3000)
    assert _select_all_state(page) == {"checked": True, "indeterminate": False}


_NOT_STOCK = "isn't an inventory item, so it will be left as it is."


def test_set_available_sends_held_and_item_less_rows(page, ui_server, api):
    asked: list[str] = []
    answer_confirms(page, lambda question: asked.append(question) or True)

    # A finalized invoice: a reserved stone, an available stone and a certificate, all ticked.
    held, free = _stone(api), _stone(api)
    doc_id = _invoice(api, [_stone_line(held), _stone_line(free), dict(_GIA)])
    held_id, free_id, gia_id = _line_ids(api, f"/docs/{doc_id}")
    r = api.post(f"/docs/{doc_id}/reserve-lines", json={"line_ids": [held_id], "new_status": "reserved"})
    assert r.status_code in (200, 201), r.text
    page.goto(f"{ui_server}/docs/{doc_id}", wait_until="domcontentloaded")
    page.check("#li-select-all")
    page.locator("#li-bulk-select").select_option(value="li-revert")
    with page.expect_request(_bound("/set-available")) as sent:
        page.locator("#li-bulk-revert-btn").click()
    # The confirm counts the one line it acts on and names the two it leaves up front.
    assert asked[-1] == (f'Set 1 line as available? Line 2 "{free["sku"]}" has nothing held or out, so it will '
                         f'be left as it is. Line 3 "GIA Certificate" {_NOT_STOCK}')
    assert _posted(sent.value, "line_id") == [held_id, free_id, gia_id]
    assert _posted(sent.value, "selected") == []
    assert '3 "GIA Certificate"' in _persistent_toast(page, "GIA Certificate")
    assert f'2 "{free["sku"]}"' in _persistent_toast(page, "nothing held or out")
    assert _status(api, held) == "available" and _status(api, free) == "available"

    # Reserve on the finalized invoice posts every ticked row, the certificate included.
    page.check("#li-select-all")
    page.locator("#li-bulk-select").select_option(value="li-reserve")
    with page.expect_request(_bound("/reserve-lines")) as sent:
        page.evaluate("liBulkReserveConfirmed()")
    assert asked[-1] == f'Set 2 lines as reserved? Line 3 "GIA Certificate" {_NOT_STOCK}'
    assert _posted(sent.value, "line_id") == [held_id, free_id, gia_id]

    # A List: a reserved stone and a certificate. Set as available releases the stone.
    stone = _stone(api)
    list_id = _quotation(api, [_stone_line(stone), dict(_GIA)])
    stone_line, gia_line = _line_ids(api, f"/lists/{list_id}")
    r = api.post(f"/lists/{list_id}/reserve-lines", json={"line_ids": [stone_line], "new_status": "reserved"})
    assert r.status_code in (200, 201), r.text
    assert _status(api, stone) == "reserved"
    page.goto(f"{ui_server}/lists/{list_id}", wait_until="domcontentloaded")
    page.check("#li-select-all")
    with page.expect_request(_bound("/set-available")) as sent:
        page.evaluate("liBulkAvailableConfirmed()")
    assert asked[-1] == f'Set 1 line as available? Line 2 "GIA Certificate" {_NOT_STOCK}'
    assert _posted(sent.value, "line_id") == [stone_line, gia_line]
    assert '2 "GIA Certificate"' in _persistent_toast(page, "GIA Certificate")
    assert _status(api, stone) == "available"

    # The List status action Reserve posts every ticked row too.
    page.check("#li-select-all")
    with page.expect_request(_bound("/reserve-lines")) as sent:
        page.evaluate("liBulkReserveConfirmed()")
    assert _posted(sent.value, "line_id") == [stone_line, gia_line]


_STOCK_ACTIONS = [("li-fulfill", "#li-bulk-fulfill-btn", "shipped"),
                  ("li-reserve", "#li-bulk-reserve-btn", "reserved"),
                  ("li-revert", "#li-bulk-revert-btn", "available")]


@pytest.mark.parametrize("action, button, word", _STOCK_ACTIONS, ids=[a[2] for a in _STOCK_ACTIONS])
def test_every_stock_action_confirms_the_count_and_names_the_lines_it_leaves(page, ui_server, api, action, button, word):
    """Set as shipped, reserved and available ask the same way: the count of lines they act on,
    then the lines they leave as they are; the notice leads with the count. A choice of only
    such lines goes without a confirm and is refused in the action's own words. Red statement:
    Set as shipped asked "Set the selected lines as shipped?" and named no line."""
    asked: list[str] = []
    answer_confirms(page, lambda question: asked.append(question) or True)
    stones = [_stone(api), _stone(api)]
    doc_id = _invoice(api, [*map(_stone_line, stones), dict(_GIA)])
    if word == "available":
        r = api.post(f"/docs/{doc_id}/reserve-lines", json={"line_ids": _line_ids(api, f"/docs/{doc_id}")[:2],
                                                           "new_status": "reserved"})
        assert r.status_code in (200, 201), r.text
    page.goto(f"{ui_server}/docs/{doc_id}", wait_until="domcontentloaded")

    # Only the certificate: no question, and the refusal says why in this action's words.
    page.locator("tbody .li-select").nth(2).check()
    page.locator("#li-bulk-select").select_option(value=action)
    page.locator(button).click()
    toast = page.locator(".toast-container .toast--error").last
    toast.wait_for(state="visible", timeout=8000)
    assert asked == []
    assert toast.inner_text().startswith('Line 3 "GIA Certificate" isn\'t an inventory item, so it has no status '
                                         'to change. Choose a line that holds stock instead.')

    # Every line: the question counts the two stones and names the certificate.
    page.goto(f"{ui_server}/docs/{doc_id}", wait_until="domcontentloaded")
    page.check("#li-select-all")
    page.locator("#li-bulk-select").select_option(value=action)
    page.locator(button).click()
    notice = _persistent_toast(page, "GIA Certificate")
    assert asked == [f'Set 2 lines as {word}? Line 3 "GIA Certificate" {_NOT_STOCK}']
    assert notice.startswith(f"2 lines set as {word}.")
    expected = {"shipped": "sold", "reserved": "reserved", "available": "available"}[word]
    assert [_status(api, s) for s in stones] == [expected, expected]


def test_set_as_shipped_asks_about_the_lines_it_ships_and_names_the_rest(page, ui_server, api):
    """An invoice with a shipped stone, an available stone and a certificate, all ticked: Set as
    shipped asks about the one line it ships and names the other two, then ships it. Red
    statement: the page counted the shipped stone too ("Set 2 lines as shipped?") and the
    server then refused the whole request, the available stone included."""
    asked: list[str] = []
    answer_confirms(page, lambda question: asked.append(question) or True)
    shipped, free = _stone(api), _stone(api)
    doc_id = _invoice(api, [_stone_line(shipped), _stone_line(free), dict(_GIA)])
    shipped_id = _line_ids(api, f"/docs/{doc_id}")[0]
    r = api.post(f"/docs/{doc_id}/fulfill-lines", json={"line_ids": [shipped_id]})
    assert r.status_code in (200, 201), r.text
    page.goto(f"{ui_server}/docs/{doc_id}", wait_until="domcontentloaded")
    page.check("#li-select-all")
    page.locator("#li-bulk-select").select_option(value="li-fulfill")
    with page.expect_request(_bound("/fulfill-lines")):
        page.locator("#li-bulk-fulfill-btn").click()
    notice = _persistent_toast(page, "GIA Certificate")
    assert asked == [f'Set 1 line as shipped? Line 1 "{shipped["sku"]}" has already shipped, so it will be left '
                     f'as it is. Line 3 "GIA Certificate" {_NOT_STOCK}']
    assert notice.startswith("1 line set as shipped.")
    assert _status(api, free) == "sold"


def test_set_available_on_lines_with_nothing_out_does_not_send_the_user_back(page, ui_server, api):
    """A List of stones that are all available, and a certificate: Set as available has
    nothing to change. Red statement: the refusal said "Choose the inventory lines instead"
    to a user who had chosen them."""
    stones = [_stone(api), _stone(api)]
    list_id = _quotation(api, [*map(_stone_line, stones), dict(_GIA)])
    answer_confirms(page)
    page.goto(f"{ui_server}/lists/{list_id}", wait_until="domcontentloaded")
    page.check("#li-select-all")
    page.evaluate("liBulkAvailableConfirmed()")
    toast = page.locator(".toast-container .toast--error").last
    toast.wait_for(state="visible", timeout=8000)
    said = toast.inner_text()
    assert "instead" not in said
    assert "nothing held or out" in said and '3 "GIA Certificate"' in said


def test_line_selection_sends_an_empty_id_for_a_row_without_one(page, ui_server, api):
    first, second = _stone(api), _stone(api)
    doc_id = _invoice(api, [_stone_line(first), _stone_line(second)])
    first_id, _ = _line_ids(api, f"/docs/{doc_id}")
    answer_confirms(page)
    page.goto(f"{ui_server}/docs/{doc_id}", wait_until="domcontentloaded")
    page.check("#li-select-all")
    page.locator("tbody .li-select").nth(1).evaluate("cb => cb.removeAttribute('data-line-id')")
    page.locator("#li-bulk-select").select_option(value="li-fulfill")
    # The preview names the rows as the action would; it is refused, so the action never runs.
    with page.expect_request(lambda q: q.method == "POST" and q.url.endswith("/fulfill-lines")) as sent:
        page.locator("#li-bulk-fulfill-btn").click()
    assert _posted(sent.value, "line_id") == [first_id, ""]
    assert _posted(sent.value, "selected") == []
    toast = page.locator(".toast-container .toast--error").last
    toast.wait_for(state="visible", timeout=8000)
    assert "no longer on this record" in toast.inner_text()
    assert _status(api, first) == "available" and _status(api, second) == "available"


def test_set_scanned_and_print_labels_name_skipped_rows(page, ui_server, api):
    loc = str(api.post("/companies/me/locations", json={"name": f"Ill-{uuid.uuid4().hex[:6]}",
                                                         "type": "warehouse"}).json()["id"])
    stones = [_stone(api, loc), _stone(api, loc)]
    audit_id = api.post("/lists/audit", json={"location_id": loc}).json()["id"]
    audit = api.get(f"/lists/{audit_id}").json()
    r = api.patch(f"/lists/{audit_id}", json={"expected_version": audit["version"], "fields_changed": {
        "line_items": {"new": [*audit["line_items"], dict(_GIA)]}}})
    assert r.status_code == 200, r.text
    assert page.request.post(f"{ui_server}/lists/{audit_id}/action/finalize").ok

    # Set as scanned: the two stones are scanned; the certificate is named.
    page.goto(f"{ui_server}/lists/{audit_id}", wait_until="domcontentloaded")
    page.check("#li-select-all")
    gia_row = _row_number(page, "GIA Certificate")
    with page.expect_request(_sent("/set-scanned")) as sent:
        page.evaluate("liBulkSetScanned(true)")
    lids = [li["line_id"] for li in api.get(f"/lists/{audit_id}").json()["line_items"]]
    assert sorted(_posted(sent.value, "line_id")) == sorted(lids)
    # The notice leads with the count, then names the certificate in scanning's own words.
    scan_notice = _persistent_toast(page, "GIA Certificate")
    assert scan_notice.startswith(f'2 lines marked as scanned. Line {gia_row} "GIA Certificate" isn\'t an '
                                  'inventory item, so there is nothing to scan'), scan_notice
    # The rows come back unticked, and so does Select all.
    assert page.evaluate("[...document.querySelectorAll('#line-body .li-select')].some(c => c.checked)") is False
    assert page.evaluate("(s => [s.checked, s.indeterminate])(document.getElementById('li-select-all'))") == [False, False]
    scanned = [li for li in api.get(f"/lists/{audit_id}").json()["line_items"] if li.get("audited_at")]
    assert sorted(li["item_id"] for li in scanned) == sorted(s["entity_id"] for s in stones)

    # Print labels on the List: labels for the stones; the certificate is named.
    page.goto(f"{ui_server}/lists/{audit_id}", wait_until="domcontentloaded")
    _record_label_submits(page)
    page.check("#li-select-all")
    page.evaluate("liBulkLabelsConfirmed()")
    assert [sorted(s) for s in page.evaluate("window.__labelSubmits")] == [sorted(s["entity_id"] for s in stones)]
    assert f'{gia_row} "GIA Certificate"' in _persistent_toast(page, "GIA Certificate")

    # Print labels on a finalized invoice: the same.
    doc_stones = [_stone(api), _stone(api)]
    doc_id = _invoice(api, [*map(_stone_line, doc_stones), dict(_GIA)])
    page.goto(f"{ui_server}/docs/{doc_id}", wait_until="domcontentloaded")
    _record_label_submits(page)
    page.check("#li-select-all")
    page.evaluate("liBulkLabelsConfirmed()")
    assert [sorted(s) for s in page.evaluate("window.__labelSubmits")] == [sorted(s["entity_id"] for s in doc_stones)]
    assert '3 "GIA Certificate"' in _persistent_toast(page, "GIA Certificate")
