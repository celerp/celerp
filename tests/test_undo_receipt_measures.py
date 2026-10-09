# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Undoing a receipt that topped up a lot already on hand gives back the measures it added.

A purchase order receives onto a lot on hand and is then converted to a bill, whose
receipt can be undone. The weight and pieces the receipt stated are taken back off the
lot. A measure the receipt left out, or one changed on the lot since, cannot be worked
out, so it becomes unknown rather than a guessed figure.
"""
from __future__ import annotations

import pytest

from test_cost_restatement import _state
from test_receipt_accounting import _doc, _finalize
from test_receive_lot_measures import _lot, _pieces

pytestmark = pytest.mark.asyncio


async def _topped_up_bill(client, auth, lot: str, *receipts: dict) -> str:
    """A purchase order for the lot, received once per entry of ``receipts`` and converted to a bill."""
    total = sum(float(r["quantity_received"]) for r in receipts)
    po = await _doc(client, auth, "purchase_order",
                    [{"item_id": lot, "name": "Stones", "quantity": total, "unit_price": 10.0}])
    for receipt in receipts:
        r = await client.post(f"/docs/{po}/receive", headers=auth["headers"], json={
            "location_id": "", "received_items": [
                {"po_line_index": 0, "item_id": lot, "receive_as": "stock", **receipt}]})
        assert r.status_code == 200, r.text
    await _finalize(client, auth, po)
    return po


async def _undo(client, auth, bill: str):
    return await client.delete(f"/docs/{bill}/receive", headers=auth["headers"])


async def test_undo_gives_back_the_weight_a_top_up_added(client, session, auth):
    lot = await _lot(client, auth)  # 5 pieces, 15 ct, sold by piece
    bill = await _topped_up_bill(client, auth, lot, {"quantity_received": 3, "weight": 9})
    assert (await _state(session, auth, bill))["doc_type"] == "bill"
    s = await _state(session, auth, lot)
    assert (float(s["quantity"]), float(s["weight"]), _pieces(s)) == (8.0, 24.0, 8)

    r = await _undo(client, auth, bill)
    assert r.status_code == 200, r.text
    # A top-up is not a parcel the receipt created: nothing is archived.
    assert r.json()["item_ids"] == []
    s = await _state(session, auth, lot)
    assert s["status"] == "available"
    assert (float(s["quantity"]), float(s["weight"]), _pieces(s), float(s["cost_total"])) == (5.0, 15.0, 5, 50.0)

    # A second undo reports the receipt already undone and leaves the lot alone.
    r = await _undo(client, auth, bill)
    assert r.status_code == 200 and r.json().get("already_undone") is True, r.text
    s = await _state(session, auth, lot)
    assert (float(s["quantity"]), float(s["weight"]), _pieces(s)) == (5.0, 15.0, 5)


async def test_undo_gives_back_the_pieces_a_top_up_added(client, session, auth):
    lot = await _lot(client, auth, qty=15, weight=15, pieces=5, sell_by="carat")
    bill = await _topped_up_bill(client, auth, lot, {"quantity_received": 9, "pieces": 3})
    s = await _state(session, auth, lot)
    assert (float(s["quantity"]), float(s["weight"]), _pieces(s)) == (24.0, 24.0, 8)

    r = await _undo(client, auth, bill)
    assert r.status_code == 200, r.text
    s = await _state(session, auth, lot)
    assert (float(s["quantity"]), float(s["weight"]), _pieces(s)) == (15.0, 15.0, 5)


async def test_undo_gives_back_what_every_receipt_on_the_bill_added(client, session, auth):
    lot = await _lot(client, auth)
    bill = await _topped_up_bill(client, auth, lot, {"quantity_received": 2, "weight": 6},
                                 {"quantity_received": 1, "weight": 3.5})
    s = await _state(session, auth, lot)
    assert (float(s["quantity"]), float(s["weight"])) == (8.0, 24.5)

    r = await _undo(client, auth, bill)
    assert r.status_code == 200, r.text
    s = await _state(session, auth, lot)
    assert (float(s["quantity"]), float(s["weight"]), _pieces(s)) == (5.0, 15.0, 5)


async def test_a_weight_one_receipt_left_out_stays_unknown_after_undo(client, session, auth):
    lot = await _lot(client, auth)
    bill = await _topped_up_bill(client, auth, lot, {"quantity_received": 2, "weight": 6},
                                 {"quantity_received": 1})
    assert (await _state(session, auth, lot)).get("weight") is None

    r = await _undo(client, auth, bill)
    assert r.status_code == 200, r.text
    s = await _state(session, auth, lot)
    assert (float(s["quantity"]), s.get("weight"), _pieces(s)) == (5.0, None, 5)


async def test_a_weight_changed_on_the_lot_since_the_receipt_becomes_unknown(client, session, auth):
    lot = await _lot(client, auth)
    bill = await _topped_up_bill(client, auth, lot, {"quantity_received": 3, "weight": 9})
    r = await client.patch(f"/items/{lot}", headers=auth["headers"], json={"fields_changed": {"weight": {"old": 24.0, "new": 25.0}}})
    assert r.status_code == 200, r.text
    assert float((await _state(session, auth, lot))["weight"]) == 25.0

    r = await _undo(client, auth, bill)
    assert r.status_code == 200, r.text
    s = await _state(session, auth, lot)
    # Neither 16 nor 15: the edit may have corrected what the receipt stated.
    assert (float(s["quantity"]), s.get("weight"), _pieces(s)) == (5.0, None, 5)


async def test_a_bill_receipt_naming_a_lot_makes_a_new_parcel_and_its_undo_leaves_the_lot(client, session, auth):
    lot = await _lot(client, auth)
    bill = await _doc(client, auth, "bill", [{"item_id": lot, "name": "Stones", "quantity": 3, "unit_price": 10.0}])
    await _finalize(client, auth, bill)
    r = await client.post(f"/docs/{bill}/receive", headers=auth["headers"], json={
        "location_id": "", "received_items": [
            {"po_line_index": 0, "item_id": lot, "quantity_received": 3, "receive_as": "stock", "weight": 9}]})
    assert r.status_code == 200, r.text
    [parcel] = (await _state(session, auth, bill))["received_item_ids"]

    r = await _undo(client, auth, bill)
    assert r.status_code == 200, r.text
    assert r.json()["item_ids"] == [parcel]
    assert (await _state(session, auth, parcel))["status"] == "archived"
    s = await _state(session, auth, lot)
    assert (float(s["quantity"]), float(s["weight"]), _pieces(s)) == (5.0, 15.0, 5)
