# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Playwright: a History filter is a searchable box; typing searches it and picking a choice
shows only the matching rows, with the choice kept in the URL. The item History reloads the
page, a document's History card reloads in place."""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.browser


def _pick_field(page, value: str) -> None:
    box = page.locator(".history-filter:has(input[name=hist_field]) .combobox-input")
    box.click()
    box.fill(value[:3])
    # Typing alone reloads nothing.
    assert "hist_field" not in page.url
    page.locator(f".history-filter:has(input[name=hist_field]) .combobox-option[data-value='{value}']").click()


def _fields_shown(page) -> set[str]:
    page.wait_for_timeout(400)
    return {c.strip() for c in page.locator(".history-table td.col-field").all_inner_texts()
            if c.strip() not in ("", "--")}


def test_item_history_field_filter_searches_and_keeps_the_choice_in_the_url(page, ui_server, fresh_company):
    api = fresh_company
    item = api.post("/items", json={"status": "available", "sku": "HF-1", "sell_by": "piece", "name": "Ring",
                                    "quantity": 1, "cost_total": 10.0}).json()["id"]
    for change in ({"name": {"old": "Ring", "new": "Gold ring"}}, {"quantity": {"old": 1, "new": 3}}):
        assert api.patch(f"/items/{item}", json={"fields_changed": change}).status_code == 200
    page.goto(f"{ui_server}/inventory/{item}/history", wait_until="load")
    before = _fields_shown(page)
    assert len(before) >= 2, before
    chosen = sorted(before)[0]
    _pick_field(page, chosen)
    page.wait_for_url("**hist_field=**")
    assert _fields_shown(page) == {chosen}


def test_document_history_field_filter_reloads_the_card_in_place(page, ui_server, fresh_company):
    api = fresh_company
    contact = api.post("/crm/contacts", json={"name": "Riverside", "contact_type": "customer"}).json()["id"]
    doc = api.post("/docs", json={"doc_type": "invoice", "contact_id": contact, "line_items": [
        {"description": "Lot", "quantity": 1, "unit_price": 10.0, "line_total": 10.0}]}).json()["id"]
    for change in ({"notes": {"old": None, "new": "Call first"}}, {"due_date": {"old": None, "new": "2026-12-01"}}):
        assert api.patch(f"/docs/{doc}", json={"fields_changed": change}).status_code == 200
    page.goto(f"{ui_server}/docs/{doc}", wait_until="load")
    page.wait_for_selector(".history-filters .combobox-input", timeout=8000)
    before = _fields_shown(page)
    assert len(before) >= 2, before
    chosen = sorted(before)[0]
    _pick_field(page, chosen)
    page.wait_for_url("**hist_field=**")
    assert _fields_shown(page) == {chosen}
