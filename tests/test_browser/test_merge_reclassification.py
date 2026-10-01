# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Merging items held in different inventory accounts tells the user, before they
confirm and again in the result, how much value moves between which accounts, and
the merged item offers an undo that restores the original items."""
from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.browser


def _lot(api, sku: str, cost: float) -> str:
    r = api.post("/items", json={"status": "available", "sku": sku, "name": "Lot", "quantity": 1,
                                 "sell_by": "piece", "cost_total": cost})
    assert r.status_code in {200, 201}, r.text
    return r.json()["id"]


def test_merge_across_inventory_accounts_is_disclosed_and_can_be_undone(page, ui_server, fresh_company):
    api = fresh_company
    tag = uuid.uuid4().hex[:6].upper()
    a = _lot(api, f"MRC-{tag}", 600.0)
    r = api.post("/accounting/accounts", json={"code": "1131", "name": "Stock Room B",
                                               "account_type": "asset", "parent_code": "1130"})
    assert r.status_code == 200, r.text
    r = api.put("/accounting/posting-accounts/inventory_purchased", json={"code": "1131"})
    assert r.status_code == 200, r.text
    b = _lot(api, f"MRC-{tag}-B", 400.0)

    page.set_viewport_size({"width": 1440, "height": 1000})
    page.goto(f"{ui_server}/inventory?q=MRC-{tag}", wait_until="domcontentloaded")
    page.wait_for_selector("input.row-select", timeout=8000)
    page.locator("input.row-select").nth(0).click()
    page.locator("input.row-select").nth(1).click()
    page.wait_for_selector("#bulk-toolbar.is-active", timeout=5000)
    page.select_option("#bulk-action-select", "merge")
    page.wait_for_selector("#merge-target-select", timeout=5000)
    page.select_option("#merge-target-select", a)

    note = page.locator("#merge-reclass-note")
    note.wait_for(state="visible", timeout=8000)
    assert note.inner_text() == ("These items are held in different inventory accounts. "
                                 "Merging will move $400.00 from Stock Room B to Inventory - Purchased.")

    page.click("#merge-confirm button:has-text('Confirm')")
    result = page.locator(".toast-container .toast--success")
    result.wait_for(timeout=8000)
    assert "$400.00 from Stock Room B moved to Inventory - Purchased." in result.inner_text()

    items = api.get("/items", params={"q": f"MRC-{tag}", "status": "all"}).json()["items"]
    [merged] = [i["id"] for i in items if i["id"] not in {a, b}]

    page.goto(f"{ui_server}/inventory/{merged}?tab=activity", wait_until="domcontentloaded")
    page.once("dialog", lambda d: d.accept())
    page.click("#merge-undo button:has-text('Undo merge')")
    page.wait_for_selector("#merge-undo .flash--success", timeout=8000)

    assert {api.get(f"/items/{i}").json()["status"] for i in (a, b)} == {"available"}
    assert api.get(f"/items/{merged}").json()["status"] == "archived"
