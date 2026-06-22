# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Browser test: invoice-level (header) discount.

A faint pencil by the Total opens a small popover (amount + %/$ toggle). Applying a 10% discount
reduces the taxable base, scales the tax, updates the Total live, and persists; Remove clears it.
"""
from __future__ import annotations

import time

import pytest

pytestmark = pytest.mark.browser


def _poll(api, doc_id, predicate, tries: int = 40, delay: float = 0.25) -> bool:
    for _ in range(tries):
        if predicate(api.get(f"/docs/{doc_id}").json()):
            return True
        time.sleep(delay)
    return False


@pytest.fixture()
def discount_doc_id(api):
    r = api.post("/docs", json={
        "doc_type": "invoice", "status": "draft",
        # subtotal 1000, 5% tax -> tax 50, total 1050
        "line_items": [{"name": "Widget", "quantity": 10, "unit_price": 100.0,
                        "line_total": 1000.0, "tax_rate": 5}],
        "subtotal": 1000.0, "tax": 50.0, "total": 1050.0,
    })
    assert r.status_code in {200, 201}, r.text
    return r.json()["id"]


def _open_disc(page):
    page.locator(".btn-disc-edit").click()
    page.wait_for_selector("#discount-popover.disc-popover--open", timeout=5000)


def _apply_disc(page, value, kind="percentage"):
    _open_disc(page)
    page.locator("#disc-type-pct" if kind == "percentage" else "#disc-type-flat").click()
    page.locator("#disc-pop-value").fill(str(value))
    page.locator("#discount-popover button:has-text('Apply')").click()


def _total_has(page, needle, timeout=5000):
    page.wait_for_function(
        "(s) => (document.querySelector('#doc-total')||{}).textContent.replace(/,/g,'').includes(s)",
        arg=needle, timeout=timeout)


def _simple_doc(api, price=100.0, tax_rate=0):
    r = api.post("/docs", json={
        "doc_type": "invoice", "status": "draft",
        "line_items": [{"name": "A", "sku": "A1", "quantity": 1, "unit_price": price,
                        "line_total": price, "tax_rate": tax_rate}],
        "subtotal": price, "tax": 0.0, "total": price,
    })
    assert r.status_code in {200, 201}, r.text
    return r.json()["id"]


def test_percent_discount_recomputes_when_line_added(page, ui_server, api):
    """A % discount must re-apply to the NEW subtotal when a line is added (the worry case)."""
    doc_id = _simple_doc(api, price=100.0)
    page.goto(f"{ui_server}/docs/{doc_id}", wait_until="domcontentloaded")
    page.wait_for_selector("#doc-total", timeout=8000)
    _apply_disc(page, 10, "percentage")          # 10% of 100 -> -10, total 90
    _total_has(page, "90")
    assert _poll(api, doc_id, lambda d: abs(float(d.get("discount_amount") or 0) - 10) < 0.01)

    # Add a second $100 line -> subtotal 200, so the 10% discount must re-apply to -20. (A newly
    # added line can pick up the company default tax, so assert the discount - the value under test -
    # not a tax-dependent grand total.)
    page.evaluate("celerpAddLine()")
    last_price = page.locator("table.doc-lines tbody tr").last.locator('[data-name="unit_price"]')
    last_price.fill("100")
    last_price.dispatch_event("input")   # celerpFieldEdited -> live recompute
    page.wait_for_function(
        "() => (document.querySelector('#doc-header-discount')||{}).textContent.replace(/,/g,'').includes('20')",
        timeout=5000)
    last_price.dispatch_event("blur")    # celerpAutoSave -> persist
    assert _poll(api, doc_id, lambda d: abs(float(d.get("discount_amount") or 0) - 20) < 0.01), \
        "percent discount did not re-apply to the new $200 subtotal"


def test_percent_header_discount_stacks_with_line_discount(page, ui_server, api):
    """Header % applies on the NET subtotal (after a per-line discount), not the gross."""
    doc_id = _simple_doc(api, price=100.0)
    page.goto(f"{ui_server}/docs/{doc_id}", wait_until="domcontentloaded")
    page.wait_for_selector("#doc-total", timeout=8000)
    _apply_disc(page, 10, "percentage")
    _total_has(page, "90")

    # Add a 20% line discount: line net 80 -> header 10% of 80 = 8 -> total 72.
    disc_pct = page.locator('table.doc-lines tbody tr [data-name="discount_pct"]').first
    disc_pct.fill("20")
    disc_pct.blur()
    _total_has(page, "72")
    assert _poll(api, doc_id, lambda d: abs(float(d.get("discount_amount") or 0) - 8) < 0.01
                 and abs(float(d.get("total") or 0) - 72) < 0.01), "header % should apply to the net-of-line-discount subtotal"


def test_flat_discount_stays_fixed_when_line_changes(page, ui_server, api):
    """A flat discount is a fixed amount: it does NOT scale when the subtotal changes."""
    doc_id = _simple_doc(api, price=100.0)
    page.goto(f"{ui_server}/docs/{doc_id}", wait_until="domcontentloaded")
    page.wait_for_selector("#doc-total", timeout=8000)
    _apply_disc(page, 30, "flat")                # -30, total 70
    _total_has(page, "70")

    # Raise the line price to 200 -> subtotal 200, flat discount still 30 -> total 170.
    price = page.locator('table.doc-lines tbody tr [data-name="unit_price"]').first
    price.fill("200")
    price.blur()
    _total_has(page, "170")
    assert _poll(api, doc_id, lambda d: abs(float(d.get("discount_amount") or 0) - 30) < 0.01
                 and abs(float(d.get("total") or 0) - 170) < 0.01), "flat discount must stay fixed"


def test_discount_value_and_type_can_be_changed(page, ui_server, api):
    """Re-opening the editor and changing value/type updates correctly (change -> change again)."""
    doc_id = _simple_doc(api, price=100.0)
    page.goto(f"{ui_server}/docs/{doc_id}", wait_until="domcontentloaded")
    page.wait_for_selector("#doc-total", timeout=8000)
    _apply_disc(page, 10, "percentage")          # total 90
    _total_has(page, "90")
    _apply_disc(page, 20, "percentage")          # total 80
    _total_has(page, "80")
    _apply_disc(page, 25, "flat")                # switch to flat 25 -> total 75
    _total_has(page, "75")
    assert _poll(api, doc_id, lambda d: d.get("discount_type") == "flat"
                 and abs(float(d.get("total") or 0) - 75) < 0.01), "final flat discount not persisted"


def test_header_discount_apply_and_remove(page, ui_server, api, discount_doc_id):
    page.goto(f"{ui_server}/docs/{discount_doc_id}", wait_until="domcontentloaded")
    page.wait_for_selector("#doc-total", timeout=8000)

    # No discount row yet.
    assert page.locator("#doc-header-discount-row").count() == 0

    # Open the popover via the pencil, choose %, enter 10, Apply.
    page.locator(".btn-disc-edit").click()
    page.wait_for_selector("#discount-popover.disc-popover--open", timeout=5000)
    page.locator("#disc-type-pct").click()
    page.locator("#disc-pop-value").fill("10")
    page.locator("#discount-popover button:has-text('Apply')").click()

    # Discount row appears and the total drops to the discounted figure (900 + 45 = 945).
    page.wait_for_selector("#doc-header-discount-row", timeout=5000)
    disc_text = page.locator("#doc-header-discount-row").inner_text()
    assert "10%" in disc_text and "100" in disc_text, f"discount row: {disc_text!r}"
    assert "945" in page.locator("#doc-total").inner_text(), page.locator("#doc-total").inner_text()

    # Persisted to the backend.
    assert _poll(api, discount_doc_id, lambda d: float(d.get("discount") or 0) == 10
                 and d.get("discount_type") == "percentage"
                 and abs(float(d.get("discount_amount") or 0) - 100.0) < 0.01), "discount not persisted"

    # Remove it: row disappears, total returns to 1050, persistence clears.
    page.locator(".btn-disc-edit").click()
    page.wait_for_selector("#discount-popover.disc-popover--open", timeout=5000)
    page.locator("#discount-popover button:has-text('Remove')").click()
    page.wait_for_selector("#doc-header-discount-row", state="detached", timeout=5000)
    assert "1,050" in page.locator("#doc-total").inner_text() or "1050" in page.locator("#doc-total").inner_text()
    assert _poll(api, discount_doc_id, lambda d: float(d.get("discount") or 0) == 0), "discount not cleared"
