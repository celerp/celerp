# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Receiving goods onto a lot already on hand keeps its weight and pieces true.

A receipt line may state the weight and pieces of what it brings in; they are added to
the lot's. A measure it does not state makes the lot's unknown, since what the lot now
measures cannot be worked out. The measure the lot is sold by is its quantity. Undoing a
receipt takes its measures back off the same way.
"""
from __future__ import annotations

import base64
import json
import uuid

import pytest

from test_cost_restatement import _state
from test_receipt_accounting import _doc, _finalize

pytestmark = pytest.mark.asyncio


async def _lot(client, auth, *, qty: float = 5, weight: float | None = 15.0, pieces: int | None = 5,
               sell_by: str = "piece") -> str:
    data = {"status": "available", "sku": f"RM-{uuid.uuid4().hex[:6]}", "name": "Stones", "quantity": qty,
            "sell_by": sell_by, "cost_price": 10}
    if weight is not None:
        data.update({"weight": weight, "weight_unit": "carat"})
    if pieces is not None:
        data["attributes"] = {"pieces": pieces}
    r = await client.post("/items", headers=auth["headers"], json=data)
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _po_receipt(client, auth, lot: str, qty: float, **measures):
    po = await _doc(client, auth, "purchase_order",
                    [{"item_id": lot, "name": "Stones", "quantity": qty, "unit_price": 10.0}])
    return await client.post(f"/docs/{po}/receive", headers=auth["headers"], json={
        "location_id": "", "received_items": [
            {"po_line_index": 0, "item_id": lot, "quantity_received": qty, "receive_as": "stock", **measures}]})


def _pieces(state: dict):
    return state.get("pieces", (state.get("attributes") or {}).get("pieces"))


async def test_stated_measures_are_added_to_the_lot(client, session, auth):
    lot = await _lot(client, auth)
    r = await _po_receipt(client, auth, lot, 3, weight=9)
    assert r.status_code == 200, r.text
    s = await _state(session, auth, lot)
    assert float(s["quantity"]) == 8.0 and float(s["weight"]) == 24.0 and _pieces(s) == 8


async def test_an_unstated_weight_makes_the_lot_weight_unknown(client, session, auth):
    lot = await _lot(client, auth)
    r = await _po_receipt(client, auth, lot, 3)
    assert r.status_code == 200, r.text
    s = await _state(session, auth, lot)
    assert float(s["quantity"]) == 8.0 and s.get("weight") is None and _pieces(s) == 8


async def test_a_lot_sold_by_weight_weighs_its_quantity(client, session, auth):
    lot = await _lot(client, auth, qty=10, weight=10, pieces=4, sell_by="carat")
    r = await _po_receipt(client, auth, lot, 4)
    assert r.status_code == 200, r.text
    s = await _state(session, auth, lot)
    assert float(s["quantity"]) == 14.0 and float(s["weight"]) == 14.0 and _pieces(s) is None


async def test_a_sold_by_measure_that_differs_from_the_quantity_is_refused(client, session, auth):
    lot = await _lot(client, auth)
    r = await _po_receipt(client, auth, lot, 3, pieces=4)
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["message_key"] == "docs.receive_measure_is_quantity"
    s = await _state(session, auth, lot)
    assert float(s["quantity"]) == 5.0 and float(s["weight"]) == 15.0 and _pieces(s) == 5


async def test_a_new_parcel_takes_the_measures_its_receipt_states(client, session, auth):
    sku = f"RN-{uuid.uuid4().hex[:6]}"
    bill = await _doc(client, auth, "bill", [{"sku": sku, "name": "Stones", "quantity": 3, "unit_price": 10.0,
                                              "sell_by": "piece"}])
    await _finalize(client, auth, bill)
    r = await client.post(f"/docs/{bill}/receive", headers=auth["headers"], json={
        "location_id": "", "received_items": [
            {"po_line_index": 0, "quantity_received": 3, "receive_as": "stock", "weight": 4.5, "pieces": 3}]})
    assert r.status_code == 200, r.text
    (parcel,) = (await _state(session, auth, bill))["received_item_ids"]
    s = await _state(session, auth, parcel)
    assert float(s["weight"]) == 4.5 and _pieces(s) == 3


async def test_undoing_a_receipt_onto_a_lot_makes_its_weight_unknown(client, session, auth):
    from celerp_docs.routes import record_historical_receipt

    lot = await _lot(client, auth, qty=11, weight=33, pieces=11)
    bill = await _doc(client, auth, "bill", [{"item_id": lot, "name": "Stones", "quantity": 6, "unit_price": 4.0}])
    await _finalize(client, auth, bill)
    sub = json.loads(base64.b64decode(auth["headers"]["Authorization"].split(".")[1] + "=="))["sub"]
    await record_historical_receipt(session, auth["company_id"], bill,
                                    lines=[{"line": 0, "item_id": lot, "quantity": 6, "cost": 24}],
                                    received_on="2025-01-02", actor_id=sub, source="migration",
                                    idempotency_key=f"m:{bill}:received")
    await session.commit()

    r = await client.delete(f"/docs/{bill}/receive", headers=auth["headers"])
    assert r.status_code == 200, r.text
    s = await _state(session, auth, lot)
    assert float(s["quantity"]) == 5.0 and s.get("weight") is None and _pieces(s) == 5

