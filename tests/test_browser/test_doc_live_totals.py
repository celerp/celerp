# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Browser test: the document editor's totals while lines are edited and saved.

The figures the page works out while a line is edited are marked as not saved yet. A successful
save puts the server's tax rows, subtotal and total in their place, so a compound tax or a tax on
the whole document never shows a figure the saved document does not hold. A failed save puts the
stored figures back.
"""
from __future__ import annotations

import re

import pytest

from .test_from_items_tax_e2e import _COMPOUND_AFTER_VAT, _demo_company

pytestmark = pytest.mark.browser

_LEVY_WHOLE = {"code": "Levy 5%", "rate": 5.0, "amount": 0, "order": 0, "is_compound": False, "label": ""}
_PENDING = ".total-panel.total-panel--pending"


def _amount(text: str) -> float:
    return float(re.sub(r"[^\d.\-]", "", text))


def _shown(page) -> dict:
    """The money the panel shows: each tax row (line and document taxes), subtotal and total."""
    taxes = [_amount(t) for t in page.locator(
        "#doc-tax-rows .total-value, #doc-stored-tax-rows .total-value").all_inner_texts()]
    return {"taxes": round(sum(taxes), 2), "subtotal": _amount(page.inner_text("#doc-subtotal")),
            "total": _amount(page.inner_text("#doc-total"))}


def _stored(company, doc_id: str, kind: str = "docs") -> dict:
    doc = company.get(f"/{kind}/{doc_id}").json()
    return {"taxes": round(float(doc["total"]) - float(doc["subtotal"]), 2),
            "subtotal": float(doc["subtotal"]), "total": float(doc["total"])}


def _open(page, ui_server, token: str, doc_id: str, kind: str = "docs"):
    ctx = page.context.browser.new_context(base_url=ui_server)
    ctx.add_cookies([{"name": "celerp_token", "value": token, "domain": "127.0.0.1", "path": "/"}])
    tab = ctx.new_page()
    tab.goto(f"/{kind}/{doc_id}", wait_until="domcontentloaded")
    tab.wait_for_selector('table.doc-lines tbody tr input[data-name="unit_price"]', timeout=8000)
    return ctx, tab


def _held_saves(tab, doc_id: str, kind: str = "docs") -> list:
    """The first line save, held for the test to let through (route.continue_) or answer itself;
    any later save goes straight through."""
    held: list = []
    tab.route(re.compile(rf".*/{kind}/{doc_id}/lines$"),
              lambda route: route.continue_() if held else held.append(route))
    return held


def _type_price(tab, price: str) -> None:
    cell = tab.locator('table.doc-lines tbody tr input[data-name="unit_price"]').first
    cell.fill(price)
    cell.dispatch_event("input")
    cell.blur()


def _wait_for(tab, held: list) -> None:
    for _ in range(80):
        if held:
            return
        tab.wait_for_timeout(100)
    raise AssertionError("the line save never left the page")


def _draft(company, ui, item: dict, doc_taxes: list | None = None) -> str:
    r = company.post("/docs", json={
        "doc_type": "invoice", "status": "draft", "doc_taxes": doc_taxes or [],
        "line_items": [{"item_id": item["id"], "sku": item["sku"], "name": item.get("name") or item["sku"],
                        "quantity": 1, "unit_price": 50.0}]})
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


def test_a_compound_line_tax_shows_the_saved_figures_once_saved(page, api_server, ui_server, api):
    with _demo_company(api_server, ui_server, api, "GB", _COMPOUND_AFTER_VAT) as (company, ui, token, items):
        r = ui.post("/docs/from-items/new", data={"selected": [items[0]["id"]]})
        assert r.status_code == 204, r.text
        doc_id = r.headers["HX-Redirect"].rsplit("/", 1)[-1]
        ctx, tab = _open(page, ui_server, token, doc_id)
        try:
            # Before an edit: the panel shows what is stored, not marked as unsaved.
            assert _shown(tab) == _stored(company, doc_id)
            assert tab.locator(_PENDING).count() == 0

            held = _held_saves(tab, doc_id)
            _type_price(tab, "100")
            _wait_for(tab, held)
            # While the save is out, the page's own figures are marked as not saved yet.
            tab.locator(_PENDING).wait_for(timeout=3000)
            assert tab.locator(".total-pending-note").is_visible()

            held[0].continue_()
            tab.wait_for_selector(_PENDING, state="detached", timeout=8000)
            stored = _stored(company, doc_id)
            qty = float(company.get(f"/docs/{doc_id}").json()["line_items"][0]["quantity"])
            # Per unit, VAT 5% then a 10% levy on the price with VAT: 5.00 + 10.50.
            assert stored == {"taxes": round(15.5 * qty, 2), "subtotal": 100.0 * qty,
                              "total": round(115.5 * qty, 2)}
            assert _shown(tab) == stored
        finally:
            ctx.close()


def test_a_whole_document_tax_follows_a_line_edit_once_saved(page, api_server, ui_server, api):
    with _demo_company(api_server, ui_server, api, "GB", []) as (company, ui, token, items):
        doc_id = _draft(company, ui, items[0], [_LEVY_WHOLE])
        ctx, tab = _open(page, ui_server, token, doc_id)
        try:
            held = _held_saves(tab, doc_id)
            _type_price(tab, "200")
            _wait_for(tab, held)
            # The levy row still holds the amount for the old price: never shown as final.
            tab.locator(_PENDING).wait_for(timeout=3000)
            assert _amount(tab.inner_text("#doc-stored-tax-rows .total-value")) == 2.5

            held[0].continue_()
            tab.wait_for_selector(_PENDING, state="detached", timeout=8000)
            assert _shown(tab) == _stored(company, doc_id) == {"taxes": 10.0, "subtotal": 200.0, "total": 210.0}
        finally:
            ctx.close()


def test_a_failed_save_puts_the_stored_figures_back(page, api_server, ui_server, api):
    with _demo_company(api_server, ui_server, api, "GB", _COMPOUND_AFTER_VAT) as (company, ui, token, items):
        doc_id = _draft(company, ui, items[0])
        before = _stored(company, doc_id)
        ctx, tab = _open(page, ui_server, token, doc_id)
        try:
            held = _held_saves(tab, doc_id)
            _type_price(tab, "100")
            _wait_for(tab, held)
            held[0].fulfill(status=400, content_type="application/json", body='{"error": "Could not save"}')
            tab.wait_for_selector(_PENDING, state="detached", timeout=8000)
            assert _stored(company, doc_id) == before
            assert _shown(tab) == before
        finally:
            ctx.close()


def test_escape_on_an_edited_price_puts_the_stored_figures_back(page, api_server, ui_server, api):
    with _demo_company(api_server, ui_server, api, "GB", _COMPOUND_AFTER_VAT) as (company, ui, token, items):
        doc_id = _draft(company, ui, items[0])
        before = _stored(company, doc_id)
        ctx, tab = _open(page, ui_server, token, doc_id)
        try:
            held = _held_saves(tab, doc_id)
            cell = tab.locator('table.doc-lines tbody tr input[data-name="unit_price"]').first
            cell.focus()
            cell.fill("100")
            cell.dispatch_event("input")
            tab.locator(_PENDING).wait_for(timeout=3000)
            assert _shown(tab) != before

            cell.press("Escape")
            tab.wait_for_selector(_PENDING, state="detached", timeout=3000)
            assert _shown(tab) == before
            tab.wait_for_timeout(500)
            assert held == [] and _stored(company, doc_id) == before
        finally:
            ctx.close()


def test_a_quotation_shows_the_saved_figures_once_saved(page, api_server, ui_server, api):
    with _demo_company(api_server, ui_server, api, "GB", []) as (company, ui, token, items):
        item = items[0]
        r = company.post("/lists", json={"list_type": "quotation", "line_items": [
            {"item_id": item["id"], "sku": item["sku"], "name": item.get("name") or item["sku"],
             "quantity": 1, "unit_price": 50.0}]})
        assert r.status_code == 200, r.text
        list_id = r.json()["id"]
        ctx, tab = _open(page, ui_server, token, list_id, "lists")
        try:
            held = _held_saves(tab, list_id, "lists")
            _type_price(tab, "200")
            _wait_for(tab, held)
            tab.locator(_PENDING).wait_for(timeout=3000)

            held[0].continue_()
            tab.wait_for_selector(_PENDING, state="detached", timeout=8000)
            stored = _stored(company, list_id, "lists")
            assert stored["subtotal"] == 200.0
            assert _shown(tab) == stored
        finally:
            ctx.close()
