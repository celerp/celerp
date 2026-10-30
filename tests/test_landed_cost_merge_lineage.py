# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A bill's freight is found wherever its goods went: merged, split off a merged lot, merged
again, or made into another product with their cost kept.

The bill's own units go back from the lot they came into, which also holds other stock, so the
bill empties and goes back to its order while part of its goods sit in a merged lot.

- Taking a bill back to its order takes its freight off every lot holding it, nothing goes to
  cost of goods sold for freight still in stock, and the books tie.
- Units sold before the revert took their share of the freight with them; the revert takes
  that share back off cost of goods sold.
- Another bill's freight on the same lot stays.
- A merge undone first gives the freight back to the lot it came from, which the revert finds.
- Finalizing a bill whose goods were merged before it was finalized puts the freight of the
  units still held on the merged lot, not on cost of goods sold, and finalizing again after a
  revert puts it back where it was.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select, update

from celerp.models.ledger import LedgerEntry
from celerp.models.projections import Projection
from mfg_runs import give_back, issue, product, refusal, run, snapshot
from stock_books import assert_settled
from test_cost_restatement import _state
from test_helpers import merge_items
from test_landed_cost_lineage import _doc_pools, _opening
from test_landed_cost_pools import _b, _goods, _po_into, _post, _sell
from test_landed_cost_removals import _seed, _split
from test_receipt_accounting import _doc, _finalize, _receive, _return

FREIGHT = 30.0


async def _shared_lot(client, auth, freight: float = FREIGHT) -> tuple[str, str]:
    """A lot of 10 opening units at 10.00 that an order brought 10 more into at 10.00 with
    ``freight`` shipping, finalized: the lot holds the bill's freight over 20 units."""
    lot = await _opening(client, auth, 10, 100.0)
    return await _po_into(client, auth, lot, 10, 10.0, shipping=freight), lot


async def _like(client, session, auth, lot: str, qty: float, cost: float) -> str:
    """Other stock of the same product as ``lot`` (``qty`` units costing ``cost``), from no bill."""
    sku = (await _state(session, auth, lot))["sku"]
    r = await client.post("/items", headers=auth["headers"], json={
        "sku": sku, "name": "Opening", "quantity": qty, "sell_by": "piece", "status": "available",
        "cost_total": cost})
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _merge(client, auth, ids: list[str], **extra) -> str:
    r = await merge_items(client, headers=auth["headers"],
                          json={"source_entity_ids": ids, "target_sku_from": ids[0], **extra})
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _send_back_and_revert(client, auth, doc: str, lot: str) -> None:
    """The bill's 10 units go back from the lot they came into, then the bill goes back to its order."""
    r = await _return(client, auth, doc, lot, 10)
    assert r.status_code == 200, r.text
    r = await _post(client, auth, doc, "revert-to-draft")
    assert r.status_code == 200, r.text


async def _pools(session, auth, doc: str, *lots: str) -> list[float]:
    return [round(sum(_doc_pools(await _state(session, auth, x), doc).values()), 2) for x in lots]


async def _transform(client, auth, lot: str, qty: float = 3) -> str:
    r = await client.post(f"/items/{lot}/transform", headers=auth["headers"], json={
        "child_sku": f"LCM-{uuid.uuid4().hex[:6]}", "child_category": "Processed", "child_sell_by": "piece",
        "child_quantity": qty})
    assert r.status_code == 200, r.text
    return r.json()["child_id"]


# ── P7c: the revert finds the freight through merges, splits and kept transforms ────────

async def test_a_revert_takes_the_freight_off_the_merged_lot(client, session, auth):
    await _seed(session, auth)
    doc, lot = await _shared_lot(client, auth)
    [part] = await _split(client, auth, lot, 10)
    x = await _merge(client, auth, [part, await _like(client, session, auth, lot, 5, 50.0)])
    assert await _pools(session, auth, doc, x) == [15.0]
    before = await _b(session, auth, "5100")
    await _send_back_and_revert(client, auth, doc, lot)
    assert await _pools(session, auth, doc, x, lot) == [0.0, 0.0]
    assert await _b(session, auth, "5100") == before
    await assert_settled(client, session, auth)


