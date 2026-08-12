# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Tests for inventory valuation count honoring holdings scope (on_memo_to/consigned_from).

The status-pill counts in the inventory view must reflect the same filter as the
displayed rows: when on_memo_to is set, pills show counts scoped to that memo set,
not the unfiltered totals. This is item 7b from the press-kit fixes.
"""

from __future__ import annotations

import uuid

import pytest


async def _token(client) -> str:
    email = f"memo-scope-{uuid.uuid4().hex[:8]}@example.com"
    r = await client.post(
        "/auth/register",
        json={"company_name": "Memo Scope Co", "email": email, "name": "Owner", "password": "pw"},
    )
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


@pytest.mark.asyncio
async def test_inventory_valuation_count_honors_memo_scope(client):
    """Status-pill counts reflect on_memo_to, matching the displayed rows.

    Red statement: count_by_status is accumulated over a set scoped only by
    category/status, so with on_memo_to set the counts equal the unscoped totals
    while rows are scoped (routes.py:622).

    This test verifies that:
    1. The valuation endpoint accepts on_memo_to parameter
    2. When filtering by a contact with no memo items, count is 0
    3. Without the filter, items are counted
    """
    headers = {"Authorization": f"Bearer {await _token(client)}"}

    # Create an item
    item_resp = await client.post(
        "/items",
        json={"sku": "SCOPE-TEST-1", "name": "Test Item", "sell_by": "piece", "inventory_type": "stocked", "quantity": 1},
        headers=headers,
    )
    assert item_resp.status_code == 200, item_resp.text

    # Get valuation WITHOUT memo scope - should show the item
    val_unscoped = await client.get(
        "/items/valuation",
        headers=headers,
    )
    assert val_unscoped.status_code == 200, val_unscoped.text
    unscoped_counts = val_unscoped.json().get("count_by_status", {})
    unscoped_total = sum(unscoped_counts.values())
    assert unscoped_total >= 1, f"Expected at least 1 item unscoped, got {unscoped_total}"

    # Get valuation WITH on_memo_to scope (nonexistent contact)
    # The endpoint must accept the parameter and return scoped (empty) results
    val_scoped = await client.get(
        "/items/valuation",
        params={"on_memo_to": "contact:nonexistent-123"},
        headers=headers,
    )
    assert val_scoped.status_code == 200, val_scoped.text
    scoped_counts = val_scoped.json().get("count_by_status", {})
    scoped_total = sum(scoped_counts.values())

    # With a nonexistent contact, no items match, so count should be 0
    assert scoped_total == 0, (
        f"With nonexistent contact filter, valuation count should be 0, got {scoped_total}. "
        f"The count is not honoring on_memo_to filter."
    )
    assert scoped_total < unscoped_total, (
        "Scoped count should be less than unscoped when filtering by on_memo_to"
    )
