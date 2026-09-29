# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A bill's freight and duty are shared out line by line.

Every line a bill brings in as stock takes its share of the bill's freight, insurance and
duty by value, whether or not it names an item or SKU, and two lines for the same SKU at
different prices each take their own share. Receiving everything clears the charges.
"""
from __future__ import annotations

import pytest

from test_cost_restatement import _state, auth, ids  # noqa: F401  (auth and ids are fixtures)
from test_receipt_accounting import _books, _doc, _finalize, _parcels, _receive


async def _freight_line(client, auth, amount: float) -> dict:
    r = await client.post("/items", headers=auth["headers"], json={
        "status": "available", "sku": "FRT", "name": "Freight", "quantity": 0, "sell_by": "piece",
        "inventory_type": "freight", "landed_cost_kind": "freight"})
    assert r.status_code == 200, r.text
    return {"entity_id": r.json()["id"], "sku": "FRT", "name": "Freight", "quantity": 1, "unit_price": amount}


@pytest.mark.asyncio
async def test_a_stock_line_with_no_item_takes_its_share_of_the_freight(client, session, auth):
    bill = await _doc(client, auth, "bill", [
        {"name": "Hand-thrown vase", "receive_as": "stock", "quantity": 2, "unit_price": 15.0},
        await _freight_line(client, auth, 10.0),
    ])
    await _finalize(client, auth, bill)

    r = await _receive(client, auth, bill, {"po_line_index": 0, "name": "Hand-thrown vase", "quantity_received": 2})
    assert r.status_code == 200, r.text
    [parcel] = await _parcels(session, auth, bill)
    assert (parcel["quantity"], parcel["cost_total"]) == (2, 40.0)
    assert await _books(session, auth, "1130-P", "1130-FRT", "2110") == {"1130-P": 40.0, "1130-FRT": 0.0, "2110": -40.0}


@pytest.mark.asyncio
async def test_two_lines_for_the_same_sku_each_take_their_own_share(client, session, auth):
    bill = await _doc(client, auth, "bill", [
        {"sku": "GOODS", "name": "Goods", "quantity": 1, "unit_price": 10.0},
        {"sku": "GOODS", "name": "Goods", "quantity": 1, "unit_price": 30.0},
        await _freight_line(client, auth, 20.0),
    ])
    await _finalize(client, auth, bill)

    r = await _receive(client, auth, bill,
                       {"po_line_index": 0, "sku": "GOODS", "name": "Goods", "quantity_received": 1},
                       {"po_line_index": 1, "sku": "GOODS", "name": "Goods", "quantity_received": 1})
    assert r.status_code == 200, r.text
    assert sorted(p["cost_total"] for p in await _parcels(session, auth, bill)) == [15.0, 45.0]
    assert await _books(session, auth, "1130-P", "1130-FRT", "2110") == {"1130-P": 60.0, "1130-FRT": 0.0, "2110": -60.0}


@pytest.mark.asyncio
async def test_freight_on_a_line_bought_by_the_box_is_shared_over_the_pieces(client, session, auth):
    r = await client.post("/items", headers=auth["headers"], json={
        "status": "available", "sku": "BOXED", "name": "Boxed", "quantity": 0, "sell_by": "piece",
        "purchase_conversion_factor": 12})
    assert r.status_code == 200, r.text
    bill = await _doc(client, auth, "bill", [
        {"item_id": r.json()["id"], "sku": "BOXED", "name": "Boxed", "quantity": 1, "unit_price": 120.0},
        await _freight_line(client, auth, 12.0),
    ])
    await _finalize(client, auth, bill)

    r = await _receive(client, auth, bill, {"po_line_index": 0, "sku": "BOXED", "name": "Boxed", "quantity_received": 1})
    assert r.status_code == 200, r.text
    [parcel] = await _parcels(session, auth, bill)
    assert (parcel["quantity"], parcel["cost_total"]) == (12, 132.0)
    assert await _books(session, auth, "1130-P", "1130-FRT", "2110") == {"1130-P": 132.0, "1130-FRT": 0.0, "2110": -132.0}
