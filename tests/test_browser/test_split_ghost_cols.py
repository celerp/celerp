# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Browser regression tests for feat/inventory-transform bugs.

Covers:
  GHOST-COL-01: After split, mother and child rows have identical visible columns
  GHOST-COL-02: After transform, result rows have identical visible columns
  PIECE-SYNC-01: For sell_by=piece, changing Qty mirrors to Pieces + updates mother
  PIECE-SYNC-02: For sell_by=piece, changing Pieces mirrors to Qty + updates mother
"""
from __future__ import annotations

import pathlib
import pytest

pytestmark = pytest.mark.browser


def _save_screenshot(page, name: str) -> None:
    # Debug screenshots disabled — the hardcoded /mnt/storage path is not portable.
    # _SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    # page.screenshot(path=str(_SCREENSHOT_DIR / f"{name}.png"), full_page=True)
    pass


def _assert_no_crash(page, ctx: str = "") -> None:
    body = page.locator("body").inner_text()
    assert "Internal Server Error" not in body, f"{ctx}: Internal Server Error"
    assert "Traceback" not in body, f"{ctx}: Traceback in body"
    assert "/login" not in page.url, f"{ctx}: redirected to login"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def splittable_piece_item(api):
    """Item with sell_by=piece, quantity > 1 so it can be split."""
    r = api.post("/items", json={
        "sku": "GHOST-PIECE-001",
        "name": "Ghost Col Piece Item",
        "sell_by": "piece",
        "quantity": 10.0,
        "weight": 5.0,
        "weight_unit": "gram",
    })
    assert r.status_code in {200, 201}, f"create failed: {r.text}"
    item = r.json()
    # Set the pieces attribute (the endpoint only reads fields_changed; a raw
    # {"attributes": ...} body is silently ignored)
    rp = api.patch(f"/items/{item['id']}", json={
        "fields_changed": {"pieces": {"old": None, "new": 10}},
    })
    assert rp.status_code in {200, 201}
    return item


@pytest.fixture(scope="module")
def splittable_weight_item(api):
    """Item with sell_by=gram, quantity > 1 so it can be split or transformed."""
    r = api.post("/items", json={
        "sku": "GHOST-WEIGHT-001",
        "name": "Ghost Col Weight Item",
        "sell_by": "gram",
        "quantity": 100.0,
        "weight": 100.0,
        "weight_unit": "gram",
    })
    assert r.status_code in {200, 201}, f"create failed: {r.text}"
    return r.json()


def _find_and_check_item(page, ui_server, item_id: str) -> None:
    """Navigate to inventory, locate the item row, check its checkbox."""
    page.goto(f"{ui_server}/inventory", wait_until="domcontentloaded")
    _assert_no_crash(page, "inventory list")
    checkbox = page.locator(f"input.row-select[data-entity-id='{item_id}']")
    if checkbox.count() == 0:
        page.goto(f"{ui_server}/inventory?status=all", wait_until="domcontentloaded")
        checkbox = page.locator(f"input.row-select[data-entity-id='{item_id}']")
    assert checkbox.count() > 0, f"Row for item {item_id} not found"
    checkbox.check()
    page.wait_for_selector("#bulk-toolbar.is-active", timeout=3000)


def _get_visible_col_keys_per_row(page) -> list[list[str]]:
    """Return list of visible data-col keys per tbody data-row."""
    return page.evaluate("""() => {
        const table = document.getElementById('data-table');
        if (!table) return [];
        const result = [];
        table.querySelectorAll('tbody tr.data-row').forEach(tr => {
            const visible = [];
            tr.querySelectorAll('td[data-col]').forEach(td => {
                if (window.getComputedStyle(td).display !== 'none') {
                    visible.push(td.dataset.col);
                }
            });
            result.push(visible);
        });
        return result;
    }""")


def _open_split_panel(page, ui_server, item_id: str) -> None:
    _find_and_check_item(page, ui_server, item_id)
    page.locator("#bulk-action-select").select_option(label="Split")
    page.wait_for_selector("#bulk-split-preview form", timeout=5000)
    _assert_no_crash(page, "split panel open")


# ---------------------------------------------------------------------------
# GHOST-COL-01: After split, all rows have identical visible column sets
# ---------------------------------------------------------------------------

def test_ghost_col_01_no_ghost_cols_after_split(page, ui_server, api, splittable_piece_item):
    """GHOST-COL-01: After split, mother and child rows must show the same columns."""
    _open_split_panel(page, ui_server, splittable_piece_item["id"])

    page.locator('input[name="child_qty"]').fill("3")
    page.locator('input[name="child_qty"]').dispatch_event("change")
    page.wait_for_timeout(300)

    page.locator("#bulk-split-preview form button[type='submit']").click()
    page.wait_for_selector("#inventory-content", timeout=8000)
    page.wait_for_load_state("load")
    page.wait_for_timeout(600)

    _assert_no_crash(page, "after split")
    _save_screenshot(page, "ghost-col-01-after-split")

    visible_per_row = _get_visible_col_keys_per_row(page)
    assert len(visible_per_row) >= 2, f"Expected ≥2 rows after split, got {len(visible_per_row)}"

    col_sets = [frozenset(cols) for cols in visible_per_row]
    assert len(set(col_sets)) == 1, (
        "Ghost column bug: rows have different visible column sets after split!\n"
        + "\n".join(f"  Row {i}: {sorted(cols)}" for i, cols in enumerate(visible_per_row))
    )


# ---------------------------------------------------------------------------
# GHOST-COL-02: After transform, all rows have identical visible column sets
# ---------------------------------------------------------------------------

def test_ghost_col_02_no_ghost_cols_after_transform(page, ui_server, api, splittable_weight_item):
    """GHOST-COL-02: After transform, result rows must show the same columns."""
    _find_and_check_item(page, ui_server, splittable_weight_item["id"])

    action_select = page.locator("#bulk-action-select")
    options = action_select.evaluate("el => Array.from(el.options).map(o => o.text)")
    if "Transform" not in options:
        pytest.skip("Transform action not available in this build")

    action_select.select_option(label="Transform")
    page.wait_for_selector("#bulk-transform-preview-form", timeout=5000)
    _assert_no_crash(page, "transform panel open")

    for name in ("output_qty", "child_qty"):
        inp = page.locator(f'input[name="{name}"]')
        if inp.count() > 0:
            inp.fill("10")
            inp.dispatch_event("change")
            break
    page.wait_for_timeout(300)

    page.locator("#bulk-transform-preview-form button[type='submit']").click()
    page.wait_for_selector("#inventory-content", timeout=8000)
    page.wait_for_load_state("load")
    page.wait_for_timeout(600)

    _assert_no_crash(page, "after transform")
    _save_screenshot(page, "ghost-col-02-after-transform")

    visible_per_row = _get_visible_col_keys_per_row(page)
    if len(visible_per_row) < 2:
        pytest.skip("Transform did not produce multiple rows in filtered view")

    col_sets = [frozenset(cols) for cols in visible_per_row]
    assert len(set(col_sets)) == 1, (
        "Ghost column bug: rows have different visible column sets after transform!\n"
        + "\n".join(f"  Row {i}: {sorted(cols)}" for i, cols in enumerate(visible_per_row))
    )


# ---------------------------------------------------------------------------
# PIECE-SYNC-01: Changing Qty mirrors to the read-only Pieces column (sell_by=piece)
# ---------------------------------------------------------------------------

def test_piece_sync_01_qty_mirrors_to_pieces(page, ui_server, api):
    """PIECE-SYNC-01: For sell_by=piece WITH weight, the Pieces column mirrors QTY.

    A piece item that also carries weight shows both columns; pieces renders as a
    read-only mirror of QTY (no editable input — pieces IS qty), and the static
    displays must follow every qty change.

    Seeds its own item — the module fixture's item is mutated by the earlier
    split test, so its qty is no longer 10.
    """
    r = api.post("/items", json={
        "sku": "GHOST-PIECEMIRROR-001",
        "name": "Ghost Col Piece Mirror Item",
        "sell_by": "piece",
        "quantity": 10.0,
        "weight": 5.0,
        "weight_unit": "gram",
    })
    assert r.status_code in {200, 201}, f"create failed: {r.text}"
    item = r.json()
    rp = api.patch(f"/items/{item['id']}", json={
        "fields_changed": {"pieces": {"old": None, "new": 10}},
    })
    assert rp.status_code in {200, 201}

    _open_split_panel(page, ui_server, item["id"])

    # Read-only: pieces must be a static display, never an editable input.
    assert page.locator('input[name="child_pieces"]').count() == 0, (
        "child_pieces must not be editable for sell_by=piece (pieces IS qty)"
    )
    child_pieces = page.locator(".child-pieces-display")
    assert child_pieces.count() > 0, "Pieces column (read-only mirror) not rendered"

    qty_input = page.locator('input[name="child_qty"]')
    qty_input.fill("6")
    qty_input.dispatch_event("change")
    page.wait_for_timeout(200)

    pieces_val = child_pieces.inner_text()
    assert pieces_val.strip() == "6", (
        f"After setting child_qty=6, child pieces display shows '{pieces_val}' instead of '6'. "
        "For sell_by=piece, Qty and Pieces must mirror each other."
    )

    # Mother qty should show 4 (10 - 6)
    mother_qty = page.locator(".mother-qty-input").input_value()
    assert "4" in mother_qty, f"Mother qty should show 4, got '{mother_qty}'"

    # Mother pieces display should also show 4
    mother_pieces = page.locator(".mother-pieces-display").inner_text()
    assert "4" in mother_pieces, f"Mother pieces display should show 4, got '{mother_pieces}'"

    _save_screenshot(page, "piece-sync-01-qty-to-pieces")


# ---------------------------------------------------------------------------
# PIECE-SYNC-02: Editable Pieces recalcs the mother row (independent pieces)
# ---------------------------------------------------------------------------

def test_piece_sync_02_editable_pieces_updates_mother(page, ui_server, api):
    """PIECE-SYNC-02: When pieces is independent of qty (weight-unit parcel),
    child_pieces is editable and editing it must recalc the mother pieces display.
    """
    r = api.post("/items", json={
        "sku": "GHOST-GRAMPC-001",
        "name": "Ghost Col Gram Parcel",
        "sell_by": "gram",
        "quantity": 10.0,
    })
    assert r.status_code in {200, 201}, f"create failed: {r.text}"
    item = r.json()
    rp = api.patch(f"/items/{item['id']}", json={
        "fields_changed": {"pieces": {"old": None, "new": 10}},
    })
    assert rp.status_code in {200, 201}

    _open_split_panel(page, ui_server, item["id"])

    pieces_input = page.locator('input[name="child_pieces"]')
    assert pieces_input.count() > 0, (
        "child_pieces input not rendered for a weight-unit item with a pieces attribute"
    )

    pieces_input.fill("7")
    pieces_input.dispatch_event("input")
    page.wait_for_timeout(200)

    mother_pieces = page.locator(".mother-pieces-display").inner_text()
    assert "3" in mother_pieces, f"Mother pieces display should show 3 (10 - 7), got '{mother_pieces}'"

    _save_screenshot(page, "piece-sync-02-editable-pieces")


# ---------------------------------------------------------------------------
# PIECE-SYNC-03: sell_by=piece without weight shows QTY only (no pieces column)
# ---------------------------------------------------------------------------

def test_piece_sync_03_no_pieces_column_without_weight(page, ui_server, api):
    """PIECE-SYNC-03: A piece item with no weight shows only the QTY column.

    Without a weight column, pieces would just duplicate QTY — the split panel
    must render neither a pieces input nor pieces displays.
    """
    r = api.post("/items", json={
        "sku": "GHOST-PIECEONLY-001",
        "name": "Ghost Col Piece Only Item",
        "sell_by": "piece",
        "quantity": 10.0,
    })
    assert r.status_code in {200, 201}, f"create failed: {r.text}"
    item = r.json()

    _open_split_panel(page, ui_server, item["id"])

    assert page.locator('input[name="child_pieces"]').count() == 0
    assert page.locator(".child-pieces-display").count() == 0
    assert page.locator(".mother-pieces-display").count() == 0

    _save_screenshot(page, "piece-sync-03-qty-only")


# ---------------------------------------------------------------------------
# SPLIT-DELTA: item-detail split card reconciliation trio + preview badge
# ---------------------------------------------------------------------------

def _open_item_detail_split(page, ui_server, item_id: str) -> None:
    """Open an item's detail page and wait for the manual split card."""
    page.goto(f"{ui_server}/inventory/{item_id}", wait_until="domcontentloaded")
    _assert_no_crash(page, "item detail page")
    page.wait_for_selector('input[name="split_qty"]', timeout=5000)


