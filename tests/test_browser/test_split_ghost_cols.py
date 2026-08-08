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

from .inline_edit import set_cell

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
    """Open an item's detail page and wait for the shared split table card."""
    page.goto(f"{ui_server}/inventory/{item_id}", wait_until="domcontentloaded")
    _assert_no_crash(page, "item detail page")
    page.wait_for_selector(".action-card .split-preview-table", timeout=5000)


def _detail_delta(page) -> str:
    """The item-detail split card's live delta badge value."""
    return page.locator(".action-card .sp-delta-badge .sp-delta-val").first.inner_text().strip()


def test_item_detail_delta_balanced_zero(page, ui_server, api):
    """J1: carving a child from the mother on item-detail keeps the delta at 0.00; the
    mother auto-derives to the remainder (mother_post + child = parcel)."""
    r = api.post("/items", json={
        "sku": "SPLIT-DELTA-WEIGHT-001",
        "name": "Split Delta Weight Item",
        "sell_by": "gram",
        "quantity": 100.0,
    })
    assert r.status_code in {200, 201}, f"create failed: {r.text}"
    item = r.json()

    _open_item_detail_split(page, ui_server, item["id"])
    form = page.locator(".action-card form:has(.split-preview-table)")

    child = form.locator('input[name="child_qty"]').first
    child.fill("30")
    child.dispatch_event("change")
    page.wait_for_timeout(200)

    mother_val = form.locator(".mother-qty-input").first.input_value()
    assert float(mother_val) == pytest.approx(70.0), f"mother should auto-derive to 70, got '{mother_val}'"

    delta = _detail_delta(page)
    assert delta not in {"--", ""} and float(delta) == pytest.approx(0.0), (
        f"delta should be 0 (100 - (70+30)), got '{delta}'"
    )
    _save_screenshot(page, "item-detail-delta-balanced")


def test_item_detail_delta_nonzero_when_mother_edited(page, ui_server, api):
    """J3: editing the Mother row on item-detail breaks the balance so the delta shows the
    net gain/loss, not 0."""
    r = api.post("/items", json={
        "sku": "SPLIT-DELTA-MOTHER-001",
        "name": "Split Delta Mother Item",
        "sell_by": "gram",
        "quantity": 100.0,
    })
    assert r.status_code in {200, 201}, f"create failed: {r.text}"
    item = r.json()

    _open_item_detail_split(page, ui_server, item["id"])
    form = page.locator(".action-card form:has(.split-preview-table)")

    child = form.locator('input[name="child_qty"]').first
    child.fill("30")
    child.dispatch_event("change")
    page.wait_for_timeout(200)

    mother = form.locator(".mother-qty-input").first
    mother.fill("20")
    mother.dispatch_event("change")
    page.wait_for_timeout(200)

    delta = _detail_delta(page)
    assert delta not in {"--", ""} and float(delta) == pytest.approx(50.0), (
        f"delta should be 50 (100 - (20+30)), got '{delta}'"
    )
    _save_screenshot(page, "item-detail-delta-mother-edited")


def test_item_detail_add_second_child_updates_totals(page, ui_server, api):
    """J2: the + button appends a second child row; the QTY total footer equals the mother
    plus the sum of children."""
    r = api.post("/items", json={
        "sku": "SPLIT-ADD-CHILD-001",
        "name": "Split Add Child Item",
        "sell_by": "gram",
        "quantity": 100.0,
    })
    assert r.status_code in {200, 201}, f"create failed: {r.text}"
    item = r.json()

    _open_item_detail_split(page, ui_server, item["id"])
    form = page.locator(".action-card form:has(.split-preview-table)")

    form.locator(".split-add-child-btn").first.click()
    page.wait_for_timeout(150)

    child_inputs = form.locator('input[name="child_qty"]')
    assert child_inputs.count() == 2, f"expected 2 child rows, got {child_inputs.count()}"

    child_inputs.nth(0).fill("30")
    child_inputs.nth(0).dispatch_event("change")
    child_inputs.nth(1).fill("20")
    child_inputs.nth(1).dispatch_event("change")
    page.wait_for_timeout(200)

    mother_val = form.locator(".mother-qty-input").first.input_value()
    assert float(mother_val) == pytest.approx(50.0), f"mother should auto-derive to 50 (100-30-20), got '{mother_val}'"

    total = form.locator(".sp-total-qty").first.inner_text().strip()
    assert float(total) == pytest.approx(100.0), f"QTY total should be mother+children = 100, got '{total}'"
    _save_screenshot(page, "item-detail-add-second-child")


