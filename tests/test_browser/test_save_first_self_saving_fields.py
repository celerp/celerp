# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The save-first gate saves the page's lines before an action only while they differ from the
stored ones. A field that saved itself, or one put back with Escape, leaves nothing to save, so
the action posts on its own with no line save in front of it."""
from __future__ import annotations

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
    assert _sent(page, path, lambda: _delete(page)) == []
    assert page.locator("#save-status").inner_text() == "\u2717 Enter a number"
    assert api.get(path).json()["status"] == "draft"