async def test_units_sold_from_the_merged_lot_took_their_share(client, session, auth):
    await _seed(session, auth)
    doc, lot = await _shared_lot(client, auth)
    [part] = await _split(client, auth, lot, 10)
    x = await _merge(client, auth, [part, await _like(client, session, auth, lot, 5, 50.0)])
    await _sell(client, auth, x, 3)
    assert await _pools(session, auth, doc, x) == [12.0]
    before = await _b(session, auth, "5100")
    await _send_back_and_revert(client, auth, doc, lot)
    assert await _pools(session, auth, doc, x) == [0.0]
    assert round((await _b(session, auth, "5100"))["5100"] - before["5100"], 2) == -3.0
    await assert_settled(client, session, auth)


async def test_another_bills_freight_stays(client, session, auth):
    await _seed(session, auth)
    doc, lot = await _shared_lot(client, auth)
    other = await _like(client, session, auth, lot, 5, 50.0)
    other_doc = await _po_into(client, auth, other, 5, 10.0, shipping=20.0)
    [part] = await _split(client, auth, lot, 10)
    x = await _merge(client, auth, [part, other])
    assert await _pools(session, auth, other_doc, x) == [20.0]
    await _send_back_and_revert(client, auth, doc, lot)
    assert await _pools(session, auth, doc, x) == [0.0]
    assert await _pools(session, auth, other_doc, x) == [20.0]
    await assert_settled(client, session, auth)


async def test_parts_split_off_the_merged_lot_lose_their_share(client, session, auth):
    await _seed(session, auth)
    doc, lot = await _shared_lot(client, auth)
    [part] = await _split(client, auth, lot, 10)
    x = await _merge(client, auth, [part, await _like(client, session, auth, lot, 5, 50.0)])
    [y] = await _split(client, auth, x, 5)
    assert await _pools(session, auth, doc, x, y) == [10.0, 5.0]
    before = await _b(session, auth, "5100")
    await _send_back_and_revert(client, auth, doc, lot)
    assert await _pools(session, auth, doc, x, y) == [0.0, 0.0]
    assert await _b(session, auth, "5100") == before
    await assert_settled(client, session, auth)


async def test_the_freight_is_found_two_merges_on(client, session, auth):
    await _seed(session, auth)
    doc, lot = await _shared_lot(client, auth)
    [part] = await _split(client, auth, lot, 10)
    x = await _merge(client, auth, [part, await _like(client, session, auth, lot, 5, 50.0)])
    [y] = await _split(client, auth, x, 5)
    z = await _merge(client, auth, [y, await _like(client, session, auth, lot, 2, 20.0)])
    assert await _pools(session, auth, doc, x, z) == [10.0, 5.0]
    before = await _b(session, auth, "5100")
    await _send_back_and_revert(client, auth, doc, lot)
    assert await _pools(session, auth, doc, x, z) == [0.0, 0.0]
    assert await _b(session, auth, "5100") == before
    await assert_settled(client, session, auth)


async def test_a_merge_undone_first_gives_the_freight_back_to_its_lot(client, session, auth):
    await _seed(session, auth)
    doc, lot = await _shared_lot(client, auth)
    [part] = await _split(client, auth, lot, 10)
    x = await _merge(client, auth, [part, await _like(client, session, auth, lot, 5, 50.0)])
    r = await client.post(f"/items/{x}/undo-merge", headers=auth["headers"])
    assert r.status_code == 200, r.text
    assert await _pools(session, auth, doc, part) == [15.0]
    before = await _b(session, auth, "5100")
    await _send_back_and_revert(client, auth, doc, lot)
    assert await _pools(session, auth, doc, part, x) == [0.0, 0.0]
    assert await _b(session, auth, "5100") == before
    await assert_settled(client, session, auth)


