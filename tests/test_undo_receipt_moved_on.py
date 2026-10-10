# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A receipt is undone only while what it brought in is still as it left it. Once a parcel
has been split, partly sold or reserved, undoing would leave that part outside the receipt
and let the document be received in full again, so the undo is refused and says why."""
from __future__ import annotations

import re
import uuid

import pytest

from test_cost_restatement import _state
from test_landed_cost_pools import _ORDERS, _goods
from test_landed_cost_removals import _split
from test_landed_cost_structural import _undo
from test_receipt_accounting import _doc, _finalize, _receive


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
async def test_a_receipt_whose_parcel_has_goods_reserved_cannot_be_undone(client, session, auth):
    bill, parcel = await _received_parcel(client, session, auth)
    await _reserve_one(client, auth, parcel)
    r = await client.delete(f"/docs/{bill}/receive", headers=auth["headers"])
    assert r.status_code == 409, r.text
    assert "UNDO-G" in r.json()["detail"] and "has 1 reserved" in r.json()["detail"], r.json()["detail"]
    assert (await _state(session, auth, bill))["received_item_ids"] == [parcel]


def _assert_split_refusal(r, parcel_sku: str) -> None:
    """The one answer when a received parcel was split: keyed, names the SKU, and points to
    Return to supplier, where the goods (all on hand in the split lots) go back at cost."""
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert isinstance(detail, dict) and detail.get("message_key") == "docs.undo_receipt_split", detail
    assert detail["params"] == {"sku": parcel_sku}, detail
    assert parcel_sku in detail["message"] and "Return to supplier" in detail["message"], detail
    assert "cannot archive" not in detail["message"] and "manually correct" not in detail["message"], detail


@pytest.mark.asyncio
async def test_a_receipt_whose_parcel_was_partly_split_points_to_return_to_supplier(client, session, auth):
    bill, parcel = await _received_parcel(client, session, auth)
    await _split_one(client, auth, parcel)
    r = await client.delete(f"/docs/{bill}/receive", headers=auth["headers"])
    _assert_split_refusal(r, "UNDO-G")
    assert (await _state(session, auth, bill))["received_item_ids"] == [parcel]


@_ORDERS
@pytest.mark.asyncio
async def test_a_receipt_whose_parcel_was_split_whole_points_to_return_to_supplier(client, session, auth, from_order):
    """Splitting all of a parcel archives it. The goods are on hand in the split lots, so the
    refusal must not call the parent 'archived - cannot archive': the way back is a return."""
    goods = _goods(14.0, 5)
    doc = await _doc(client, auth, "purchase_order" if from_order else "bill", [goods], shipping=7.0)
    if not from_order:
        await _finalize(client, auth, doc)
    r = await _receive(client, auth, doc, {"po_line_index": 0, "sku": goods["sku"], "name": goods["name"],
                                           "quantity_received": 5})
    assert r.status_code == 200, r.text
    [lot] = (await _state(session, auth, doc))["received_item_ids"]
    if from_order:
        await _finalize(client, auth, doc)
    await _split(client, auth, lot, 2, 3)
    r = await _undo(client, auth, doc)
    _assert_split_refusal(r, goods["sku"])
    assert (await _state(session, auth, doc))["received_item_ids"] == [lot]


_NEW_KEYS = ("docs.undo_receipt_split", "item.deleted")


@pytest.mark.parametrize("key", _NEW_KEYS)
def test_undo_receipt_split_and_item_deleted_copy_is_translated_in_every_locale(key):
    """Read from each catalog file, so a missing key or an English placeholder cannot hide
    behind the fallback to English."""
    import json
    from pathlib import Path

    catalogs = {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in (Path(__file__).resolve().parent.parent / "ui" / "locales").glob("*.json")}
    english = catalogs.pop("en")[key]
    assert len(catalogs) == 11
    placeholders = {"{sku}"}
    for code, catalog in catalogs.items():
        assert catalog.get(key), (key, code)
        assert catalog[key] != english, (key, code)
        assert placeholders <= set(re.findall(r"\{[a-z_]+\}", catalog[key])), (key, code)


@pytest.mark.asyncio
async def test_a_receipt_whose_parcel_was_partly_sold_cannot_be_undone(client, session, auth):
    """A unit sold from the parcel took its cost with it, so the receipt cannot be taken
    back as if it never happened: the one refusal for units gone, pointing to a return."""
    bill, parcel = await _received_parcel(client, session, auth)
    await _sell_one(client, auth, parcel)
    r = await client.delete(f"/docs/{bill}/receive", headers=auth["headers"])
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert detail["message_key"] == "docs.undo_receipt_units_sold", detail
    assert detail["params"] == {"sku": "UNDO-G", "gone": "1", "needed": "4"}, detail
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
