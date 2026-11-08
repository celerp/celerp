# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The save-first gate saves the page's lines before an action only while they differ from the
stored ones. A field that saved itself, or one put back with Escape, leaves nothing to save, so
the action posts on its own with no line save in front of it, once a cell's own save is done."""
from __future__ import annotations

import re
import time
import uuid

import pytest

from .conftest import answer_confirms
from .test_list_mutation_serialization import _quotation

pytestmark = pytest.mark.browser


def _line_saves(page) -> list:
    saves: list = []
    page.on("request", lambda q: q.method == "POST" and q.url.endswith("/lines") and saves.append(q.url))
    return saves


def _issued(page, api, path: str) -> None:
    """Issue the draft open in `page` and wait until the API no longer reports it as a draft."""
    page.locator("button[hx-post$='/action/finalize']").first.click()
    deadline = time.time() + 10
    while time.time() < deadline and api.get(path).json().get("status") == "draft":
        time.sleep(0.2)
    assert api.get(path).json().get("status") != "draft"


def test_a_list_type_change_leaves_issue_without_a_line_save(page, ui_server, api):
    list_id = _quotation(api)
    answer_confirms(page)
    page.goto(f"{ui_server}/lists/{list_id}", wait_until="domcontentloaded")
    saves = _line_saves(page)
    page.locator(".list-type-bar select").select_option("transfer")
    deadline = time.time() + 8
    while time.time() < deadline and api.get(f"/lists/{list_id}").json().get("list_type") != "transfer":
        time.sleep(0.2)
    page.wait_for_load_state("load")
    _issued(page, api, f"/lists/{list_id}")
    assert saves == []


def test_a_customer_picked_on_a_list_leaves_issue_without_a_line_save(page, ui_server, api):
    r = api.post("/crm/contacts", json={"name": "GateCustomer", "contact_type": "customer"})
    assert r.status_code in (200, 201), r.text
    list_id = _quotation(api)
    answer_confirms(page)
    page.goto(f"{ui_server}/lists/{list_id}", wait_until="domcontentloaded")
    saves = _line_saves(page)
    page.locator("[hx-get*='field/contact_id/edit']").first.click()
    inp = page.locator(".combobox-wrap[data-search-url] .combobox-input").first
    inp.press_sequentially("GateCust", delay=20)
    page.locator(".combobox-list.open .combobox-option", has_text="GateCustomer").first.click()
    page.locator("[hx-get*='field/contact_id/edit']", has_text="GateCustomer").first.wait_for(timeout=10000)
    page.wait_for_load_state("load")
    _issued(page, api, f"/lists/{list_id}")
    assert saves == []


def test_a_price_put_back_with_escape_leaves_issue_without_a_line_save(page, ui_server, api):
    r = api.post("/docs", json={"doc_type": "invoice", "status": "draft", "line_items": [
        {"name": "Widget", "quantity": 1, "unit_price": 50.0, "line_total": 50.0}]})
    assert r.status_code in (200, 201), r.text
    doc_id = r.json()["id"]
    answer_confirms(page)
    page.goto(f"{ui_server}/docs/{doc_id}", wait_until="domcontentloaded")
    saves = _line_saves(page)
    price = page.locator('#line-body input[data-name="unit_price"]').first
    price.focus()
    price.fill("100")
    price.press("Escape")
    _issued(page, api, f"/docs/{doc_id}")
    assert saves == []
    assert float(api.get(f"/docs/{doc_id}").json()["line_items"][0]["unit_price"]) == 50.0


# A page whose stored lines no save would accept as they stand: a purchase order's reorder line
# shows its suggested quantity only as a placeholder (no writer stores a line with no quantity, so
# this is the one such page). Nobody has edited it, so nothing is unsaved and every action sends
# its own request.

