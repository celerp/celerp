# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Landed cost on receipt (P3): receiving a bill's goods creates parcels whose cost includes the
allocated freight/duty (by value, prorated per received quantity). See freight-tracking-plan."""
from __future__ import annotations

import uuid

import pytest


async def _register(client) -> str:
    addr = f"admin-{uuid.uuid4().hex[:8]}@landed.test"
    r = await client.post("/auth/register", json={"company_name": "Landed Co", "email": addr, "name": "A", "password": "pw"})
    assert r.status_code == 200
    return r.json()["access_token"]


def _h(t):
    return {"Authorization": f"Bearer {t}"}


async def _location(client, t) -> str:
    r = await client.post("/companies/me/locations", headers=_h(t), json={"name": "WH", "type": "warehouse"})
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _item(client, t, sku, *, inv="stocked", kind=None):
    body = {"status": "available", "sku": sku, "name": sku, "quantity": 0, "sell_by": "piece", "inventory_type": inv}
    if kind:
        body["landed_cost_kind"] = kind
    r = await client.post("/items", headers=_h(t), json=body)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _line(eid, sku, qty, price):
    return {"entity_id": eid, "sku": sku, "name": sku, "quantity": qty, "unit_price": price,
            "line_total": qty * price, "sell_by": "piece"}


@pytest.mark.asyncio
async def test_received_parcel_cost_includes_allocated_landed(client):
    t = await _register(client)
    loc = await _location(client, t)
    goods = await _item(client, t, "WIDGET", inv="stocked")
    frt = await _item(client, t, "FREIGHT", inv="freight", kind="freight")

    # Bill: 10 widgets @ 10 (value 100) + freight 40. All freight lands on the one goods line.
    bill = (await client.post("/docs", headers=_h(t), json={"doc_type": "bill", "line_items": [
        _line(goods, "WIDGET", 10, 10),
        _line(frt, "FREIGHT", 1, 40)],
        "total": 140})).json()["id"]
    assert (await client.post(f"/docs/{bill}/finalize", headers=_h(t))).status_code == 200

    # Receive all 10 widgets at cost 10 each -> a new parcel with base 100 + landed 40 = cost_total 140.
    r = await client.post(f"/docs/{bill}/receive", headers=_h(t), json={
        "location_id": loc,
        "received_items": [{"item_id": goods, "sku": "WIDGET", "name": "WIDGET",
                            "quantity_received": 10, "cost_price": 10, "receive_as": "stock"}]})
    assert r.status_code == 200, r.text

    # Find the created parcel and verify its landed-inclusive cost.
    items = (await client.get("/items?q=WIDGET", headers=_h(t))).json()["items"]
    parcel = next(i for i in items if float(i.get("quantity") or 0) == 10 and i["id"] != goods)
    assert parcel["cost_base"] == 100
    assert parcel["cost_landed"] == 40
    assert parcel["cost_total"] == 140


@pytest.mark.asyncio
async def test_landed_prorates_to_partial_receipt(client):
    t = await _register(client)
    loc = await _location(client, t)
    goods = await _item(client, t, "GADGET", inv="stocked")
    frt = await _item(client, t, "FRT2", inv="freight", kind="freight")

    # Bill: 10 gadgets @ 10 + freight 50 -> unit landed 5/each. Receive only 4 -> landed 20.
    bill = (await client.post("/docs", headers=_h(t), json={"doc_type": "bill", "line_items": [
        _line(goods, "GADGET", 10, 10),
        _line(frt, "FRT2", 1, 50)],
        "total": 150})).json()["id"]
    assert (await client.post(f"/docs/{bill}/finalize", headers=_h(t))).status_code == 200
    r = await client.post(f"/docs/{bill}/receive", headers=_h(t), json={
        "location_id": loc,
        "received_items": [{"item_id": goods, "sku": "GADGET", "name": "GADGET",
                            "quantity_received": 4, "cost_price": 10, "receive_as": "stock"}]})
    assert r.status_code == 200, r.text
    items = (await client.get("/items?q=GADGET", headers=_h(t))).json()["items"]
    parcel = next(i for i in items if float(i.get("quantity") or 0) == 4 and i["id"] != goods)
    assert parcel["cost_base"] == 40       # 4 @ 10
    assert parcel["cost_landed"] == 20     # 5/unit × 4
    assert parcel["cost_total"] == 60


@pytest.mark.asyncio
async def test_two_goods_split_freight_by_value(client):
    t = await _register(client)
    loc = await _location(client, t)
    a = await _item(client, t, "AA", inv="stocked")
    b = await _item(client, t, "BB", inv="stocked")
    frt = await _item(client, t, "FRT3", inv="freight", kind="freight")

    # A value 100, B value 300, freight 40 -> A 10, B 30.
    bill = (await client.post("/docs", headers=_h(t), json={"doc_type": "bill", "line_items": [
        _line(a, "AA", 10, 10),
        _line(b, "BB", 3, 100),
        _line(frt, "FRT3", 1, 40)],
        "total": 440})).json()["id"]
    assert (await client.post(f"/docs/{bill}/finalize", headers=_h(t))).status_code == 200
    r = await client.post(f"/docs/{bill}/receive", headers=_h(t), json={
        "location_id": loc, "received_items": [
            {"item_id": a, "sku": "AA", "name": "AA", "quantity_received": 10, "cost_price": 10, "receive_as": "stock"},
            {"item_id": b, "sku": "BB", "name": "BB", "quantity_received": 3, "cost_price": 100, "receive_as": "stock"}]})
    assert r.status_code == 200, r.text
    items = (await client.get("/items", headers=_h(t))).json()["items"]
    pa = next(i for i in items if i.get("sku") == "AA" and i["id"] != a)
    pb = next(i for i in items if i.get("sku") == "BB" and i["id"] != b)
    assert pa["cost_landed"] == 10 and pa["cost_total"] == 110
    assert pb["cost_landed"] == 30 and pb["cost_total"] == 330
