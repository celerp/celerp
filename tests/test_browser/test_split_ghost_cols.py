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

