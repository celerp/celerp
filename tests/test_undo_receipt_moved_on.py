# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A receipt is undone only while what it brought in is still as it left it. Once a parcel
has been split, partly sold or reserved, undoing would leave that part outside the receipt
and let the document be received in full again, so the undo is refused and says why."""
from __future__ import annotations

import uuid

import pytest

from test_cost_restatement import _state
from test_receipt_accounting import _doc


async def _received_parcel(client, session, auth) -> tuple[str, str]:
    h = auth["headers"]
    line = {"sku": "UNDO-G", "name": "Goods", "quantity": 4, "unit_price": 10.0, "line_total": 40.0,
            "receive_as": "stock"}
    bill = await _doc(client, auth, "bill", [line], total=40.0)
    assert (await client.post(f"/docs/{bill}/finalize", headers=h)).status_code == 200
    r = await client.post(f"/docs/{bill}/receive", headers=h, json={"location_id": "", "received_items": [
        {"po_line_index": 0, "sku": "UNDO-G", "name": "Goods", "quantity_received": 4, "receive_as": "stock"}]})
    assert r.status_code == 200, r.text
    [parcel] = (await _state(session, auth, bill))["received_item_ids"]
    return bill, parcel


async def _sell_one(client, auth, lot) -> None:
    h = auth["headers"]
    r = await client.post("/docs", headers=h, json={"doc_type": "invoice", "total": 25.0, "line_items": [
        {"entity_id": lot, "sku": "UNDO-G", "name": "Goods", "quantity": 1, "unit_price": 25.0, "sell_by": "piece"}]})
    assert r.status_code == 200, r.text
    inv = r.json()["id"]
    assert (await client.post(f"/docs/{inv}/finalize", headers=h)).status_code == 200
    r = await client.post(f"/docs/{inv}/fulfill-lines", headers=h, json={"line_entity_ids": [lot]})
    assert r.status_code == 200, r.text


async def _split_one(client, auth, lot) -> None:
    r = await client.post(f"/items/{lot}/split", headers=auth["headers"], json={"children": [{"quantity": 1}]})
    assert r.status_code == 200, r.text


async def _reserve_one(client, auth, lot) -> None:
    r = await client.post(f"/items/{lot}/reserve", headers=auth["headers"], json={"quantity": 1})
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
@pytest.mark.parametrize("move, why", [
    (_split_one, "holds 3 of the 4 that came in"),
    (_sell_one, "of the 4 that came in"),
    (_reserve_one, "has 1 reserved"),
], ids=["split", "partly-sold", "reserved"])
async def test_a_receipt_whose_parcel_moved_on_cannot_be_undone(client, session, auth, move, why):
    bill, parcel = await _received_parcel(client, session, auth)
    await move(client, auth, parcel)
    r = await client.delete(f"/docs/{bill}/receive", headers=auth["headers"])
    assert r.status_code == 409, r.text
    assert "UNDO-G" in r.json()["detail"] and why in r.json()["detail"], r.json()["detail"]
    assert (await _state(session, auth, bill))["received_item_ids"] == [parcel]


@pytest.mark.asyncio
async def test_a_receipt_left_as_it_came_in_is_undone(client, session, auth):
    bill, parcel = await _received_parcel(client, session, auth)
    r = await client.delete(f"/docs/{bill}/receive", headers=auth["headers"])
    assert r.status_code == 200, r.text
    assert (await _state(session, auth, parcel))["status"] == "archived"


@pytest.mark.asyncio
async def test_a_returned_parcel_that_was_split_cannot_be_unreturned(client, session, auth):
    from test_helpers import sell_item

    h = auth["headers"]
    sku = f"RR-{uuid.uuid4().hex[:6]}"
    r = await client.post("/items", headers=h, json={
        "status": "available", "sku": sku, "name": "Widget", "quantity": 2, "cost_price": 40.0, "sell_by": "piece"})
    assert r.status_code == 200, r.text
    inv = await sell_item(client, h, r.json()["id"], unit_price=50.0)
    r = await client.post("/docs", headers=h, json={
        "doc_type": "credit_note", "original_doc_id": inv, "total": 100.0,
        "line_items": [{"name": "Widget", "sku": sku, "quantity": 2, "unit_price": 50.0}]})
    assert r.status_code == 200, r.text
    cn = r.json()["id"]
    assert (await client.post(f"/docs/{cn}/finalize", headers=h)).status_code == 200
    r = await client.post(f"/docs/{cn}/receive-return", headers=h, json={"items": [{"sku": sku, "quantity": 2}]})
    assert r.status_code == 200, r.text
    [returned] = [x["item_id"] for x in (await _state(session, auth, cn))["return_received_items"]]
    await _split_one(client, auth, returned)
    r = await client.delete(f"/docs/{cn}/receive-return", headers=h)
    assert r.status_code == 409, r.text
    assert "holds 1 of the 2 that came in" in r.json()["detail"], r.json()["detail"]
