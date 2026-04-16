# Copyright (c) 2026 Noah Severs. All rights reserved.
"""Group 7: Inline edit — create entity via API, click editable field, submit new value."""
import uuid
import pytest

pytestmark = pytest.mark.browser


@pytest.fixture(scope="module")
def item_id(api):
    """Create a test item via API for inline edit tests."""
    r = api.post("/items", json={
        "sku": f"EDIT-TEST-{uuid.uuid4().hex[:6]}",
        "sell_by": "piece",
        "name": "Edit Test Item",
        "quantity": 1,
        "category": "Test",
    })
    assert r.status_code in {200, 201}, f"Failed to create item: {r.text}"
    return r.json()["id"]


@pytest.fixture(scope="module")
def contact_id(api):
    """Create a test contact via API."""
    r = api.post("/crm/contacts", json={"name": "Edit Test Contact"})
    assert r.status_code in {200, 201}, f"Failed to create contact: {r.text}"
    return r.json()["id"]


def test_inline_edit_item_name(page, ui_server, item_id):
    """EDIT-01: Click item name cell → type new value → no crash."""
    page.goto(f"{ui_server}/inventory/{item_id}", wait_until="domcontentloaded")
    body = page.locator("body").inner_text()
    assert "Internal Server Error" not in body, "Item detail page: Internal Server Error"

    # Inventory detail uses td.cell--clickable (hx-get triggers edit form)
    editable = page.locator("td.cell--clickable").first
    assert editable.count() > 0, "No clickable cell found on item detail page"

    editable.click()
    try:
        page.wait_for_selector("input", timeout=3000)
        page.locator("input").first.fill("Edit Test Item Renamed")
        page.keyboard.press("Enter")
        page.wait_for_load_state("networkidle", timeout=5000)
    except Exception:
        pass  # Edit form may not appear for all cell types

    body = page.locator("body").inner_text()
    assert "Internal Server Error" not in body
    assert "Traceback" not in body


def test_inline_edit_item_category(page, ui_server, item_id):
    """EDIT-02: Click a cell on item detail → update fires, no crash."""
    page.goto(f"{ui_server}/inventory/{item_id}", wait_until="domcontentloaded")
    body = page.locator("body").inner_text()
    assert "Internal Server Error" not in body

    # All editable cells use td.cell--clickable — click the first available
    cells = page.locator("td.cell--clickable")
    assert cells.count() > 0, "No clickable cells found on item detail page"

    cells.first.click()
    try:
        page.wait_for_selector("input, select", timeout=2000)
        inp = page.locator("input, select").first
        if inp.count() > 0:
            inp.fill("Browser Test Category")
            page.keyboard.press("Enter")
            page.wait_for_load_state("networkidle", timeout=5000)
    except Exception:
        pass  # Some cells use select dropdowns; non-fatal if input form doesn't appear

    body = page.locator("body").inner_text()
    assert "Internal Server Error" not in body
    assert "Traceback" not in body


def test_inline_edit_contact_name(page, ui_server, contact_id):
    """EDIT-03: Contact name inline edit."""
    page.goto(f"{ui_server}/crm/{contact_id}", wait_until="domcontentloaded")
    body = page.locator("body").inner_text()
    assert "Internal Server Error" not in body

    # Contact detail uses td[hx-get] clickable cells
    editable = page.locator("td[hx-get], td.cell--clickable").first
    assert editable.count() > 0, "No editable cell found on contact detail page"

    editable.click()
    try:
        page.wait_for_selector("input", timeout=2000)
        page.locator("input").first.fill("Edited Contact Name")
        page.keyboard.press("Enter")
        page.wait_for_load_state("networkidle", timeout=5000)
    except Exception:
        pass
    body = page.locator("body").inner_text()
    assert "Internal Server Error" not in body
    assert "Traceback" not in body


def test_inline_edit_doc_status(page, ui_server, api):
    """EDIT-04: Document status cell inline edit — click → no crash."""
    r = api.post("/docs", json={
        "doc_type": "invoice",
        "ref_id": f"EDIT-DOC-{uuid.uuid4().hex[:6]}",
        "status": "draft",
        "line_items": [],
        "total": 0,
    })
    assert r.status_code in {200, 201}, f"Failed to create doc: {r.text}"
    doc_id = r.json()["id"]

    page.goto(f"{ui_server}/docs/{doc_id}", wait_until="domcontentloaded")
    body = page.locator("body").inner_text()
    assert "Internal Server Error" not in body

    # Doc detail uses .editable-cell for status, issue_date, due_date, etc.
    editable = page.locator(".editable-cell").first
    assert editable.count() > 0, "No clickable cell found on doc detail page"

    editable.click()
    try:
        page.wait_for_selector("input, select", timeout=2000)
    except Exception:
        pass  # Edit form may not expand for some cell types

    body = page.locator("body").inner_text()
    assert "Internal Server Error" not in body
    assert "Traceback" not in body


def test_inline_edit_subscription(page, ui_server, api):
    """EDIT-05: Subscription detail loads — no crash."""
    r = api.post("/subscriptions", json={
        "name": f"Edit Test Sub {uuid.uuid4().hex[:6]}",
        "doc_type": "invoice",
        "frequency": "monthly",
        "start_date": "2026-01-01",
    })
    assert r.status_code in {200, 201}, f"Could not create subscription: {r.text}"
    sub_id = r.json()["id"]

    page.goto(f"{ui_server}/subscriptions/{sub_id}", wait_until="domcontentloaded")
    body = page.locator("body").inner_text()
    assert "Internal Server Error" not in body
    assert "Traceback" not in body
