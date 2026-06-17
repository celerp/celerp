# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Regression: editing one invoice line's total must not silently change another line's total.

Bug: celerpUpdateTotals re-derived every non-focused row's line total from qty x unit_price, so a
line whose total had been typed directly (which back-calculates a 2-dp unit price) drifted by the
rounding error the moment any other line was edited.
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.browser

SHOTS = Path("context/reviews/documents")


@pytest.fixture()
def total_doc_id(api):
    r = api.post("/docs", json={
        "doc_type": "invoice",
        "status": "draft",
        "line_items": [
            # qty 3 so a typed total of 100 back-calcs unit_price 33.33 -> qty*price = 99.99 (drift).
            {"name": "Alpha", "quantity": 3, "unit_price": 1.0, "line_total": 3.0},
            {"name": "Beta", "quantity": 2, "unit_price": 10.0, "line_total": 20.0},
        ],
        "total": 23.0,
    })
    assert r.status_code in {200, 201}, r.text
    return r.json()["id"]


def _line_total(row):
    return row.locator("input.line-total")


def test_editing_one_line_total_does_not_change_another(page, ui_server, total_doc_id):
    page.goto(f"{ui_server}/docs/{total_doc_id}", wait_until="domcontentloaded")
    page.wait_for_selector("table.doc-lines tbody tr input.line-total", timeout=8000)
    rows = page.locator("table.doc-lines tbody tr")

    alpha_total = _line_total(rows.nth(0))
    beta_total = _line_total(rows.nth(1))

    # Type a total on Alpha (this back-calculates Alpha's unit price to 2 dp).
    alpha_total.fill("100")
    alpha_total.dispatch_event("input")
    # Now edit Beta's total. This must NOT touch Alpha's typed total.
    beta_total.fill("55")
    beta_total.dispatch_event("input")
    beta_total.dispatch_event("blur")

    assert alpha_total.input_value() == "100.00" or alpha_total.input_value() == "100", \
        f"Alpha total drifted to {alpha_total.input_value()!r} after editing Beta"
    # Beta keeps its own typed total too.
    assert beta_total.input_value() in ("55", "55.00")

    # The document subtotal reflects the two typed totals (100 + 55 = 155), not the qty*price drift.
    subtotal = page.locator("#doc-subtotal").inner_text()
    assert "155" in subtotal, f"subtotal {subtotal!r} does not reflect the typed line totals"

    SHOTS.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(SHOTS / "invoice-line-totals.png"), full_page=True)


def test_editing_unit_price_rederives_that_rows_total(page, ui_server, total_doc_id):
    """A typed total is an override; editing that row's unit price drops the override and recomputes."""
    page.goto(f"{ui_server}/docs/{total_doc_id}", wait_until="domcontentloaded")
    page.wait_for_selector("table.doc-lines tbody tr input.line-total", timeout=8000)
    row = page.locator("table.doc-lines tbody tr").nth(1)  # Beta: qty 2

    beta_total = row.locator("input.line-total")
    beta_total.fill("99")          # override
    beta_total.dispatch_event("input")
    # Now set Beta's unit price to 20 -> total must re-derive (2 * 20 = 40), not stay 99.
    price = row.locator("[data-name='unit_price']")
    price.fill("20")
    price.dispatch_event("input")
    assert beta_total.input_value() not in ("99", "99.00"), "price edit did not re-derive the overridden total"
    assert beta_total.input_value() == "40.00"
