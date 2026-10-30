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
