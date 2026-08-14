# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Browser tests for the reservation UI corrections on draft documents.

Covers:
  - RESMOD-01: A row added via the scan bar populates its Status cell with the
    rest of the row (badge label + class straight from the status map), no reload.
  - RESMOD-02: Saving a draft holding a foreign-reserved line opens the
    resolution dialog (linked to the reserving document); ESC keeps the rows;
    "Remove from draft" deletes them and the clean re-save persists.
"""
from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.browser


def _create_item(api, sku, qty=1):
    r = api.post("/items", json={"sku": sku, "name": sku, "quantity": qty, "sell_by": "piece"})
    assert r.status_code in {200, 201}, f"create item failed: {r.text}"
    return r.json()["id"]


def _create_draft_invoice(api, ref, line_items, total):
    r = api.post("/docs", json={
        "doc_type": "invoice",
        "ref_id": ref,
        "status": "draft",
        "line_items": line_items,
        "subtotal": total,
        "total": total,
    })
    assert r.status_code in {200, 201}, f"create draft failed: {r.text}"
    return r.json()["id"]


def _reserve_on_new_invoice(api, sku, eid):
    """Finalize an invoice holding the line and reserve it there.
    Returns (doc_id, doc_number) of the owning invoice - doc_number is the
    finalize-assigned display number the status badge and dialog must show."""
    ref = f"RESMOD-OWN-{uuid.uuid4().hex[:6]}"
    doc_id = _create_draft_invoice(api, ref, [
        {"sku": sku, "name": sku, "quantity": 1, "unit_price": 100.0,
         "line_total": 100.0, "entity_id": eid},
    ], total=100.0)
    r = api.post(f"/docs/{doc_id}/finalize")
    assert r.status_code in {200, 201}, f"finalize failed: {r.text}"
    r = api.post(f"/docs/{doc_id}/reserve-lines",
                 json={"line_entity_ids": [eid], "new_status": "reserved"})
    assert r.status_code == 200, f"reserve-lines failed: {r.text}"
    doc = api.get(f"/docs/{doc_id}").json()
    number = doc.get("doc_number") or doc.get("ref_id") or ref
    return doc_id, number


def _scan_add(page, sku):
    """Add an item to the open draft via the scan bar (client-side append)."""
    scan = page.locator("#scan-bar-input")
    scan.fill(sku)
    scan.press("Enter")


# celerpFillRow sets the hidden entity_id input's value PROPERTY (not the HTML
# attribute), so CSS [value=...] selectors never match a scanned row - the row
# lookups below read the live property in-page instead.

def _row_count(page, eid):
    return page.evaluate(
        """(eid) => Array.from(document.querySelectorAll('#line-body tr')).filter(
            r => r.querySelector('[data-name="entity_id"]')?.value === eid).length""",
        eid)


def _wait_status_cell_html(page, eid, timeout=5000):
    """Wait until this entity's row has a populated (non '-') status cell;
    return the cell's innerHTML."""
    handle = page.wait_for_function(
        """(eid) => {
            const row = Array.from(document.querySelectorAll('#line-body tr')).find(
                r => r.querySelector('[data-name="entity_id"]')?.value === eid);
            const cell = row && row.querySelector('.col-item-status');
            return (cell && cell.querySelector('.badge')) ? cell.innerHTML : null;
        }""",
        arg=eid, timeout=timeout)
    return handle.json_value()


