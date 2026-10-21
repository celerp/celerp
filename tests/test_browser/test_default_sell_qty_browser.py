# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Default Sell Qty on the item page and in the scan bar.

The cell shows what a scan will add when nothing is set, takes Full from its list or a typed
number, refuses a bad value with its message, keeps the old value on Esc, and locks while Allow
Splitting is off without losing what was stored. A scan names the quantity it placed.
"""
from __future__ import annotations

import time
import uuid

import pytest
from playwright.sync_api import expect

from .inline_edit import ready, set_cell, settled

pytestmark = pytest.mark.browser

CELL = 'td[data-col="default_sell_qty"]'
SCOPE = "body"
EDITING = "td.cell--editing"


def _item(api, *, sell_by="carat", quantity=6, **extra) -> str:
    sku = f"DSQ-{uuid.uuid4().hex[:6].upper()}"
    r = api.post("/items", json={"status": "available", "sku": sku, "name": sku, "sell_by": sell_by,
                                 "quantity": quantity, "allow_splitting": True, **extra})
    assert r.status_code in {200, 201}, r.text
    return r.json()["id"]


def _stored(api, item_id: str):
    return api.get(f"/items/{item_id}").json().get("default_sell_qty")


def _wait_stored(api, item_id: str, expected, timeout: float = 6.0):
    deadline = time.monotonic() + timeout
    while _stored(api, item_id) != expected and time.monotonic() < deadline:
        time.sleep(0.2)
    assert _stored(api, item_id) == expected


def _open(page, ui_server, item_id: str):
    page.goto(f"{ui_server}/inventory/{item_id}", wait_until="domcontentloaded")
    page.locator(CELL).first.wait_for(state="visible", timeout=8000)
    page.evaluate("window.__sameView = true")


def _open_editor(page):
    settled(page, SCOPE)
    page.dblclick(CELL)
    box = page.locator(f"{EDITING} .combobox-input")
    box.wait_for(state="visible", timeout=4000)
    ready(page, f"{EDITING} input[name=value]")
    return box


def test_unset_cell_shows_the_resolved_default_in_grey(page, ui_server, api):
    parcel = _item(api, sell_by="carat")
    _open(page, ui_server, parcel)
    expect(page.locator(f"{CELL} .cell-suggestion")).to_contain_text(
        "Full (everything left)")

    screws = _item(api, sell_by="piece", quantity=10)
    _open(page, ui_server, screws)
    expect(page.locator(f"{CELL} .cell-suggestion")).to_have_text("1")
    assert _stored(api, parcel) is None and _stored(api, screws) is None


def test_full_chosen_in_a_translated_ui_stores_full_and_shows_the_label(page, ui_server, api):
    parcel = _item(api)
    page.context.add_cookies([{"name": "celerp_lang", "value": "de", "url": ui_server}])
    try:
        _open(page, ui_server, parcel)
        box = _open_editor(page)
        box.click()
        page.locator(f'{EDITING} .combobox-option[data-value="full"]').click()
        settled(page, SCOPE)
        _wait_stored(api, parcel, "full")
        expect(page.locator(CELL)).to_contain_text("Voll (alles Verbleibende)")
    finally:
        page.context.add_cookies([{"name": "celerp_lang", "value": "en", "url": ui_server}])
    assert page.evaluate("window.__sameView === true"), "the page reloaded"


@pytest.mark.parametrize("commit", ["Enter", "Tab"])
def test_a_typed_number_saves(page, ui_server, api, commit):
    parcel = _item(api)
    _open(page, ui_server, parcel)
    box = _open_editor(page)
    box.fill("2.5")
    box.press(commit)
    settled(page, SCOPE)
    _wait_stored(api, parcel, 2.5)
    expect(page.locator(CELL)).to_contain_text("2.5")


def test_a_refused_value_shows_its_message(page, ui_server, api):
    screws = _item(api, sell_by="piece", quantity=10)
    _open(page, ui_server, screws)
    box = _open_editor(page)
    box.fill("0.5")
    box.press("Enter")
    expect(page.locator(".cell-error")).to_be_visible(timeout=6000)
    assert page.locator(".cell-error").inner_text().strip()
    assert _stored(api, screws) is None


def test_esc_leaves_the_old_value(page, ui_server, api):
    parcel = _item(api, default_sell_qty=2)
    _open(page, ui_server, parcel)
    box = _open_editor(page)
    box.fill("9")
    box.press("Escape")
    settled(page, SCOPE)
    expect(page.locator(CELL)).to_contain_text("2")
    expect(page.locator(CELL)).not_to_contain_text("9")
    assert _stored(api, parcel) == 2


def test_allow_splitting_locks_and_unlocks_the_cell_without_a_reload(page, ui_server, api):
    parcel = _item(api, default_sell_qty=3)
    _open(page, ui_server, parcel)
    cell = page.locator(CELL)
    expect(cell).not_to_have_class("cell--locked")

    set_cell(page, "allow_splitting", "false", scope=SCOPE, kind="select")
    expect(page.locator(CELL)).to_have_attribute("title", "Not used while Allow Splitting is off: a scan always "
                                                           "adds the whole item. Turn Allow Splitting on to change this.")
    assert "cell--locked" in (page.locator(CELL).get_attribute("class") or "")
    expect(page.locator(CELL)).to_contain_text("3")
    assert _stored(api, parcel) == 3

    set_cell(page, "allow_splitting", "true", scope=SCOPE, kind="select")
    page.wait_for_function(
        "(sel) => !document.querySelector(sel).classList.contains('cell--locked')", arg=CELL, timeout=6000)
    expect(page.locator(CELL)).to_contain_text("3")
    assert page.evaluate("window.__sameView === true"), "the page reloaded"
    _open_editor(page).press("Escape")


def test_a_customised_schema_shows_the_field_after_allow_splitting(page, ui_server, fresh_company):
    schema = fresh_company.get("/companies/me/item-schema").json()
    fields = [f for f in (schema.get("fields") if isinstance(schema, dict) else schema)
              if f["key"] != "default_sell_qty"]
    for f in fields:
        if f["key"] == "name":
            f["label"] = "Item Name"
    assert fresh_company.patch("/companies/me/item-schema", json={"fields": fields}).status_code == 200
    r = fresh_company.post("/items", json={"status": "available", "sku": "DSQ-SCHEMA", "name": "Parcel",
                                           "sell_by": "carat", "quantity": 6, "allow_splitting": True})
    assert r.status_code in {200, 201}, r.text

    page.goto(f"{ui_server}/inventory/{r.json()['id']}", wait_until="domcontentloaded")
    page.locator(CELL).first.wait_for(state="visible", timeout=8000)
    order = page.evaluate(
        "() => Array.from(document.querySelectorAll('td[data-col]')).map(td => td.dataset.col)")
    assert order.index("default_sell_qty") == order.index("allow_splitting") + 1, order


def test_the_scan_bar_names_the_placed_quantity(page, ui_server, api):
    sku = f"DSQ-{uuid.uuid4().hex[:6].upper()}"
    r = api.post("/items", json={"status": "available", "sku": sku, "name": sku, "sell_by": "carat",
                                 "quantity": 0.6, "allow_splitting": True})
    assert r.status_code in {200, 201}, r.text
    doc_id = api.post("/docs", json={"doc_type": "invoice", "status": "draft"}).json()["id"]
    page.goto(f"{ui_server}/docs/{doc_id}", wait_until="domcontentloaded")
    page.wait_for_selector("#scan-bar-input", timeout=8000)

    inp = page.locator("#scan-bar-input")
    inp.click()
    inp.fill(sku)
    inp.press("Enter")
    page.locator("#scan-bar-add").click()
    page.wait_for_function(
        "() => /\\u2713/.test(document.querySelector('.scan-bar-status')?.textContent || '')", timeout=8000)
    status = page.locator(".scan-bar-status").inner_text()
    assert sku in status and "0.60" in status and "carat" in status, status
    [line] = api.get(f"/docs/{doc_id}").json()["line_items"]
    assert line["quantity"] == pytest.approx(0.6)
