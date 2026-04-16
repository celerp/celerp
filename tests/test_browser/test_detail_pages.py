# Copyright (c) 2026 Noah Severs. All rights reserved.
"""
Group 12: Detail page smoke tests.

Strategy: create entity via API → navigate to its detail URL → assert no 500/traceback.
Uses `api` (pre-authed httpx) and `page` (Playwright) fixtures from conftest.py.
"""
import pytest
from playwright.sync_api import Page

pytestmark = pytest.mark.browser


def _assert_no_crash(page: Page, context: str = "") -> None:
    body = page.locator("body").inner_text()
    assert "Internal Server Error" not in body, f"{context}: Internal Server Error in body"
    assert "Traceback (most recent call last)" not in body, f"{context}: Traceback in body"


# ── SUB-01: Subscription detail ───────────────────────────────────────────────

def test_subscription_detail_loads(page, ui_server, api):
    """SUB-01: Create subscription via API → navigate to /subscriptions/{id} → no crash."""
    r = api.post("/subscriptions", json={
        "name": "Detail Test Sub",
        "doc_type": "invoice",
        "frequency": "monthly",
        "start_date": "2026-01-01",
    })
    assert r.status_code in {200, 201}, f"POST /subscriptions failed: {r.text}"
    sub_id = r.json().get("id", "")
    assert sub_id, f"No id in response: {r.json()}"

    resp = page.goto(f"{ui_server}/subscriptions/{sub_id}", wait_until="domcontentloaded")
    assert resp.status != 500, f"/subscriptions/{sub_id} returned HTTP 500"
    assert "/login" not in page.url, "Redirected to login on subscription detail"
    _assert_no_crash(page, f"/subscriptions/{sub_id}")


# ── SUB-02: Manufacturing order detail ────────────────────────────────────────

def test_manufacturing_order_detail_loads(page, ui_server, api):
    """SUB-02: Create mfg order via API → navigate to /manufacturing/{id} → no crash.

    Requires ≥1 input item. We seed a dummy item first.
    """
    # Seed an inventory item for use as input
    item_r = api.post("/items", json={
        "sku": "MFG-DETAIL-INPUT",
        "sell_by": "piece",
        "name": "Mfg Detail Input Item",
        "quantity": 50,
        "category": "Raw Material",
    })
    if item_r.status_code not in {200, 201}:
        # Item may already exist — fetch it
        search_r = api.get("/items", params={"search": "MFG-DETAIL-INPUT"})
        items = search_r.json().get("items", []) if search_r.status_code == 200 else []
        if not items:
            pytest.skip(f"Could not seed input item: {item_r.text}")
        item_id = items[0].get("entity_id") or items[0].get("id", "")
    else:
        item_id = item_r.json().get("id", item_r.json().get("entity_id", ""))

    if not item_id:
        pytest.skip("Could not determine item_id for mfg order seed")

    r = api.post("/manufacturing", json={
        "description": "Detail Test Order",
        "order_type": "assembly",
        "inputs": [{"item_id": item_id, "quantity": 1}],
    })
    if r.status_code not in {200, 201}:
        pytest.skip(f"POST /manufacturing not available or failed: {r.status_code} {r.text}")

    order_id = r.json().get("id", "")
    if not order_id:
        pytest.skip(f"No id in mfg order response: {r.json()}")

    resp = page.goto(f"{ui_server}/manufacturing/{order_id}", wait_until="domcontentloaded")
    assert resp.status != 500, f"/manufacturing/{order_id} returned HTTP 500"
    assert "/login" not in page.url, "Redirected to login on mfg order detail"
    _assert_no_crash(page, f"/manufacturing/{order_id}")


# ── SUB-03: BOM detail ────────────────────────────────────────────────────────

def test_bom_detail_loads(page, ui_server, api):
    """SUB-03: Create BOM via API → navigate to /manufacturing/boms/{id} → no crash."""
    r = api.post("/manufacturing/boms", json={
        "name": "Detail Test BOM",
        "output_qty": 1.0,
        "components": [],
    })
    if r.status_code not in {200, 201}:
        pytest.skip(f"POST /manufacturing/boms failed: {r.status_code} {r.text}")

    # BOM create returns {"event_id": ..., "bom_id": "bom:uuid"}
    bom_id = r.json().get("bom_id", r.json().get("id", ""))
    if not bom_id:
        pytest.skip(f"No bom_id in response: {r.json()}")

    resp = page.goto(f"{ui_server}/manufacturing/boms/{bom_id}", wait_until="domcontentloaded")
    assert resp.status != 500, f"/manufacturing/boms/{bom_id} returned HTTP 500"
    assert "/login" not in page.url, "Redirected to login on BOM detail"
    _assert_no_crash(page, f"/manufacturing/boms/{bom_id}")
