# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Browser regression tests for feat/inventory-transform: ghost column bug.

After split or transform, all rows in the filtered result must have the same
visible column set. Before the fix, the mother row showed ghost columns
(hidden cols appeared visible) because applyVis() used a stale row reference
during the HTMX partial swap.

Covers:
  GHOST-COL-01: After split, mother and child rows have identical visible columns
  GHOST-COL-02: After transform, result rows have identical visible columns
"""
from __future__ import annotations

import pathlib
import pytest

pytestmark = pytest.mark.browser

_SCREENSHOT_DIR = pathlib.Path("/mnt/storage/agent_storage/celerp/screenshots/split-regression")


def _save_screenshot(page, name: str) -> None:
    _SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(_SCREENSHOT_DIR / f"{name}.png"), full_page=True)


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
    return r.json()


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
    page.goto(f"{ui_server}/inventory", wait_until="networkidle")
    _assert_no_crash(page, "inventory list")
    checkbox = page.locator(f"input.row-select[data-entity-id='{item_id}']")
    if checkbox.count() == 0:
        page.goto(f"{ui_server}/inventory?status=all", wait_until="networkidle")
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


# ---------------------------------------------------------------------------
# GHOST-COL-01: After split, all rows have identical visible column sets
# ---------------------------------------------------------------------------

def test_ghost_col_01_no_ghost_cols_after_split(page, ui_server, api, splittable_piece_item):
    """GHOST-COL-01: After split, mother and child rows must show the same columns.

    Regression: applyVis() used a stale closed-over `rows` array during the HTMX
    partial swap, leaving the mother row with all columns visible (ghost columns)
    while the child row was correctly hidden.
    """
    _find_and_check_item(page, ui_server, splittable_piece_item["id"])

    # Select Split
    page.locator("#bulk-action-select").select_option(label="Split")
    page.wait_for_selector("#bulk-split-preview form", timeout=5000)
    _assert_no_crash(page, "split panel open")

    # Enter a valid child qty
    qty_input = page.locator('input[name="child_qty"]')
    qty_input.fill("3")
    qty_input.dispatch_event("change")
    page.wait_for_timeout(300)

    # Submit
    page.locator("#bulk-split-preview form button[type='submit']").click()

    # Wait for HTMX swap to settle
    page.wait_for_selector("#inventory-content", timeout=8000)
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(600)  # let htmx:afterSettle + applyVis fire

    _assert_no_crash(page, "after split")
    _save_screenshot(page, "ghost-col-01-after-split")

    visible_per_row = _get_visible_col_keys_per_row(page)

    assert len(visible_per_row) >= 2, (
        f"Expected ≥2 rows after split, got {len(visible_per_row)}"
    )

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

    # Fill qty
    for name in ("output_qty", "child_qty"):
        inp = page.locator(f'input[name="{name}"]')
        if inp.count() > 0:
            inp.fill("10")
            inp.dispatch_event("change")
            break
    page.wait_for_timeout(300)

    page.locator("#bulk-transform-preview-form button[type='submit']").click()

    page.wait_for_selector("#inventory-content", timeout=8000)
    page.wait_for_load_state("networkidle")
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
