# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Carving part of a lot through the line actions keeps its physical measures honest.

A part reserved, shipped or taken back with no weight stated has an unknown weight, and
so has what the lot keeps: neither side is guessed and neither is refused. A stated
measure is still checked and used. The measure a lot is sold by is its quantity, so it
always splits as a known count, even when the stored figure has drifted from it.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from line_actions_support import doc, h, item, line, line_ids, state  # noqa: F401

pytestmark = pytest.mark.asyncio


async def _parcel(client, h, sku: str, *, qty: float = 5, weight: float | None = 15.0,
                  sell_by: str = "piece", pieces: int | None = None, allow_splitting: bool = True) -> str:
    data = {"sku": sku, "name": sku, "quantity": qty, "sell_by": sell_by, "status": "available",
            "allow_splitting": allow_splitting}
    if weight is not None:
        data.update({"weight": weight, "weight_unit": "carat"})
    if pieces is not None:
        data["attributes"] = {"pieces": pieces}
    r = await client.post("/items", headers=h, json=data)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _pieces(it: dict):
    return it.get("pieces", (it.get("attributes") or {}).get("pieces"))


async def _ship_whole_on_memo(client, h, sku: str, lot_id: str, qty: float) -> tuple[str, str]:
    memo = await doc(client, h, [line(lot_id, qty, sku=sku)], doc_type="memo")
    (l0,) = await line_ids(client, h, memo)
    r = await client.post(f"/docs/{memo}/fulfill-lines", headers=h, json={"line_ids": [l0]})
    assert r.status_code == 200, r.text
    assert (await item(client, h, lot_id))["status"] == "memo_out"
    return memo, l0


async def test_partial_reserve_with_no_weight_leaves_both_weights_unknown(client, h):
    lot = await _parcel(client, h, "CM-RES")
    d = await doc(client, h, [line(lot, 2, sku="CM-RES")])
    r = await client.post(f"/docs/{d}/reserve-lines", headers=h,
                          json={"line_ids": await line_ids(client, h, d), "new_status": "reserved"})
    assert r.status_code == 200, r.text
    part = await item(client, h, (await state(client, h, d))["line_items"][0]["item_id"])
    mother = await item(client, h, lot)
    assert part["status"] == "reserved" and float(part["quantity"]) == 2.0 and part.get("weight") is None
    assert float(mother["quantity"]) == 3.0 and mother.get("weight") is None


async def test_partial_ship_with_no_weight_leaves_both_weights_unknown(client, h):
    lot = await _parcel(client, h, "CM-SHIP")
    d = await doc(client, h, [line(lot, 2, sku="CM-SHIP")])
    r = await client.post(f"/docs/{d}/fulfill-lines", headers=h, json={"line_ids": await line_ids(client, h, d)})
    assert r.status_code == 200, r.text
    part = await item(client, h, (await state(client, h, d))["line_items"][0]["item_id"])
    mother = await item(client, h, lot)
    assert part["status"] == "sold" and float(part["quantity"]) == 2.0 and part.get("weight") is None
    assert float(mother["quantity"]) == 3.0 and mother.get("weight") is None


async def test_partial_take_back_with_no_weight_leaves_both_weights_unknown(client, h):
    lot = await _parcel(client, h, "CM-BACK")
    memo, l0 = await _ship_whole_on_memo(client, h, "CM-BACK", lot, 5)
    r = await client.post(f"/docs/{memo}/revert-lines", headers=h,
                          json={"line_ids": [l0], "quantities": {l0: 2}})
    assert r.status_code == 200, r.text
    (back,) = r.json()["partially_returned"]
    part = await item(client, h, back["item_id"])
    out = await item(client, h, lot)
    assert part["status"] == "available" and float(part["quantity"]) == 2.0 and part.get("weight") is None
    assert out["status"] == "memo_out" and float(out["quantity"]) == 3.0 and out.get("weight") is None


async def test_take_back_uses_and_checks_a_stated_weight(client, h):
    lot = await _parcel(client, h, "CM-WT")
    memo, l0 = await _ship_whole_on_memo(client, h, "CM-WT", lot, 5)
    too_heavy = await client.post(f"/docs/{memo}/revert-lines", headers=h,
                                  json={"line_ids": [l0], "quantities": {l0: 2}, "weights": {l0: 20}})
    assert too_heavy.status_code == 409, too_heavy.text
    assert (await item(client, h, lot))["status"] == "memo_out"
    r = await client.post(f"/docs/{memo}/revert-lines", headers=h,
                          json={"line_ids": [l0], "quantities": {l0: 2}, "weights": {l0: 6}})
    assert r.status_code == 200, r.text
    part = await item(client, h, r.json()["partially_returned"][0]["item_id"])
    assert float(part["weight"]) == 6.0
    assert float((await item(client, h, lot))["weight"]) == 9.0


