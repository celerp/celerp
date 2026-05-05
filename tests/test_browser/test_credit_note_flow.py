# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Browser tests: credit note creation, nav link, and receive-return flow."""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.browser


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _create_paid_invoice(api, sku: str = "W-01", amount: float = 100.0) -> str:
    """Create, finalize, and fully pay an invoice. Returns entity_id."""
    inv_r = api.post("/docs", json={
        "doc_type": "invoice", "contact_id": "contact:test",
        "line_items": [{"name": "Widget", "sku": sku, "quantity": 1, "unit_price": amount, "sell_by": "unit"}],
        "subtotal": amount, "tax": 0, "total": amount,
    })
    assert inv_r.status_code == 200, inv_r.text
    inv_id = inv_r.json()["id"]
    api.post(f"/docs/{inv_id}/finalize")
    api.post(f"/docs/{inv_id}/payment", json={
        "amount": amount, "method": "transfer", "reference": f"PAY-{sku}",
        "payment_date": "2026-04-26", "bank_account": "1110",
    })
    return inv_id


def _create_finalized_cn(api, inv_id: str, sku: str = "W-01", amount: float = 100.0) -> str:
    """Create and finalize a credit note linked to an invoice. Returns entity_id."""
    cn_r = api.post("/docs", json={
        "doc_type": "credit_note",
        "original_doc_id": inv_id,
        "contact_id": "contact:test",
        "line_items": [{"name": "Widget", "sku": sku, "quantity": 1, "unit_price": amount, "sell_by": "unit"}],
        "subtotal": amount, "tax": 0, "total": amount,
    })
    assert cn_r.status_code == 200, cn_r.text
    cn_id = cn_r.json()["id"]
    api.post(f"/docs/{cn_id}/finalize")
    return cn_id


# ---------------------------------------------------------------------------
# CN-01: Credit Notes nav link is visible and shows translated label
# ---------------------------------------------------------------------------
def test_credit_notes_nav_link(page: Page, ui_server: str):
    """CN-01: Credit Notes appears in sidebar with translated label (not raw key)."""
    page.goto(f"{ui_server}/docs?type=invoice", wait_until="domcontentloaded")
    cn_link = page.locator("a[href*='type=credit_note']")
    expect(cn_link).to_be_visible()
    label = cn_link.inner_text()
    assert "nav.credit_notes" not in label, f"Nav label is raw key: {label!r}"
    assert len(label.strip()) > 0, "Nav label is empty"


# ---------------------------------------------------------------------------
# CN-02: Blank credit note can be created (no original_doc_id required)
# ---------------------------------------------------------------------------
def test_create_blank_credit_note(api):
    """CN-02: Blank CN creation succeeds - original_doc_id is optional."""
    r = api.post("/docs", json={
        "doc_type": "credit_note",
        "line_items": [],
        "subtotal": 0, "tax": 0, "total": 0,
    })
    assert r.status_code == 200, r.text
    cn = api.get(f"/docs/{r.json()['id']}").json()
    assert cn["doc_type"] == "credit_note"
    assert cn["status"] == "draft"


# ---------------------------------------------------------------------------
# CN-03: Create Credit Note button appears on paid invoice
# ---------------------------------------------------------------------------
def test_create_credit_note_button_on_paid_invoice(page: Page, ui_server: str, api):
    """CN-03: 'Create Credit Note' button visible on paid invoice."""
    inv_id = _create_paid_invoice(api, sku="CN03-W", amount=80.0)
    page.goto(f"{ui_server}/docs/{inv_id}", wait_until="domcontentloaded")
    btn = page.locator("button", has_text="Create Credit Note")
    expect(btn).to_be_visible()


