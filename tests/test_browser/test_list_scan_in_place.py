# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Scanning into a building list is in place, like invoices and memos: the new line appears
without a page reload and the scan bar keeps focus so the next scan can be entered immediately.
"""
from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.browser


def test_building_list_scan_appends_without_reload(page, ui_server, api):
    tag = uuid.uuid4().hex[:6]
    barcode = str(uuid.uuid4().int)[:12]
    api.post("/items", json={"status": "available", "sku": f"SCN-{tag}", "name": "Scan Widget", "sell_by": "piece",
             "quantity": 10, "barcode": barcode})
    list_id = api.post("/lists", json={"list_type": "quotation"}).json()["id"]

    page.goto(f"{ui_server}/lists/{list_id}", wait_until="domcontentloaded")
    page.wait_for_selector("#scan-bar-input", timeout=8000)

    # A reload would wipe this flag; it surviving the scan proves the update was in place.
    page.evaluate("window.__scanProbe = 1")

    page.locator("#scan-bar-input").click()
    page.locator("#scan-bar-input").fill(barcode)
    page.locator("#scan-bar-input").press("Enter")

    # The scanned line lands in the tbody without a navigation.
    page.wait_for_selector(f'#line-body [data-name="sku"][value="SCN-{tag}"]', timeout=8000)
    assert page.evaluate("window.__scanProbe") == 1, "page reloaded on scan"
    # Focus stays on the scan bar, cleared and ready for the next scan.
    assert page.evaluate("document.activeElement && document.activeElement.id") == "scan-bar-input"
    assert page.locator("#scan-bar-input").input_value() == ""