async def test_weight_sold_lot_keeps_weight_equal_to_quantity(client, h):
    lot = await _parcel(client, h, "CM-CT", qty=10, weight=10, sell_by="carat")
    d = await doc(client, h, [line(lot, 4, sku="CM-CT", sell_by="carat")])
    r = await client.post(f"/docs/{d}/fulfill-lines", headers=h, json={"line_ids": await line_ids(client, h, d)})
    assert r.status_code == 200, r.text
    part = await item(client, h, (await state(client, h, d))["line_items"][0]["item_id"])
    mother = await item(client, h, lot)
    assert float(part["quantity"]) == 4.0 and float(part["weight"]) == 4.0
    assert float(mother["quantity"]) == 6.0 and float(mother["weight"]) == 6.0


async def test_a_lot_that_may_not_be_split_is_still_refused(client, h):
    lot = await _parcel(client, h, "CM-OFF", allow_splitting=False)
    memo, l0 = await _ship_whole_on_memo(client, h, "CM-OFF", lot, 5)
    r = await client.post(f"/docs/{memo}/revert-lines", headers=h, json={"line_ids": [l0], "quantities": {l0: 2}})
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["message_key"] == "lots.splitting_off"
    out = await item(client, h, lot)
    assert out["status"] == "memo_out" and float(out["quantity"]) == 5.0 and float(out["weight"]) == 15.0


async def test_five_stones_reserve_ship_and_take_back_two_each(client, h):
    """5 stones / 15 ct: reserve 2, ship 2 and take back 2 on a memo, none with a weight.
    Every part splits its pieces as a known count and has an unknown weight."""
    lot = await _parcel(client, h, "CM-5", pieces=5)
    reserved = await doc(client, h, [line(lot, 2, sku="CM-5")])
    r = await client.post(f"/docs/{reserved}/reserve-lines", headers=h,
                          json={"line_ids": await line_ids(client, h, reserved), "new_status": "reserved"})
    assert r.status_code == 200, r.text
    shipped = await doc(client, h, [line(lot, 2, sku="CM-5")])
    r = await client.post(f"/docs/{shipped}/fulfill-lines", headers=h,
                          json={"line_ids": await line_ids(client, h, shipped)})
    assert r.status_code == 200, r.text
    memo, l0 = await _ship_whole_on_memo(client, h, "CM-5", lot, 1)
    mother = await item(client, h, lot)
    assert float(mother["quantity"]) == 1.0 and _pieces(mother) == 1 and mother.get("weight") is None

    for d in (reserved, shipped):
        part = await item(client, h, (await state(client, h, d))["line_items"][0]["item_id"])
        assert float(part["quantity"]) == 2.0 and _pieces(part) == 2 and part.get("weight") is None

    # A memo of three stones taken back two.
    lot3 = await _parcel(client, h, "CM-5B", qty=3, weight=9, pieces=3)
    memo, l0 = await _ship_whole_on_memo(client, h, "CM-5B", lot3, 3)
    r = await client.post(f"/docs/{memo}/revert-lines", headers=h, json={"line_ids": [l0], "quantities": {l0: 2}})
    assert r.status_code == 200, r.text
    part = await item(client, h, r.json()["partially_returned"][0]["item_id"])
    out = await item(client, h, lot3)
    assert _pieces(part) == 2 and part.get("weight") is None
    assert _pieces(out) == 1 and out.get("weight") is None


async def test_piece_sold_lot_whose_stored_pieces_drifted_still_carves(client, h, session):
    """A piece-sold lot of 12 whose stored pieces reads 3 (as an imported lot can): its
    pieces are its quantity, so a memo ship and take-back carve it without refusing."""
    from celerp.models.projections import Projection

    lot = await _parcel(client, h, "CM-DRIFT", qty=12, weight=None)
    session.expire_all()
    row = (await session.execute(select(Projection).where(Projection.entity_id == lot))).scalar_one()
    row.state = {**row.state, "attributes": {**(row.state.get("attributes") or {}), "pieces": 3}}
    await session.commit()

    memo = await doc(client, h, [line(lot, 4, sku="CM-DRIFT")], doc_type="memo")
    (l0,) = await line_ids(client, h, memo)
    r = await client.post(f"/docs/{memo}/fulfill-lines", headers=h, json={"line_ids": [l0]})
    assert r.status_code == 200, r.text
    out_id = (await state(client, h, memo))["line_items"][0]["item_id"]
    out = await item(client, h, out_id)
    mother = await item(client, h, lot)
    assert float(out["quantity"]) == 4.0 and _pieces(out) == 4
    assert float(mother["quantity"]) == 8.0 and _pieces(mother) == 8

    r = await client.post(f"/docs/{memo}/revert-lines", headers=h, json={"line_ids": [l0], "quantities": {l0: 3}})
    assert r.status_code == 200, r.text
    back = await item(client, h, r.json()["partially_returned"][0]["item_id"])
    still_out = await item(client, h, out_id)
    assert float(back["quantity"]) == 3.0 and _pieces(back) == 3
    assert float(still_out["quantity"]) == 1.0 and _pieces(still_out) == 1
