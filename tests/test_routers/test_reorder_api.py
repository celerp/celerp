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
    await client.post("/items", json={"status": "available", "sku": "LOW", "name": "Low", "quantity": 2, "sell_by": "piece", "reorder_point": 5}, headers=h)
    # above: qty 20 > reorder_point 5
    await client.post("/items", json={"status": "available", "sku": "OK", "name": "Ok", "quantity": 20, "sell_by": "piece", "reorder_point": 5}, headers=h)
    # unset item at qty 0 counts as low (== out of stock, byte-identical to old behaviour)
    await client.post("/items", json={"status": "available", "sku": "ZERO", "name": "Zero", "quantity": 0, "sell_by": "piece"}, headers=h)

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
    r = await client.post("/items", json={"status": "available", "sku": "SUG", "name": "S", "quantity": 5, "sell_by": "piece"}, headers=h)
    item_id = r.json()["id"]
    r = await client.get(f"/items/{item_id}/reorder-suggestion", headers=h)
    assert r.status_code == 200, r.text
    # No outbound history -> blank, never a fabricated number.
    assert r.json() == {"reorder_point": None, "reorder_qty": None}


# The "Draft purchase order" action ships as a Send-to target on the inventory bulk
# toolbar (peer of Send-to Invoice/List/Consignment Out), not a bespoke endpoint. Its
# purchase-line building (reorder_qty -> purchase unit, blank when unset) is covered in
# tests/test_ui.py::TestSendToPurchaseOrder.