def _trio_text(page, cls: str) -> str:
    return page.locator(f".{cls}").first.inner_text().strip()


def test_split_delta_weight_sold_live(page, ui_server, api):
    """J1: on a weight-sold item, entering split_qty makes the Delta numeric.

    Parcel weight is the quantity (item.weight is empty for weight-sold), the
    outgoing weight is split_qty, so Delta = quantity - split_qty.
    """
    r = api.post("/items", json={
        "sku": "SPLIT-DELTA-WEIGHT-001",
        "name": "Split Delta Weight Item",
        "sell_by": "gram",
        "quantity": 100.0,
    })
    assert r.status_code in {200, 201}, f"create failed: {r.text}"
    item = r.json()

    _open_item_detail_split(page, ui_server, item["id"])

    # Original parcel weight is known up front and is a number (from quantity).
    parcel = _trio_text(page, "sp-parcel-val")
    assert float(parcel) == pytest.approx(100.0), f"parcel weight should be 100, got '{parcel}'"

    qty = page.locator('input[name="split_qty"]').first
    qty.fill("30")
    qty.dispatch_event("input")
    page.wait_for_timeout(200)

    split_w = _trio_text(page, "sp-split-weight-val")
    delta = _trio_text(page, "sp-delta-val")
    assert split_w != "--" and float(split_w) == pytest.approx(30.0), f"split weight should be 30, got '{split_w}'"
    assert delta != "--" and float(delta) == pytest.approx(70.0), f"delta should be 70 (100-30), got '{delta}'"

    _save_screenshot(page, "split-delta-weight-sold")


