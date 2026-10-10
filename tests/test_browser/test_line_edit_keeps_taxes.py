# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Editing a line on a draft must keep the taxes the user did not change.

Bug: the per-line tax select read only tax_rate/tax_code, so a line whose tax lives in its
``taxes`` list (every imported line) showed "No Tax" and the next line save posted an empty
tax list for every line, dropping all VAT from the document.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.browser

_VAT = {"code": "VAT", "rate": 10.0, "amount": 0.0, "order": 0, "is_compound": False, "label": "VAT"}
_WHT = {"code": "WHT", "rate": 3.0, "amount": 0.0, "order": 1, "is_compound": False, "label": "WHT"}


@pytest.fixture()
def taxed_draft(api):
    r = api.post("/docs", json={
        "doc_type": "invoice", "status": "draft",
        "line_items": [
            {"name": "Single tax", "quantity": 5, "unit_price": 10.0, "line_total": 50.0,
             "taxes": [_VAT]},
            {"name": "Two taxes", "quantity": 2, "unit_price": 100.0, "line_total": 200.0,
             "taxes": [_VAT, _WHT]},
        ],
    })
    assert r.status_code in {200, 201}, r.text
    return r.json()["id"]


def _stored(api, doc_id):
    r = api.get(f"/docs/{doc_id}")
    assert r.status_code == 200, r.text
    return r.json()


def test_line_edit_keeps_stored_taxes(page, ui_server, api, taxed_draft):
    page.goto(f"{ui_server}/docs/{taxed_draft}", wait_until="domcontentloaded")
    rows = page.locator("table.doc-lines tbody tr")
    rows.first.locator('[data-name="quantity"]').wait_for(timeout=8000)

    # The select shows the line's real tax, not "No Tax".
    for i in range(2):
        assert rows.nth(i).locator('[data-name="tax_select"]').input_value() != "|0"

    qty = rows.first.locator('[data-name="quantity"]')
    qty.fill("6")
    qty.dispatch_event("input")
    with page.expect_response(lambda r: r.url.endswith("/lines") and r.request.method == "POST") as resp:
        qty.dispatch_event("blur")
    assert resp.value.ok, resp.value.text()

    doc = _stored(api, taxed_draft)
    lines = doc["line_items"]
    assert lines[0]["quantity"] == 6
    assert [(tx["code"], tx["rate"]) for tx in lines[0]["taxes"]] == [("VAT", 10.0)]
    assert [(tx["code"], tx["rate"]) for tx in lines[1]["taxes"]] == [("VAT", 10.0), ("WHT", 3.0)]
    # 60 x 10% + 200 x 13%
    assert doc["tax"] == pytest.approx(32.0)


def test_changing_the_tax_still_saves_the_new_choice(page, ui_server, api, taxed_draft):
    page.goto(f"{ui_server}/docs/{taxed_draft}", wait_until="domcontentloaded")
    rows = page.locator("table.doc-lines tbody tr")
    sel = rows.first.locator('[data-name="tax_select"]')
    sel.wait_for(timeout=8000)
    with page.expect_response(lambda r: r.url.endswith("/lines") and r.request.method == "POST") as resp:
        sel.select_option("|0")
        sel.dispatch_event("blur")
    assert resp.value.ok, resp.value.text()

    lines = _stored(api, taxed_draft)["line_items"]
    assert lines[0]["taxes"] == []
    assert [(tx["code"], tx["rate"]) for tx in lines[1]["taxes"]] == [("VAT", 10.0), ("WHT", 3.0)]
