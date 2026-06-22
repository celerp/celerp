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