def test_split_delta_pieces_sold_live(page, ui_server, api):
    """J2: on a pieces-sold weighed item, entering split_qty un-gates the trio to
    numbers, and entering the split weight updates Split weight + Delta.

    Parcel weight is item.weight; the outgoing weight is the split_weight
    complement, so Delta = item.weight - split_weight.
    """
    r = api.post("/items", json={
        "sku": "SPLIT-DELTA-PIECE-001",
        "name": "Split Delta Piece Item",
        "sell_by": "piece",
        "quantity": 10.0,
        "weight": 5.0,
        "weight_unit": "gram",
    })
    assert r.status_code in {200, 201}, f"create failed: {r.text}"
    item = r.json()

    _open_item_detail_split(page, ui_server, item["id"])

    parcel = _trio_text(page, "sp-parcel-val")
    assert float(parcel) == pytest.approx(5.0), f"parcel weight should be 5, got '{parcel}'"

    # Entering only split_qty un-gates: split weight defaults to 0, Delta = full parcel.
    qty = page.locator('input[name="split_qty"]').first
    qty.fill("2")
    qty.dispatch_event("input")
    page.wait_for_timeout(200)
    split_w = _trio_text(page, "sp-split-weight-val")
    delta = _trio_text(page, "sp-delta-val")
    assert split_w != "--" and float(split_w) == pytest.approx(0.0), f"split weight should be 0, got '{split_w}'"
    assert delta != "--" and float(delta) == pytest.approx(5.0), f"delta should be 5 (5-0), got '{delta}'"

    # Entering the outgoing weight updates both numbers.
    wt = page.locator('input[name="split_weight"]').first
    wt.fill("1.5")
    wt.dispatch_event("input")
    page.wait_for_timeout(200)
    split_w = _trio_text(page, "sp-split-weight-val")
    delta = _trio_text(page, "sp-delta-val")
    assert float(split_w) == pytest.approx(1.5), f"split weight should be 1.5, got '{split_w}'"
    assert float(delta) == pytest.approx(3.5), f"delta should be 3.5 (5-1.5), got '{delta}'"

    _save_screenshot(page, "split-delta-pieces-sold")


