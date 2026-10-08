# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Receiving and returning the selected lines of a bill from its page.

The receipt reloads the bill and the summary of what it did with each line is shown on the
reloaded page. Returning one received line sends only that line's goods back.
"""
from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.browser


def _bill(api, tag: str) -> str:
    r = api.post("/docs", json={"doc_type": "bill", "line_items": [
        {"sku": f"RRL-{tag}", "name": "Goods", "quantity": 2, "unit_price": 5.0},
        {"name": "Courier fee", "quantity": 1, "unit_price": 25.0, "receive_as": "expense"},
    ]})
    assert r.status_code in {200, 201}, r.text
    doc_id = r.json()["id"]
    assert api.post(f"/docs/{doc_id}/finalize").status_code == 200
    return doc_id


def _select_all_and_act(page, action: str, form_id: str) -> None:
    page.wait_for_selector("#li-select-all", timeout=8000)
    page.check("#li-select-all")
    page.select_option("#li-bulk-select", action)
    page.wait_for_selector(f"#{form_id}:visible", timeout=5000)


def test_receipt_summary_shows_after_the_reload_and_one_line_goes_back(page, ui_server, api):
    tag = uuid.uuid4().hex[:6].upper()
    bill = _bill(api, tag)
    page.on("dialog", lambda d: d.accept())
    page.goto(f"{ui_server}/docs/{bill}", wait_until="domcontentloaded")

    _select_all_and_act(page, "li-fulfill", "li-bulk-fulfill-btn")
    # Only the selected lines' rows are offered, each with its quantity.
    assert page.locator("#li-bulk-fulfill-btn fieldset.receive-row:visible").count() == 2
    page.click("#li-bulk-fulfill-btn button[type=submit]")

    page.wait_for_selector(".toast-container .toast", timeout=10000)
    toast = page.locator(".toast-container .toast").first.inner_text()
    assert "1 line added stock. 1 expense line added no stock." in toast, toast
    doc = api.get(f"/docs/{bill}").json()
    assert doc["status"] == "received", doc["status"]

    _select_all_and_act(page, "li-revert", "li-bulk-revert-btn")
    rows = page.locator("#li-bulk-revert-btn fieldset.receive-row:visible")
    assert rows.count() == 2
    # The expense line has no goods to send back; the stock line offers both units.
    assert rows.nth(0).locator("input.line-qty-input").input_value() == "2"
    assert rows.nth(1).locator("input.line-qty-input").count() == 0
    rows.nth(0).locator("input.line-qty-input").fill("1")
    with page.expect_response(lambda r: r.url.endswith(f"/docs/{bill}/return-goods"), timeout=10000) as resp:
        page.click("#li-bulk-revert-btn button[type=submit]")
    assert resp.value.status == 204, resp.value.headers.get("hx-trigger")

    doc = api.get(f"/docs/{bill}").json()
    assert doc["status"] == "partial_returned", doc["status"]
    assert doc["line_items"][0]["returnable_quantity"] == 1
