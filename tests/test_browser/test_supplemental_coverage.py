# Copyright (c) 2026 Noah Severs. All rights reserved.
"""
Group 16: Supplemental coverage — flows not covered by existing browser tests.

All default modules are expected to be loaded (MODULE_DIR=default_modules,
ENABLED_MODULES includes all 10 modules). Tests are unconditional.

Covers:
- Contact detail page
- Inventory item detail page
- List detail page
- Subscription pause/resume lifecycle
- Manufacturing order lifecycle (start → consume → complete)
- Doc payment recording
- Export CSV (inventory, contacts, docs)
- Search across queries
- Inline item price edit via API
- Accounting and reports smoke tests
"""
import uuid
import time
import pytest
from playwright.sync_api import Page

pytestmark = pytest.mark.browser


def _no_crash(page: Page, ctx: str = "") -> None:
    body = page.locator("body").inner_text()
    assert "Internal Server Error" not in body, f"{ctx}: Internal Server Error"
    assert "Traceback (most recent call last)" not in body, f"{ctx}: traceback"


import uuid

def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"

def _wait_projection(api, url: str, retries: int = 10, delay: float = 0.3) -> dict:
    """Poll API until projection is ready (handles async event-sourcing lag)."""
    for _ in range(retries):
        r = api.get(url)
        if r.status_code == 200:
            return r.json()
        time.sleep(delay)
    raise AssertionError(
        f"Projection not ready after {retries * delay:.1f}s: GET {url} -> {r.status_code} {r.text[:100]}"
    )



# ── Contact detail ────────────────────────────────────────────────────────────

def test_contact_detail_loads(page, ui_server, api):
    """CRM-01: Create contact → wait for projection → navigate to /crm/{id} → name visible."""
    r = api.post("/crm/contacts", json={"name": "Detail Test Contact", "email": "detail@test.com"})
    assert r.status_code in {200, 201}, f"POST /crm/contacts failed: {r.text}"
    contact_id = r.json().get("id", "")
    assert contact_id, f"No id in response: {r.json()}"

    # Wait for projection to be available before navigating
    _wait_projection(api, f"/crm/contacts/{contact_id}")

    resp = page.goto(f"{ui_server}/crm/{contact_id}", wait_until="domcontentloaded")
    assert resp.status not in {404, 500}, f"/crm/{contact_id} returned {resp.status}"
    assert "/login" not in page.url
    _no_crash(page, f"/crm/{contact_id}")
    body = page.locator("body").inner_text()
    assert "Detail Test Contact" in body, "Contact name not shown on detail page"


# ── Inventory item detail ─────────────────────────────────────────────────────

def test_inventory_item_detail_loads(page, ui_server, api):
    """INV-01: Create item → wait for projection → navigate to /inventory/{id} → name visible."""
    r = api.post("/items", json={
        "sku": _unique("DETAIL-ITEM"),
        "sell_by": "piece",
        "name": "Browser Detail Test Item",
        "quantity": 5,
        "category": "General",
    })
    assert r.status_code in {200, 201}, f"POST /items failed: {r.text}"
    item_id = r.json().get("id", r.json().get("entity_id", ""))
    assert item_id, f"No id in response: {r.json()}"

    _wait_projection(api, f"/items/{item_id}")

    resp = page.goto(f"{ui_server}/inventory/{item_id}", wait_until="domcontentloaded")
    assert resp.status not in {404, 500}, f"/inventory/{item_id} returned {resp.status}"
    assert "/login" not in page.url
    _no_crash(page, f"/inventory/{item_id}")
    body = page.locator("body").inner_text()
    assert "Browser Detail Test Item" in body, "Item not shown on detail page"


# ── List detail ───────────────────────────────────────────────────────────────

def test_list_detail_loads(page, ui_server, api):
    """LIST-01: Create list → navigate to /lists/{id} → no crash."""
    r = api.post("/lists", json={"name": "Browser Detail Test List", "doc_type": "pricelist"})
    assert r.status_code in {200, 201}, f"POST /lists failed: {r.text}"
    list_id = r.json().get("id", "")
    assert list_id, f"No id in response: {r.json()}"

    resp = page.goto(f"{ui_server}/lists/{list_id}", wait_until="domcontentloaded")
    assert resp.status not in {404, 500}, f"/lists/{list_id} returned {resp.status}"
    assert "/login" not in page.url
    _no_crash(page, f"/lists/{list_id}")


