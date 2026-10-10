# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
# Copyright (c) 2026 Noah Severs. All rights reserved.
"""
Group 12: Detail page smoke tests.

Strategy: create entity via API → navigate to its detail URL → assert no 500/traceback.
Uses `api` (pre-authed httpx) and `page` (Playwright) fixtures from conftest.py.
"""
import uuid

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
    # Subscriptions are documents: created as a draft via POST /docs, then activated.
    r = api.post("/docs", json={
        "doc_type": "subscription_invoice",
        "frequency": "monthly",
        "start_date": "2026-01-01",
        "line_items": [{"description": "Service", "quantity": 1, "unit_price": 100.0, "line_total": 100.0}],
    })
    assert r.status_code in {200, 201}, f"POST /docs (subscription) failed: {r.text}"
    sub_id = r.json().get("id", "")
    assert sub_id, f"No id in response: {r.json()}"
    assert api.post(f"/subscriptions/{sub_id}/activate").status_code == 200

    resp = page.goto(f"{ui_server}/subscriptions/{sub_id}", wait_until="domcontentloaded")
    assert resp.status != 500, f"/subscriptions/{sub_id} returned HTTP 500"
    assert "/login" not in page.url, "Redirected to login on subscription detail"
    _assert_no_crash(page, f"/subscriptions/{sub_id}")


# ── SUB-02: Manufacturing order detail ────────────────────────────────────────

def test_manufacturing_order_detail_loads(page, ui_server, api):
    """SUB-02: Create mfg order via API → navigate to /manufacturing/{id} → no crash.

    Seeds an input item and the product the run makes first.
    """
    tag = uuid.uuid4().hex[:6].upper()
    item_id = api.post("/items", json={
        "sku": f"MFG-DETAIL-IN-{tag}", "sell_by": "piece", "name": "Mfg Detail Input Item", "quantity": 50,
        "category": "Raw Material",
    }).json()["id"]
    output_id = api.post("/items", json={
        "sku": f"MFG-DETAIL-OUT-{tag}", "sell_by": "piece", "name": "Mfg Detail Output Item", "quantity": 0,
    }).json()["id"]

    r = api.post("/manufacturing", json={
        "description": "Detail Test Order",
        "order_type": "assembly",
        "inputs": [{"item_id": item_id, "quantity": 1}],
        "output_item_id": output_id,
        "quantity": 1,
    })
    assert r.status_code in {200, 201}, f"POST /manufacturing failed: {r.text}"
    order_id = r.json()["id"]

    resp = page.goto(f"{ui_server}/manufacturing/{order_id}", wait_until="domcontentloaded")
    assert resp.status != 500, f"/manufacturing/{order_id} returned HTTP 500"
    assert "/login" not in page.url, "Redirected to login on mfg order detail"
    _assert_no_crash(page, f"/manufacturing/{order_id}")


# (Removed test_bom_detail_loads: the BOM entity was retired in favour of item-level recipes, so
# POST /manufacturing/boms 405s and /manufacturing/boms/{id} no longer exists. The endpoint's
# removal is asserted by default_modules/celerp-manufacturing/tests/test_bom_removed.py.)
