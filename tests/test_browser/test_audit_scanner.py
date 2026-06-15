# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Inventory audit scanner UI: scan to audit a batch (highlight + count up), set a count, then
adjust stock - all from /audits/{id}. Proves the scan-first screen end to end in the browser."""
from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.browser


def _no_crash(page, ctx=""):
    body = page.locator("body").inner_text()
    assert "Internal Server Error" not in body, f"{ctx}: 500"
    assert "Traceback (most recent call last)" not in body, f"{ctx}: traceback"


def test_audit_scanner_flow(page, ui_server, api):
    tag = uuid.uuid4().hex[:6]
    ntag = str(uuid.uuid4().int)[:8]
    bc_a, bc_b = f"{ntag}1", f"{ntag}2"  # digit-only barcodes
    loc = api.post("/companies/me/locations", json={"name": f"Aud-{tag}", "type": "warehouse"})
    assert loc.status_code in {200, 201}, loc.text
    loc_id = loc.json()["id"]
    a = api.post("/items", json={"sku": f"AUD-{tag}-A", "name": "Widget A", "quantity": 10,
                                 "sell_by": "piece", "location_id": loc_id, "barcode": bc_a, "cost_total": 100})
    b = api.post("/items", json={"sku": f"AUD-{tag}-B", "name": "Widget B", "quantity": 4,
                                 "sell_by": "piece", "location_id": loc_id, "barcode": bc_b, "cost_total": 40})
    assert a.status_code in {200, 201} and b.status_code in {200, 201}
    a_id = a.json()["id"]

    audit = api.post("/audits", json={"location_id": loc_id})
    assert audit.status_code in {200, 201}, audit.text
    audit_id = audit.json()["id"]

    page.set_viewport_size({"width": 1440, "height": 1000})
    page.goto(f"{ui_server}/audits/{audit_id}", wait_until="domcontentloaded")
    page.wait_for_selector("#audit-body", timeout=8000)
    _no_crash(page, "detail")
    assert page.locator(".badge--unaudited").count() == 1
    assert "Audited 0 / 2" in page.locator("#audit-progress").inner_text()

    # Scan barcode A -> its row becomes audited (highlighted) and the progress ticks up.
    page.locator("#audit-scan-input").fill(bc_a)
    page.locator("#audit-scan-input").press("Enter")
    page.wait_for_selector(".data-row--audited", timeout=8000)
    assert page.locator(".data-row--audited").count() == 1
    assert "Audited 1 / 2" in page.locator("#audit-progress").inner_text()

    # Re-scanning A is rejected with the standard lower-right toast (no extra audit).
    page.locator("#audit-scan-input").fill(bc_a)
    page.locator("#audit-scan-input").press("Enter")
    page.wait_for_selector(".toast-container .toast--error", timeout=8000)
    assert page.locator(".data-row--audited").count() == 1

    # Set a count on A (10 -> 8) via the inline cell.
    cell = page.locator(f"input.audit-count-input").first
    cell.fill("8")
    cell.blur()
    page.wait_for_timeout(500)
    _no_crash(page, "after-count")

    from pathlib import Path
    Path("context/reviews/inventory").mkdir(parents=True, exist_ok=True)
    page.screenshot(path="context/reviews/inventory/audit-scan-counting.png", full_page=True)

    # Done auditing -> status flips to audited, Adjust stock appears.
    page.click(".audit-actions button:has-text('Done auditing')")
    page.wait_for_selector(".audit-actions button:has-text('Adjust stock')", timeout=8000)
    assert page.locator(".badge--audited").count() == 1

    # Adjust stock (confirm dialog) -> status stock_adjusted, A's row marked adjusted.
    page.on("dialog", lambda d: d.accept())
    page.click(".audit-actions button:has-text('Adjust stock')")
    page.wait_for_selector(".badge--stock-adjusted", timeout=8000)
    page.wait_for_selector(".data-row--adjusted", timeout=8000)
    _no_crash(page, "after-adjust")
    page.screenshot(path="context/reviews/inventory/audit-adjusted.png", full_page=True)

    # Stock was actually applied.
    assert api.get(f"/items/{a_id}").json()["quantity"] == 8

    # Undo restores the quantity and returns to audited.
    page.click(".audit-actions button:has-text('Undo stock adjustment')")
    page.wait_for_selector(".badge--audited", timeout=8000)
    assert api.get(f"/items/{a_id}").json()["quantity"] == 10


def test_audits_list_and_new(page, ui_server, api):
    tag = uuid.uuid4().hex[:6]
    loc = api.post("/companies/me/locations", json={"name": f"List-{tag}", "type": "warehouse"})
    loc_id = loc.json()["id"]

    # The list page loads and the New audit flow creates one and redirects to its detail.
    page.set_viewport_size({"width": 1440, "height": 1000})
    page.goto(f"{ui_server}/audits/new", wait_until="domcontentloaded")
    page.wait_for_selector("select[name='location_id']", timeout=8000)
    page.select_option("select[name='location_id']", loc_id)
    page.click("button:has-text('Start audit')")
    page.wait_for_selector("#audit-body", timeout=8000)
    _no_crash(page, "new-redirect")
    assert "/audits/" in page.url

    page.goto(f"{ui_server}/audits", wait_until="domcontentloaded")
    page.wait_for_selector(".data-table", timeout=8000)
    _no_crash(page, "list")
    assert page.locator("a:has-text('New audit')").count() == 1
