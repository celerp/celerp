# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A bulk action bar shows only while something is selected (or, on the bank page, while it
carries the last post's outcome and its Undo). Every list page with a bulk bar loads with
nothing selected, so no bar may be visible there; ticking a row brings it up. The bank page
needs an imported statement for its bar, so its journey in test_bank_page.py holds both."""
from __future__ import annotations

import uuid

import pytest
from playwright.sync_api import expect

from .conftest import htmx_idle
pytestmark = pytest.mark.browser


@pytest.fixture(scope="module")
def lists_with_rows(api):
    """One row on every list that carries a bulk bar, so the bar and its checkboxes render."""
    tag = uuid.uuid4().hex[:6]
    r = api.post("/items", json={"sku": f"BAR-{tag}", "sell_by": "piece", "name": f"Bar {tag}", "quantity": 1})
    assert r.status_code in {200, 201}, r.text
    r = api.post("/crm/contacts", json={"name": f"Bar Contact {tag}", "contact_type": "customer"})
    assert r.status_code in {200, 201}, r.text
    r = api.post("/docs", json={"doc_type": "invoice", "status": "draft", "line_items": []})
    assert r.status_code in {200, 201}, r.text


@pytest.mark.parametrize("path", ["/inventory", "/contacts", "/docs"])
def test_bulk_bar_hidden_with_nothing_selected(page, ui_server, lists_with_rows, path):
    page.goto(f"{ui_server}{path}", wait_until="load")
    htmx_idle(page)
    bars = page.locator(".bulk-toolbar")
    assert bars.count() > 0, f"{path} renders no bulk bar"
    assert page.locator("input.row-select:checked").count() == 0
    for i in range(bars.count()):
        expect(bars.nth(i)).to_be_hidden()


def test_bulk_bar_shows_once_a_row_is_ticked(page, ui_server, lists_with_rows):
    page.goto(f"{ui_server}/inventory", wait_until="load")
    htmx_idle(page)
    page.locator("input.row-select").first.check()
    expect(page.locator("#bulk-toolbar")).to_be_visible()