async def test_a_product_made_with_the_cost_kept_loses_the_freight(client, session, auth):
    """The bill's own lots all went back while a product made from part of them holds its freight."""
    await _seed(session, auth)
    doc, lot = await _shared_lot(client, auth)
    [part] = await _split(client, auth, lot, 10)
    product = await _transform(client, auth, part)
    assert await _pools(session, auth, doc, product) == [15.0]
    before = await _b(session, auth, "5100")
    await _send_back_and_revert(client, auth, doc, lot)
    assert await _pools(session, auth, doc, product) == [0.0]
    assert await _b(session, auth, "5100") == before
    await assert_settled(client, session, auth)


# ── P7d: the freight spread at finalize follows the same lineage ────────────────────────

async def _ordered(client, session, auth, qty: float = 10, freight: float = FREIGHT) -> tuple[str, str]:
    """An order of ``qty`` new units at 10.00 with ``freight`` shipping, received, not finalized."""
    line = _goods(10.0, qty)
    doc = await _doc(client, auth, "purchase_order", [line], shipping=freight)
    r = await _receive(client, auth, doc, {"po_line_index": 0, "sku": line["sku"], "name": line["name"],
                                           "quantity_received": qty})
    assert r.status_code == 200, r.text
    return doc, (await _state(session, auth, doc))["received_item_ids"][0]


async def test_finalizing_after_a_merge_puts_the_freight_on_the_merged_lot(client, session, auth):
    await _seed(session, auth)
    doc, lot = await _ordered(client, session, auth)
    x = await _merge(client, auth, [lot, await _like(client, session, auth, lot, 5, 50.0)])
    before = await _b(session, auth, "5100")
    await _finalize(client, auth, doc)
    assert await _pools(session, auth, doc, x) == [FREIGHT]
    assert await _b(session, auth, "5100") == before
    await assert_settled(client, session, auth)


async def test_units_sold_from_the_merged_lot_before_finalizing_expense_their_share(client, session, auth):
    """The merged lot's 15 units are 10 of the bill's and 5 others, so 3 sold took 2 of the bill's."""
    await _seed(session, auth)
    doc, lot = await _ordered(client, session, auth)
    x = await _merge(client, auth, [lot, await _like(client, session, auth, lot, 5, 50.0)])
    await _sell(client, auth, x, 3)
    before = await _b(session, auth, "5100")
    await _finalize(client, auth, doc)
    assert await _pools(session, auth, doc, x) == [24.0]
    assert round((await _b(session, auth, "5100"))["5100"] - before["5100"], 2) == 6.0
    await assert_settled(client, session, auth)


async def test_a_hand_set_merged_quantity_keeps_the_bills_share(client, session, auth):
    """Counted at 14 (pieces are whole), the merged lot holds the bill's 10 units as 10 x 14 / 15 of
    its own, so the freight stays whole on it and 3 sold take 3/14 of it."""
    await _seed(session, auth)
    doc, lot = await _ordered(client, session, auth)
    x = await _merge(client, auth, [lot, await _like(client, session, auth, lot, 5, 50.0)], resulting_quantity=14)
    await _sell(client, auth, x, 3)
    before = await _b(session, auth, "5100")
    await _finalize(client, auth, doc)
    held, = await _pools(session, auth, doc, x)
    expensed = round((await _b(session, auth, "5100"))["5100"] - before["5100"], 2)
    assert (held, expensed) == (round(FREIGHT * (14 - 3) / 14, 2), round(FREIGHT * 3 / 14, 2))
    await assert_settled(client, session, auth)


async def test_finalizing_after_a_kept_transform_puts_the_freight_on_the_product(client, session, auth):
    await _seed(session, auth)
    doc, lot = await _ordered(client, session, auth)
    product = await _transform(client, auth, lot)
    before = await _b(session, auth, "5100")
    await _finalize(client, auth, doc)
    assert await _pools(session, auth, doc, product) == [FREIGHT]
    assert await _b(session, auth, "5100") == before
    await assert_settled(client, session, auth)