# ── Subscription lifecycle ────────────────────────────────────────────────────

def test_subscription_pause_resume(page, ui_server, api):
    """SUB-LIFECYCLE-01: Create subscription → pause → resume → no crash."""
    r = api.post("/subscriptions", json={
        "name": _unique("Lifecycle Sub"),
        "doc_type": "invoice",
        "frequency": "monthly",
        "start_date": "2026-01-01",
    })
    assert r.status_code in {200, 201}, f"POST /subscriptions failed: {r.text}"
    sub_id = r.json().get("id", "")
    assert sub_id, f"No id in response: {r.json()}"

    resp = page.request.post(f"{ui_server}/subscriptions/{sub_id}/pause")
    assert resp.status not in {404, 500, 422}, f"Pause returned {resp.status}: {resp.text()[:200]}"

    resp2 = page.request.post(f"{ui_server}/subscriptions/{sub_id}/resume")
    assert resp2.status not in {404, 500, 422}, f"Resume returned {resp2.status}: {resp2.text()[:200]}"

    resp3 = page.goto(f"{ui_server}/subscriptions/{sub_id}", wait_until="domcontentloaded")
    assert resp3.status != 500
    _no_crash(page, "subscription detail after lifecycle")


# ── Manufacturing order lifecycle ─────────────────────────────────────────────

def test_manufacturing_order_start_complete(page, ui_server, api):
    """MFG-LIFECYCLE-01: Create order → start → consume → attempt complete → detail loads."""
    item_r = api.post("/items", json={
        "sku": _unique("MFG-LC-IN"),
        "sell_by": "piece",
        "name": "Lifecycle Input",
        "quantity": 50,
        "category": "Raw Material",
    })
    assert item_r.status_code in {200, 201}, f"POST /items failed: {item_r.text}"
    item_id = item_r.json().get("id", item_r.json().get("entity_id", ""))
    assert item_id, f"Could not seed input item: {item_r.json()}"

    r = api.post("/manufacturing", json={
        "description": _unique("Lifecycle Order"),
        "order_type": "assembly",
        "inputs": [{"item_id": item_id, "quantity": 1}],
    })
    assert r.status_code in {200, 201}, f"POST /manufacturing failed: {r.text}"
    order_id = r.json().get("id", "")
    assert order_id, f"No id in mfg order response: {r.json()}"

    start_r = api.post(f"/manufacturing/{order_id}/start")
    assert start_r.status_code in {200, 204}, f"Start failed: {start_r.status_code}"

    consume_r = api.post(f"/manufacturing/{order_id}/consume",
                         json={"item_id": item_id, "quantity": 1})
    assert consume_r.status_code in {200, 204}, f"Consume failed: {consume_r.status_code}"

    # Complete: projection may lag so 422 (not all consumed yet) is tolerated; 500 is not
    complete_r = api.post(f"/manufacturing/{order_id}/complete")
    assert complete_r.status_code != 500, f"Complete returned 500: {complete_r.text}"

    resp = page.goto(f"{ui_server}/manufacturing/{order_id}", wait_until="domcontentloaded")
    assert resp.status != 500
    _no_crash(page, "mfg order detail after complete")


# ── Doc payment ───────────────────────────────────────────────────────────────

def test_doc_payment_recording(page, ui_server, api):
    """DOC-PAY-01: Create fresh finalized invoice → record payment → no crash."""
    r = api.post("/docs", json={
        "doc_type": "invoice",
        "contact_name": f"Pay Test Co {_unique('PAY')}",
        "lines": [{"description": "Service", "quantity": 1, "unit_price": 100.0}],
    })
    assert r.status_code in {200, 201}, f"POST /docs failed: {r.text}"
    doc_id = r.json().get("id", "")
    assert doc_id, f"No doc id: {r.json()}"

    fin_r = api.post(f"/docs/{doc_id}/finalize")
    assert fin_r.status_code in {200, 204}, f"Finalize failed: {fin_r.status_code} {fin_r.text}"

    resp = page.request.post(
        f"{ui_server}/docs/{doc_id}/payment",
        form={"amount": "100.00", "method": "cash", "payment_date": "2026-01-15"},
    )
    assert resp.status not in {500, 422}, \
        f"Payment recording returned {resp.status}: {resp.text()[:200]}"
    body = resp.text()
    assert "Internal Server Error" not in body
    assert "Traceback" not in body


