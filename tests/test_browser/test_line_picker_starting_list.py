# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Browser test (SAL-014): clicking an empty SKU or description cell on a draft invoice, a
vendor bill and a quotation opens the starting list before anything is typed. Lots held by
another record are covered by tests/test_picker_starting_list_per_type.py."""
from __future__ import annotations

import uuid

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.browser

_TAG = uuid.uuid4().hex[:6]


@pytest.fixture(scope="module")
def stock(api):
    """Two free items for the starting list to offer."""
    for i in range(2):
        r = api.post("/items", json={"status": "available", "sku": f"BR-{_TAG}-{i}", "name": f"Browser {_TAG} {i}",
                                     "quantity": 1, "sell_by": "piece"})
        assert r.status_code in (200, 201), r.text
    return [f"BR-{_TAG}-{i}" for i in range(2)]


def _draft_url(ui_server, api, doc_type: str) -> str:
    if doc_type == "quotation":
        r = api.post("/lists", json={"list_type": "quotation", "line_items": []})
        assert r.status_code in (200, 201), r.text
        return f"{ui_server}/lists/{r.json()['id']}"
    r = api.post("/docs", json={"doc_type": doc_type, "line_items": []})
    assert r.status_code in (200, 201), r.text
    return f"{ui_server}/docs/{r.json()['id']}"


@pytest.mark.parametrize("doc_type", ["invoice", "bill", "quotation"])
@pytest.mark.parametrize("field", ["sku", "description"])
def test_clicking_an_empty_line_cell_opens_the_starting_list(page, ui_server, api, stock, doc_type, field):
    page.goto(_draft_url(ui_server, api, doc_type), wait_until="domcontentloaded")
    add = page.locator("button[onclick='celerpAddLine()']")
    if add.count():
        add.first.click()
    cell = page.locator(f".catalog-ac-input[data-name='{field}']").last
    expect(cell).to_be_visible(timeout=5000)
    cell.click()
    options = page.locator(".catalog-ac-list .catalog-ac-option").filter(has_text=f"BR-{_TAG}-")
    expect(options.first).to_be_visible(timeout=10000)
    # The starting list has no custom-entry row; that only follows typed text.
    expect(page.locator(".catalog-ac-list .catalog-ac-option--custom")).to_have_count(0)
