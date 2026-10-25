# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The same draft open in two tabs: the second tab's save never overwrites the first tab's.

Tab A changes a quantity and saves. Tab B, still showing the older lines, changes a price. Its save
is refused: B shows the conflict with Try again, keeps the price it typed on screen and never shows
the saved tick, and the stored document keeps A's quantity with B's price unwritten.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.browser


def _open(page, ui_server, doc_id):
    page.goto(f"{ui_server}/docs/{doc_id}", wait_until="domcontentloaded")
    page.wait_for_selector('table.doc-lines tbody tr input[data-name="quantity"]', timeout=8000)


def _edit(page, doc_id, field, value):
    cell = page.locator(f'table.doc-lines tbody tr input[data-name="{field}"]').first
    with page.expect_response(lambda r: r.url.endswith(f"/docs/{doc_id}/lines"), timeout=10000) as resp:
        cell.fill(value)
        cell.dispatch_event("input")
        cell.dispatch_event("change")
        cell.blur()
    return resp.value


def test_a_stale_tab_cannot_overwrite_the_other_tabs_save(page, browser_context, ui_server, api):
    r = api.post("/docs", json={"doc_type": "invoice", "line_items": [
        {"name": "Fitting", "quantity": 1, "unit_price": 100.0}]})
    assert r.status_code in {200, 201}, r.text
    doc_id = r.json()["id"]
    tab_b = browser_context.new_page()
    try:
        _open(page, ui_server, doc_id)
        _open(tab_b, ui_server, doc_id)

        assert _edit(page, doc_id, "quantity", "5").status == 200

        refused = _edit(tab_b, doc_id, "unit_price", "120")
        assert refused.status == 409
        assert refused.json()["code"] == "stale_version"
        status = tab_b.locator("#save-status")
        status.locator(".try-again").wait_for(timeout=5000)
        assert "✓" not in status.inner_text()
        assert tab_b.locator('table.doc-lines tbody tr input[data-name="unit_price"]').first.input_value() in ("120", "120.00")

        line = api.get(f"/docs/{doc_id}").json()["line_items"][0]
        assert (line["quantity"], line["unit_price"]) == (5, 100.0)
    finally:
        tab_b.close()
