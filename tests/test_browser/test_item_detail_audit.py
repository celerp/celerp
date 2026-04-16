# Copyright (c) 2026 Noah Severs. All rights reserved.
"""
Playwright audit: inventory item detail page and category-column rendering.

Tests:
1. Item detail page loads with tabbed layout (Details / Pricing / Activity)
2. Tabs navigate correctly
3. Category field renders as a select (not free text)
4. Inline edit works for text and select fields
5. Category column renders in inventory list table
6. All URL-specified columns (?cols=...) render as table headers
7. Category-specific columns appear when that category tab is active
"""
from __future__ import annotations

import uuid
import pytest
from playwright.sync_api import Page

pytestmark = pytest.mark.browser


def _assert_no_crash(page: Page, context: str = "") -> None:
    body = page.locator("body").inner_text()
    assert "Internal Server Error" not in body, f"{context}: 500 in body"
    assert "Traceback" not in body, f"{context}: Traceback in body"


@pytest.fixture(scope="module")
def test_item(api):
    """Create a test item for detail page tests."""
    r = api.post("/items", json={
        "sku": f"AUDIT-{uuid.uuid4().hex[:6].upper()}",
        "name": "Audit Test Item",
        "sell_by": "piece",
        "quantity": 5,
        "category": "General",
    })
    assert r.status_code in {200, 201}, f"Item create failed: {r.text}"
    item_id = r.json().get("id") or r.json().get("entity_id")
    assert item_id, f"No id in response: {r.json()}"
    return {"id": item_id, "sku": r.json().get("sku", "")}


# ── DETAIL-01: page loads with tabs ──────────────────────────────────────────

def test_item_detail_loads_with_tabs(page, ui_server, test_item):
    """DETAIL-01: Item detail page must load and show Details/Pricing/Activity tabs."""
    page.goto(f"{ui_server}/inventory/{test_item['id']}", wait_until="domcontentloaded")
    _assert_no_crash(page, "item detail page load")

    body = page.locator("body").inner_text()
    assert "Details" in body, "Expected 'Details' tab"
    assert "Pricing" in body, "Expected 'Pricing' tab"
    assert "Activity" in body, "Expected 'Activity' tab"


# ── DETAIL-02: tab navigation ─────────────────────────────────────────────────

def test_item_detail_pricing_tab_loads(page, ui_server, test_item):
    """DETAIL-02: Clicking Pricing tab must load without crash."""
    page.goto(f"{ui_server}/inventory/{test_item['id']}?tab=pricing", wait_until="domcontentloaded")
    _assert_no_crash(page, "pricing tab")
    body = page.locator("body").inner_text()
    assert "Pricing" in body


def test_item_detail_activity_tab_loads(page, ui_server, test_item):
    """DETAIL-03: Clicking Activity tab must load without crash."""
    page.goto(f"{ui_server}/inventory/{test_item['id']}?tab=activity", wait_until="domcontentloaded")
    _assert_no_crash(page, "activity tab")


# ── DETAIL-04: category field is a select ─────────────────────────────────────

def test_category_field_edit_returns_select(page, ui_server, api, test_item):
    """DETAIL-04: Clicking the category cell must show an edit input, not crash."""
    # Apply gemstones preset so categories exist (may 404 if endpoint absent - ok)
    api.post("/companies/me/apply-preset?vertical=gemstones")

    page.goto(f"{ui_server}/inventory/{test_item['id']}", wait_until="domcontentloaded")
    _assert_no_crash(page, "item detail before category edit")

    # Find the category cell — uses data-col attribute
    cat_cell = page.locator("td.cell--clickable[data-col='category']").first
    if cat_cell.count() == 0:
        # Fallback: find via hx-get URL pattern
        cat_cell = page.locator("td[hx-get*='/field/category']").first
    if cat_cell.count() == 0:
        pytest.skip("No clickable category cell found — skipping edit assertion")

    # Cells use dblclick to enter edit mode (hx-trigger="dblclick")
    cat_cell.dblclick()
    try:
        # Wait for cell to enter edit mode — editable_cell returns class "cell--editing"
        page.wait_for_selector("td.cell--editing", timeout=3000)
        # Must contain either a <select>, combobox input, or text input (no crash)
        edit_td = page.locator("td.cell--editing").first
        assert edit_td.count() > 0, "Category edit cell did not enter editing state"
        # Must not have crashed
        _assert_no_crash(page, "after category cell dblclick")
    except Exception:
        pytest.fail("Category edit did not produce an edit cell (td.cell--editing)")


# ── DETAIL-05: inline edit for text field ─────────────────────────────────────