async def test_finalizing_again_after_a_revert_puts_the_freight_back(client, session, auth):
    await _seed(session, auth)
    doc, lot = await _shared_lot(client, auth)
    [part] = await _split(client, auth, lot, 10)
    x = await _merge(client, auth, [part, await _like(client, session, auth, lot, 5, 50.0)])
    await _sell(client, auth, x, 3)
    r = await _return(client, auth, doc, lot, 10)
    assert r.status_code == 200, r.text
    held = await _pools(session, auth, doc, x)
    books = await _b(session, auth, "5100", "6970", "1130-FRT")
    assert (await _post(client, auth, doc, "revert-to-draft")).status_code == 200
    await _finalize(client, auth, doc)
    assert await _pools(session, auth, doc, x) == held
    assert await _b(session, auth, "5100", "6970", "1130-FRT") == books
    await assert_settled(client, session, auth)


async def test_a_second_finalize_after_a_merge_capitalises_once(client, session, auth):
    await _seed(session, auth)
    doc, lot = await _ordered(client, session, auth)
    x = await _merge(client, auth, [lot, await _like(client, session, auth, lot, 5, 50.0)])
    await _finalize(client, auth, doc)
    books = await _b(session, auth, "5100", "1130-FRT")
    await _post(client, auth, doc, "finalize")
    assert await _pools(session, auth, doc, x) == [FREIGHT]
    assert await _b(session, auth, "5100", "1130-FRT") == books
    await assert_settled(client, session, auth)


# ── P7b: a revert that would need freight folded into a goods cost is refused, writing nothing ──

async def _refused(client, session, auth, doc: str, lot: str, *names: str) -> dict:
    """The bill's units go back from ``lot``; taking the bill back to its order is then refused,
    nothing changes, and the reasons name each of ``names`` (lot ids)."""
    r = await _return(client, auth, doc, lot, 10)
    assert r.status_code == 200, r.text
    before = await snapshot(session, auth, doc, *names)
    r = await _post(client, auth, doc, "revert-to-draft")
    detail = refusal(r, 409, "docs.revert_lineage_folded")
    assert await snapshot(session, auth, doc, *names) == before
    assert [x["params"]["id"] for x in detail["params"]["lots"]] == list(names), detail
    for x in names:
        assert f"SKU '{(await _state(session, auth, x))['sku']}'" in detail["message"], detail
    return detail


async def _as_an_earlier_release_merged_it(session, auth, x: str) -> None:
    """The merge made ``x`` the way an earlier release did: its cost total is the whole value,
    freight included, and it holds no freight of its own, while the sources keep theirs."""
    session.expire_all()
    cid = auth["company_id"]
    row = (await session.execute(select(LedgerEntry).where(
        LedgerEntry.company_id == cid, LedgerEntry.entity_id == x, LedgerEntry.event_type == "item.created"))).scalar_one()
    data = {k: v for k, v in row.data.items() if k not in ("landed_costs", "cost_base")}
    await session.execute(update(LedgerEntry).where(LedgerEntry.id == row.id).values(data=data))
    proj = await session.get(Projection, {"company_id": cid, "entity_id": x})
    proj.state = {k: v for k, v in proj.state.items() if k not in ("landed_costs", "cost_base")}
    await session.commit()


async def test_a_merge_made_before_merges_carried_freight_is_refused_until_undone(client, session, auth):
    await _seed(session, auth)
    doc, lot = await _shared_lot(client, auth)
    [part] = await _split(client, auth, lot, 10)
    x = await _merge(client, auth, [part, await _like(client, session, auth, lot, 5, 50.0)])
    await _as_an_earlier_release_merged_it(session, auth, x)
    detail = await _refused(client, session, auth, doc, lot, part)
    assert detail["params"]["lots"][0]["message_key"] == "docs.revert_folded_merged", detail
    r = await client.post(f"/items/{x}/undo-merge", headers=auth["headers"])
    assert r.status_code == 200, r.text
    assert (await _post(client, auth, doc, "revert-to-draft")).status_code == 200
    assert await _pools(session, auth, doc, part) == [0.0]
    await assert_settled(client, session, auth)


