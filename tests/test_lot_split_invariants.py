# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A lot is only ever cut where it may be cut, and its measures stay honest.

An item with Allow Splitting off moves only as a whole parcel: reserving, shipping,
taking back from a memo, returning to the supplier and writing off part of it are all
refused with one message, and a refused action changes nothing. With splitting on, the
part that leaves is its own lot, carrying its share of the cost and the measures given
for it; a measure nobody gave is unknown, never 0. Picking, costing and finalizing an
invoice never take part of a lot that may not be split.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from celerp.models.projections import Projection
from test_cost_restatement import _merge
from test_money_stock_and_contact_invariants import _account_net

pytestmark = pytest.mark.asyncio

_SPLIT_OFF = "lots.splitting_off"


# --- helpers -------------------------------------------------------------------------

async def _st(session, auth, eid: str) -> dict:
    session.expire_all()
    row = await session.get(Projection, {"company_id": auth["company_id"], "entity_id": eid})
    return dict(row.state) if row is not None else {}


def _pieces(state: dict):
    return (state.get("attributes") or {}).get("pieces")


async def _lots(session, auth, sku: str) -> dict[str, dict]:
    """Every lot of ``sku``, by id."""
    session.expire_all()
    rows = (await session.execute(select(Projection).where(
        Projection.company_id == auth["company_id"], Projection.entity_type == "item"))).scalars().all()
    return {r.entity_id: dict(r.state) for r in rows if r.state.get("sku") == sku}


def _snap(lots: dict[str, dict]) -> dict:
    keys = ("quantity", "status", "cost_total", "weight", "status_doc_id", "consignment_flag")
    return {eid: ({k: st.get(k) for k in keys}, _pieces(st)) for eid, st in lots.items()}


async def _new_item(client, auth, sku: str | None = None, **kw) -> str:
    body = {"sku": sku or f"LS-{uuid.uuid4().hex[:6]}", "status": "available", "inventory_type": "stocked",
            "sell_by": "piece"} | kw
    body.setdefault("name", body["sku"])
    r = await client.post("/items", headers=auth["headers"], json=body)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _line(eid: str, sku: str, qty: float, **kw) -> dict:
    return {"item_id": eid, "sku": sku, "description": sku, "quantity": qty, "unit_price": 10.0,
            "sell_by": "piece"} | kw


async def _doc(client, auth, lines: list[dict], doc_type: str = "invoice", finalize: bool = True) -> str:
    h = auth["headers"]
    r = await client.post("/docs", headers=h, json={
        "doc_type": doc_type, "line_items": lines, "total": sum(l["quantity"] * 10 for l in lines)})
    assert r.status_code == 200, r.text
    doc_id = r.json()["id"]
    if finalize:
        r = await client.post(f"/docs/{doc_id}/finalize", headers=h)
        assert r.status_code == 200, r.text
    return doc_id


async def _lids(client, auth, doc_id: str) -> list[str]:
    r = await client.get(f"/docs/{doc_id}", headers=auth["headers"])
    return [li["line_id"] for li in r.json()["line_items"]]


def _key(r) -> str | None:
    detail = r.json().get("detail")
    return detail.get("message_key") if isinstance(detail, dict) else None


async def _bill_parcel(client, session, auth, *, sell_by: str, qty: float, allow: bool,
                       unit_price: float = 10.0, doc_type: str = "bill", **template) -> tuple[str, str, str]:
    """A catalog template, then a finalized ``doc_type`` receiving one parcel of ``qty``:
    returns (doc id, parcel id, sku)."""
    sku = f"LSB-{uuid.uuid4().hex[:6]}"
    await _new_item(client, auth, sku, quantity=0, sell_by=sell_by, allow_splitting=allow, **template)
    r = await client.post("/docs", headers=auth["headers"], json={
        "doc_type": doc_type, "contact_id": "supplier:1",
        "line_items": [{"sku": sku, "name": sku, "quantity": qty, "unit_price": unit_price}]})
    assert r.status_code == 200, r.text
    doc_id = r.json()["id"]
    r = await client.post(f"/docs/{doc_id}/finalize", headers=auth["headers"])
    assert r.status_code == 200, r.text
    r = await client.post(f"/docs/{doc_id}/receive", headers=auth["headers"], json={
        "location_id": "", "received_items": [{"po_line_index": 0, "sku": sku, "name": sku,
                                               "quantity_received": qty, "receive_as": "stock"}]})
    assert r.status_code == 200, r.text
    parcel = (await _st(session, auth, doc_id))["received_item_ids"][0]
    return doc_id, parcel, sku