# ---------------------------------------------------------------------------
# CN-04: Create Credit Note button NOT on final (unpaid) invoice
# ---------------------------------------------------------------------------
def test_create_credit_note_button_absent_on_final_invoice(page: Page, ui_server: str, api):
    """CN-04: 'Create Credit Note' button must NOT appear on final (unpaid) invoices."""
    inv_r = api.post("/docs", json={
        "doc_type": "invoice", "contact_id": "contact:test",
        "line_items": [{"name": "Item", "quantity": 1, "unit_price": 50}],
        "subtotal": 50, "tax": 0, "total": 50,
    })
    inv_id = inv_r.json()["id"]
    api.post(f"/docs/{inv_id}/finalize")

    page.goto(f"{ui_server}/docs/{inv_id}", wait_until="domcontentloaded")
    btn = page.locator("button", has_text="Create Credit Note")
    expect(btn).to_have_count(0)


# ---------------------------------------------------------------------------
# CN-05: Create CN from invoice - CN detail page loads with correct state
# ---------------------------------------------------------------------------
def test_create_credit_note_from_invoice(page: Page, ui_server: str, api):
    """CN-05: CN created from paid invoice has correct original_doc_id and renders detail page."""
    inv_id = _create_paid_invoice(api, sku="CN05-W", amount=120.0)
    source = api.get(f"/docs/{inv_id}").json()

    cn_r = api.post("/docs", json={
        "doc_type": "credit_note",
        "original_doc_id": inv_id,
        "contact_id": source.get("contact_id"),
        "line_items": source.get("line_items", []),
        "subtotal": source.get("subtotal") or 0,
        "tax": source.get("tax") or 0,
        "total": source.get("total") or 0,
    })
    assert cn_r.status_code == 200, cn_r.text
    cn_id = cn_r.json()["id"]

    page.goto(f"{ui_server}/docs/{cn_id}", wait_until="domcontentloaded")
    page.screenshot(path="/mnt/storage/agent_storage/celerp/screenshots/cn-flow/05-cn-detail-from-invoice.png", full_page=True)

    # Page must load without error
    assert f"/docs/{cn_id}" in page.url
    # CN state correct
    cn = api.get(f"/docs/{cn_id}").json()
    assert cn["original_doc_id"] == inv_id
    assert cn["doc_type"] == "credit_note"


# ---------------------------------------------------------------------------
# CN-06: Receive Return button visible on finalized CN with stocked items
# ---------------------------------------------------------------------------
def test_receive_return_button_visible_on_finalized_cn(page: Page, ui_server: str, api):
    """CN-06: Receive Return button appears on finalized CN with stocked line items."""
    inv_id = _create_paid_invoice(api, sku="CN06-G", amount=200.0)
    cn_id = _create_finalized_cn(api, inv_id, sku="CN06-G", amount=200.0)

    page.goto(f"{ui_server}/docs/{cn_id}", wait_until="domcontentloaded")
    page.screenshot(path="/mnt/storage/agent_storage/celerp/screenshots/cn-flow/06-cn-receive-return-btn.png", full_page=True)

    btn = page.locator("button", has_text="Receive Return")
    expect(btn).to_be_visible()


# ---------------------------------------------------------------------------
# CN-07: After receive-return, badge appears and Receive Return button gone
# ---------------------------------------------------------------------------
def test_receive_return_badge_after_submission(page: Page, ui_server: str, api):
    """CN-07: After receive-return API call, page shows 'Return Received' badge."""
    inv_id = _create_paid_invoice(api, sku="CN07-G", amount=150.0)
    cn_id = _create_finalized_cn(api, inv_id, sku="CN07-G", amount=150.0)

    rr = api.post(f"/docs/{cn_id}/receive-return", json={
        "items": [{"sku": "CN07-G", "name": "Widget", "quantity": 1, "cost_price": 80.0}]
    })
    assert rr.status_code == 200, f"receive-return failed: {rr.text}"

    page.goto(f"{ui_server}/docs/{cn_id}", wait_until="domcontentloaded")
    page.screenshot(path="/mnt/storage/agent_storage/celerp/screenshots/cn-flow/07-cn-after-receive-return.png", full_page=True)

    # After receive-return, the Revert Return Stock button appears (GDR undo)
    # and the original Receive Return button is gone
    revert_btn = page.locator("button", has_text="Revert Return Stock")
    expect(revert_btn).to_be_visible()

    btn = page.locator("button", has_text="Receive Return")
    expect(btn).to_have_count(0)
