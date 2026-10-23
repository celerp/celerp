# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Browser test: document-level taxes stored on a document.

A stored document tax shows with the server's amount, can be removed on a draft (and only on a
draft), and the shown total always ends equal to the stored total, including after a line edit
on a document with a compound tax.
"""
from __future__ import annotations

import re
import time
import uuid

import pytest

pytestmark = pytest.mark.browser

_PLAIN = {"code": "Levy 5%", "rate": 5.0, "order": 1, "is_compound": False, "label": "Levy 5%"}
_COMPOUND = {"code": "Duty 10%", "rate": 10.0, "order": 2, "is_compound": True, "label": "Duty 10%"}


def _poll(api, doc_id, predicate, tries: int = 180, delay: float = 0.25) -> dict | None:
    for _ in range(tries):
        doc = api.get(f"/docs/{doc_id}").json()
        if predicate(doc):
            return doc
        time.sleep(delay)
    return None


def _amount(text: str) -> float:
    return float(re.sub(r"[^\d.\-]", "", text))


def _draft(api, doc_taxes: list[dict]) -> dict:
    r = api.post("/docs", json={
        "doc_type": "invoice", "status": "draft",
        "line_items": [{"name": "A", "sku": "A1", "quantity": 1, "unit_price": 100.0,
                        "line_total": 100.0}],
        "doc_taxes": doc_taxes,
    })
    assert r.status_code in {200, 201}, r.text
    return api.get(f"/docs/{r.json()['id']}").json() | {"id": r.json()["id"]}


def _shown_amount_equals(page, selector: str, amount: float, timeout: int = 5000) -> None:
    page.wait_for_function(
        "([s, t]) => { const el = document.querySelector(s); if (!el) return false;"
        " const v = parseFloat(el.textContent.replace(/[^0-9.\\-]/g, ''));"
        " return Math.abs(v - t) < 0.005; }",
        arg=[selector, amount], timeout=timeout)


def _shown_total_equals(page, total: float, timeout: int = 5000) -> None:
    _shown_amount_equals(page, "#doc-total", total, timeout)


def test_stored_doc_tax_can_be_removed_on_a_draft(page, ui_server, api):
    doc = _draft(api, [_PLAIN])
    assert abs(float(doc["total"]) - 105.0) < 0.01, doc
    page.goto(f"{ui_server}/docs/{doc['id']}", wait_until="domcontentloaded")
    row = page.locator('#doc-stored-tax-rows [data-doc-tax-index="0"]')
    row.wait_for(timeout=8000)
    assert abs(_amount(row.locator(".total-value").inner_text()) - 5.0) < 0.005

    row.locator("button").click()
    page.wait_for_selector('#doc-stored-tax-rows [data-doc-tax-index="0"]', state="detached", timeout=5000)
    stored = _poll(api, doc["id"], lambda d: not (d.get("doc_taxes") or []))
    assert stored is not None, "the removed document tax was not saved"
    assert abs(float(stored["total"]) - 100.0) < 0.01, stored
    _shown_total_equals(page, float(stored["total"]))
    # Every figure the save changes follows it on screen, Outstanding included.
    _shown_amount_equals(page, "#doc-outstanding", float(stored["amount_outstanding"]))

    issued = _draft(api, [_PLAIN])
    r = api.post(f"/docs/{issued['id']}/finalize")
    assert r.status_code in {200, 201}, r.text
    page.goto(f"{ui_server}/docs/{issued['id']}", wait_until="domcontentloaded")
    page.locator('#doc-stored-tax-rows [data-doc-tax-index="0"]').wait_for(timeout=8000)
    assert page.locator("#doc-stored-tax-rows button").count() == 0, "an issued document offers to remove its tax"


def test_stored_doc_tax_survives_a_line_edit_on_screen(page, ui_server, api):
    doc = _draft(api, [_PLAIN, _COMPOUND])
    page.goto(f"{ui_server}/docs/{doc['id']}", wait_until="domcontentloaded")
    page.wait_for_selector("#doc-total", timeout=8000)
    _shown_total_equals(page, float(doc["total"]))

    qty = page.locator('table.doc-lines tbody tr [data-name="quantity"]').first
    qty.fill("2")
    qty.blur()
    stored = _poll(api, doc["id"], lambda d: abs(float(d.get("subtotal") or 0) - 200.0) < 0.01)
    assert stored is not None, "the quantity edit was not saved"
    assert len(stored.get("doc_taxes") or []) == 2, stored.get("doc_taxes")
    _shown_total_equals(page, float(stored["total"]))
    _shown_amount_equals(page, "#doc-outstanding", float(stored["amount_outstanding"]))


def test_stored_doc_tax_follows_a_scan_on_screen(page, ui_server, api):
    # A scan reinstalls the stored rows; the tax, total and outstanding beside them are the stored ones too.
    tag = uuid.uuid4().hex[:6]
    r = api.post("/items", json={"status": "available", "sku": f"STX-{tag}", "name": "Scan Taxed", "sell_by": "piece",
                                  "quantity": 10, "retail_price": 6.0})
    assert r.status_code in {200, 201}, r.text
    doc = _draft(api, [_PLAIN])
    page.goto(f"{ui_server}/docs/{doc['id']}", wait_until="domcontentloaded")
    page.wait_for_selector("#scan-bar-input", timeout=8000)
    page.locator("#scan-bar-input").fill(f"STX-{tag}")
    page.locator("#scan-bar-add").click()
    stored = _poll(api, doc["id"], lambda d: len(d.get("line_items") or []) == 2)
    assert stored is not None, "the scanned item was not added"
    page.wait_for_selector(f'#line-body [data-name="sku"][value="STX-{tag}"]', timeout=8000)
    tax = float(stored["doc_taxes"][0]["amount"])
    assert abs(tax - 5.0) > 0.005, stored["doc_taxes"]
    _shown_amount_equals(page, '#doc-stored-tax-rows [data-doc-tax-index="0"] .total-value', tax)
    _shown_total_equals(page, float(stored["total"]))
    _shown_amount_equals(page, "#doc-outstanding", float(stored["amount_outstanding"]))