async def _set_measures(client, auth, item_id: str, **fields) -> None:
    r = await client.patch(f"/items/{item_id}", headers=auth["headers"], json={
        "fields_changed": {k: {"old": None, "new": v} for k, v in fields.items()}})
    assert r.status_code == 200, r.text


async def _return(client, auth, doc_id: str, item_id: str, qty: float, **measures):
    return await client.post(f"/docs/{doc_id}/return-items", headers=auth["headers"],
                             json={"items": [{"item_id": item_id, "quantity_returned": qty, **measures}]})


async def _writeoff(client, auth, lot: str, qty_out: float, account: str = "6950", **measures):
    """A write-off list for ``lot`` with one line of ``qty_out``, run: (response, list id)."""
    h = auth["headers"]
    r = await client.post("/lists/writeoff", headers=h, json={"entity_ids": [lot]})
    assert r.status_code == 200, r.text
    wo = r.json()["id"]
    lines = (await client.get(f"/lists/{wo}", headers=h)).json()["line_items"]
    lid = next(l["line_id"] for l in lines if l["item_id"] == lot)
    r = await client.post(f"/lists/{wo}/writeoff-line", headers=h,
                          json={"line_id": lid, "qty_out": qty_out, "account": account, **measures})
    assert r.status_code == 200, r.text
    return await client.post(f"/lists/{wo}/write-off", headers=h), wo


async def _memo_out(client, auth, lot: str, sku: str, qty: float) -> tuple[str, list[str]]:
    d = await _doc(client, auth, [_line(lot, sku, qty)], doc_type="memo")
    lids = await _lids(client, auth, d)
    r = await client.post(f"/docs/{d}/fulfill-lines", headers=auth["headers"], json={"line_ids": lids})
    assert r.status_code == 200, r.text
    return d, lids


def _child_of(lots: dict[str, dict], parent: str) -> tuple[str, dict]:
    [(eid, st)] = [(e, s) for e, s in lots.items() if s.get("split_from") == parent]
    return eid, st


# --- item 1: one no-split guard --------------------------------------------------------

async def test_whole_parcel_of_a_non_splittable_lot_moves_in_every_operation(client, session, auth):
    h = auth["headers"]
    # reserve and ship
    sku = f"LSW-{uuid.uuid4().hex[:6]}"
    lot = await _new_item(client, auth, sku, quantity=5, allow_splitting=False, cost_total=50)
    memo = await _doc(client, auth, [_line(lot, sku, 5)], doc_type="memo")
    lids = await _lids(client, auth, memo)
    r = await client.post(f"/docs/{memo}/reserve-lines", headers=h, json={"line_ids": lids, "new_status": "reserved"})
    assert r.status_code == 200, r.text
    assert (await _st(session, auth, lot))["status"] == "reserved"
    r = await client.post(f"/docs/{memo}/fulfill-lines", headers=h, json={"line_ids": lids})
    assert r.status_code == 200, r.text
    assert (await _st(session, auth, lot))["status"] == "memo_out"
    # memo take-back of the whole parcel
    r = await client.post(f"/docs/{memo}/set-available", headers=h, json={"line_ids": lids, "quantities": {lids[0]: 5}})
    assert r.status_code == 200, r.text
    assert (await _st(session, auth, lot))["status"] == "available"
    assert len(await _lots(session, auth, sku)) == 1
    # write-off of the whole parcel
    r, _wo = await _writeoff(client, auth, lot, 5)
    assert r.status_code == 200, r.text
    assert (await _st(session, auth, lot))["status"] == "disposed"
    assert len(await _lots(session, auth, sku)) == 1
    # supplier return of the whole parcel
    bill, parcel, sku2 = await _bill_parcel(client, session, auth, sell_by="piece", qty=5, allow=False)
    r = await _return(client, auth, bill, parcel, 5)
    assert r.status_code == 200, r.text
    lots = await _lots(session, auth, sku2)
    assert lots[parcel]["status"] == "disposed"
    assert [e for e, s in lots.items() if s.get("split_from")] == []


