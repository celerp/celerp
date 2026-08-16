# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Browser tests for split UX - weight/pieces independence.

Covers:
  SPLIT-UI-01: Typing decimal weight character-by-character stays as typed
  SPLIT-UI-02: Changing child_qty does NOT alter child_weight
  SPLIT-UI-03: Changing child_qty does NOT alter child_pieces
  SPLIT-UI-04: Mother weight display updates live as child_weight is typed
  SPLIT-UI-05: Child weight is clamped only on blur, not during typing
  SPLIT-UI-06: sell_by=ct item accepts fractional weight end-to-end
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.browser

_SCREENSHOT_DIR = None  # set per-test if needed


def _assert_no_crash(page, ctx: str = "") -> None:
    body = page.locator("body").inner_text()
    assert "Internal Server Error" not in body, f"{ctx}: Internal Server Error"
    assert "Traceback" not in body, f"{ctx}: Traceback in body"
    assert "/login" not in page.url, f"{ctx}: redirected to login"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def split_item_piece(api):
    """Create a sell_by=piece item with weight and pieces for split tests."""
    r = api.post("/items", json={"status": "available", 
        "sku": "SPLIT-UX-PIECE-001",
        "name": "Split UX Piece Item",
        "sell_by": "piece",
        "quantity": 10.0,
        "weight": 5.0,
        "weight_unit": "gram",
    })
    assert r.status_code in {200, 201}, f"Failed to create piece item: {r.text}"
    item = r.json()
    # Set pieces via patch (the endpoint only reads fields_changed; a raw
    # {"attributes": ...} body is silently ignored)
    rp = api.patch(f"/items/{item['id']}", json={
        "fields_changed": {"pieces": {"old": None, "new": 10}},
    })
    assert rp.status_code in {200, 201}, f"Failed to set pieces: {rp.text}"
    return item


@pytest.fixture(scope="module")
def split_item_gram_pieces(api):
    """sell_by=gram parcel with an independent pieces attribute (10 pieces
    weighed as 10g) — the shape where child_pieces renders as an editable input.
    (For sell_by=piece items the pieces column is hidden: redundant with QTY.)"""
    r = api.post("/items", json={"status": "available", 
        "sku": "SPLIT-UX-GRAM-001",
        "name": "Split UX Gram Parcel",
        "sell_by": "gram",
        "quantity": 10.0,
    })
    assert r.status_code in {200, 201}, f"Failed to create gram item: {r.text}"
    item = r.json()
    rp = api.patch(f"/items/{item['id']}", json={
        "fields_changed": {"pieces": {"old": None, "new": 10}},
    })
    assert rp.status_code in {200, 201}, f"Failed to set pieces: {rp.text}"
    return item


@pytest.fixture(scope="module")
def split_item_ct(api):
    """Create a sell_by=carat item for fractional-split tests.

    New format: a carat item tracks its weight *as* the quantity (in carats) —
    there's no separate weight/weight_unit, and the quantity accepts fractions.
    """
    r = api.post("/items", json={"status": "available", 
        "sku": "SPLIT-UX-CT-001",
        "name": "Split UX CT Item",
        "sell_by": "carat",
        "quantity": 10.0,
    })
    assert r.status_code in {200, 201}, f"Failed to create ct item: {r.text}"
    return r.json()