async def test_a_merged_lot_sold_to_zero_does_not_stop_a_revert(client, session, auth):
    await _seed(session, auth)
    doc, lot = await _shared_lot(client, auth)
    [part] = await _split(client, auth, lot, 10)
    x = await _merge(client, auth, [part, await _like(client, session, auth, lot, 5, 50.0)])
    await _sell(client, auth, x, 15)
    await _send_back_and_revert(client, auth, doc, lot)
    await assert_settled(client, session, auth)


async def test_a_product_made_at_another_cost_is_refused(client, session, auth):
    await _seed(session, auth)
    doc, lot = await _shared_lot(client, auth)
    [part] = await _split(client, auth, lot, 10)
    r = await client.post(f"/items/{part}/transform", headers=auth["headers"], json={
        "child_sku": f"LCM-{uuid.uuid4().hex[:6]}", "child_category": "Processed", "child_sell_by": "piece",
        "child_quantity": 3, "child_cost_total": 200.0})
    assert r.status_code == 200, r.text
    detail = await _refused(client, session, auth, doc, lot, part)
    assert detail["params"]["lots"][0]["message_key"] == "docs.revert_folded_merged", detail


async def test_goods_a_customer_returned_are_refused(client, session, auth):
    """3 units sold from a part holding the freight come back as a lot of their own."""
    await _seed(session, auth)
    doc, lot = await _shared_lot(client, auth)
    [part] = await _split(client, auth, lot, 10)
    inv = await _sell(client, auth, part, 3)
    r = await client.post("/docs", headers=auth["headers"], json={
        "doc_type": "credit_note", "original_doc_id": inv, "total": 150.0, "line_items": [
            {"entity_id": part, "name": "Lot", "quantity": 3, "unit_price": 50.0, "sell_by": "piece"}]})
    assert r.status_code == 200, r.text
    cn = r.json()["id"]
    assert (await _post(client, auth, cn, "finalize")).status_code == 200
    r = await client.post(f"/docs/{cn}/receive-return", headers=auth["headers"], json={
        "items": [{"sku": (await _state(session, auth, part))["sku"], "quantity": 3}]})
    assert r.status_code == 200, r.text
    back = r.json()["received_items"][0]["item_id"]
    detail = await _refused(client, session, auth, doc, lot, back)
    assert detail["params"]["lots"][0]["message_key"] == "docs.revert_folded_returned", detail


@pytest.mark.parametrize("given_back", [False, True], ids=["issued", "issued_and_returned"])
async def test_units_a_production_run_took_are_refused_naming_the_run(client, session, auth, given_back):
    await _seed(session, auth)
    doc, lot = await _shared_lot(client, auth)
    [part] = await _split(client, auth, lot, 10)
    order = await run(client, auth, await product(client, auth, [(part, 4)]), 1)
    assert (await issue(client, auth, order)).status_code == 200, order
    if given_back:
        assert (await give_back(client, auth, order)).status_code == 200
    detail = await _refused(client, session, auth, doc, lot, part)
    [fold] = detail["params"]["lots"]
    ref = (await _state(session, auth, order)).get("ref_id") or order
    assert (fold["message_key"], fold["params"]["run"]) == ("docs.revert_folded_production", ref), detail
    assert detail["params"]["runs"] == [{"id": order, "ref": ref}], detail


async def test_a_lot_counted_to_zero_and_back_is_refused(client, session, auth):
    await _seed(session, auth)
    doc, lot = await _shared_lot(client, auth)
    [part] = await _split(client, auth, lot, 10)
    for qty in (0, 4):
        r = await client.post(f"/items/{part}/adjust", headers=auth["headers"], json={"new_qty": qty})
        assert r.status_code == 200, r.text
    detail = await _refused(client, session, auth, doc, lot, part)
    assert detail["params"]["lots"][0]["message_key"] == "docs.revert_folded_counted", detail