async def test_reserving_part_of_a_non_splittable_lot_is_refused_and_changes_nothing(client, session, auth):
    sku = f"LSR-{uuid.uuid4().hex[:6]}"
    lot = await _new_item(client, auth, sku, quantity=5, allow_splitting=False, cost_total=50)
    memo = await _doc(client, auth, [_line(lot, sku, 2)], doc_type="memo")
    before = _snap(await _lots(session, auth, sku))
    r = await client.post(f"/docs/{memo}/reserve-lines", headers=auth["headers"],
                          json={"line_ids": await _lids(client, auth, memo), "new_status": "reserved"})
    assert r.status_code == 409, r.text
    assert _key(r) == _SPLIT_OFF
    assert r.json()["detail"]["params"]["sku"] == sku
    assert _snap(await _lots(session, auth, sku)) == before


async def test_shipping_part_of_a_non_splittable_lot_is_refused_and_changes_nothing(client, session, auth):
    sku = f"LSS-{uuid.uuid4().hex[:6]}"
    lot = await _new_item(client, auth, sku, quantity=5, allow_splitting=False, cost_total=50)
    memo = await _doc(client, auth, [_line(lot, sku, 2)], doc_type="memo")
    before = _snap(await _lots(session, auth, sku))
    r = await client.post(f"/docs/{memo}/fulfill-lines", headers=auth["headers"],
                          json={"line_ids": await _lids(client, auth, memo)})
    assert r.status_code == 409, r.text
    assert _key(r) == _SPLIT_OFF
    assert _snap(await _lots(session, auth, sku)) == before


async def test_taking_back_part_of_a_non_splittable_lot_is_refused_and_changes_nothing(client, session, auth):
    sku = f"LST-{uuid.uuid4().hex[:6]}"
    lot = await _new_item(client, auth, sku, quantity=5, allow_splitting=False, cost_total=50)
    memo, lids = await _memo_out(client, auth, lot, sku, 5)
    before = _snap(await _lots(session, auth, sku))
    r = await client.post(f"/docs/{memo}/set-available", headers=auth["headers"],
                          json={"line_ids": lids, "quantities": {lids[0]: 2}})
    assert r.status_code == 409, r.text
    assert _key(r) == _SPLIT_OFF
    assert _snap(await _lots(session, auth, sku)) == before
    assert (await _st(session, auth, memo)).get("fulfillment_status") != "unfulfilled"


async def test_returning_part_of_a_non_splittable_parcel_is_refused_and_changes_nothing(client, session, auth):
    bill, parcel, sku = await _bill_parcel(client, session, auth, sell_by="piece", qty=5, allow=False)
    before = _snap(await _lots(session, auth, sku))
    books = {a: await _account_net(session, auth["company_id"], a) for a in ("1130-P", "2110")}
    r = await _return(client, auth, bill, parcel, 2)
    assert r.status_code == 409, r.text
    assert _key(r) == _SPLIT_OFF
    assert _snap(await _lots(session, auth, sku)) == before
    assert {a: await _account_net(session, auth["company_id"], a) for a in books} == books
    assert not (await _st(session, auth, bill)).get("returned_items")


async def test_writing_off_part_of_a_non_splittable_lot_is_refused_and_changes_nothing(client, session, auth):
    sku = f"LSO-{uuid.uuid4().hex[:6]}"
    lot = await _new_item(client, auth, sku, quantity=5, allow_splitting=False, cost_total=50)
    before = _snap(await _lots(session, auth, sku))
    r, wo = await _writeoff(client, auth, lot, 2)
    assert r.status_code == 409, r.text
    assert _key(r) == _SPLIT_OFF
    assert _snap(await _lots(session, auth, sku)) == before
    assert (await _st(session, auth, wo))["status"] == "draft"
    assert await _account_net(session, auth["company_id"], "6950") == 0.0


# --- item 2: supplier return carves its own lot ----------------------------------------

