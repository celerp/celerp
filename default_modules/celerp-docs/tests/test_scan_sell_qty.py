# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""How much one scan places on a goods-out record: the item's Default Sell Qty, or the default for
its unit when unset (a piece adds 1, anything sold by measure adds everything left). On a record
with the stock cap the placed quantity never exceeds what is left of the lot and of the product's
stock; a quotation reserves nothing and is never capped. Buying, returns, counts and other lists
still add 1 per scan."""
from __future__ import annotations

import pytest

from test_doc_scan import (
    _codes, _doc, _h, _ids, _item, _lines, _list_lines, _list_scan, _quotation_list, _reasons, _register, _scan,
)


async def _ct(client, t, sku, qty, **fields) -> str:
    return await _item(client, t, sku, qty=qty, sell_by="carat", allow_splitting=True, **fields)


def _qtys(lines) -> list[float]:
    return [float(line["quantity"]) for line in lines]


async def test_unset_parcel_under_one_carat_scans_whole(client):
    t = await _register(client)
    lot = await _ct(client, t, "P06", 0.6)
    zero = await _ct(client, t, "P00", 0)
    doc = await _doc(client, t)

    body = (await _scan(client, t, doc, "P06,P00")).json()
    assert body["scanned"] == 1 and _reasons(body) == {"P00": "invalid_quantity"}, body
    assert body["results"][0]["quantity"] == 0.6 and body["results"][0]["unit"] == "carat"
    lines = await _lines(client, t, doc)
    assert _ids(lines) == [lot] and _qtys(lines) == [0.6]
    assert zero not in _ids(lines)


async def test_a_set_amount_above_what_is_left_places_what_is_left(client):
    t = await _register(client)
    await _ct(client, t, "P06N", 0.6, default_sell_qty=1)
    doc = await _doc(client, t)

    body = (await _scan(client, t, doc, "P06N")).json()
    assert body["scanned"] == 1 and body["failed"] == [], body
    assert body["results"][0]["quantity"] == 0.6 and body["results"][0]["capped"] is True
    assert _qtys(await _lines(client, t, doc)) == [0.6]


async def test_full_takes_the_parcel_and_a_repeat_is_already_on(client):
    t = await _register(client)
    await _ct(client, t, "P124", 12.4, default_sell_qty="full")
    doc = await _doc(client, t)

    body = (await _scan(client, t, doc, "P124")).json()
    assert body["scanned"] == 1 and body["results"][0]["quantity"] == 12.4, body
    body = (await _scan(client, t, doc, "P124")).json()
    assert body["scanned"] == 0 and _reasons(body) == {"P124": "duplicate_scan"}, body
    assert _qtys(await _lines(client, t, doc)) == [12.4]


async def test_full_by_barcode_then_by_sku_raises_the_line_to_what_is_left(client):
    t = await _register(client)
    bc_a, bc_b = _codes()
    a = await _ct(client, t, "PAB", 6, barcode=bc_a, default_sell_qty="full")
    await _ct(client, t, "PAB", 4, barcode=bc_b, default_sell_qty="full")
    doc = await _doc(client, t)

    assert (await _scan(client, t, doc, bc_a)).json()["scanned"] == 1
    body = (await _scan(client, t, doc, "PAB")).json()
    assert body["scanned"] == 1 and body["failed"] == [], body
    assert body["results"][0]["quantity"] == 4.0
    lines = await _lines(client, t, doc)
    assert _ids(lines) == [a] and _qtys(lines) == [10.0]


async def test_a_set_amount_steps_up_to_what_is_left_then_is_refused(client):
    t = await _register(client)
    await _ct(client, t, "P25", 6, default_sell_qty=2.5)
    doc = await _doc(client, t)

    body = (await _scan(client, t, doc, "P25,P25,P25,P25")).json()
    assert body["scanned"] == 3 and _reasons(body) == {"P25": "insufficient_stock"}, body
    assert [r["quantity"] for r in body["results"] if r["state"] == "added"] == [2.5, 2.5, 1.0]
    assert body["failed"][0]["label"] == "P25: insufficient stock - 8.5 on this document, available 6"
    assert _qtys(await _lines(client, t, doc)) == [6.0]


async def test_full_by_sku_over_a_pool_sums_exactly(client):
    t = await _register(client)
    await _ct(client, t, "PSUM", 0.1, default_sell_qty="full")
    await _ct(client, t, "PSUM", 0.7, default_sell_qty="full")
    doc = await _doc(client, t)

    body = (await _scan(client, t, doc, "PSUM")).json()
    assert body["scanned"] == 1, body
    assert _qtys(await _lines(client, t, doc)) == [0.8]


async def test_a_piece_item_left_unset_still_adds_one_per_scan(client):
    t = await _register(client)
    await _item(client, t, "PC", qty=5, allow_splitting=True)
    doc = await _doc(client, t)

    body = (await _scan(client, t, doc, "PC,PC")).json()
    assert [r["quantity"] for r in body["results"]] == [1.0, 1.0], body
    assert _qtys(await _lines(client, t, doc)) == [2.0]


async def test_meter_unset_is_full_and_a_set_one_adds_one(client):
    t = await _register(client)
    await _item(client, t, "ROPE", qty=30, sell_by="meter", allow_splitting=True)
    await _item(client, t, "ROPE1", qty=30, sell_by="meter", allow_splitting=True, default_sell_qty=1)
    doc = await _doc(client, t)

    assert (await _scan(client, t, doc, "ROPE,ROPE1,ROPE1")).json()["scanned"] == 3
    assert _qtys(await _lines(client, t, doc)) == [30.0, 2.0]


async def test_a_whole_lot_ignores_its_stored_amount(client):
    t = await _register(client)
    await _item(client, t, "WH", qty=7, sell_by="carat", allow_splitting=False, default_sell_qty=3)
    doc = await _doc(client, t)

    body = (await _scan(client, t, doc, "WH")).json()
    assert body["results"][0]["quantity"] == 7.0, body
    assert _qtys(await _lines(client, t, doc)) == [7.0]


async def test_an_rfid_tag_adds_its_one_piece_whatever_the_amount(client):
    t = await _register(client)
    await _ct(client, t, "TAGD", 10, rfid_epc="E200DD01", default_sell_qty=5)
    doc = await _doc(client, t)

    body = (await _scan(client, t, doc, "E200DD01")).json()
    assert body["scanned"] == 1 and body["results"][0]["quantity"] == 1.0, body
    assert _qtys(await _lines(client, t, doc)) == [1.0]


async def test_a_service_takes_its_set_amount_uncapped_and_full_as_one(client):
    t = await _register(client)
    await _item(client, t, "SV2", qty=0, inventory_type="service", default_sell_qty=2)
    await _item(client, t, "SVF", qty=0, inventory_type="service", default_sell_qty="full")
    doc = await _doc(client, t)

    assert (await _scan(client, t, doc, "SV2,SV2,SVF,SVF")).json()["scanned"] == 4
    assert _qtys(await _lines(client, t, doc)) == [4.0, 2.0]


@pytest.mark.parametrize("record", ["purchase_order", "credit_note", "transfer"])
async def test_buying_returns_and_other_lists_still_add_one(client, record):
    t = await _register(client)
    await _ct(client, t, "ONE", 5, default_sell_qty="full")
    if record == "transfer":
        r = await client.post("/lists", headers=_h(t), json={"list_type": "transfer", "line_items": []})
        assert r.status_code == 200, r.text
        lst = r.json()["id"]
        body = (await _list_scan(client, t, lst, "ONE,ONE")).json()
        lines = await _list_lines(client, t, lst)
    else:
        doc = await _doc(client, t, record)
        body = (await _scan(client, t, doc, "ONE,ONE")).json()
        lines = await _lines(client, t, doc)
    assert [r["quantity"] for r in body["results"]] == [1.0, 1.0], body
    assert _qtys(lines) == [2.0]


async def test_a_quotation_takes_a_set_amount_uncapped(client):
    t = await _register(client)
    await _ct(client, t, "Q25", 6, default_sell_qty=2.5)
    doc = await _doc(client, t, "quotation")

    assert (await _scan(client, t, doc, "Q25,Q25,Q25")).json()["scanned"] == 3
    assert _qtys(await _lines(client, t, doc)) == [7.5]


async def test_a_quotation_takes_full_once(client):
    t = await _register(client)
    await _ct(client, t, "QF", 6, default_sell_qty="full")
    await _ct(client, t, "QZERO", 0, default_sell_qty="full")
    doc = await _doc(client, t, "quotation")

    body = (await _scan(client, t, doc, "QF,QF,QZERO")).json()
    assert body["scanned"] == 2 and _reasons(body) == {"QF": "duplicate_scan"}, body
    assert _qtys(await _lines(client, t, doc)) == [6.0, 1.0]


async def test_a_quotation_list_takes_full_as_the_matched_lot(client):
    t = await _register(client)
    await _ct(client, t, "QL", 6)
    lst = await _quotation_list(client, t)

    body = (await _list_scan(client, t, lst, "QL")).json()
    assert body["scanned"] == 1 and body["results"][0]["quantity"] == 6.0, body
    assert _qtys(await _list_lines(client, t, lst)) == [6.0]



async def test_a_quotation_list_takes_one_when_full_has_nothing_on_hand(client):
    t = await _register(client)
    await _ct(client, t, "QLZ", 0, default_sell_qty="full")
    lst = await _quotation_list(client, t)

    body = (await _list_scan(client, t, lst, "QLZ")).json()
    assert body["scanned"] == 1, body
    assert _qtys(await _list_lines(client, t, lst)) == [1.0]


async def test_a_value_under_attributes_scans_as_unset(client):
    t = await _register(client)
    r = await client.post("/items", headers=_h(t), json={
        "status": "available", "sku": "ATTR", "name": "Attr", "quantity": 5, "sell_by": "piece",
        "allow_splitting": True, "attributes": {"default_sell_qty": 3}})
    assert r.status_code == 200, r.text
    doc = await _doc(client, t)

    assert (await _scan(client, t, doc, "ATTR")).json()["scanned"] == 1
    assert _qtys(await _lines(client, t, doc)) == [1.0]

async def test_race_for_the_last_parcel_lands_once(_db_engine):
    from test_doc_scan_concurrency import _race, _seed_draft_doc, _seed_unique_item
    from test_list_outcome_concurrency import _barcode, _cleanup, _factory, _seed_company, _state

    factory = _factory(_db_engine)
    company_id, user_id, user = await _seed_company(factory)
    try:
        bc = _barcode()
        item = await _seed_unique_item(factory, company_id, user, sku="RACE", barcode=bc, splitting=True,
                                       quantity=0.6, sell_by="carat")
        doc = await _seed_draft_doc(factory, company_id, user, "invoice")

        outcome = await _race(factory, company_id, user, doc, {"k1": bc, "k2": bc})

        assert sorted(r["scanned"] for r in outcome.values()) == [0, 1], outcome
        state = await _state(factory, company_id, doc)
        assert [line.get("item_id") for line in state["line_items"]] == [item]
        assert [float(line["quantity"]) for line in state["line_items"]] == [0.6]
    finally:
        await _cleanup(factory, company_id, user_id)


@pytest.mark.parametrize("stored", ["abc", 0, -2, True, float("nan"), None])
def test_a_stored_value_that_is_not_full_or_above_zero_reads_as_unset(stored):
    from celerp.services.line_measures import FULL, proposed_sell_qty

    units = {"carat": {"name": "carat", "decimals": 2, "unit_type": "weight"},
             "piece": {"name": "piece", "decimals": 0, "unit_type": "pieces"}}
    assert proposed_sell_qty({"sell_by": "carat", "default_sell_qty": stored}, units) == FULL
    assert proposed_sell_qty({"sell_by": "piece", "default_sell_qty": stored}, units) == 1
    assert proposed_sell_qty({"sell_by": "piece", "attributes": {"default_sell_qty": 3}}, units) == 1


def test_resolver_rounds_a_finer_amount_down_and_ignores_it_on_a_whole_piece():
    from decimal import Decimal

    from celerp.services.line_measures import FULL, proposed_sell_qty

    units = {"carat": {"name": "carat", "decimals": 2, "unit_type": "weight"},
             "box": {"name": "box", "decimals": 0, "unit_type": "quantity"}}
    assert proposed_sell_qty({"sell_by": "carat", "default_sell_qty": 0.129}, units) == Decimal("0.12")
    assert proposed_sell_qty({"sell_by": "carat", "default_sell_qty": "FULL"}, units) == FULL
    assert proposed_sell_qty({"sell_by": "box"}, units) == FULL
    assert proposed_sell_qty({"sell_by": "carat", "allow_splitting": False, "default_sell_qty": 3}, units) == FULL
    assert proposed_sell_qty({"sell_by": "nounit", "default_sell_qty": 2}, units) == Decimal(2)
    assert proposed_sell_qty({"sell_by": "nounit"}, units) == 1


def test_only_goods_out_scans_read_the_amount():
    from celerp.services.list_behavior import DOC_SCAN_BEHAVIOR, scan_behavior

    on = {d for d, b in DOC_SCAN_BEHAVIOR.items() if b.sell_qty}
    assert on == {"invoice", "memo", "proforma", "subscription_invoice", "debit_note", "production_order",
                  "quotation"}
    assert scan_behavior("lists", "quotation").sell_qty is True
    assert not any(scan_behavior("lists", k).sell_qty for k in ("transfer", "audit", "writeoff", "shipping_doc"))