def test_added_row_status_populates(page, ui_server, api):
    """RESMOD-01: scanning a reserved item onto a draft fills the Status cell
    immediately - exact badge label ('Reserved') and class (badge--reserved)
    from the status map, with the reserving doc number linked."""
    sku = f"RESMOD-FILL-{uuid.uuid4().hex[:6]}"
    eid = _create_item(api, sku)
    owner_doc, owner_number = _reserve_on_new_invoice(api, sku, eid)

    draft_id = _create_draft_invoice(api, f"RESMOD-DRAFT-{uuid.uuid4().hex[:6]}", [], total=0)
    page.goto(f"{ui_server}/docs/{draft_id}", wait_until="domcontentloaded")
    _scan_add(page, sku)

    cell_html = _wait_status_cell_html(page, eid)
    assert "badge--reserved" in cell_html, f"Expected badge--reserved, got: {cell_html}"
    assert ">Reserved</a>" in cell_html, f"Expected 'Reserved' label anchor, got: {cell_html}"
    assert f'href="/docs/{owner_doc}"' in cell_html, \
        f"Status cell must link the reserving document, got: {cell_html}"
    assert f">{owner_number}</a>" in cell_html, \
        f"Doc link must show the owning doc number {owner_number}, got: {cell_html}"
    assert 'class="badge__doc-link"' in cell_html


def test_conflict_modal_removes_line(page, ui_server, api):
    """RESMOD-02: the foreign-reserved save rejection opens the resolution
    dialog; ESC keeps the rows; 'Remove from draft' deletes the conflicted
    line and the re-save persists (verified via the API, not just the DOM)."""
    sku_res = f"RESMOD-CONF-{uuid.uuid4().hex[:6]}"
    eid_res = _create_item(api, sku_res)
    owner_doc, owner_number = _reserve_on_new_invoice(api, sku_res, eid_res)

    sku_ok = f"RESMOD-PLAIN-{uuid.uuid4().hex[:6]}"
    eid_ok = _create_item(api, sku_ok)
    draft_id = _create_draft_invoice(api, f"RESMOD-DRAFT-{uuid.uuid4().hex[:6]}", [
        {"sku": sku_ok, "name": sku_ok, "quantity": 1, "unit_price": 50.0,
         "line_total": 50.0, "entity_id": eid_ok},
    ], total=50.0)

    page.goto(f"{ui_server}/docs/{draft_id}", wait_until="domcontentloaded")
    _scan_add(page, sku_res)

    # The scan-triggered autosave hits the foreign-reserved 422: dialog opens.
    dlg = page.locator("#reserved-conflict-modal")
    dlg.wait_for(state="visible", timeout=5000)
    line = dlg.locator(".reserved-conflict-line")
    assert sku_res in (line.first.inner_text() or "")
    doc_link = dlg.locator(f'a[href="/docs/{owner_doc}"]')
    assert doc_link.count() == 1, "Dialog must link the reserving document"
    assert (doc_link.inner_text() or "").strip() == owner_number

    # ESC closes the dialog and KEEPS the row (user can release stock first).
    page.keyboard.press("Escape")
    dlg.wait_for(state="detached", timeout=5000)
    assert _row_count(page, eid_res) == 1, "Closing the dialog must keep the conflicted row"

    # Re-trigger the save: the dialog comes back; confirm the removal.
    page.evaluate("_celerpPersist()")
    dlg.wait_for(state="visible", timeout=5000)
    dlg.locator("button:has-text('Remove from draft')").click()
    dlg.wait_for(state="detached", timeout=5000)
    assert _row_count(page, eid_res) == 0, "'Remove from draft' must delete the conflicted row"

    # Persistence: the clean re-save reaches the API - the draft holds only the
    # plain line, and a fresh page load shows no trace of the reserved SKU.
    skus = None
    for _ in range(20):
        doc = api.get(f"/docs/{draft_id}").json()
        skus = [li.get("sku") for li in doc.get("line_items", [])]
        if skus == [sku_ok]:
            break
        page.wait_for_timeout(250)
    assert skus == [sku_ok], f"Draft must persist only the plain line after removal, got {skus}"

    # Draft lines render as input fields, so assert on the row inputs (input
    # values are not part of the body text), not on inner_text.
    page.goto(f"{ui_server}/docs/{draft_id}", wait_until="domcontentloaded")
    assert _row_count(page, eid_res) == 0, "Reloaded draft must not show the removed reserved line"
    assert _row_count(page, eid_ok) == 1, "Reloaded draft must keep the plain line"
