# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Taking a bill back to its order undoes its freight exactly, whatever happened to its goods.

Each case generates a history for half of a bill's goods: splits, merges with other stock of the
product, products made from them with their cost kept, sales of part, write-offs and counts down.
The bill's other half then goes back to the supplier and the bill goes back to its order. The same
history is played on a twin bill without freight. After both reverts no lot holds either bill's
freight, every balance sheet account moved exactly as it did for the twin and the expenses moved
by the same total: the freight the lots still held and the freight sold, written-off and returned
units took add up to what the bill capitalised, on each account. The revert takes what left with
units back off cost of goods sold and stock shrinkage, so a write-off's or a count's share may
come back off another expense account than the one it was charged to. A history that folds the freight into a goods cost on the way (a lot counted to zero
and back) has its revert refused, writing nothing."""
from __future__ import annotations

import random
from decimal import Decimal

import pytest
from sqlalchemy import select

from celerp.models.projections import Projection
from mfg_runs import refusal, snapshot
from stock_books import assert_settled
from test_landed_cost_lineage import _doc_pools, _opening
from test_landed_cost_merge_lineage import _merge, _transform
from test_landed_cost_pools import _po_into, _post, _sell
from test_landed_cost_removals import _seed, _split, _writeoff
from test_cost_restatement import _state
from test_receipt_accounting import _return

pytestmark = pytest.mark.asyncio

FREIGHT = 30.0
SEEDS = range(20261031, 20261043)


async def _books(session, cid) -> dict[str, Decimal]:
    session.expire_all()
    out: dict[str, Decimal] = {}
    for p in (await session.execute(select(Projection).where(
            Projection.company_id == cid, Projection.entity_type == "journal_entry"))).scalars():
        if p.state.get("status") == "posted":
            for e in p.state.get("entries") or []:
                out[e["account"]] = (out.get(e["account"], Decimal("0"))
                                     + Decimal(str(e.get("debit") or 0)) - Decimal(str(e.get("credit") or 0)))
    return out


def _moved(before: dict, after: dict) -> dict[str, Decimal]:
    """How each balance sheet account moved, and every expense account (5xxx, 6xxx) as one total."""
    out: dict[str, Decimal] = {}
    for k in before.keys() | after.keys():
        key = "expenses" if k[:1] in ("5", "6") else k
        out[key] = out.get(key, Decimal("0")) + after.get(k, Decimal("0")) - before.get(k, Decimal("0"))
    return {k: d.quantize(Decimal("0.01")) for k, d in out.items() if d.quantize(Decimal("0.01"))}


def _on_hand(state: dict) -> bool:
    return (state.get("status") not in ("merged", "archived", "sold", "disposed")
            and float(state.get("quantity") or 0) > 0)


async def _qty(session, auth, lot: str) -> int:
    return int(float((await _state(session, auth, lot)).get("quantity") or 0))


async def _other(client, session, auth, lot: str) -> str:
    """5 units of the product ``lot`` holds, costing 50.00, from no bill."""
    st = await _state(session, auth, lot)
    r = await client.post("/items", headers=auth["headers"], json={
        "sku": st["sku"], "name": "Other", "quantity": 5, "sell_by": "piece", "status": "available",
        "cost_total": 50.0, **({"category": st["category"]} if st.get("category") else {})})
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _history(client, session, auth, rnd: random.Random, freight: float, *, fold: bool):
    """A bill of 10 units with ``freight`` into a lot of 10 opening units; 10 of the lot are split
    off and go through the history ``rnd`` draws; returns the bill, the lot and every lot made."""
    lot = await _opening(client, auth, 10, 100.0)
    doc = await _po_into(client, auth, lot, 10, 10.0, shipping=freight)
    [held] = await _split(client, auth, lot, 10)
    holders, made = [held], [held]
    steps = rnd.randint(4, 7)
    for _ in range(steps):
        h = rnd.choice(holders)
        n = await _qty(session, auth, h)
        op = rnd.choice(("split", "merge", "transform", "sell", "writeoff", "count") if n >= 2
                        else ("merge", "transform"))
        if op == "split":
            [child] = await _split(client, auth, h, n // 2)
            holders.append(child)
            made.append(child)
        elif op == "merge":
            x = await _merge(client, auth, [h, await _other(client, session, auth, h)])
            holders[holders.index(h)] = x
            made.append(x)
        elif op == "transform":
            p = await _transform(client, auth, h, rnd.randint(2, 4))
            holders[holders.index(h)] = p
            made.append(p)
        elif op == "sell":
            await _sell(client, auth, h, rnd.randint(1, n - 1))
        elif op == "writeoff":
            await _writeoff(client, auth, h, 1)
        else:
            r = await client.post(f"/items/{h}/adjust", headers=auth["headers"], json={"new_qty": n - 1})
            assert r.status_code == 200, r.text
    if fold:
        h = rnd.choice(holders)
        for qty in (0, 2):
            r = await client.post(f"/items/{h}/adjust", headers=auth["headers"], json={"new_qty": qty})
            assert r.status_code == 200, r.text
    r = await _return(client, auth, doc, lot, 10)
    assert r.status_code == 200, r.text
    return doc, lot, made, steps


async def _reverted(client, session, auth, seed: int, freight: float):
    """Play the history of ``seed`` on a bill with ``freight`` and revert it; return how the
    accounts moved from before the bill to after the revert, and the pools the lots held before."""
    cid = auth["company_id"]
    start = await _books(session, cid)
    doc, lot, made, steps = await _history(client, session, auth, random.Random(seed), freight, fold=False)
    held = 0.0
    for x in [lot, *made]:
        if _on_hand(st := await _state(session, auth, x)):
            held += sum(_doc_pools(st, doc).values())
    r = await _post(client, auth, doc, "revert-to-draft")
    assert r.status_code == 200, r.text
    for x in [lot, *made]:
        assert _doc_pools(await _state(session, auth, x), doc) == {}, x
    await assert_settled(client, session, auth)
    return _moved(start, await _books(session, cid)), held, steps


@pytest.mark.parametrize("seed", SEEDS)
async def test_a_revert_undoes_the_freight_on_every_account(client, session, auth, seed):
    await _seed(session, auth)
    plain, _, steps = await _reverted(client, session, auth, seed, 0.0)
    with_freight, held, _ = await _reverted(client, session, auth, seed, FREIGHT)
    assert 0 <= held <= FREIGHT + 0.01
    # A removal rounds the lot's value and its pool to the cent apart: a cent per step at most.
    assert {k: abs(with_freight.get(k, Decimal("0")) - plain.get(k, Decimal("0")))
            for k in with_freight.keys() | plain.keys()
            if abs(with_freight.get(k, Decimal("0")) - plain.get(k, Decimal("0"))) > Decimal("0.01") * steps
            } == {}, (seed, with_freight, plain)


@pytest.mark.parametrize("seed", SEEDS[:4])
async def test_a_history_that_folds_the_freight_is_refused_writing_nothing(client, session, auth, seed):
    await _seed(session, auth)
    doc, lot, made, _ = await _history(client, session, auth, random.Random(seed), FREIGHT, fold=True)
    before = await snapshot(session, auth, doc, lot, *made)
    refusal(await _post(client, auth, doc, "revert-to-draft"), 409, "docs.revert_lineage_folded")
    assert await snapshot(session, auth, doc, lot, *made) == before