def _open_split_panel(page, ui_server, item_id: str) -> None:
    """Navigate to inventory, check the item row, open split panel."""
    page.goto(f"{ui_server}/inventory", wait_until="domcontentloaded")
    _assert_no_crash(page, "inventory list")

    # Check the row for this item
    checkbox = page.locator(f"input.row-select[data-entity-id='{item_id}']")
    if checkbox.count() == 0:
        # May be on a different page; try searching
        page.goto(f"{ui_server}/inventory?search=SPLIT-UX", wait_until="domcontentloaded")
        checkbox = page.locator(f"input.row-select[data-entity-id='{item_id}']")

    assert checkbox.count() > 0, f"Row checkbox not found for item {item_id}"
    checkbox.check()
    page.wait_for_selector("#bulk-toolbar.is-active", timeout=3000)

    # Select "Split" from bulk action dropdown
    action_select = page.locator("#bulk-action-select")
    action_select.select_option(label="Split")
    page.wait_for_selector("#bulk-split-preview form", timeout=5000)
    _assert_no_crash(page, "split panel open")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_split_ui_01_decimal_weight_input_accepted(page, ui_server, split_item_piece):
    """SPLIT-UI-01: Typing '1.5' character-by-character into child_weight stays '1.5'."""
    _open_split_panel(page, ui_server, split_item_piece["id"])

    weight_input = page.locator('input[name="child_weight"]')
    assert weight_input.count() > 0, "child_weight input not found in split panel"

    weight_input.click()
    weight_input.fill("")  # clear any default
    # Type character by character to simulate real user input
    weight_input.type("1")
    weight_input.type(".")
    weight_input.type("5")

    val = weight_input.input_value()
    assert val == "1.5", (
        f"After typing '1.5' char-by-char, field shows '{val}'. "
        "The oninput handler is rewriting the value mid-type (Bug B)."
    )


def test_split_ui_02_qty_change_does_not_alter_weight(page, ui_server, split_item_piece):
    """SPLIT-UI-02: Changing child_qty must NOT auto-set child_weight."""
    _open_split_panel(page, ui_server, split_item_piece["id"])

    weight_input = page.locator('input[name="child_weight"]')
    qty_input = page.locator('input[name="child_qty"]')
    assert weight_input.count() > 0, "child_weight input not found"
    assert qty_input.count() > 0, "child_qty input not found"

    # Record initial weight value
    initial_weight = weight_input.input_value()

    # Change qty
    qty_input.fill("3")
    qty_input.dispatch_event("change")
    page.wait_for_timeout(300)  # let any JS settle

    weight_after_qty_change = weight_input.input_value()
    assert weight_after_qty_change == initial_weight, (
        f"After changing child_qty to 3, child_weight changed from '{initial_weight}' "
        f"to '{weight_after_qty_change}'. Qty change must not touch weight (Bug A)."
    )

    # Change qty again
    qty_input.fill("7")
    qty_input.dispatch_event("change")
    page.wait_for_timeout(300)

    weight_after_second_change = weight_input.input_value()
    assert weight_after_second_change == initial_weight, (
        f"After changing child_qty to 7, child_weight changed to '{weight_after_second_change}'. "
        "Qty changes must never touch weight."
    )


def test_split_ui_03_qty_change_does_not_alter_pieces(page, ui_server, split_item_gram_pieces):
    """SPLIT-UI-03: Changing child_qty must NOT auto-set child_pieces.

    Uses a weight-unit parcel with an independent pieces attribute — the only
    shape where child_pieces is editable (for sell_by=piece items the pieces
    column is hidden entirely; pieces is redundant with QTY there).
    """
    _open_split_panel(page, ui_server, split_item_gram_pieces["id"])

    pieces_input = page.locator('input[name="child_pieces"]')
    qty_input = page.locator('input[name="child_qty"]')

    assert pieces_input.count() > 0, (
        "child_pieces input not rendered for a weight-unit item with a pieces attribute"
    )

    initial_pieces = pieces_input.input_value()

    qty_input.fill("4")
    qty_input.dispatch_event("change")
    page.wait_for_timeout(300)

    pieces_after = pieces_input.input_value()
    assert pieces_after == initial_pieces, (
        f"After changing child_qty to 4, child_pieces changed from '{initial_pieces}' "
        f"to '{pieces_after}'. Qty change must not touch pieces (Bug A)."
    )


