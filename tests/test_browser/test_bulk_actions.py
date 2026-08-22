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
    page.goto(f"{ui_server}/inventory", wait_until="domcontentloaded")
    body = page.locator("body").inner_text()
    assert "Internal Server Error" not in body
    # Row checkboxes (cls="row-select") are rendered server-side
    assert page.locator("input.row-select").count() > 0, "No row-select checkboxes on inventory list"
    # Select-all checkbox is a static input in the header (id="select-all-rows")
    assert page.locator("#select-all-rows").count() > 0, "No #select-all-rows checkbox in table header"


def test_bulk_select_all(page, ui_server, bulk_item_ids):
    """BULK-01: Click #select-all-rows → all row checkboxes checked → bulk toolbar activates."""
    page.goto(f"{ui_server}/inventory", wait_until="domcontentloaded")

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
    page.goto(f"{ui_server}/inventory", wait_until="domcontentloaded")

    checkboxes = page.locator("input.row-select")
    assert checkboxes.count() >= 2, f"Expected >=2 row checkboxes, got {checkboxes.count()}"

    checkboxes.nth(0).click()
    checkboxes.nth(1).click()

    # Bulk toolbar becomes active on row checkbox change
    page.wait_for_selector("#bulk-toolbar.is-active", timeout=3000)

    body = page.locator("body").inner_text()
    assert "Internal Server Error" not in body
    assert "Traceback" not in body

    # Action dropdown is present with the inventory bulk actions
    action_select = page.locator("#bulk-action-select")
    assert action_select.count() > 0, "No action select dropdown in bulk toolbar"
    options_text = page.locator("#bulk-action-select option").all_inner_texts()
    assert "Transfer" in options_text, f"Transfer option not found. Got: {options_text}"
    # "Dispose" was a misleading label for what only archives; the menu now reads plain "Archive",
    # with a separate real "Write off" action that seeds a write-off list.
    assert "Archive" in options_text and "Archive / Dispose" not in options_text, \
        f"Archive option not renamed. Got: {options_text}"
    assert any("Write off" in o for o in options_text), f"No Write off bulk action. Got: {options_text}"


def test_archive_qtypositive_offers_choice_no_autoledger(page, ui_server, api):
    """J2: archiving a still-stocked item surfaces an unmissable two-way choice (keep the stock on
    the books vs write it off) instead of silently archiving, and takes no ledger action on its own.
    Control: a zero-stock item archives plain, with no choice dialog."""
    native: list[str] = []
    page.on("dialog", lambda d: (native.append(d.message), d.accept()))

    # qty>0 -> the two-way choice modal, no automatic archive or JE. The item must be available
    # (on the books) for the guard to matter: a draft holds no stock value and cannot be archived
    # through the generic status path in the first place.
    pos_id = api.post("/items", json={
        "sku": "WO-GUARD-POS", "sell_by": "piece", "name": "Guard Pos", "quantity": 7}).json()["id"]
    api.post("/items/bulk/make-available", json={"entity_ids": [pos_id]})
    page.goto(f"{ui_server}/inventory", wait_until="domcontentloaded")
    row = page.locator(f'input.row-select[value="{pos_id}"]')
    row.wait_for(timeout=5000)
    row.click()
    page.wait_for_selector("#bulk-toolbar.is-active", timeout=3000)
    n0 = len(native)
    page.locator("#bulk-action-select").select_option("archive")
    # A real in-page choice appears (not a native confirm, not a silent archive).
    page.get_by_text("Write off remaining stock").wait_for(state="visible", timeout=3000)
    assert page.get_by_text("Keep stock on books").count() > 0
    assert len(native) == n0, "archive fired a native confirm instead of the two-way choice"
    # No automatic ledger/status action while the choice is pending.
    assert api.get(f"/items/{pos_id}").json()["status"] == "available"

    # Control: an available but depleted (qty=0) item archives plain, with no two-way choice
    # (behaviour as at merge-base).
    zero_id = api.post("/items", json={
        "sku": "WO-GUARD-ZERO", "sell_by": "piece", "name": "Guard Zero", "quantity": 0}).json()["id"]
    api.post("/items/bulk/make-available", json={"entity_ids": [zero_id]})
    page.goto(f"{ui_server}/inventory", wait_until="domcontentloaded")
    # Selection persists across pages by design; drop the still-selected stocked item from the first
    # scenario so only the depleted item is under the archive action here.
    page.evaluate("window.CelerpSelection && window.CelerpSelection.clear()")
    zrow = page.locator(f'input.row-select[value="{zero_id}"]')
    zrow.wait_for(timeout=5000)
    zrow.click()
    page.wait_for_selector("#bulk-toolbar.is-active", timeout=3000)
    page.locator("#bulk-action-select").select_option("archive")
    assert page.get_by_text("Write off remaining stock").count() == 0, "zero-stock archive showed the choice"
    page.wait_for_timeout(500)
    assert api.get(f"/items/{zero_id}").json()["status"] == "archived"


def test_writeoff_journey_terminal_and_undo(page, ui_server, api):
    """J1 end to end: bulk write-off -> fill qty out and account on-page -> Issue -> the Write off
    stock terminal disposes the stock and closes the list -> Undo write-off restores it. Guards the
    terminal's UI wiring: finalize alone must never be a dead end."""
    api.post("/accounting/chart/seed")
    iid = api.post("/items", json={
        "sku": "WO-E2E-001", "sell_by": "piece", "name": "Writeoff E2E", "quantity": 8}).json()["id"]
    api.post("/items/bulk/make-available", json={"entity_ids": [iid]})
    page.on("dialog", lambda d: d.accept())

    page.goto(f"{ui_server}/inventory", wait_until="domcontentloaded")
    page.evaluate("window.CelerpSelection && window.CelerpSelection.clear()")
    row = page.locator(f'input.row-select[value="{iid}"]')
    row.wait_for(timeout=5000)
    row.click()
    page.wait_for_selector("#bulk-toolbar.is-active", timeout=3000)
    page.locator("#bulk-action-select").select_option("write_off")
    page.wait_for_url("**/lists/**", timeout=15000)

    # Full-quantity line: qty out 8 of 8, destination account from the seeded chart.
    page.locator("td.col-qtyout").first.dblclick()
    qty = page.locator("td.col-qtyout input:visible").first
    qty.fill("8")
    qty.press("Enter")
    page.wait_for_timeout(500)
    page.locator("td.col-account").first.dblclick()
    acct = page.locator("td.col-account input:visible").first
    acct.click()
    acct.fill("69")
    page.locator(".combobox-option:visible").first.click()
    page.wait_for_timeout(500)

    page.get_by_role("button", name="Issue").click()
    # Finalized view must offer the terminal - this is the regression under guard.
    page.get_by_role("button", name="Write off stock").wait_for(state="visible", timeout=10000)
    assert api.get(f"/items/{iid}").json()["status"] == "available"  # nothing disposed yet

    page.get_by_role("button", name="Write off stock").click()
    page.get_by_role("button", name="Undo write-off").wait_for(state="visible", timeout=10000)
    assert api.get(f"/items/{iid}").json()["status"] == "disposed"

    page.get_by_role("button", name="Undo write-off").click()
    page.get_by_role("button", name="Write off stock").wait_for(state="visible", timeout=10000)
    assert api.get(f"/items/{iid}").json()["status"] == "available"
