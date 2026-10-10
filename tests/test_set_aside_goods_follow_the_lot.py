# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Goods a finalized invoice has costed and not shipped are wherever the lot they came
from went while still in stock: the parts split off it. Another invoice that ships them takes
their cost from the invoice that set them aside, so inventory gives up their cost once.
Taking them off stock by hand, or merging their lot into another, while an invoice holds
them is refused, naming the invoice."""
from __future__ import annotations

import pytest
from sqlalchemy import select

from celerp.models.projections import Projection
from stock_books import assert_settled
from test_cost_follows_goods import COGS, OPENING, _doc_number, _expect, _invoice, _lots, _ship
from test_invoice_unshipped_books import _lot, _ok
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
    assert number in r.json()["detail"] and "cannot go below 2" in r.json()["detail"]
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
    assert await _doc_number(session, auth, a) in detail and await _doc_number(session, auth, b) in detail
    assert detail.startswith(f"{sku}: invoices ")


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
    assert await _doc_number(session, auth, a) in r.json()["detail"]
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