def _reorder_po(api) -> str:
    """A draft purchase order with a reorder line: an item sold lately, bought by the case (a unit
    the company does not define), stored with quantity 0, so its quantity shows blank with the
    suggestion as a placeholder."""
    sku = f"CASE-{uuid.uuid4().hex[:6]}"
    item = api.post("/items", json={"sku": sku, "name": "Cased tea", "sell_by": "piece", "quantity": 10,
                                     "status": "available"}).json()["id"]
    sale = api.post("/docs", json={"doc_type": "invoice", "status": "draft", "line_items": [
        {"sku": sku, "name": "Cased tea", "quantity": 10, "unit_price": 5.0, "line_total": 50.0, "entity_id": item}]})
    assert sale.status_code in (200, 201), sale.text
    sale_id = sale.json()["id"]
    assert api.post(f"/docs/{sale_id}/finalize").status_code in (200, 201)
    assert api.post(f"/docs/{sale_id}/fulfill-lines", json={"line_entity_ids": [item]}).status_code in (200, 201)
    assert api.get(f"/items/{item}/reorder-suggestion").json()["reorder_qty"]
    r = api.post("/docs", json={"doc_type": "purchase_order", "status": "draft", "line_items": [
        {"description": f"{sku} - Cased tea", "sku": sku, "quantity": 0, "unit": "case", "sell_by": "case",
         "purchase_conversion_factor": 1, "entity_id": item}]})
    assert r.status_code in (200, 201), r.text
    return f"/docs/{r.json()['id']}"


def _sent(page, path: str, run) -> list:
    """The state-changing requests to `path` that `run` sends, once they have answered."""
    seen: list = []

    def answered(resp):
        q = resp.request
        if q.method != "GET" and path in q.url and not q.url.endswith("/lines"):
            seen.append((q.method, q.url.split(path, 1)[1], resp.status))

    page.on("response", answered)
    run()
    deadline = time.time() + 8
    while time.time() < deadline and not seen:
        page.wait_for_timeout(100)
    page.remove_listener("response", answered)
    return seen


def _customer_note(page) -> None:
    note = page.locator("textarea[hx-post$='/field/customer_note']").first
    note.fill("Deliver to the back door")
    note.blur()


def _add_note(page) -> None:
    form = page.locator("form[hx-post$='/notes']").first
    form.evaluate("f => { f.querySelector('textarea').value = 'Call the supplier first'; htmx.trigger(f, 'submit'); }")


def _delete(page) -> None:
    page.locator("summary", has_text="Delete").first.click()
    page.locator("form[hx-post$='/action/delete'] button[type=submit]").first.click()


def test_a_reorder_line_shown_blank_leaves_every_action_its_request(page, ui_server, api):
    path = _reorder_po(api)
    answer_confirms(page)
    page.goto(f"{ui_server}{path}", wait_until="load")
    saves = _line_saves(page)
    assert page.evaluate("_celerpLinesDirty()") is False
    assert [u for _, u, s in _sent(page, path, lambda: _customer_note(page)) if s < 300] == ["/field/customer_note"]
    assert [u for _, u, s in _sent(page, path, lambda: _add_note(page)) if s < 300] == ["/notes"]
    assert [u for _, u, s in _sent(page, path, lambda: _delete(page)) if s < 300] == ["/action/delete"]
    assert saves == []
    assert api.get(path).status_code == 404


def test_a_quantity_the_user_blanks_is_saved_first_and_refused(page, ui_server, api):
    r = api.post("/docs", json={"doc_type": "invoice", "status": "draft", "line_items": [
        {"name": "Widget", "quantity": 2, "unit_price": 50.0, "line_total": 100.0}]})
    assert r.status_code in (200, 201), r.text
    path = f"/docs/{r.json()['id']}"
    answer_confirms(page)
    page.goto(f"{ui_server}{path}", wait_until="load")
    page.locator('#line-body input[data-name="quantity"]').first.fill("")
    sent: list = []
    page.on("request", lambda q: q.method != "GET" and path in q.url and sent.append(q.url))
    _delete(page)
    # The refusal is said where the user is looking, in the words shown beside the lines.
    page.locator(".toast__msg", has_text="Enter a number").first.wait_for(timeout=8000)
    assert page.locator(".toast__msg").all_inner_texts() == ["Enter a number"]
    assert page.locator("#save-status").inner_text() == "\u2717 Enter a number"
    assert sent == []
    assert api.get(path).json()["status"] == "draft"


_LEVY = {"code": "Levy 5%", "rate": 5.0, "amount": 0, "order": 0, "is_compound": False, "label": ""}


def _draft(api, doc_taxes=None) -> str:
    r = api.post("/docs", json={"doc_type": "invoice", "status": "draft", "doc_taxes": doc_taxes or [],
                                "line_items": [{"name": "Widget", "quantity": 1, "unit_price": 100.0,
                                                "line_total": 100.0}]})
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


