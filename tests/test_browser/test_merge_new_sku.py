# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Issue #190 UI: the merge target dropdown offers a last "New SKU" option that
reveals an inline [dropdown] -> [enter SKU] box; picking a plain item shows no
extra box and the merged item keeps that item's SKU."""
from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.browser


def _open_merge_modal(page, ui_server, tag):
    page.set_viewport_size({"width": 1440, "height": 1000})
    page.goto(f"{ui_server}/inventory?q=NSK-{tag}", wait_until="domcontentloaded")
    page.wait_for_selector("input.row-select", timeout=8000)
    assert page.locator("input.row-select").count() == 2
    page.locator("input.row-select").nth(0).click()
    page.locator("input.row-select").nth(1).click()
    page.wait_for_selector("#bulk-toolbar.is-active", timeout=5000)
    page.select_option("#bulk-action-select", "merge")
    page.wait_for_selector("#merge-target-select", timeout=5000)


def _seed_pair(api, tag):
    a = api.post("/items", json={"sku": f"NSK-{tag}-A", "name": "Widget A", "quantity": 5, "sell_by": "piece"})
    b = api.post("/items", json={"sku": f"NSK-{tag}-B", "name": "Widget B", "quantity": 3, "sell_by": "piece"})
    assert a.status_code in {200, 201} and b.status_code in {200, 201}
    return a.json()["id"], b.json()["id"]


def test_new_sku_option_reveals_input_and_merges(page, ui_server, api):
    tag = uuid.uuid4().hex[:6].upper()
    _seed_pair(api, tag)
    _open_merge_modal(page, ui_server, tag)

    # "New SKU" is the last option in the dropdown.
    last = page.locator("#merge-target-select option").last
    assert last.get_attribute("value") == "__new__"

    # The SKU box is hidden until "New SKU" is picked.
    assert page.locator("#merge-resulting-sku").is_hidden()
    page.select_option("#merge-target-select", "__new__")
    page.wait_for_selector("#merge-resulting-sku", state="visible", timeout=5000)
    assert page.locator("#merge-sku-arrow").is_visible()

    # Confirm without a SKU does nothing (input demands a value).
    page.click("#merge-confirm button:has-text('Confirm')")
    assert page.locator("#merge-resulting-sku").is_visible()

    page.fill("#merge-resulting-sku", f"NSK-{tag}-CUSTOM")
    page.click("#merge-confirm button:has-text('Confirm')")
    # Success redirects to the inventory filtered by the merged item's actual
    # (custom) SKU - this also pins the redirect filter behavior.
    page.wait_for_url(f"**/inventory?q=NSK-{tag}-CUSTOM", timeout=8000)

    # Default listing hides merged sources: only the merged item remains.
    active = {i["sku"] for i in api.get(f"/items?q=NSK-{tag}").json()["items"]}
    assert active == {f"NSK-{tag}-CUSTOM"}


def test_item_target_keeps_box_hidden_and_target_sku(page, ui_server, api):
    tag = uuid.uuid4().hex[:6].upper()
    a_id, _ = _seed_pair(api, tag)
    _open_merge_modal(page, ui_server, tag)

    page.select_option("#merge-target-select", a_id)
    page.wait_for_selector("#merge-confirm button:has-text('Confirm')", timeout=5000)
    # No extra box for a plain item target.
    assert page.locator("#merge-resulting-sku").is_hidden()
    assert page.locator("#merge-sku-arrow").is_hidden()

    page.click("#merge-confirm button:has-text('Confirm')")
    page.wait_for_url(f"**/inventory?q=NSK-{tag}-A", timeout=8000)

    active = [i["sku"] for i in api.get(f"/items?q=NSK-{tag}").json()["items"]]
    assert active == [f"NSK-{tag}-A"]