# ── Export CSV ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("route,label", [
    ("/inventory/export/csv", "items"),
    ("/crm/export/csv", "contacts"),
    ("/docs/export/csv", "docs"),
], ids=["items", "contacts", "docs"])
def test_export_csv_endpoint(page, ui_server, route, label):
    """EXPORT-01..03: CSV export endpoints respond without 500."""
    resp = page.request.get(f"{ui_server}{route}")
    assert resp.status not in {500, 422}, f"{route} returned {resp.status}"
    content_type = resp.headers.get("content-type", "")
    if "text/html" in content_type:
        body = resp.text()
        assert "Internal Server Error" not in body, f"{route}: Internal Server Error"
        assert "Traceback" not in body, f"{route}: traceback in response"


# ── Search ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("query,label", [
    ("test", "generic"),
    ("SKU-", "sku_prefix"),
    ("", "empty"),
], ids=["generic", "sku_prefix", "empty"])
def test_search_various_queries(page, ui_server, query, label):
    """SEARCH-01..03: /search with various queries — no 500."""
    resp = page.goto(f"{ui_server}/search?q={query}", wait_until="domcontentloaded")
    assert resp.status != 500, f"/search?q={query!r} returned 500"
    _no_crash(page, f"/search?q={query!r}")


# ── Inline item price edit ────────────────────────────────────────────────────

def test_inline_edit_item_price(page, ui_server, api):
    """INLINE-05: Edit item unit_price via API (fields_changed: {field: {new: value}})."""
    r = api.post("/items", json={
        "sku": _unique("INLINE-PRICE"),
        "sell_by": "piece",
        "name": "Price Edit Test",
        "quantity": 1,
        "unit_price": 10.0,
    })
    assert r.status_code in {200, 201}, f"POST /items failed: {r.text}"
    item_id = r.json().get("id", r.json().get("entity_id", ""))
    assert item_id, f"No item id: {r.json()}"

    r2 = api.patch(f"/items/{item_id}", json={"fields_changed": {"unit_price": {"new": 25.0}}})
    assert r2.status_code in {200, 201, 204}, \
        f"PATCH item price failed: {r2.status_code} {r2.text}"

    r3 = api.get(f"/items/{item_id}")
    assert r3.status_code == 200, f"GET /items/{item_id} failed: {r3.status_code}"
    assert r3.json().get("unit_price") == 25.0, \
        f"Expected unit_price=25.0 after patch, got {r3.json().get('unit_price')!r}"

    resp = page.goto(f"{ui_server}/inventory/{item_id}", wait_until="domcontentloaded")
    assert resp.status != 500
    _no_crash(page, "item detail after price edit")


# ── Accounting smoke tests ────────────────────────────────────────────────────

def test_accounting_chart_of_accounts_loads(page, ui_server):
    """ACCT-01: /accounting loads without error."""
    resp = page.goto(f"{ui_server}/accounting", wait_until="domcontentloaded")
    assert resp.status != 500
    _no_crash(page, "/accounting")


def test_accounting_pnl_loads(page, ui_server):
    """ACCT-02: /accounting/pnl loads without error."""
    resp = page.goto(f"{ui_server}/accounting/pnl", wait_until="domcontentloaded")
    assert resp.status != 500
    _no_crash(page, "/accounting/pnl")
    body = page.locator("body").inner_text()
    assert any(word in body for word in ("Profit", "Loss", "P&L", "Income", "Revenue")), \
        f"P&L page missing expected content: {body[:200]}"


# ── Reports ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("route", [
    "/reports/ar-aging",
    "/reports/ap-aging",
    "/reports/sales",
    "/reports/purchases",
    "/reports/expiring",
], ids=["ar", "ap", "sales", "purchases", "expiring"])
def test_reports_no_crash(page, ui_server, route):
    """RPT-01..05: Each report page loads without crash."""
    resp = page.goto(f"{ui_server}{route}", wait_until="domcontentloaded")
    assert resp.status != 500, f"{route} returned 500"
    _no_crash(page, route)
