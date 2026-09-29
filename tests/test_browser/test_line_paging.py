# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Line paging on list detail, as the user sees it.

A finalized audit is counted, not built: paging it must move between pages without
writing the draft line page. A draft pages without saving unless it has unsaved edits,
saves exactly once when it does, and stays put when that save fails. Scanning or
marking rows on page 2 keeps the user on page 2.
"""
from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.browser

_AUDIT_LINES = 105


def _seed_audit(api, n_lines: int = _AUDIT_LINES) -> tuple[str, dict[str, str]]:
    """A draft audit over `n_lines` items (by default more than one page holds). Returns
    its id and a sku -> barcode map of its items."""
    tag = uuid.uuid4().hex[:6]
    loc_id = api.post("/companies/me/locations", json={"name": f"Pg-{tag}", "type": "warehouse"}).json()["id"]
    barcodes = {}
    for i in range(n_lines):
        sku, bc = f"PG-{tag}-{i:03d}", str(uuid.uuid4().int)[:12]
        r = api.post("/items", json={"status": "available", "sku": sku, "name": f"Thing {i}", "sell_by": "piece",
                                     "quantity": 1, "barcode": bc, "location_id": str(loc_id)})
        assert r.status_code in (200, 201), r.text
        barcodes[sku] = bc
    audit_id = api.post("/lists/audit", json={"location_id": str(loc_id)}).json()["id"]
    return audit_id, barcodes


def _finalize(page, ui_server, audit_id: str) -> None:
    assert page.request.post(f"{ui_server}/lists/{audit_id}/action/finalize").ok


def _seed_draft(api, n_lines: int = 60) -> str:
    r = api.post("/lists", json={
        "list_type": "quotation", "status": "draft",
        "line_items": [{"name": f"Line {i:03d}", "description": f"row {i}", "quantity": 1, "unit_price": 10}
                       for i in range(n_lines)],
    })
    assert r.status_code in (200, 201), r.text
    return r.json().get("entity_id") or r.json().get("id")


def _active(page) -> str:
    a = page.locator("[id^=list-line-section-] .page-btn--active").first
    a.wait_for(state="visible", timeout=8000)
    return a.inner_text().strip()


def _goto_page(page, n: str) -> None:
    page.locator("[id^=list-line-section-] .pagination").get_by_role("link", name=n, exact=True).click()
    page.wait_for_function(
        "n => {const a=document.querySelector('[id^=list-line-section-] .page-btn--active');"
        " return a && a.textContent.trim() === n;}", arg=n, timeout=8000)


def _line_writes(page) -> list[str]:
    writes: list[str] = []
    page.on("request", lambda r: writes.append(r.url) if r.method == "POST" and r.url.endswith("/lines") else None)
    return writes


def _row_skus(page) -> list[str]:
    return [s.strip() for s in page.locator("#line-body td.col-sku").all_inner_texts()]


def test_finalized_audit_pages_without_line_writes(page, ui_server, api):
    audit_id, _ = _seed_audit(api)
    _finalize(page, ui_server, audit_id)
    writes = _line_writes(page)
    page.goto(f"{ui_server}/lists/{audit_id}", wait_until="domcontentloaded")
    assert _active(page) == "1"
    first = _row_skus(page)
    _goto_page(page, "2")
    assert len(_row_skus(page)) == _AUDIT_LINES - 100
    assert "101-105 of 105" in page.locator("[id^=list-line-section-] .page-count").inner_text()
    _goto_page(page, "1")
    assert _row_skus(page) == first
    assert writes == [], f"paging a finalized audit wrote the draft line page: {writes}"


def test_draft_clean_navigation_does_not_save(page, ui_server, api):
    entity_id = _seed_draft(api)
    writes = _line_writes(page)
    page.goto(f"{ui_server}/lists/{entity_id}?limit=25", wait_until="domcontentloaded")
    assert _active(page) == "1"
    _goto_page(page, "2")
    assert writes == [], f"a clean page saved on navigation: {writes}"


def _make_dirty(page) -> None:
    """Type a new quantity into the first row, as a user would. Leaving the field (the
    next click) commits the edit, so the page is dirty when the pager is used."""
    page.locator("#line-body input[data-name=quantity]").first.fill("7")


def test_draft_dirty_navigation_saves_once_then_moves(page, ui_server, api):
    entity_id = _seed_draft(api)
    writes = _line_writes(page)
    page.goto(f"{ui_server}/lists/{entity_id}?limit=25", wait_until="domcontentloaded")
    assert _active(page) == "1"
    _make_dirty(page)
    _goto_page(page, "2")
    assert len(writes) == 1, f"a dirty page must save exactly once before leaving: {writes}"


def test_draft_failed_save_blocks_navigation(page, ui_server, api):
    entity_id = _seed_draft(api)
    page.route("**/lines", lambda route: route.fulfill(status=500, body="nope")
               if route.request.method == "POST" else route.continue_())
    page.goto(f"{ui_server}/lists/{entity_id}?limit=25", wait_until="domcontentloaded")
    assert _active(page) == "1"
    _make_dirty(page)
    page.locator("[id^=list-line-section-] .pagination").get_by_role("link", name="2", exact=True).click()
    page.wait_for_timeout(1500)
    assert _active(page) == "1", "a failed save must hold the user on the unsaved page"


def test_audit_scan_and_mark_on_page_two_stay_on_page_two(page, ui_server, api):
    """Scanning can move the scanned line within the audit, so page 2 may hold different
    lines afterwards; what must hold is that the body is still page 2's window (5 of 105
    lines, not page 1's 100) under a page-2 pager."""
    audit_id, barcodes = _seed_audit(api)
    _finalize(page, ui_server, audit_id)
    page.goto(f"{ui_server}/lists/{audit_id}", wait_until="domcontentloaded")
    _goto_page(page, "2")
    page2 = _row_skus(page)
    assert len(page2) == _AUDIT_LINES - 100

    # Scan an item that lives on page 2.
    page.locator("#scan-bar-input").fill(barcodes[page2[0]])
    page.locator("#scan-bar-input").press("Enter")
    with page.expect_response(lambda r: r.url.endswith("/scan")):
        page.locator("#scan-bar-add").click()
    page.wait_for_timeout(500)
    assert _active(page) == "2"
    assert len(_row_skus(page)) == _AUDIT_LINES - 100, "the scan refresh swapped page 1's rows under the page-2 pager"

    # Mark a page-2 row as scanned through the row selection.
    page.locator("#line-body tr").nth(1).locator(".li-select").check()
    with page.expect_response(lambda r: r.url.endswith("/set-scanned")):
        page.evaluate("liBulkSetScanned(true)")
    page.wait_for_timeout(500)
    assert _active(page) == "2"
    assert len(_row_skus(page)) == _AUDIT_LINES - 100, "Mark as scanned swapped page 1's rows under the page-2 pager"