def _issue_after(page, api, doc_id: str, js: str) -> dict:
    page.evaluate(js + "; document.querySelector(\"button[hx-post$='/action/finalize']\").click();")
    deadline = time.time() + 10
    while time.time() < deadline and api.get(f"/docs/{doc_id}").json().get("status") == "draft":
        time.sleep(0.2)
    time.sleep(1.0)
    doc = api.get(f"/docs/{doc_id}").json()
    assert doc.get("status") != "draft"
    return doc


def test_a_discount_applied_right_before_issue_is_saved_first(page, ui_server, api):
    doc_id = _draft(api)
    answer_confirms(page)
    page.goto(f"{ui_server}/docs/{doc_id}", wait_until="load")
    doc = _issue_after(page, api, doc_id, "document.getElementById('disc-pop-value').value = '10';"
                       "celerpDiscType('percentage'); celerpApplyDiscount()")
    assert (float(doc.get("discount") or 0), doc.get("discount_type")) == (10.0, "percentage"), doc


def test_a_document_tax_removed_right_before_issue_is_saved_first(page, ui_server, api):
    doc_id = _draft(api, [_LEVY])
    answer_confirms(page)
    page.goto(f"{ui_server}/docs/{doc_id}", wait_until="load")
    doc = _issue_after(page, api, doc_id, "celerpRemoveDocTax(0)")
    assert [t for t in (doc.get("doc_taxes") or []) if t] == [], doc


def test_an_edit_made_while_a_save_is_in_flight_is_saved_before_issue(page, ui_server, api):
    """Price 150 is saving (held); meanwhile the price reads 175, typed but not yet blurred. The
    save comes back, then Issue: 175 is saved first, so the issued invoice holds 175."""
    doc_id = _draft(api)
    answer_confirms(page)
    page.goto(f"{ui_server}/docs/{doc_id}", wait_until="load")
    held: list = []
    page.route(re.compile(rf".*/docs/{doc_id}/lines$"), lambda route: route.continue_() if held else held.append(route))
    cell = page.locator('#line-body input[data-name="unit_price"]').first
    cell.fill("150")
    cell.dispatch_event("input")
    cell.blur()
    for _ in range(80):
        if held:
            break
        page.wait_for_timeout(100)
    assert held, "the line save never left the page"
    page.evaluate("document.querySelector('#line-body input[data-name=\"unit_price\"]').value = '175'")
    with page.expect_response(re.compile(rf".*/docs/{doc_id}/lines$")):
        held[0].continue_()
    page.wait_for_timeout(300)
    doc = _issue_after(page, api, doc_id, "void 0")
    assert float(doc["line_items"][0]["unit_price"]) == 175.0, doc


def test_a_quantity_typed_then_written_off_from_the_field_is_saved_first(page, ui_server, api):
    """The write-off qty cell saves itself on blur after a short delay. Clicking Write off stock
    straight from the field waits for that save, so 5 of the 8 are written off, not 8."""
    api.post("/accounting/chart/seed")
    iid = api.post("/items", json={"sku": "WO-FIELD-WAIT", "sell_by": "piece", "name": "Field wait",
                                   "quantity": 8}).json()["id"]
    api.post("/items/bulk/make-available", json={"entity_ids": [iid]})
    answer_confirms(page)
    page.goto(f"{ui_server}/inventory", wait_until="domcontentloaded")
    page.evaluate("window.CelerpSelection && window.CelerpSelection.clear()")
    page.locator(f'input.row-select[value="{iid}"]').click()
    page.wait_for_selector("#bulk-toolbar.is-active", timeout=3000)
    page.locator("#bulk-action-select").select_option("write_off")
    page.wait_for_url("**/lists/**", timeout=15000)
    page.locator("td.col-account").first.dblclick()
    acct = page.locator("td.col-account input:visible").first
    acct.click()
    acct.fill("69")
    page.locator(".combobox-option:visible").first.click()
    page.wait_for_timeout(500)
    for qty, then in (("8", "Enter"), ("5", None)):
        page.locator("td.col-qtyout").first.dblclick()
        cell = page.locator("td.col-qtyout input:visible").first
        cell.fill(qty)
        if then:
            cell.press(then)
            page.wait_for_timeout(600)
    page.get_by_role("button", name="Write off stock").click()
    page.get_by_role("button", name="Undo write-off").wait_for(state="visible", timeout=10000)
    page.wait_for_timeout(500)
    item = api.get(f"/items/{iid}").json()
    assert (item["status"], float(item["quantity"])) == ("available", 3.0), item
