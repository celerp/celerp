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
        json={"company_name": "Memo Scope Co", "email": email, "name": "Owner", "password": "pwvalid1"},
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


@pytest.mark.asyncio
async def test_valuation_counts_honor_the_search_like_the_rows(client):
    """A search that matches a subset: the category tabs, the All tab and the status
    cards count exactly the rows the list returns for the same search.

    Red statement: get_valuation reads no q, so with q=[DEMO] the All tab counts
    every active item (4) while the list returns 2."""
    headers = {"Authorization": f"Bearer {await _token(client)}"}
    for sku, name, cat in [("D-1", "[DEMO] Rice", "Grain"), ("D-2", "[DEMO] Feed", "Feed"),
                           ("R-1", "House rice", "Grain"), ("R-2", "Teak chair", "Furniture")]:
        r = await client.post("/items", headers=headers, json={
            "sku": sku, "name": name, "category": cat, "sell_by": "piece", "quantity": 1})
        assert r.status_code == 200, r.text

    params = {"q": "name:[DEMO]"}
    every = (await client.get("/items", headers=headers)).json()["items"]
    rows = (await client.get("/items", params=params, headers=headers)).json()["items"]
    assert {"D-1", "D-2"} <= {i["sku"] for i in rows}
    assert not {"R-1", "R-2"} & {i["sku"] for i in rows}
    assert len(rows) < len(every)

    def _by_category(items):
        out: dict[str, int] = {}
        for i in items:
            if i.get("category"):
                out[i["category"]] = out.get(i["category"], 0) + 1
        return out

    val = await client.get("/items/valuation", params=params, headers=headers)
    assert val.status_code == 200, val.text
    v = val.json()
    assert v["total_scoped_count"] == len(rows), v
    assert v["category_counts"] == _by_category(rows), v
    assert sum(v["count_by_status"].values()) == len(rows), v

    # A category tab under the search counts the searched rows in that category only.
    grain = (await client.get("/items/valuation", params={**params, "category": "Grain"}, headers=headers)).json()
    assert sum(grain["count_by_status"].values()) == _by_category(rows)["Grain"], grain
    assert grain["category_counts"] == _by_category(rows), grain

    # No search: unchanged, every active item counted.
    assert (await client.get("/items/valuation", headers=headers)).json()["total_scoped_count"] == len(every)