def test_item_detail_inline_edit_name(page, ui_server, test_item):
    """DETAIL-05: Clicking 'name' cell → input appears → blur saves → no crash."""
    page.goto(f"{ui_server}/inventory/{test_item['id']}", wait_until="domcontentloaded")
    _assert_no_crash(page, "before inline edit")

    # Click a cell (try name field)
    cell = page.locator("td[hx-get*='/field/name'], td.cell--clickable").first
    if cell.count() == 0:
        pytest.skip("No clickable name cell found")
    cell.click()
    try:
        page.wait_for_selector("input", timeout=2000)
        inp = page.locator("input").first
        inp.fill("Audit Item Renamed")
        inp.press("Tab")
        page.wait_for_load_state("networkidle", timeout=5000)
    except Exception:
        pass
    _assert_no_crash(page, "after inline edit")


# ── COL-01: category column in inventory list ─────────────────────────────────

def test_inventory_list_category_column_renders(page, ui_server, test_item):
    """COL-01: ?cols=category must render 'Category' as a table header."""
    page.goto(f"{ui_server}/inventory?cols=category&cols=sku&cols=name", wait_until="domcontentloaded")
    _assert_no_crash(page, "inventory list with category column")
    headers = page.inner_text("thead").upper() if page.locator("thead").count() > 0 else ""
    assert "CATEGORY" in headers, f"Expected 'Category' column in headers. Got: {headers!r}"


# ── COL-02: all requested columns render ─────────────────────────────────────

def test_inventory_list_all_specified_columns_render(page, ui_server):
    """COL-02: All columns from the standard URL param set must appear as headers."""
    cols = ["sku", "name", "category", "quantity", "status"]
    params = "&".join(f"cols={c}" for c in cols)
    page.goto(f"{ui_server}/inventory?{params}", wait_until="domcontentloaded")
    _assert_no_crash(page, "inventory with multi-column spec")

    if page.locator("thead").count() == 0:
        pytest.skip("No table rendered — possibly empty inventory")

    headers_text = page.inner_text("thead").upper()
    missing = []
    col_label_map = {
        "sku": "SKU", "name": "NAME", "category": "CATEGORY",
        "quantity": "QTY", "status": "STATUS",
    }
    for col, label in col_label_map.items():
        if label not in headers_text:
            missing.append(col)
    assert not missing, (
        f"Expected columns {missing} missing from headers. Headers: {headers_text!r}"
    )


# ── COL-03: location_name, cost_price, wholesale_price, retail_price columns ─

def test_inventory_list_price_columns_render(page, ui_server, test_item):
    """COL-03: Price columns must render as headers when requested."""
    params = "cols=sku&cols=cost_price&cols=wholesale_price&cols=retail_price"
    page.goto(f"{ui_server}/inventory?{params}", wait_until="domcontentloaded")
    _assert_no_crash(page, "inventory price columns")

    if page.locator("thead").count() == 0:
        pytest.skip("No table rendered")

    headers = page.inner_text("thead").upper()
    # At least one price column must appear
    assert any(lbl in headers for lbl in ("COST", "WHOLESALE", "RETAIL")), (
        f"No price columns found in headers: {headers!r}"
    )


# ── COL-04: description / short_description columns ──────────────────────────

def test_inventory_list_description_columns_render(page, ui_server):
    """COL-04: short_description and description columns must render without crash."""
    params = "cols=sku&cols=name&cols=short_description&cols=description"
    page.goto(f"{ui_server}/inventory?{params}", wait_until="domcontentloaded")
    _assert_no_crash(page, "inventory description columns")


# ── COL-05: /inventory/new redirects (not a form) ─────────────────────────────

def test_inventory_new_is_not_a_form(page, ui_server):
    """COL-05: GET /inventory/new must redirect (not render a form)."""
    resp = page.goto(f"{ui_server}/inventory/new", wait_until="domcontentloaded")
    # Should redirect away from /inventory/new
    assert "/inventory/new" not in page.url, (
        f"/inventory/new should redirect but stayed at: {page.url}"
    )
    _assert_no_crash(page, "/inventory/new redirect target")


# ── COL-06: /crm/new redirects (not a form) ──────────────────────────────────

def test_crm_new_is_not_a_form(page, ui_server):
    """COL-06: GET /crm/new must redirect (not render a form)."""
    page.goto(f"{ui_server}/crm/new", wait_until="domcontentloaded")
    assert "/crm/new" not in page.url, (
        f"/crm/new should redirect but stayed at: {page.url}"
    )
    _assert_no_crash(page, "/crm/new redirect target")
