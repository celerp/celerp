# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Goods a finalized invoice has costed and not shipped are wherever the lot they came
from went while still in stock: the parts split off it. Another invoice that ships them takes
their cost from the invoice that set them aside, so inventory gives up their cost once.
Taking them off stock by hand, or merging their lot into another, while an invoice holds
them is refused, naming the invoice."""
from __future__ import annotations

import uuid
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from celerp.models.projections import Projection
from stock_books import assert_settled
from test_cost_follows_goods import COGS, OPENING, _doc_number, _expect, _invoice, _lots, _ship
from test_invoice_unshipped_books import _lot, _ok
from test_landed_cost_pools import _po_into
from test_receipt_accounting import _return
from test_money_stock_and_contact_invariants import _account_net

pytestmark = pytest.mark.asyncio


async def _per_doc(session, auth) -> dict[str, float]:
    """Cost of goods sold per invoice: a cost move's debit to the invoice that shipped, its
    credit to the invoice the cost came from."""
    session.expire_all()
    rows = (await session.execute(select(Projection).where(
        Projection.company_id == auth["company_id"], Projection.entity_type == "journal_entry",
        Projection.entity_id.like("je:auto:%")))).scalars().all()
    out: dict[str, float] = {}
    for r in rows:
        st = r.state or {}
        if st.get("status") != "posted":
            continue
        rest = r.entity_id[len("je:auto:"):]
        doc = ":".join(rest.split(":")[:2])
        src = ":".join(rest.split(":")[-2:]) if ":cost-move:" in rest else None
        for e in st.get("entries", []):
            if e["account"] != COGS:
                continue
            d, c = float(e.get("debit") or 0), float(e.get("credit") or 0)
            if src and c:
                out[src] = round(out.get(src, 0) - c, 2)
            else:
                out[doc] = round(out.get(doc, 0) + d - c, 2)
    return {k: v for k, v in out.items() if abs(v) > 0.001}


async def _split(client, auth, lot: str, qty: float) -> str:
    r = await client.post(f"/items/{lot}/split", headers=auth["headers"], json={"children": [{"quantity": qty}]})
    assert r.status_code == 200, r.text
    child = (r.json().get("children") or r.json().get("child_ids"))[0]
    return child["id"] if isinstance(child, dict) else child


async def _merge(client, auth, *lots: str):
    body = {"source_entity_ids": list(lots), "target_sku_from": lots[0]}
    r = await client.post("/items/merge/preview", headers=auth["headers"], json=body)
    assert r.status_code == 200, r.text
    return await client.post("/items/merge", headers=auth["headers"],
                             json={**body, "plan_fingerprint": r.json().get("plan_fingerprint")})


async def _adjust(client, auth, lot: str, qty: float):
    return await client.post(f"/items/{lot}/adjust", headers=auth["headers"], json={"new_qty": qty})


async def test_a_part_split_off_a_set_aside_lot_and_shipped_elsewhere_moves_its_cost(client, session, auth):
    sku = "SAF-SPLIT"
    lot = await _lot(client, auth, sku, 4, 40.0)
    a = await _invoice(client, auth, [(lot, sku, 4)])
    part = await _split(client, auth, lot, 1)
    await assert_settled(client, session, auth)
    b = await _invoice(client, auth, [(part, sku, 1)])
    await _ship(client, auth, b, part)
    await _expect(client, session, auth, shipped=10.0, set_aside=30.0, received=40.0)
    assert await _per_doc(session, auth) == {a: 30.0, b: 10.0}


async def test_the_lot_left_after_a_split_stays_set_aside(client, session, auth):
    """The invoice's goods stay on the lot first, so shipping the rest of the lot on
    another invoice takes the cost of what it ships, never more."""
    sku = "SAF-REST"
    lot = await _lot(client, auth, sku, 4, 40.0)
    a = await _invoice(client, auth, [(lot, sku, 2)])
    await _split(client, auth, lot, 1)
    b = await _invoice(client, auth, [(lot, sku, 3)])
    await _ship(client, auth, b, lot)
    await _expect(client, session, auth, shipped=30.0, set_aside=0.0, received=40.0)
    assert await _per_doc(session, auth) == {b: 30.0}
    assert a not in await _per_doc(session, auth)


@pytest.mark.parametrize("ship_qty", [1, 2])
async def test_a_set_aside_lot_is_merged_only_after_its_invoice_lets_it_go(client, session, auth, ship_qty):
    """Merging the lot an invoice holds would strand the invoice's goods in another lot, so
    it is refused naming the invoice and the books stay as they were. Once the invoice is
    voided the merge goes through, and shipping from the merged lot gives up its cost once."""
    sku, (k, m) = await _lots(client, auth, 10.0, 30.0)
    a = await _invoice(client, auth, [(k, sku, 1)])
    r = await _merge(client, auth, k, m)
    assert r.status_code == 409, r.text
    await session.rollback()
    assert await _doc_number(session, auth, a) in r.text
    await _expect(client, session, auth, shipped=0.0, set_aside=10.0, received=40.0)
    assert (await client.post(f"/docs/{a}/void", headers=auth["headers"], json={})).status_code == 200
    r = await _merge(client, auth, k, m)
    assert r.status_code == 200, r.text
    merged = r.json()["id"]
    b = await _invoice(client, auth, [(merged, sku, ship_qty)])
    await _ship(client, auth, b, merged)
    await assert_settled(client, session, auth)
    cid = auth["company_id"]
    cogs, left = await _account_net(session, cid, COGS), await _account_net(session, cid, OPENING)
    assert round(cogs + left, 2) == 40.0
    assert cogs <= 40.0 and left >= 0.0


async def test_taking_set_aside_goods_off_by_hand_is_refused_naming_the_invoice(client, session, auth):
    sku = "SAF-ADJ"
    lot = await _lot(client, auth, sku, 3, 30.0)
    a = await _invoice(client, auth, [(lot, sku, 2)])
    number = await _doc_number(session, auth, a)
    r = await _adjust(client, auth, lot, 1)
    assert r.status_code == 409, r.text
    assert number in r.json()["detail"]["message"] and "cannot go below 2" in r.json()["detail"]["message"]
    r = await _adjust(client, auth, lot, 0)
    assert r.status_code == 409, r.text
    r = await _adjust(client, auth, lot, 2)
    assert r.status_code == 200, r.text
    await assert_settled(client, session, auth)
    r = await _adjust(client, auth, lot, 5)
    assert r.status_code == 200, r.text
    await assert_settled(client, session, auth)


async def test_two_invoices_holding_a_lot_are_both_named(client, session, auth):
    sku = "SAF-ADJ2"
    lot = await _lot(client, auth, sku, 2, 20.0)
    a = await _invoice(client, auth, [(lot, sku, 1)])
    b = await _invoice(client, auth, [(lot, sku, 1)])
    r = await _adjust(client, auth, lot, 1)
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    numbers = f"{await _doc_number(session, auth, a)}, {await _doc_number(session, auth, b)}"
    assert detail["params"]["docs"] == numbers, detail
    assert detail["message"].startswith(f"{sku}: 2 of this lot are set aside for invoice {numbers}"), detail


async def test_set_aside_goods_can_be_taken_off_once_the_invoice_lets_go(client, session, auth):
    sku = "SAF-VOID"
    lot = await _lot(client, auth, sku, 2, 20.0)
    a = await _invoice(client, auth, [(lot, sku, 2)])
    assert (await _adjust(client, auth, lot, 0)).status_code == 409
    await _ok(client, auth, f"/docs/{a}/void")
    r = await _adjust(client, auth, lot, 0)
    assert r.status_code == 200, r.text
    await assert_settled(client, session, auth)


async def test_a_split_part_holding_set_aside_goods_cannot_be_taken_off(client, session, auth):
    sku = "SAF-PART"
    lot = await _lot(client, auth, sku, 2, 20.0)
    await _invoice(client, auth, [(lot, sku, 2)])
    part = await _split(client, auth, lot, 1)
    r = await _adjust(client, auth, part, 0)
    assert r.status_code == 409, r.text
    await assert_settled(client, session, auth)


async def test_making_set_aside_goods_into_something_else_is_refused(client, session, auth):
    sku = "SAF-TX"
    lot = await _lot(client, auth, sku, 1, 20.0)
    a = await _invoice(client, auth, [(lot, sku, 1)])
    body = {"child_sku": "SAF-TX-OUT", "child_category": "Processed", "child_sell_by": "piece", "child_quantity": 1.0}
    r = await client.post(f"/items/{lot}/transform", headers=auth["headers"], json=body)
    assert r.status_code == 409, r.text
    assert await _doc_number(session, auth, a) in r.json()["detail"]["message"]
    await assert_settled(client, session, auth)
    await _ok(client, auth, f"/docs/{a}/void")
    r = await client.post(f"/items/{lot}/transform", headers=auth["headers"], json=body)
    assert r.status_code == 200, r.text
    await assert_settled(client, session, auth)


async def _two_invoices_then_split(client, auth, sku: str) -> str:
    """A lot of 4 that two invoices each set 2 aside from, split in half; the part."""
    lot = await _lot(client, auth, sku, 4, 40.0)
    await _invoice(client, auth, [(lot, sku, 2)])
    await _invoice(client, auth, [(lot, sku, 2)])
    return await _split(client, auth, lot, 2)


async def test_goods_two_invoices_set_aside_give_up_their_cost_once_after_a_split(client, session, auth):
    sku = "SAF-TWO"
    part = await _two_invoices_then_split(client, auth, sku)
    c = await _invoice(client, auth, [(part, sku, 2)])
    await _ship(client, auth, c, part)
    cid = auth["company_id"]
    assert (round(await _account_net(session, cid, COGS), 2), round(await _account_net(session, cid, OPENING), 2)) == (40.0, 0.0)
    await assert_settled(client, session, auth)


async def test_a_part_holding_goods_two_invoices_set_aside_is_not_taken_off_by_hand(client, session, auth):
    part = await _two_invoices_then_split(client, auth, "SAF-TWO-ADJ")
    r = await _adjust(client, auth, part, 0)
    assert r.status_code == 409, r.text
    await assert_settled(client, session, auth)


async def test_goods_reserved_for_the_invoice_are_the_ones_it_holds(client, session, auth):
    """Lot 5, an invoice for 2 reserves its line: the reserve carves the 2 into a part
    reserved for the invoice, so those are the goods it holds and the 3 left on the lot
    are free: sending the whole rest of the lot out on memo is allowed, and the invoice
    still holds its 2 on the reserved part."""
    sku = "SAF-RSV"
    lot = await _lot(client, auth, sku, 5, 50.0)
    inv = await _invoice(client, auth, [(lot, sku, 2)])
    h = auth["headers"]
    line_ids = [li["line_id"] for li in (await client.get(f"/docs/{inv}", headers=h)).json()["line_items"]]
    r = await client.post(f"/docs/{inv}/reserve-lines", headers=h, json={"line_ids": line_ids, "new_status": "reserved"})
    assert r.status_code == 200, r.text
    part = (await client.get(f"/docs/{inv}", headers=h)).json()["line_items"][0]["item_id"]
    assert part != lot
    r = await client.post("/docs", headers=h, json={"doc_type": "memo", "line_items": [
        {"item_id": lot, "sku": sku, "name": sku, "quantity": 3, "unit_price": 10.0}]})
    assert r.status_code == 200, r.text
    memo = r.json()["id"]
    assert (r := await client.post(f"/docs/{memo}/finalize", headers=h)).status_code == 200, r.text
    memo_lines = [li["line_id"] for li in (await client.get(f"/docs/{memo}", headers=h)).json()["line_items"]]
    r = await client.post(f"/docs/{memo}/fulfill-lines", headers=h, json={"line_ids": memo_lines})
    assert r.status_code == 200, r.text
    from celerp.services.auto_je import set_aside
    number = await _doc_number(session, auth, inv)
    session.expire_all()
    lots = [SimpleNamespace(entity_id=lot), SimpleNamespace(entity_id=part)]
    assert await set_aside(session, auth["company_id"], lots) == {part: {number: 2.0}}
    await assert_settled(client, session, auth)


async def test_a_count_down_to_the_held_goods_keeps_their_cost_on_the_books(client, session, auth):
    """15 at 177.00 (10 at 100.00, then 5 at 14.00 with 7.00 freight billed into the lot), 2
    sent back to the bill, 1 set aside for an invoice, then the lot counted down to that 1: the
    lot keeps the cent share the invoice costed it at, so the books carry the stock to the cent
    after the count and after the invoice ships."""
    sku = f"CNT-{uuid.uuid4().hex[:6]}"
    lot = await _lot(client, auth, sku, 10, 100.0)
    bill = await _po_into(client, auth, lot, 5, 14.0, shipping=7.0)
    r = await _return(client, auth, bill, lot, 2)
    assert r.status_code == 200, r.text
    inv = await _invoice(client, auth, [(lot, sku, 1)])
    await assert_settled(client, session, auth)
    r = await _adjust(client, auth, lot, 1)
    assert r.status_code == 200, r.text
    await assert_settled(client, session, auth)
    await _ship(client, auth, inv, lot)
    await assert_settled(client, session, auth)


async def test_a_carve_keeps_the_cent_share_of_the_whole_lot_cost():
    """What a lot keeps is its cent share of its whole cost, goods and freight together, the
    share an invoice costs the same units at; the part takes the rest."""
    from celerp.services.money import allocate_pro_rata
    from celerp_inventory.services import carve_cost

    state = {"quantity": 13.0, "cost_base": 142.0, "landed_costs": {"bill:1::shipping": 4.2}}
    carve = carve_cost(state, 12.0, "USD")
    kept, _part = allocate_pro_rata(Decimal("146.20"), [Decimal(1), Decimal(12)], "USD")
    assert round(carve.rest_goods + sum(carve.rest_landed.values()), 2) == float(kept) == 11.25
    assert round(carve.part_goods + sum(carve.part_landed.values()), 2) == 134.95
    assert carve.rest_landed == {"bill:1::shipping": 0.32}


async def test_a_held_claim_is_an_estimate_that_reconciles_when_the_goods_ship(client, session, auth):
    """15 at 177.00 (10 at 100.00, then 5 at 14.00 with 7.00 freight billed into the lot): two
    invoices set goods aside at 11.80 a unit, one more ships, units are written off, one goes
    back to the bill line at 15.40, and the last invoice reserves its unit. The held claims stay
    the 11.80 estimates they were costed at while the lot moves under them, and once every held
    unit ships the books carry the stock to the cent."""
    from celerp.services.auto_je import unshipped_claims
    from test_landed_cost_removals import _writeoff

    sku = f"EST-{uuid.uuid4().hex[:6]}"
    lot = await _lot(client, auth, sku, 10, 100.0)
    bill = await _po_into(client, auth, lot, 5, 14.0, shipping=7.0)
    three = await _invoice(client, auth, [(lot, sku, 3)])
    shipped = await _invoice(client, auth, [(lot, sku, 1)])
    await _writeoff(client, auth, lot, 1, "6970")
    await _ship(client, auth, shipped, lot)
    one = await _invoice(client, auth, [(lot, sku, 1)])
    await _writeoff(client, auth, lot, 1, "6970")
    session.expire_all()
    [bill_line] = (await session.get(Projection, {"company_id": auth["company_id"], "entity_id": bill})
                   ).state["line_items"]
    r = await client.post(f"/docs/{bill}/return-items", headers=auth["headers"],
                          json={"lines": [{"line_id": bill_line["line_id"], "quantity_returned": 1}]})
    assert r.status_code == 200, r.text
    r = await client.post(f"/docs/{one}/reserve-lines", headers=auth["headers"],
                          json={"line_entity_ids": [lot], "new_status": "reserved"})
    assert r.status_code == 200, r.text

    session.expire_all()
    held = await unshipped_claims(session, auth["company_id"])
    assert sorted((c.doc_id, c.qty, c.amount) for c in held) == sorted([(three, 3.0, 35.4), (one, 1.0, 11.8)])

    for doc in (three, one):
        session.expire_all()
        state = (await session.get(Projection, {"company_id": auth["company_id"], "entity_id": doc})).state
        await _ship(client, auth, doc, *{li.get("entity_id") or li.get("item_id") for li in state["line_items"]})
    session.expire_all()
    assert await unshipped_claims(session, auth["company_id"]) == []
    await assert_settled(client, session, auth)
