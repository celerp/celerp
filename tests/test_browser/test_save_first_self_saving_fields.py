# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The save-first gate saves the page's lines before an action only while they differ from the
stored ones. A field that saved itself, or one put back with Escape, leaves nothing to save, so
the action posts on its own with no line save in front of it."""
from __future__ import annotations

import time

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
