# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
# Copyright (c) 2026 Noah Severs. All rights reserved.
"""Group 9: Bulk actions — inventory checkboxes + bulk operations."""
import pytest

pytestmark = pytest.mark.browser


@pytest.fixture(scope="module")
def bulk_item_ids(api):
    """Create 3 test items for bulk action tests."""
    ids = []
    for i in range(3):
        r = api.post("/items", json={
            "sku": f"BULK-TEST-{i:03d}",
            "sell_by": "piece",
            "name": f"Bulk Test Item {i}",
            "quantity": 10,
        })
        if r.status_code in {200, 201}:
            ids.append(r.json()["id"])
    assert len(ids) >= 2, f"Could not create enough items for bulk test (got {len(ids)})"
    return ids


def test_inventory_list_has_checkboxes(page, ui_server, bulk_item_ids):
    """BULK sanity: Inventory list renders with row checkboxes and select-all."""
    page.goto(f"{ui_server}/inventory", wait_until="networkidle")
    body = page.locator("body").inner_text()
    assert "Internal Server Error" not in body
    # Row checkboxes (cls="row-select") are rendered server-side
    assert page.locator("input.row-select").count() > 0, "No row-select checkboxes on inventory list"
    # Select-all checkbox is a static input in the header (id="select-all-rows")
    assert page.locator("#select-all-rows").count() > 0, "No #select-all-rows checkbox in table header"


def test_bulk_select_all(page, ui_server, bulk_item_ids):
    """BULK-01: Click #select-all-rows → all row checkboxes checked → bulk toolbar activates."""
    page.goto(f"{ui_server}/inventory", wait_until="networkidle")

    select_all = page.locator("#select-all-rows")
    assert select_all.count() > 0, "No select-all checkbox found (#select-all-rows)"

    select_all.click()

    # JS updates row checkboxes and adds .is-active to bulk toolbar
    page.wait_for_selector("#bulk-toolbar.is-active", timeout=3000)

    body = page.locator("body").inner_text()
    assert "Internal Server Error" not in body
    assert "Traceback" not in body

    checked = page.locator("input.row-select:checked").count()
    total = page.locator("input.row-select").count()
    assert checked == total, f"Expected all {total} checkboxes checked, got {checked}"


def test_bulk_transfer_modal(page, ui_server, bulk_item_ids):
    """BULK-03: Select rows → bulk toolbar activates → action dropdown present."""
    page.goto(f"{ui_server}/inventory", wait_until="networkidle")

    checkboxes = page.locator("input.row-select")
    assert checkboxes.count() >= 2, f"Expected >=2 row checkboxes, got {checkboxes.count()}"

    checkboxes.nth(0).click()
    checkboxes.nth(1).click()

    # Bulk toolbar becomes active on row checkbox change
    page.wait_for_selector("#bulk-toolbar.is-active", timeout=3000)

    body = page.locator("body").inner_text()
    assert "Internal Server Error" not in body
    assert "Traceback" not in body

    # Action dropdown is present with Transfer, Delete options
    action_select = page.locator("#bulk-action-select")
    assert action_select.count() > 0, "No action select dropdown in bulk toolbar"
    # "Transfer" is an option
    options_text = page.locator("#bulk-action-select option").all_inner_texts()
    assert "Transfer" in options_text, f"Transfer option not found. Got: {options_text}"
    assert "Delete" in options_text, f"Delete option not found. Got: {options_text}"