async def test_part_return_of_a_weight_sold_parcel_carves_a_returned_lot(client, session, auth):
    bill, parcel, sku = await _bill_parcel(client, session, auth, sell_by="carat", qty=15, allow=True)
    books = {a: await _account_net(session, auth["company_id"], a) for a in ("1130-P", "2110")}
    r = await _return(client, auth, bill, parcel, 6)
    assert r.status_code == 200, r.text
    lots = await _lots(session, auth, sku)
    child, cst = _child_of(lots, parent=parcel)
    mother = lots[parcel]
    assert (mother["quantity"], mother["cost_total"], mother["status"]) == (9, 90.0, "available")
    assert (cst["quantity"], cst["weight"], cst["cost_total"], cst["status"]) == (6, 6, 60.0, "disposed")
    assert await _account_net(session, auth["company_id"], "1130-P") == pytest.approx(books["1130-P"] - 60)
    assert await _account_net(session, auth["company_id"], "2110") == pytest.approx(books["2110"] + 60)
    [entry] = (await _st(session, auth, bill))["returned_items"]
    assert (entry["item_id"], entry["quantity_returned"]) == (parcel, 6)


@pytest.mark.parametrize("returned_weight", [6.0, None])
async def test_part_return_of_a_piece_sold_parcel_keeps_qty_cost_and_measures(
        client, session, auth, returned_weight):
    bill, parcel, sku = await _bill_parcel(client, session, auth, sell_by="piece", qty=5, allow=True)
    await _set_measures(client, auth, parcel, weight=15, weight_unit="carat")
    books = {a: await _account_net(session, auth["company_id"], a) for a in ("1130-P", "2110")}
    extra = {} if returned_weight is None else {"weight": returned_weight}
    r = await _return(client, auth, bill, parcel, 2, **extra)
    assert r.status_code == 200, r.text
    lots = await _lots(session, auth, sku)
    child, cst = _child_of(lots, parent=parcel)
    mother = lots[parcel]
    assert (mother["quantity"], mother["cost_total"], mother["status"]) == (3, 30.0, "available")
    assert (cst["quantity"], cst["cost_total"], cst["status"]) == (2, 20.0, "disposed")
    if returned_weight is None:
        # Nobody said what the returned pieces weighed, so neither side's weight is known.
        assert mother.get("weight") is None and cst.get("weight") is None
    else:
        assert (mother["weight"], cst["weight"]) == (9, 6)
    assert await _account_net(session, auth["company_id"], "1130-P") == pytest.approx(books["1130-P"] - 20)
    assert await _account_net(session, auth["company_id"], "2110") == pytest.approx(books["2110"] + 20)


async def test_a_return_weighing_more_than_the_parcel_is_refused(client, session, auth):
    bill, parcel, sku = await _bill_parcel(client, session, auth, sell_by="piece", qty=5, allow=True)
    await _set_measures(client, auth, parcel, weight=15, weight_unit="carat")
    before = _snap(await _lots(session, auth, sku))
    r = await _return(client, auth, bill, parcel, 2, weight=20)
    assert r.status_code in (409, 422), r.text
    assert _snap(await _lots(session, auth, sku)) == before


async def test_part_return_of_consigned_goods_keeps_the_rest_consigned(client, session, auth):
    doc, parcel, sku = await _bill_parcel(client, session, auth, sell_by="piece", qty=5, allow=True,
                                          doc_type="consignment_in")
    assert (await _st(session, auth, parcel))["consignment_flag"] == "in"
    r = await _return(client, auth, doc, parcel, 2)
    assert r.status_code == 200, r.text
    lots = await _lots(session, auth, sku)
    child, cst = _child_of(lots, parent=parcel)
    assert (lots[parcel]["quantity"], lots[parcel]["consignment_flag"]) == (3, "in")
    assert (cst["quantity"], cst["status"], cst.get("consignment_flag")) == (2, "disposed", None)


async def test_whole_return_marks_the_parcel_itself(client, session, auth):
    bill, parcel, sku = await _bill_parcel(client, session, auth, sell_by="piece", qty=5, allow=True)
    books = {a: await _account_net(session, auth["company_id"], a) for a in ("1130-P", "2110")}
    r = await _return(client, auth, bill, parcel, 5)
    assert r.status_code == 200, r.text
    lots = await _lots(session, auth, sku)
    assert lots[parcel]["status"] == "disposed"
    assert [e for e, s in lots.items() if s.get("split_from")] == []
    assert await _account_net(session, auth["company_id"], "1130-P") == pytest.approx(books["1130-P"] - 50)
    assert await _account_net(session, auth["company_id"], "2110") == pytest.approx(books["2110"] + 50)