def test_split_delta_empty_marker_before_input(page, ui_server, api):
    """J1: the item-detail delta badge shows a live 0.00 before any child qty is entered
    (the gated '--' marker is gone; the card opens with a prefilled child qty 0)."""
    r = api.post("/items", json={
        "sku": "SPLIT-DELTA-EMPTY-001",
        "name": "Split Delta Empty Marker Item",
        "sell_by": "gram",
        "quantity": 42.0,
    })
    assert r.status_code in {200, 201}, f"create failed: {r.text}"
    item = r.json()

    _open_item_detail_split(page, ui_server, item["id"])

    delta = _detail_delta(page)
    assert delta not in {"--", ""} and float(delta) == pytest.approx(0.0), (
        f"delta should open at a live 0.00, got '{delta}'"
    )
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

    # Bulk-split preview badge: Delta is net gain/loss (parcel - (mother_post +
    # child)), not the mother's remaining weight. On open, child is 0 and the
    # mother starts at the full parcel, so the net is 0.
    _open_split_panel(page, ui_server, item["id"])
    badge = page.locator("#bulk-split-preview form .sp-delta-badge .sp-delta-val")
    assert badge.count() > 0, "bulk-split preview must render a delta badge beside Confirm"
    page.wait_for_timeout(200)
    val = badge.first.inner_text().strip()
    assert val not in {"--", ""} and float(val) == pytest.approx(0.0), (
        f"bulk-split delta badge should show 0 (parcel - (mother_post + 0)), got '{val}'"
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


def test_bulk_split_delta_zero_on_balanced_split(page, ui_server, api):
    """Carving a child from the mother keeps the bulk-split Delta at 0: the
    mother auto-derives to the remainder, so mother_post + child = parcel."""
    r = api.post("/items", json={
        "sku": "SPLIT-DELTA-BALANCED-001",
        "name": "Split Delta Balanced Item",
        "sell_by": "gram",
        "quantity": 100.0,
    })
    assert r.status_code in {200, 201}, f"create failed: {r.text}"
    item = r.json()

    _open_split_panel(page, ui_server, item["id"])
    form = page.locator("#bulk-split-preview form")
    badge = form.locator(".sp-delta-badge .sp-delta-val")

    child_qty = form.locator('input[name="child_qty"]')
    child_qty.fill("30")
    child_qty.dispatch_event("change")
    page.wait_for_timeout(200)

    mother_qty = form.locator(".mother-qty-input")
    mother_val = mother_qty.input_value()
    assert float(mother_val) == pytest.approx(70.0), (
        f"mother should auto-derive to 70 (100-30), got '{mother_val}'"
    )

    val = badge.first.inner_text().strip()
    assert val not in {"--", ""} and float(val) == pytest.approx(0.0), (
        f"delta should be 0 (100 - (70+30)), got '{val}'"
    )

    _save_screenshot(page, "bulk-split-delta-balanced")


def test_bulk_split_delta_nonzero_when_mother_modified(page, ui_server, api):
    """Overriding the mother weight after carving a child breaks the balance:
    Delta shows the net gain/loss, not 0, once mother_post + child != parcel."""
    r = api.post("/items", json={
        "sku": "SPLIT-DELTA-MOTHER-MOD-001",
        "name": "Split Delta Mother Modified Item",
        "sell_by": "gram",
        "quantity": 100.0,
    })
    assert r.status_code in {200, 201}, f"create failed: {r.text}"
    item = r.json()

    _open_split_panel(page, ui_server, item["id"])
    form = page.locator("#bulk-split-preview form")
    badge = form.locator(".sp-delta-badge .sp-delta-val")

    child_qty = form.locator('input[name="child_qty"]')
    child_qty.fill("30")
    child_qty.dispatch_event("change")
    page.wait_for_timeout(200)

    mother_qty = form.locator(".mother-qty-input")
    mother_qty.fill("20")
    mother_qty.dispatch_event("change")
    page.wait_for_timeout(200)

    mother_val = mother_qty.input_value()
    assert float(mother_val) == pytest.approx(20.0), (
        f"mother should hold the manually-entered 20, got '{mother_val}'"
    )

    val = badge.first.inner_text().strip()
    assert val not in {"--", ""} and float(val) == pytest.approx(50.0), (
        f"delta should be 50 (100 - (20+30)), got '{val}'"
    )

    _save_screenshot(page, "bulk-split-delta-mother-modified")


def test_split_esc_exits_input(page, ui_server, api):
    """GDR 2j: Esc exits every split-table field. Focusing the Mother qty input and
    pressing Escape must blur it (the shared onkeydown handler calls this.blur())."""
    r = api.post("/items", json={
        "sku": "SPLIT-ESC-001",
        "name": "Split Esc Item",
        "sell_by": "gram",
        "quantity": 100.0,
    })
    assert r.status_code in {200, 201}, f"create failed: {r.text}"
    item = r.json()

    _open_item_detail_split(page, ui_server, item["id"])
    form = page.locator(".action-card form:has(.split-preview-table)")

    mother = form.locator(".mother-qty-input").first
    mother.focus()
    assert page.evaluate(
        "() => document.activeElement && document.activeElement.classList.contains('mother-qty-input')"
    ), "mother qty input should hold focus after .focus()"

    page.keyboard.press("Escape")
    page.wait_for_timeout(100)
    assert not page.evaluate(
        "() => document.activeElement && document.activeElement.classList.contains('mother-qty-input')"
    ), "Escape must blur the split-table input (GDR 2j)"

    _save_screenshot(page, "split-esc-exits-input")


def test_split_numeric_headers_right_aligned(page, ui_server, api):
    """HTML/CSS 4a: numeric column headers (QTY/Weight/Pieces) are right-aligned over
    their figures, while the text SKU header stays centered."""
    r = api.post("/items", json={
        "sku": "SPLIT-HDR-ALIGN-001",
        "name": "Split Header Align Item",
        "sell_by": "gram",
        "quantity": 100.0,
    })
    assert r.status_code in {200, 201}, f"create failed: {r.text}"
    item = r.json()

    _open_item_detail_split(page, ui_server, item["id"])

    aligns = page.evaluate("""() => {
        const table = document.querySelector('.action-card .split-preview-table');
        const out = {num: [], text: []};
        table.querySelectorAll('thead th').forEach(th => {
            const a = window.getComputedStyle(th).textAlign;
            if (th.classList.contains('sp-th--num')) out.num.push(a);
            else if (th.textContent.trim() === 'SKU') out.text.push(a);
        });
        return out;
    }""")
    assert aligns["num"], "no numeric split-table headers found"
    assert all(a == "right" for a in aligns["num"]), (
        f"numeric headers must be right-aligned, got {aligns['num']}"
    )
    assert all(a == "center" for a in aligns["text"]), (
        f"the SKU header must be centered, got {aligns['text']}"
    )

    _save_screenshot(page, "split-numeric-headers-right-aligned")



# ---------------------------------------------------------------------------
# Add-child UX: row distribution, control placement, gutter indent, live toggle
# ---------------------------------------------------------------------------

def _splittable_gram_item(api, sku: str) -> str:
    r = api.post("/items", json={
        "sku": sku,
        "name": "Split Card Item",
        "sell_by": "gram",
        "quantity": 100.0,
    })
    assert r.status_code in {200, 201}, f"create failed: {r.text}"
    return r.json()["id"]


def _tbody_shape(page):
    """Direct-child row count, nested-table count, and per-row cell counts of the
    item-detail split table's tbody."""
    return page.evaluate("""() => {
        const tbody = document.querySelector('.action-card .split-preview-table > tbody');
        const rows = Array.from(tbody.children).filter(el => el.tagName === 'TR');
        return {
            rows: rows.length,
            nestedTables: tbody.querySelectorAll('table').length,
            cellCounts: rows.map(tr => tr.querySelectorAll(':scope > td').length),
        };
    }""")


def test_add_child_distributes_across_columns(page, ui_server, api):
    """Fix 2: clicking + appends a real <tr> whose cells distribute across the same columns
    as the mother row, with no nested <table> collapsing them into the first column."""
    item_id = _splittable_gram_item(api, "SPLIT-DISTRIBUTE-001")
    _open_item_detail_split(page, ui_server, item_id)

    before = _tbody_shape(page)
    mother_cells = before["cellCounts"][0]

    page.locator(".action-card .split-add-child-btn").first.click()
    page.wait_for_timeout(200)

    after = _tbody_shape(page)
    assert after["nestedTables"] == 0, (
        f"tbody must contain no nested <table>, found {after['nestedTables']}"
    )
    assert after["rows"] == before["rows"] + 1, (
        f"a new <tr> row must be appended (was {before['rows']}, now {after['rows']})"
    )
    new_row_cells = after["cellCounts"][-1]
    assert new_row_cells == mother_cells, (
        f"the added child row must have {mother_cells} cells like the mother, got {new_row_cells}"
    )

    _save_screenshot(page, "add-child-distributes")


def test_add_remove_controls_positioned(page, ui_server, api):
    """Fix 3 + Fix 4: the + control sits in the first child's leading label cell, and an added
    child's remove control sits in its trailing (last) cell, not the label cell."""
    item_id = _splittable_gram_item(api, "SPLIT-CONTROLS-001")
    _open_item_detail_split(page, ui_server, item_id)

    leading = page.evaluate("""() => {
        const tbody = document.querySelector('.action-card .split-preview-table > tbody');
        const rows = Array.from(tbody.children).filter(el => el.tagName === 'TR');
        const firstChild = rows[1];
        const firstCell = firstChild.querySelector(':scope > td');
        return !!firstCell.querySelector('.split-add-child-btn');
    }""")
    assert leading, "the + control must render in the first child's leading label cell"

    page.locator(".action-card .split-add-child-btn").first.click()
    page.wait_for_timeout(200)

    placement = page.evaluate("""() => {
        const tbody = document.querySelector('.action-card .split-preview-table > tbody');
        const rows = Array.from(tbody.children).filter(el => el.tagName === 'TR');
        const added = rows[rows.length - 1];
        const cells = added.querySelectorAll(':scope > td');
        const lastCell = cells[cells.length - 1];
        const firstCell = cells[0];
        return {
            inLast: !!lastCell.querySelector('.split-remove-child-btn'),
            inFirst: !!firstCell.querySelector('.split-remove-child-btn'),
        };
    }""")
    assert placement["inLast"], "the remove control must sit in the trailing (last) cell"
    assert not placement["inFirst"], "the remove control must not sit in the label cell"

    _save_screenshot(page, "add-remove-controls-positioned")


def test_split_table_indented_with_add_child(page, ui_server, api):
    """Fix 3: on the item-detail card the split table is indented so the gutter + renders
    outside its left edge (positive computed margin-left)."""
    item_id = _splittable_gram_item(api, "SPLIT-INDENT-001")
    _open_item_detail_split(page, ui_server, item_id)

    margin_left = page.evaluate("""() => {
        const t = document.querySelector('.action-card .split-preview-table');
        return parseFloat(window.getComputedStyle(t).marginLeft) || 0;
    }""")
    assert margin_left > 0, (
        f".action-card .split-preview-table must have a positive margin-left, got {margin_left}"
    )

    _save_screenshot(page, "split-table-indented")


def test_toggle_allow_splitting_shows_card_no_reload(page, ui_server, api):
    """Fix 1: toggling allow_splitting true on the item-detail page refreshes the Split card to
    show the split table in place, with no page navigation."""
    r = api.post("/items", json={
        "sku": "SPLIT-TOGGLE-001",
        "name": "Split Toggle Item",
        "sell_by": "gram",
        "quantity": 100.0,
    })
    assert r.status_code in {200, 201}, f"create failed: {r.text}"
    item = r.json()
    rp = api.patch(f"/items/{item['id']}", json={
        "fields_changed": {"allow_splitting": {"old": True, "new": False}},
    })
    assert rp.status_code in {200, 201}, f"disable splitting failed: {rp.text}"

    page.goto(f"{ui_server}/inventory/{item['id']}", wait_until="domcontentloaded")
    _assert_no_crash(page, "item detail page (splitting disabled)")
    page.wait_for_selector(".action-card--disabled", timeout=5000)
    assert page.locator(".action-card .split-preview-table").count() == 0, (
        "the disabled item must not show the split table before the toggle"
    )

    url_before = page.url
    set_cell(page, "allow_splitting", "true", scope=".detail-card", kind="select")

    page.wait_for_selector(".action-card .split-preview-table", timeout=5000)
    assert page.url == url_before, "the card must refresh in place with no navigation"

    _save_screenshot(page, "toggle-allow-splitting-no-reload")
