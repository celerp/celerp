# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Line actions never act on top of a failed save and never fail silently.

A line action that writes (reserve, finalize) first saves pending edits; when that
save fails the action does not run. Marking rows scanned shows why it failed. A
finalized audit's item list is locked, so its toolbar offers no Delete selected.
"""
from __future__ import annotations

import json
import uuid

import pytest

pytestmark = pytest.mark.browser

_BLOCKED = "Save blocked for this test"


def _item(api, loc_id: str) -> dict:
    sku = f"GD-{uuid.uuid4().hex[:8]}"
    r = api.post("/items", json={"status": "available", "sku": sku, "name": "Guarded", "sell_by": "piece",
                                 "quantity": 1, "barcode": str(uuid.uuid4().int)[:12], "location_id": loc_id})
    assert r.status_code in (200, 201), r.text
    return {"entity_id": r.json().get("id") or r.json().get("entity_id"), "sku": sku}


def _location(api) -> str:
    r = api.post("/companies/me/locations", json={"name": f"Gd-{uuid.uuid4().hex[:6]}", "type": "warehouse"})
    return str(r.json()["id"])


def _finalized_audit(page, ui_server, api) -> str:
    loc = _location(api)
    for _ in range(2):
        _item(api, loc)
    audit_id = api.post("/lists/audit", json={"location_id": loc}).json()["id"]
    assert page.request.post(f"{ui_server}/lists/{audit_id}/action/finalize").ok
    return audit_id


def _fail_line_saves(page) -> None:
    page.route("**/lines", lambda route: route.fulfill(
        status=422, content_type="application/json", body=json.dumps({"error": _BLOCKED}))
        if route.request.method == "POST" else route.continue_())


def _posts(page, suffix: str) -> list[str]:
    seen: list[str] = []
    page.on("request", lambda r: seen.append(r.url) if r.method == "POST" and suffix in r.url else None)
    return seen


def _error_toast(page) -> str:
    toast = page.locator(".toast-container .toast--error").last
    toast.wait_for(state="visible", timeout=5000)
    return toast.inner_text()


def test_finalized_audit_toolbar_offers_no_delete(page, ui_server, api):
    audit_id = _finalized_audit(page, ui_server, api)
    page.goto(f"{ui_server}/lists/{audit_id}", wait_until="domcontentloaded")
    page.locator("#li-bulk-select").wait_for(state="attached", timeout=8000)
    assert page.locator("#li-bulk-select option[value='li-delete']").count() == 0
    assert page.locator("#li-bulk-delete-btn").count() == 0
    assert page.locator("#li-bulk-select option[value='li-mark-scanned']").count() == 1


def test_draft_list_toolbar_still_offers_delete(page, ui_server, api):
    r = api.post("/lists", json={"list_type": "quotation", "status": "draft",
                                 "line_items": [{"name": "Row", "quantity": 1, "unit_price": 1}]})
    list_id = r.json().get("entity_id") or r.json().get("id")
    page.goto(f"{ui_server}/lists/{list_id}", wait_until="domcontentloaded")
    page.locator("#li-bulk-select").wait_for(state="attached", timeout=8000)
    assert page.locator("#li-bulk-select option[value='li-delete']").count() == 1


def test_set_scanned_refusal_is_shown(page, ui_server, api):
    audit_id = _finalized_audit(page, ui_server, api)
    page.route("**/set-scanned", lambda route: route.fulfill(
        status=409, content_type="text/plain", body="This audit is already closed."))
    page.goto(f"{ui_server}/lists/{audit_id}", wait_until="domcontentloaded")
    page.locator("#line-body tr .li-select").first.check()
    page.evaluate("liBulkSetScanned(true)")
    assert "This audit is already closed." in _error_toast(page)


def test_set_scanned_network_failure_is_shown(page, ui_server, api):
    audit_id = _finalized_audit(page, ui_server, api)
    page.route("**/set-scanned", lambda route: route.abort())
    page.goto(f"{ui_server}/lists/{audit_id}", wait_until="domcontentloaded")
    page.locator("#line-body tr .li-select").first.check()
    page.evaluate("liBulkSetScanned(true)")
    assert _error_toast(page).strip()


def test_reserve_does_not_run_after_a_failed_save(page, ui_server, api):
    item = _item(api, _location(api))
    r = api.post("/lists", json={"list_type": "quotation", "status": "draft", "line_items": [
        {**item, "name": "Guarded", "quantity": 1, "unit_price": 10}]})
    assert r.status_code in (200, 201), r.text
    list_id = r.json().get("entity_id") or r.json().get("id")
    _fail_line_saves(page)
    reserves = _posts(page, "/reserve-lines")
    page.on("dialog", lambda d: d.accept())
    page.goto(f"{ui_server}/lists/{list_id}", wait_until="domcontentloaded")
    page.locator("#line-body tr .li-select").first.check()
    page.evaluate("liBulkReserveConfirmed()")
    page.locator("#save-status", has_text=_BLOCKED).wait_for(timeout=5000)
    page.wait_for_timeout(500)
    assert reserves == []


def test_finalize_does_not_run_after_a_failed_save(page, ui_server, api):
    r = api.post("/docs", json={"doc_type": "invoice", "status": "draft", "total": 10.0,
                                "line_items": [{"name": "Widget", "quantity": 1, "unit_price": 10.0,
                                                "line_total": 10.0}]})
    assert r.status_code in (200, 201), r.text
    doc_id = r.json()["id"]
    _fail_line_saves(page)
    finalizes = _posts(page, "/action/finalize")
    page.goto(f"{ui_server}/docs/{doc_id}", wait_until="domcontentloaded")
    page.locator("button[onclick*='action/finalize']").first.click()
    page.locator("#save-status", has_text=_BLOCKED).wait_for(timeout=5000)
    page.wait_for_timeout(500)
    assert finalizes == []
    assert api.get(f"/docs/{doc_id}").json()["status"] == "draft"