# --- item 3: measures are given or unknown, never guessed --------------------------------

@pytest.mark.parametrize("written_weight", [6.0, None])
async def test_part_write_off_keeps_qty_cost_and_measures(client, session, auth, written_weight):
    sku = f"LSM-{uuid.uuid4().hex[:6]}"
    lot = await _new_item(client, auth, sku, quantity=5, cost_total=50, weight=15, weight_unit="carat",
                          attributes={"pieces": 5})
    extra = {} if written_weight is None else {"weight": written_weight}
    r, _wo = await _writeoff(client, auth, lot, 2, **extra)
    assert r.status_code == 200, r.text
    lots = await _lots(session, auth, sku)
    child, cst = _child_of(lots, parent=lot)
    mother = lots[lot]
    assert (mother["quantity"], mother["cost_total"], _pieces(mother)) == (3, 30.0, 3)
    assert (cst["quantity"], cst["cost_total"], cst["status"], _pieces(cst)) == (2, 20.0, "disposed", 2)
    if written_weight is None:
        assert mother.get("weight") is None and cst.get("weight") is None
    else:
        assert (mother["weight"], cst["weight"]) == (9, 6)
    assert await _account_net(session, auth["company_id"], "6950") == 20.0


async def test_part_write_off_of_a_weight_sold_lot_carries_its_weight(client, session, auth):
    sku = f"LSC-{uuid.uuid4().hex[:6]}"
    lot = await _new_item(client, auth, sku, quantity=15, cost_total=150, sell_by="carat")
    r, _wo = await _writeoff(client, auth, lot, 6)
    assert r.status_code == 200, r.text
    lots = await _lots(session, auth, sku)
    child, cst = _child_of(lots, parent=lot)
    assert (lots[lot]["quantity"], lots[lot]["cost_total"]) == (9, 90.0)
    assert (cst["quantity"], cst["weight"], cst["cost_total"]) == (6, 6, 60.0)
    assert await _account_net(session, auth["company_id"], "6950") == 60.0


async def test_a_write_off_weighing_more_than_the_lot_is_refused(client, session, auth):
    sku = f"LSN-{uuid.uuid4().hex[:6]}"
    lot = await _new_item(client, auth, sku, quantity=5, cost_total=50, weight=15, weight_unit="carat")
    before = _snap(await _lots(session, auth, sku))
    r, wo = await _writeoff(client, auth, lot, 2, weight=20)
    assert r.status_code in (409, 422), r.text
    assert _snap(await _lots(session, auth, sku)) == before
    assert (await _st(session, auth, wo))["status"] == "draft"


async def test_merge_with_an_unweighed_source_has_no_weight(client, session, auth):
    sku = f"LSG-{uuid.uuid4().hex[:6]}"
    a = await _new_item(client, auth, sku, quantity=2, cost_total=20, weight=3, weight_unit="carat")
    b = await _new_item(client, auth, sku, quantity=2, cost_total=20)
    merged = await _merge(client, auth, [a, b])
    assert (await _st(session, auth, merged)).get("weight") is None


async def test_receiving_does_not_copy_the_templates_measures(client, session, auth):
    _bill, parcel, _sku = await _bill_parcel(client, session, auth, sell_by="piece", qty=2, allow=True,
                                             weight=15, weight_unit="carat", attributes={"pieces": 5})
    st = await _st(session, auth, parcel)
    assert st.get("weight") is None
    assert _pieces(st) is None
    assert st["quantity"] == 2


# --- item 4: picking and costing never take part of a non-splittable lot ----------------