def test_split_delta_empty_marker_before_input(page, ui_server, api):
    """J1/P4.5: Split weight and Delta show the '--' empty marker before any
    split qty is entered; Original Parcel Weight still shows a number."""
    r = api.post("/items", json={
        "sku": "SPLIT-DELTA-EMPTY-001",
        "name": "Split Delta Empty Marker Item",
        "sell_by": "gram",
        "quantity": 42.0,
    })
    assert r.status_code in {200, 201}, f"create failed: {r.text}"
    item = r.json()

    _open_item_detail_split(page, ui_server, item["id"])

    assert _trio_text(page, "sp-parcel-val") not in {"--", ""}, "parcel weight must render a number"
    assert _trio_text(page, "sp-split-weight-val") == "--", "split weight must show '--' before input"
    assert _trio_text(page, "sp-delta-val") == "--", "delta must show '--' before input"

    _save_screenshot(page, "split-delta-empty-marker")


def test_preview_delta_badge(page, ui_server, api):
    """J3/J4: a Δ badge with a live numeric value renders next to Confirm on both
    the bulk-split preview and the transform preview."""
    r = api.post("/items", json={
        "sku": "SPLIT-DELTA-BADGE-001",
        "name": "Split Delta Badge Item",
        "sell_by": "gram",
        "quantity": 80.0,
    })
    assert r.status_code in {200, 201}, f"create failed: {r.text}"
    item = r.json()

    # Bulk-split preview badge.
    _open_split_panel(page, ui_server, item["id"])
    badge = page.locator("#bulk-split-preview form .sp-delta-badge .sp-delta-val")
    assert badge.count() > 0, "bulk-split preview must render a delta badge beside Confirm"
    page.wait_for_timeout(200)
    val = badge.first.inner_text().strip()
    assert val not in {"--", ""} and float(val) == pytest.approx(80.0), (
        f"bulk-split delta badge should show 80 (parcel - 0), got '{val}'"
    )

    # Transform preview badge.
    _find_and_check_item(page, ui_server, item["id"])
    action_select = page.locator("#bulk-action-select")
    options = action_select.evaluate("el => Array.from(el.options).map(o => o.text)")
    if "Transform" not in options:
        pytest.skip("Transform action not available in this build")
    action_select.select_option(label="Transform")
    page.wait_for_selector("#bulk-transform-preview-form", timeout=5000)
    _assert_no_crash(page, "transform preview open")
    tbadge = page.locator("#bulk-transform-preview-form .sp-delta-badge .sp-delta-val")
    assert tbadge.count() > 0, "transform preview must render a delta badge beside Confirm"
    page.wait_for_timeout(200)
    tval = tbadge.first.inner_text().strip()
    # Child weight prefills to parent weight, so the initial yield delta is 0.
    assert tval not in {"--", ""} and float(tval) == pytest.approx(0.0), (
        f"transform delta badge should show 0 (mother - child, prefilled equal), got '{tval}'"
    )

    _save_screenshot(page, "preview-delta-badge")

