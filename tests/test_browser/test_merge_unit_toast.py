# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Merging items with different weight units must be rejected with the standard lower-right toast."""
from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.browser


def test_merge_different_units_shows_error_toast(page, ui_server, api):
    tag = uuid.uuid4().hex[:6].upper()
    a = api.post("/items", json={"sku": f"TWU-{tag}-A", "name": "Gold A", "quantity": 5, "sell_by": "gram"})
    b = api.post("/items", json={"sku": f"TWU-{tag}-B", "name": "Gold B", "quantity": 3, "sell_by": "carat"})
    assert a.status_code in {200, 201} and b.status_code in {200, 201}
    a_id = a.json()["id"]

    page.set_viewport_size({"width": 1440, "height": 1000})
    page.goto(f"{ui_server}/inventory?q=TWU-{tag}", wait_until="domcontentloaded")
    page.wait_for_selector("input.row-select", timeout=8000)
    assert page.locator("input.row-select").count() == 2

    page.locator("input.row-select").nth(0).click()
    page.locator("input.row-select").nth(1).click()
    page.wait_for_selector("#bulk-toolbar.is-active", timeout=5000)

    page.select_option("#bulk-action-select", "merge")
    page.wait_for_selector("#merge-target-select", timeout=5000)
    page.select_option("#merge-target-select", a_id)            # picking a target reveals Confirm
    page.click("#merge-confirm button:has-text('Confirm')")

    # The standard lower-right toast appears with an informative unit-mismatch message.
    page.wait_for_selector(".toast-container .toast--error", timeout=8000)
    toast = page.locator(".toast-container .toast--error").inner_text()
    assert "unit" in toast.lower(), f"toast did not mention units: {toast!r}"
    from pathlib import Path
    Path("context/reviews/inventory").mkdir(parents=True, exist_ok=True)
    page.screenshot(path="context/reviews/inventory/merge-unit-toast.png", full_page=True)

    # The merge did not happen: both source items still exist.
    skus = {i["sku"] for i in api.get(f"/items?q=TWU-{tag}").json()["items"]}
    assert {f"TWU-{tag}-A", f"TWU-{tag}-B"} <= skus