def test_finalized_audit_delete_selected_keeps_rows_and_explains(page, ui_server, api):
    """A finalized audit's item list is locked: Delete selected must leave every row in
    place, say why, and leave nothing unsaved behind."""
    audit_id, _ = _seed_audit(api, 3)
    _finalize(page, ui_server, audit_id)
    writes = _line_writes(page)
    page.goto(f"{ui_server}/lists/{audit_id}", wait_until="domcontentloaded")
    before = _row_skus(page)
    assert len(before) == 3
    page.locator("#line-body tr").first.locator(".li-select").check()
    page.select_option("#li-bulk-select", "li-delete")
    page.locator("#li-bulk-delete-btn").click()
    toast = page.locator(".toast-container .toast--error")
    toast.wait_for(state="visible", timeout=5000)
    assert "cannot be deleted" in toast.inner_text()
    assert _row_skus(page) == before
    page.wait_for_timeout(600)
    assert page.evaluate("_celerpLinesDirty()") is False
    assert writes == []


def test_draft_scan_then_edit_saves_every_line_once(page, ui_server, api):
    """A scan adds its line on top of the page. The next autosave must store exactly the
    lines on screen, not the new line plus a repeat of the one it pushed down."""
    entity_id = _seed_draft(api, 3)
    tag = uuid.uuid4().hex[:6]
    bc = str(uuid.uuid4().int)[:12]
    r = api.post("/items", json={"status": "available", "sku": f"SC-{tag}", "name": f"Scanned {tag}",
                                 "sell_by": "piece", "quantity": 1, "barcode": bc})
    assert r.status_code in (200, 201), r.text
    page.goto(f"{ui_server}/lists/{entity_id}?limit=25", wait_until="domcontentloaded")
    assert page.locator("#line-body tr").count() == 3
    page.locator("#scan-bar-input").fill(bc)
    with page.expect_response(lambda r: r.url.endswith("/scan")):
        page.locator("#scan-bar-add").click()
    page.wait_for_function("document.querySelectorAll('#line-body tr').length === 4", timeout=8000)

    with page.expect_response(lambda r: r.request.method == "POST" and r.url.endswith("/lines")) as saved:
        qty = page.locator("#line-body tr").nth(1).locator("input[data-name=quantity]")
        qty.fill("5")
        qty.press("Tab")
    assert saved.value.ok
    stored = api.get(f"/lists/{entity_id}").json().get("line_items", [])
    descriptions = [li.get("description") for li in stored]
    assert len(stored) == 4, descriptions
    assert len(set(descriptions)) == 4, descriptions