async def test_two_holders_with_other_stock_are_not_refused(client, session, auth):
    """Parts of 5 split off twice: one stays, one merges with 5 other units and 3 of those 10 are
    sold, taking 3/10 of its 7.50 of freight. The revert takes 2.25 back off cost of goods sold."""
    await _seed(session, auth)
    doc, lot = await _shared_lot(client, auth)
    s, part = await _split(client, auth, lot, 5, 5)
    x = await _merge(client, auth, [part, await _like(client, session, auth, lot, 5, 50.0)])
    await _sell(client, auth, x, 3)
    assert await _pools(session, auth, doc, s, x) == [7.5, 5.25]
    before = await _b(session, auth, "5100")
    await _send_back_and_revert(client, auth, doc, lot)
    assert await _pools(session, auth, doc, s, x) == [0.0, 0.0]
    assert round((await _b(session, auth, "5100"))["5100"] - before["5100"], 2) == -2.25
    await assert_settled(client, session, auth)


async def test_a_supplier_credit_note_for_refused_freight_leaves_it_on_freight_clearing(client, session, auth):
    """The remedy the refusal names when the merge can no longer be undone: a supplier credit
    note for the bill's freight. The freight already in the stock's cost stays there, so the note
    leaves freight clearing 30 in credit; a later change that clears it turns this red."""
    await _seed(session, auth)
    doc, lot = await _shared_lot(client, auth)
    [part] = await _split(client, auth, lot, 10)
    x = await _merge(client, auth, [part, await _like(client, session, auth, lot, 5, 50.0)])
    await _as_an_earlier_release_merged_it(session, auth, x)
    await _refused(client, session, auth, doc, lot, part)
    before = await _b(session, auth, "1130-FRT")
    # The bill carried its freight as shipping; the note takes it back the same way (a note needs a line).
    note = await _doc(client, auth, "debit_note", [{"name": "Freight", "quantity": 1, "unit_price": 0.0}],
                      original_doc_id=doc, shipping=FREIGHT)
    await _finalize(client, auth, note)
    assert round((await _b(session, auth, "1130-FRT"))["1130-FRT"] - before["1130-FRT"], 2) == -FREIGHT


async def _freight_item(client, auth) -> str:
    r = await client.post("/items", headers=auth["headers"], json={
        "status": "available", "sku": "FRT", "name": "Freight", "quantity": 0, "sell_by": "piece",
        "inventory_type": "freight", "landed_cost_kind": "freight"})
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def test_a_bill_whose_goods_all_went_back_reverts_as_before(client, session, auth):
    """A bill (not from an order) of 10 units with a freight line, received into a lot of its own,
    every unit sent back before anything else happened to them: the revert writes as it did."""
    await _seed(session, auth)
    bill = await _doc(client, auth, "bill", [
        {"sku": "GOODS", "name": "Goods", "quantity": 10, "unit_price": 10.0},
        {"entity_id": await _freight_item(client, auth), "sku": "FRT", "name": "Freight", "quantity": 1,
         "unit_price": FREIGHT}])
    await _finalize(client, auth, bill)
    r = await _receive(client, auth, bill, {"po_line_index": 0, "sku": "GOODS", "name": "Goods",
                                            "quantity_received": 10})
    assert r.status_code == 200, r.text
    [parcel] = (await _state(session, auth, bill))["received_item_ids"]
    assert await _pools(session, auth, bill, parcel) == [FREIGHT]
    r = await _return(client, auth, bill, parcel, 10)
    assert r.status_code == 200, r.text
    assert (await _post(client, auth, bill, "revert-to-draft")).status_code == 200
    assert await _pools(session, auth, bill, parcel) == [0.0]
    assert await _b(session, auth, "1130-FRT", "1130-P") == {"1130-FRT": 0.0, "1130-P": 0.0}
    await assert_settled(client, session, auth)