async def _mixed_sku(client, auth, *, with_splittable: bool = True) -> dict:
    """Bound splittable lot of 2 at 10 each, an older non-splittable sibling of 5 at 20 each
    and (optionally) a newer splittable sibling of 5 at 30 each."""
    sku = f"LSX-{uuid.uuid4().hex[:6]}"
    out = {"sku": sku}
    out["ns"] = await _new_item(client, auth, sku, quantity=5, cost_total=100, allow_splitting=False)
    out["bound"] = await _new_item(client, auth, sku, quantity=2, cost_total=20, allow_splitting=True)
    if with_splittable:
        out["sp"] = await _new_item(client, auth, sku, quantity=5, cost_total=150, allow_splitting=True)
    return out


async def test_shipping_skips_a_non_splittable_sibling_for_a_splittable_one(client, session, auth):
    m = await _mixed_sku(client, auth)
    memo = await _doc(client, auth, [_line(m["bound"], m["sku"], 4)], doc_type="memo")
    r = await client.post(f"/docs/{memo}/fulfill-lines", headers=auth["headers"],
                          json={"line_ids": await _lids(client, auth, memo)})
    assert r.status_code == 200, r.text
    lots = await _lots(session, auth, m["sku"])
    assert (lots[m["ns"]]["quantity"], lots[m["ns"]]["status"]) == (5, "available")
    assert lots[m["sp"]]["quantity"] == 3
    assert lots[m["bound"]]["status"] == "memo_out"


async def test_reserving_skips_a_non_splittable_sibling_for_a_splittable_one(client, session, auth):
    m = await _mixed_sku(client, auth)
    memo = await _doc(client, auth, [_line(m["bound"], m["sku"], 4)], doc_type="memo")
    r = await client.post(f"/docs/{memo}/reserve-lines", headers=auth["headers"],
                          json={"line_ids": await _lids(client, auth, memo), "new_status": "reserved"})
    assert r.status_code == 200, r.text
    lots = await _lots(session, auth, m["sku"])
    assert (lots[m["ns"]]["quantity"], lots[m["ns"]]["status"]) == (5, "available")
    assert lots[m["sp"]]["quantity"] == 3
    assert lots[m["bound"]]["status"] == "reserved"


async def test_only_a_non_splittable_sibling_left_refuses_the_shipment(client, session, auth):
    m = await _mixed_sku(client, auth, with_splittable=False)
    memo = await _doc(client, auth, [_line(m["bound"], m["sku"], 4)], doc_type="memo")
    before = _snap(await _lots(session, auth, m["sku"]))
    r = await client.post(f"/docs/{memo}/fulfill-lines", headers=auth["headers"],
                          json={"line_ids": await _lids(client, auth, memo)})
    assert r.status_code == 409, r.text
    assert _key(r) == _SPLIT_OFF
    assert _snap(await _lots(session, auth, m["sku"])) == before


async def test_cogs_skips_a_non_splittable_sibling_for_a_splittable_one(client, session, auth):
    m = await _mixed_sku(client, auth)
    await _doc(client, auth, [_line(m["bound"], m["sku"], 4)])
    # 2 of the bound lot at 10 and 2 of the splittable sibling at 30; none of the sibling at 20.
    assert await _account_net(session, auth["company_id"], "5100") == 80.0


# --- item 5: finalize honours what fulfil allocates -------------------------------------

async def test_finalize_refuses_part_of_a_non_splittable_lot(client, session, auth):
    sku = f"LSF-{uuid.uuid4().hex[:6]}"
    lot = await _new_item(client, auth, sku, quantity=5, cost_total=50, allow_splitting=False)
    inv = await _doc(client, auth, [_line(lot, sku, 2)], finalize=False)
    r = await client.post(f"/docs/{inv}/finalize", headers=auth["headers"])
    assert r.status_code == 409, r.text
    assert _key(r) == _SPLIT_OFF
    assert await _account_net(session, auth["company_id"], "5100") == 0.0
    assert not (await _st(session, auth, inv)).get("finalized")


async def test_finalize_refuses_a_line_only_a_non_splittable_sibling_could_fill(client, session, auth):
    m = await _mixed_sku(client, auth, with_splittable=False)
    inv = await _doc(client, auth, [_line(m["bound"], m["sku"], 4)], finalize=False)
    r = await client.post(f"/docs/{inv}/finalize", headers=auth["headers"])
    assert r.status_code == 409, r.text
    assert _key(r) == _SPLIT_OFF
    assert await _account_net(session, auth["company_id"], "5100") == 0.0


