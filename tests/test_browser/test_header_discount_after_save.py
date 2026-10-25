# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Browser test: a whole-document discount stays in the totals panel after a line save.

The panel's figures after an on-page line save are the stored ones, the Discount row with
them, on every page whose totals panel can show one: a quotation (a list, where the discount
is not edited on the page) and each document type with a header discount.
"""
from __future__ import annotations

import time

import pytest

from celerp.services.list_behavior import LIST_BEHAVIOR

pytestmark = pytest.mark.browser

_VAT = {"code": "VAT 10%", "rate": 10.0, "amount": 0, "order": 0, "is_compound": False, "label": ""}
_MONEY_LISTS = [lt for lt, b in LIST_BEHAVIOR.items() if b.money]
_CASES = [("lists", lt) for lt in _MONEY_LISTS] + [
    ("docs", dt) for dt in ("invoice", "purchase_order", "bill", "consignment_in")]


def _create(api, base: str, kind: str) -> str:
    body = {"discount": 10, "discount_type": "flat",
            "line_items": [{"description": "Panel", "quantity": 1, "unit_price": 100.0, "taxes": [_VAT]}]}
    body |= {"list_type": kind} if base == "lists" else {"doc_type": kind, "status": "draft"}
    r = api.post(f"/{base}", json=body)
    assert r.status_code in {200, 201}, r.text
    return r.json()["id"]


def _poll(api, path: str, predicate, tries: int = 180, delay: float = 0.25) -> dict | None:
    for _ in range(tries):
        doc = api.get(path).json()
        if predicate(doc):
            return doc
        time.sleep(delay)
    return None


def _shown(page, selector: str, amount: float, timeout: int = 5000) -> None:
    page.wait_for_function(
        "([s, t]) => { const el = document.querySelector(s); if (!el) return false;"
        " const v = parseFloat(el.textContent.replace(/[^0-9.\\-]/g, ''));"
        " return Math.abs(Math.abs(v) - t) < 0.005; }",
        arg=[selector, amount], timeout=timeout)


def test_every_money_list_type_is_covered():
    assert _MONEY_LISTS == ["quotation"], "a new money list type: check its totals panel here"


@pytest.mark.parametrize("base,kind", _CASES)
def test_the_discount_row_stays_after_a_line_save(page, ui_server, api, base, kind):
    eid = _create(api, base, kind)
    page.goto(f"{ui_server}/{base}/{eid}", wait_until="domcontentloaded")
    page.wait_for_selector("#doc-header-discount", timeout=8000)

    qty = page.locator('tbody tr [data-name="quantity"]').first
    qty.fill("2")
    qty.blur()
    stored = _poll(api, f"/{base}/{eid}", lambda d: abs(float(d.get("subtotal") or 0) - 200.0) < 0.01)
    assert stored is not None, "the quantity edit was not saved"
    page.wait_for_function(
        "() => !document.querySelector('.total-panel--pending')", timeout=5000)
    _shown(page, "#doc-header-discount", float(stored["discount_amount"]))
    _shown(page, "#doc-subtotal", float(stored["subtotal"]))
    _shown(page, "#doc-total", float(stored["total"]))
