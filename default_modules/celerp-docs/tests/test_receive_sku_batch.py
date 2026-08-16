# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Receiving under non-unique SKU (2026-06-17 sku/batch plan §8.1).

Receiving a bill mints a fresh parcel entity that carries the product's own SKU,
so duplicate SKUs are the normal receiving reality - not an edge case. This proves
each received parcel now also gets a distinct, scannable barcode (the recommended
'assign a barcode on receipt' companion that makes the unique-barcode contract real).
"""
from __future__ import annotations

import uuid

import pytest


async def _register(client) -> str:
    addr = f"admin-{uuid.uuid4().hex[:8]}@recv.test"
    r = await client.post("/auth/register", json={"company_name": "Recv Co", "email": addr, "name": "A", "password": "pw"})
    assert r.status_code == 200
    return r.json()["access_token"]


def _h(t):
    return {"Authorization": f"Bearer {t}"}


async def _location(client, t) -> str:
    r = await client.post("/companies/me/locations", headers=_h(t), json={"name": "WH", "type": "warehouse"})
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _catalog_item(client, t, sku) -> str:
    r = await client.post("/items", headers=_h(t), json={"status": "available", "sku": sku, "name": sku, "quantity": 0, "sell_by": "piece"})
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _bill_and_receive(client, t, loc, goods, sku, qty):
    bill = (await client.post("/docs", headers=_h(t), json={
        "doc_type": "bill",
        "line_items": [{"item_id": goods, "sku": sku, "name": sku, "quantity": qty, "unit_price": 10, "line_total": 10 * qty}],
        "total": 10 * qty,
    })).json()["id"]
    assert (await client.post(f"/docs/{bill}/finalize", headers=_h(t))).status_code == 200
    r = await client.post(f"/docs/{bill}/receive", headers=_h(t), json={
        "location_id": loc,
        "received_items": [{"item_id": goods, "sku": sku, "name": sku,
                            "quantity_received": qty, "cost_price": 10, "receive_as": "stock"}]})
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_receiving_same_sku_twice_makes_distinct_barcoded_parcels(client):
    t = await _register(client)
    loc = await _location(client, t)
    goods = await _catalog_item(client, t, "WIDGET")

    await _bill_and_receive(client, t, loc, goods, "WIDGET", 5)
    await _bill_and_receive(client, t, loc, goods, "WIDGET", 3)

    items = (await client.get("/items?q=WIDGET", headers=_h(t))).json()["items"]
    parcels = [i for i in items if i["id"] != goods and float(i.get("quantity") or 0) > 0]
    # Two received parcels, both carrying the product's own (repeated) SKU.
    assert len(parcels) == 2
    assert all(p["sku"] == "WIDGET" for p in parcels)
    # Each received parcel now gets a distinct, scannable barcode (barcode-on-receipt).
    barcodes = [p.get("barcode") for p in parcels]
    assert all(b for b in barcodes), f"received parcels must be barcoded: {barcodes}"
    assert len(set(barcodes)) == 2, f"barcodes must be distinct: {barcodes}"
    # Quantities are the two independent lots (no merge across the shared sku).
    assert sorted(float(p["quantity"]) for p in parcels) == [3.0, 5.0]