async def test_finalize_still_allows_a_whole_lot_and_a_backorder(client, session, auth):
    sku = f"LSA-{uuid.uuid4().hex[:6]}"
    lot = await _new_item(client, auth, sku, quantity=5, cost_total=50, allow_splitting=False)
    await _doc(client, auth, [_line(lot, sku, 5)])
    sku2 = f"LSA-{uuid.uuid4().hex[:6]}"
    short = await _new_item(client, auth, sku2, quantity=2, cost_total=20, allow_splitting=True)
    await _doc(client, auth, [_line(short, sku2, 4)])


async def test_a_second_invoice_for_the_same_unshipped_lot_finalizes_without_costing_it_again(client, session, auth):
    """The first invoice holds all 5 at a recorded cost. A second invoice for the same lot
    finalizes (the goods go to whichever invoice ships them, and the cost moves with them),
    and costs nothing more: no unit is free."""
    sku = f"LSD-{uuid.uuid4().hex[:6]}"
    lot = await _new_item(client, auth, sku, quantity=5, cost_total=50)
    await _doc(client, auth, [_line(lot, sku, 5)])
    assert await _account_net(session, auth["company_id"], "5100") == 50.0
    second = await _doc(client, auth, [_line(lot, sku, 5)])
    assert (await _st(session, auth, second)).get("finalized")
    assert await _account_net(session, auth["company_id"], "5100") == 50.0


async def test_invoices_sharing_a_lot_cost_it_only_once(client, session, auth):
    """Two invoices take the lot's 5 between them; a third for 1 finalizes and costs nothing."""
    sku = f"LSE-{uuid.uuid4().hex[:6]}"
    lot = await _new_item(client, auth, sku, quantity=5, cost_total=50)
    await _doc(client, auth, [_line(lot, sku, 3)])
    await _doc(client, auth, [_line(lot, sku, 2)])
    assert await _account_net(session, auth["company_id"], "5100") == 50.0
    await _doc(client, auth, [_line(lot, sku, 1)])
    assert await _account_net(session, auth["company_id"], "5100") == 50.0


async def test_a_line_whose_lot_an_invoice_without_a_cost_record_holds_says_so(client, session, auth):
    """The line's own lot is held by an invoice finalized before cost records existed; the
    only other stock of the product may not be split. That cost cannot move, so the second
    invoice is refused, naming the invoice holding the lot, not Allow Splitting on the stock
    it might have taken instead."""
    from test_set_aside_older_paths import _strip_snapshot
    sku = f"LSG-{uuid.uuid4().hex[:6]}"
    lot = await _new_item(client, auth, sku, quantity=1, cost_total=10, allow_splitting=True)
    await _new_item(client, auth, sku, quantity=8, cost_total=80, allow_splitting=False)
    first = await _doc(client, auth, [_line(lot, sku, 1)])
    await _strip_snapshot(session, auth, first)
    number = (await _st(session, auth, first)).get("doc_number")
    second = await _doc(client, auth, [_line(lot, sku, 1)], finalize=False)
    r = await client.post(f"/docs/{second}/finalize", headers=auth["headers"])
    assert r.status_code == 409, r.text
    assert _key(r) == "lines.lot_already_invoiced", r.text
    assert number in r.json()["detail"]["message"]
    assert not (await _st(session, auth, second)).get("finalized")


async def test_a_line_whose_lot_a_costed_invoice_holds_finalizes_without_a_sibling(client, session, auth):
    """Neighbour: the same, but the holder has its cost record. The second invoice finalizes
    without cutting the non-splittable sibling, and books no cost: the lot's one unit is held."""
    sku = f"LSG-{uuid.uuid4().hex[:6]}"
    lot = await _new_item(client, auth, sku, quantity=1, cost_total=10, allow_splitting=True)
    sibling = await _new_item(client, auth, sku, quantity=8, cost_total=80, allow_splitting=False)
    await _doc(client, auth, [_line(lot, sku, 1)])
    second = await _doc(client, auth, [_line(lot, sku, 1)])
    assert (await _st(session, auth, second)).get("finalized")
    assert await _account_net(session, auth["company_id"], "5100") == 10.0
    assert float((await _st(session, auth, sibling))["quantity"]) == 8.0
