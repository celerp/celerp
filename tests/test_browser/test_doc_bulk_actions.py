# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Browser tests for doc-level bulk actions: bulk delete drafts, bulk payment.

Covers:
  - BULK-DOC-01: Draft docs list shows checkboxes; selecting rows shows bulk toolbar
  - BULK-DOC-02: Select action from dropdown → confirm button appears
  - BULK-DOC-03: Delete Selected → confirm dialog → docs removed from list
  - BULK-DOC-04: Bulk toolbar hidden on non-draft invoice list (no drafts)
  - BULK-DOC-05: Auto-redirect to drafts view when no finals exist
"""
from __future__ import annotations

import pathlib
import time

import pytest

pytestmark = pytest.mark.browser


def _save(page, name: str) -> None:
    # Debug screenshots disabled — the hardcoded /mnt/storage path is not portable.
    # _SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    # page.screenshot(path=str(_SCREENSHOT_DIR / f"{name}.png"), full_page=True)
    pass


def _no_crash(page, ctx: str = "") -> None:
    body = page.locator("body").inner_text()
    assert "Internal Server Error" not in body, f"{ctx}: server error"
    assert "Traceback" not in body, f"{ctx}: traceback"


def _create_draft(api, doc_type: str = "invoice") -> str:
    r = api.post("/docs", json={"doc_type": doc_type, "status": "draft", "line_items": [], "subtotal": 0, "tax": 0, "total": 0})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def test_draft_list_has_checkboxes(page, ui_server, api):
    """BULK-DOC-01: Draft invoice list renders checkboxes on each row."""
    _create_draft(api, "invoice")
    page.goto(f"{ui_server}/docs?type=invoice&view=drafts", wait_until="domcontentloaded")
    _no_crash(page, "BULK-DOC-01")
    _save(page, "01-draft-list")

    # Row checkboxes and select-all must be present
    assert page.locator("input.doc-row-select").count() > 0, "No doc-row-select checkboxes"
    assert page.locator("#doc-select-all").count() > 0, "No #doc-select-all checkbox"
    # Bulk toolbar exists but is hidden initially
    assert page.locator("#doc-bulk-bar").count() > 0, "No #doc-bulk-bar element"


def test_bulk_toolbar_shows_on_checkbox_select(page, ui_server, api):
    """BULK-DOC-02: Selecting a checkbox shows bulk toolbar with action dropdown."""
    _create_draft(api, "invoice")
    page.goto(f"{ui_server}/docs?type=invoice&view=drafts", wait_until="domcontentloaded")

    # Click first row checkbox
    first_cb = page.locator("input.doc-row-select").first
    first_cb.click()

    # Toolbar must become visible
    page.wait_for_selector("#doc-bulk-bar", state="visible", timeout=3000)
    _save(page, "02-toolbar-visible")
    _no_crash(page, "BULK-DOC-02")

    # Dropdown must exist with a delete option
    select = page.locator("#doc-bulk-select")
    assert select.count() > 0, "No #doc-bulk-select dropdown"
    delete_opt = select.locator("option[value='doc-delete']")
    assert delete_opt.count() > 0, "No doc-delete option in dropdown"


def test_bulk_delete_confirm_button_appears(page, ui_server, api):
    """BULK-DOC-02b: Select delete action → Delete Selected confirm button appears."""
    _create_draft(api, "invoice")
    page.goto(f"{ui_server}/docs?type=invoice&view=drafts", wait_until="domcontentloaded")

    page.locator("input.doc-row-select").first.click()
    page.wait_for_selector("#doc-bulk-bar", state="visible", timeout=3000)

    # Delete button hidden before action selected
    del_btn = page.locator("#doc-bulk-delete-btn")
    assert del_btn.is_hidden(), "Delete button should be hidden before action selected"

    # Select delete action
    page.locator("#doc-bulk-select").select_option("doc-delete")
    page.wait_for_selector("#doc-bulk-delete-btn:not([style*='none'])", timeout=2000)
    _save(page, "03-delete-button-visible")
    _no_crash(page, "BULK-DOC-02b")
    assert del_btn.is_visible(), "Delete button should appear after selecting delete action"


def test_bulk_delete_removes_draft(page, ui_server, api):
    """BULK-DOC-03: Full flow - select draft → choose delete → confirm → draft gone."""
    # Create a draft specifically for deletion
    doc_id = _create_draft(api, "invoice")

    page.goto(f"{ui_server}/docs?type=invoice&view=drafts", wait_until="domcontentloaded")
    _save(page, "04-before-delete")

    # Find the checkbox for our specific draft
    row_cb = page.locator(f"input.doc-row-select[value='{doc_id}']")
    assert row_cb.count() > 0, f"No checkbox for doc {doc_id}"
    row_cb.click()

    page.wait_for_selector("#doc-bulk-bar", state="visible", timeout=3000)
    page.locator("#doc-bulk-select").select_option("doc-delete")
    page.wait_for_selector("#doc-bulk-delete-btn:not([style*='none'])", timeout=2000)

    # Accept the hx-confirm dialog; the delete runs via htmx and returns
    # HX-Refresh, so the page reloads only once the docs are actually gone.
    page.once("dialog", lambda d: d.accept())
    with page.expect_navigation(wait_until="load", timeout=8000):
        page.locator("#doc-bulk-delete-btn").click()
    _no_crash(page, "BULK-DOC-03")

    # Verify the doc is gone from API
    r = api.get(f"/docs/{doc_id}")
    assert r.status_code == 404, f"Doc should be deleted, got {r.status_code}"


def test_select_all_then_delete(page, ui_server, api):
    """BULK-DOC-03b: Select all → delete all → list reflects removal."""
    # Seed two drafts
    id1 = _create_draft(api, "invoice")
    id2 = _create_draft(api, "invoice")

    page.goto(f"{ui_server}/docs?type=invoice&view=drafts", wait_until="domcontentloaded")
    initial_count = page.locator("input.doc-row-select").count()
    assert initial_count >= 2, f"Expected at least 2 drafts, got {initial_count}"

    # Select all
    page.locator("#doc-select-all").click()
    page.wait_for_selector("#doc-bulk-bar", state="visible", timeout=3000)

    count_text = page.locator("#doc-bulk-count").inner_text()
    assert str(initial_count) in count_text, f"Count text '{count_text}' should include {initial_count}"
    _save(page, "06-select-all")

    page.locator("#doc-bulk-select").select_option("doc-delete")
    page.wait_for_selector("#doc-bulk-delete-btn:not([style*='none'])", timeout=2000)

    # htmx delete + HX-Refresh: the page reloads only after the docs are gone.
    page.once("dialog", lambda d: d.accept())
    with page.expect_navigation(wait_until="load", timeout=8000):
        page.locator("#doc-bulk-delete-btn").click()
    _no_crash(page, "BULK-DOC-03b")

    # Both docs deleted
    assert api.get(f"/docs/{id1}").status_code == 404
    assert api.get(f"/docs/{id2}").status_code == 404


def _issued_invoice(api, contact: str) -> str:
    r = api.post("/docs", json={"doc_type": "invoice", "contact_name": contact,
                                "line_items": [{"name": "W", "quantity": 1, "unit_price": 10, "line_total": 10}],
                                "subtotal": 10, "total": 10})
    assert r.status_code == 200, r.text
    doc_id = r.json()["id"]
    r = api.post(f"/docs/{doc_id}/finalize")
    assert r.status_code == 200, r.text
    return r.json().get("new_entity_id") or doc_id


def test_bulk_ship_mixed_customers_confirms_first(page, ui_server, api):
    """BULK-DOC-06: Consolidating invoices for DIFFERENT customers pops a confirm
    (the shipment will have no customer details); Cancel aborts, OK creates the
    shipping document with a blank consignee."""
    id_a = _issued_invoice(api, "Alpha Ltd")
    id_b = _issued_invoice(api, "Beta GmbH")

    page.goto(f"{ui_server}/docs?type=invoice", wait_until="domcontentloaded")
    for did in (id_a, id_b):
        cb = page.locator(f"input.doc-row-select[value='{did}']")
        assert cb.count() > 0, f"No checkbox for {did}"
        cb.click()
    page.wait_for_selector("#doc-bulk-bar", state="visible", timeout=3000)
    page.locator("#doc-bulk-select").select_option("doc-ship")
    page.wait_for_selector("#doc-bulk-ship-btn:not([style*='none'])", timeout=2000)

    # Cancel first: the dialog explains the blank consignee; dismissing aborts.
    seen = {}
    page.once("dialog", lambda d: (seen.update(message=d.message), d.dismiss()))
    page.locator("#doc-bulk-ship-btn").click()
    page.wait_for_timeout(500)
    assert "more than one customer" in seen.get("message", ""), f"dialog text: {seen}"
    assert "/docs" in page.url, "Cancel must stay on the invoice list"

    # Accept: one shipping document, consignee left blank for manual entry.
    page.once("dialog", lambda d: d.accept())
    with page.expect_navigation(wait_until="load", timeout=8000):
        page.locator("#doc-bulk-ship-btn").click()
    _no_crash(page, "BULK-DOC-06")
    assert "/lists/" in page.url, f"Expected the new shipping document, got {page.url}"
    ship_id = page.url.split("/lists/")[1].split("?")[0]
    state = api.get(f"/lists/{ship_id}").json()
    assert state["list_type"] == "shipping_doc"
    assert not state.get("contact_name") and not state.get("customer_name")
    # ids arrive in table (DOM) order, not click order - compare as a set
    assert sorted(state["source_docs"]) == sorted([id_a, id_b])


def test_bulk_ship_single_customer_needs_no_confirm(page, ui_server, api):
    """BULK-DOC-07: One customer's invoices consolidate without any dialog."""
    id_a = _issued_invoice(api, "Gamma Co")
    id_b = _issued_invoice(api, "Gamma Co")
    page.goto(f"{ui_server}/docs?type=invoice", wait_until="domcontentloaded")
    for did in (id_a, id_b):
        page.locator(f"input.doc-row-select[value='{did}']").click()
    page.wait_for_selector("#doc-bulk-bar", state="visible", timeout=3000)
    page.locator("#doc-bulk-select").select_option("doc-ship")
    page.wait_for_selector("#doc-bulk-ship-btn:not([style*='none'])", timeout=2000)
    dialogs = []
    page.on("dialog", lambda d: (dialogs.append(d.message), d.accept()))
    with page.expect_navigation(wait_until="load", timeout=8000):
        page.locator("#doc-bulk-ship-btn").click()
    _no_crash(page, "BULK-DOC-07")
    assert not dialogs, f"No dialog expected for a single customer, got {dialogs}"
    assert "/lists/" in page.url
    ship_id = page.url.split("/lists/")[1].split("?")[0]
    state = api.get(f"/lists/{ship_id}").json()
    assert state.get("contact_name") == "Gamma Co" or state.get("customer_name") == "Gamma Co"


def test_auto_redirect_drafts_when_no_finals(page, ui_server, fresh_company):
    """BULK-DOC-05: Visiting /docs?type=invoice with no finals but with drafts auto-redirects to drafts view.

    Uses an isolated, empty company (fresh_company) — the shared session company
    accumulates finalized invoices from other tests, which would suppress the
    "no finals" redirect.
    """
    # Ensure at least one draft exists (in the fresh company)
    _create_draft(fresh_company, "invoice")

    # Navigate to invoice list (no type=draft, no view=drafts)
    page.goto(f"{ui_server}/docs?type=invoice", wait_until="domcontentloaded")
    _save(page, "08-auto-redirect-drafts")
    _no_crash(page, "BULK-DOC-05")

    # Should have been redirected to drafts view
    assert "view=drafts" in page.url or "status=draft" in page.url, \
        f"Expected redirect to drafts view, got: {page.url}"