def test_split_ui_04_mother_weight_display_updates_live(page, ui_server, split_item_piece):
    """SPLIT-UI-04: Mother weight display updates correctly as child_weight is typed."""
    _open_split_panel(page, ui_server, split_item_piece["id"])

    weight_input = page.locator('input[name="child_weight"]')
    mother_display = page.locator(".mother-weight-display")
    assert weight_input.count() > 0, "child_weight input not found"
    assert mother_display.count() > 0, "mother-weight-display not found"

    # Parent weight is 5.0; type 2 → mother should show 3.0
    weight_input.fill("2")
    weight_input.dispatch_event("input")
    page.wait_for_timeout(200)

    mother_text = mother_display.inner_text()
    # Allow for .00 suffix - just check the numeric value
    try:
        mother_val = float(mother_text)
    except ValueError:
        mother_val = None
    assert mother_val is not None and abs(mother_val - 3.0) < 0.01, (
        f"After entering child_weight=2 on parent_weight=5.0, mother shows '{mother_text}' "
        "(expected ~3.0)"
    )


def test_split_ui_05_weight_clamped_on_blur_not_on_input(page, ui_server, split_item_piece):
    """SPLIT-UI-05: Typing an over-limit value must NOT be clamped mid-type; clamped on blur."""
    _open_split_panel(page, ui_server, split_item_piece["id"])

    weight_input = page.locator('input[name="child_weight"]')
    assert weight_input.count() > 0, "child_weight input not found"

    # Type a value larger than parent weight (5.0) but don't blur yet
    weight_input.click()
    weight_input.fill("")
    weight_input.type("9")
    weight_input.type("9")

    val_during_input = weight_input.input_value()
    assert val_during_input == "99", (
        f"Field was clamped mid-type to '{val_during_input}' instead of staying '99'. "
        "Clamping must only happen on blur, not on input."
    )

    # Now blur - it should clamp to parent weight 5.0
    weight_input.dispatch_event("blur")
    page.wait_for_timeout(200)

    val_after_blur = weight_input.input_value()
    try:
        clamped = float(val_after_blur)
    except ValueError:
        clamped = None
    assert clamped is not None and clamped <= 5.0, (
        f"After blur, expected clamped value <=5.0, got '{val_after_blur}'"
    )


def test_split_ui_06_ct_item_fractional_weight_end_to_end(page, ui_server, api, split_item_ct):
    """SPLIT-UI-06: a sell_by=carat item accepts a *fractional* split quantity.

    For weight-tracked (carat) items the quantity IS the carat weight, so there
    is no separate child_weight field — the fractional value goes in child_qty.
    """
    _open_split_panel(page, ui_server, split_item_ct["id"])

    qty_input = page.locator('input[name="child_qty"]')
    sku_input = page.locator('input[name="child_sku"]')
    assert qty_input.count() > 0, "child_qty input not found"

    # Weight-type sell_by without pieces: QTY alone carries the information —
    # no redundant weight column (qty IS the carat weight).
    assert page.locator('input[name="child_weight"]').count() == 0
    assert page.locator(".child-weight-display").count() == 0

    # Type the fractional carat weight character-by-character to catch a rewrite.
    qty_input.click()
    qty_input.fill("")
    qty_input.type("1.5")
    assert qty_input.input_value() == "1.5", (
        f"child_qty shows '{qty_input.input_value()}' instead of '1.5'"
    )
    qty_input.dispatch_event("blur")
    page.wait_for_timeout(200)

    # Set a unique child SKU
    sku_input.fill("SPLIT-UX-CT-001-CHILD")

    # Submit
    form = page.locator("#bulk-split-preview form")
    form.locator('button[type="submit"]').click()
    page.wait_for_timeout(1000)

    _assert_no_crash(page, "after split submit")

    # No error flash. Match on actual text — the page can carry an empty
    # placeholder .flash--error container that is not a real error.
    errors = [t.strip() for t in
              page.locator(".flash--warning, .flash--error").all_inner_texts() if t.strip()]
    assert not errors, f"Split returned error: {errors}"

    # Verify via API: the child exists with the fractional carat quantity.
    r = api.get("/items", params={"search": "SPLIT-UX-CT-001-CHILD"})
    assert r.status_code == 200
    items = r.json().get("items", [])
    child = next((it for it in items if it.get("sku") == "SPLIT-UX-CT-001-CHILD"), None)
    assert child is not None, "Child item not found after split"
    assert abs(float(child.get("quantity") or 0) - 1.5) < 0.01, (
        f"Child quantity={child.get('quantity')}, expected 1.5"
    )
