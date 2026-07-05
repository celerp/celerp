# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""API surface for reorder points: the low_stock list filter, the velocity
suggestion endpoint, and the draft-purchase-order-from-low-stock action."""
from __future__ import annotations

import pytest


async def _token(client) -> str:
    r = await client.post(
        "/auth/register",
        json={"company_name": "ReorderCo", "email": "admin@reorder.com", "name": "Admin", "password": "pw"},
    )
    return r.json()["access_token"]


@pytest.mark.asyncio
async def test_list_filter_low_stock(client):
    token = await _token(client)
    h = {"Authorization": f"Bearer {token}"}
    # below: qty 2 <= reorder_point 5
    await client.post("/items", json={"sku": "LOW", "name": "Low", "quantity": 2, "sell_by": "piece", "reorder_point": 5}, headers=h)
    # above: qty 20 > reorder_point 5
    await client.post("/items", json={"sku": "OK", "name": "Ok", "quantity": 20, "sell_by": "piece", "reorder_point": 5}, headers=h)
    # unset item at qty 0 counts as low (== out of stock, byte-identical to old behaviour)
    await client.post("/items", json={"sku": "ZERO", "name": "Zero", "quantity": 0, "sell_by": "piece"}, headers=h)

    r = await client.get("/items?filter=low_stock", headers=h)
    assert r.status_code == 200, r.text
    skus = {i["sku"] for i in r.json()["items"]}
    assert "LOW" in skus
    assert "ZERO" in skus
    assert "OK" not in skus


@pytest.mark.asyncio
async def test_reorder_suggestion_endpoint_no_history(client):
    token = await _token(client)
    h = {"Authorization": f"Bearer {token}"}
    r = await client.post("/items", json={"sku": "SUG", "name": "S", "quantity": 5, "sell_by": "piece"}, headers=h)
    item_id = r.json()["id"]
    r = await client.get(f"/items/{item_id}/reorder-suggestion", headers=h)
    assert r.status_code == 200, r.text
    # No outbound history -> blank, never a fabricated number.
    assert r.json() == {"reorder_point": None, "reorder_qty": None}


@pytest.mark.asyncio
async def test_draft_reorder_po(client):
    token = await _token(client)
    h = {"Authorization": f"Bearer {token}"}
    # reorder_qty 24 sell-units / conversion 12 = 2 purchase units per line.
    a = await client.post("/items", json={"sku": "PO-A", "name": "A", "quantity": 1, "sell_by": "piece",
                                          "reorder_point": 10, "reorder_qty": 24, "purchase_conversion_factor": 12}, headers=h)
    b = await client.post("/items", json={"sku": "PO-B", "name": "B", "quantity": 1, "sell_by": "piece",
                                          "reorder_point": 10, "reorder_qty": 12, "purchase_conversion_factor": 12}, headers=h)
    ids = [a.json()["id"], b.json()["id"]]

    r = await client.post("/docs/reorder/draft-po", json={"item_ids": ids}, headers=h)
    assert r.status_code == 200, r.text
    doc_id = r.json()["id"]

    doc = (await client.get(f"/docs/{doc_id}", headers=h)).json()
    assert doc["doc_type"] == "purchase_order"
    lines = doc.get("line_items") or []
    assert len(lines) == 2
    by_sku = {li["sku"]: li for li in lines}
    assert by_sku["PO-A"]["quantity"] == 2  # 24 / 12
    assert by_sku["PO-B"]["quantity"] == 1  # 12 / 12


@pytest.mark.asyncio
async def test_draft_reorder_po_requires_items(client):
    token = await _token(client)
    h = {"Authorization": f"Bearer {token}"}
    r = await client.post("/docs/reorder/draft-po", json={"item_ids": []}, headers=h)
    assert r.status_code == 422
