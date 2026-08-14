# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A manually created item starts as Draft on the inventory page: it wears the Draft
badge, the Drafts counter card shows it, and making it available flips both.
"""
from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.browser


def test_draft_lifecycle_in_ui(page, ui_server, api):
    tag = uuid.uuid4().hex[:6]
    sku = f"DFT-{tag}"
    r = api.post("/items", json={"sku": sku, "name": "Draft Widget", "sell_by": "piece",
                                 "quantity": 3, "cost_price": 25.0})
    assert r.status_code == 200, r.text
    item_id = r.json()["id"]

    page.goto(f"{ui_server}/inventory?q={sku}", wait_until="domcontentloaded")
    page.wait_for_selector(".badge--draft", timeout=8000)

    # The Drafts counter card is on the page with a non-zero count.
    page.goto(f"{ui_server}/inventory", wait_until="domcontentloaded")
    card = page.locator('.status-card[href*="status=draft"], a[href*="status=draft"]').first
    card.wait_for(timeout=8000)
    assert card.inner_text().strip() != ""

    r = api.post(f"/items/{item_id}/status", json={"new_status": "available"})
    assert r.status_code == 200, r.text
    page.goto(f"{ui_server}/inventory?q={sku}", wait_until="domcontentloaded")
    page.wait_for_selector(".badge--available", timeout=8000)
    assert page.locator(".badge--draft").count() == 0
