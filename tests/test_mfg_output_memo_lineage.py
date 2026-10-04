# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Output of an open run sent out on memo keeps a cost completion can still change.

A lot sent out on memo still holds its goods and its cost, so completion re-costs it like
stock on hand. Converting the memo to an invoice sells the lot without an invoice line
that shipped it, so no later cost change can reach that sale: while the run is open the
conversion is refused with a message saying why. Taken back from the memo, or converted
after the run completes, the lot goes on like any other. Every completion here re-costs
the output (25.00 a unit down to 20.00), and supply stays what is on hand plus what each
open run still has to receive.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from celerp.models.projections import Projection
from celerp_manufacturing.routes import _all_item_states, _in_progress_by_item, _stock_by_product
from mfg_runs import refusal, snapshot
from stock_books import assert_settled
from test_cost_restatement import _sell, _state, auth, ids  # noqa: F401  (fixtures)
from test_mfg_output_lineage import completes_recosted, open_output
from test_mfg_output_lineage_legacy import older_release_rules

pytestmark = pytest.mark.asyncio


async def _cost(session, auth, item: str) -> float:
    return float((await _state(session, auth, item))["cost_total"])


async def _supply(session, auth, made: str) -> tuple[float, float]:
    """(on hand, still to come from open runs) for the product, as Demand Planning counts them."""
    session.expire_all()
    cid = auth["company_id"]
    runs = (await session.execute(select(Projection).where(
        Projection.company_id == cid, Projection.entity_type == "mfg_order"))).scalars().all()
    free, _held = _stock_by_product(await _all_item_states(session, cid))
    on_hand = free.get(made, 0.0)
    return on_hand, _in_progress_by_item(runs).get(made, 0.0)


async def _memo_out(client, session, auth, lot: str) -> str:
    """A memo for the whole lot, issued and sent out."""
    state = await _state(session, auth, lot)
    r = await client.post("/docs", headers=auth["headers"], json={"doc_type": "memo", "line_items": [
        {"entity_id": lot, "sku": state["sku"], "name": state["sku"], "quantity": state["quantity"],
         "unit_price": 150.0, "sell_by": "piece"}]})
    assert r.status_code == 200, r.text
    memo = r.json()["id"]
    assert (await client.post(f"/docs/{memo}/finalize", headers=auth["headers"])).status_code == 200
    r = await client.post(f"/docs/{memo}/fulfill-lines", headers=auth["headers"], json={"line_entity_ids": [lot]})
    assert r.status_code == 200, r.text
    assert (await _state(session, auth, lot))["status"] == "memo_out"
    return memo


async def _convert(client, auth, memo: str):
    return await client.post(f"/docs/{memo}/convert", headers=auth["headers"])


async def _bill(client, session, auth, memo: str, lot: str) -> None:
    """Convert the memo and finalize the invoice it makes, which books the sale."""
    r = await _convert(client, auth, memo)
    assert r.status_code == 200, r.text
    r = await client.post(f"/docs/{r.json()['target_doc_id']}/finalize", headers=auth["headers"])
    assert r.status_code == 200, r.text
    assert (await _state(session, auth, lot))["status"] == "sold"
    await assert_settled(client, session, auth)


async def test_whole_invoice_sale_is_recosted_at_completion(client, session, auth):
    made, order, lot = await open_output(client, session, auth)
    await _sell(client, session, auth, lot)
    assert await _supply(session, auth, made) == (0.0, 2.0)

    await completes_recosted(client, session, auth, order)

    assert await _cost(session, auth, lot) == 40.0
    assert await _supply(session, auth, made) == (2.0, 0.0)


async def test_converting_a_memo_of_open_output_waits_for_completion(client, session, auth):
    made, order, lot = await open_output(client, session, auth)
    memo = await _memo_out(client, session, auth, lot)
    assert await _supply(session, auth, made) == (0.0, 2.0)

    before = await snapshot(session, auth, order, lot, memo)
    detail = refusal(await _convert(client, auth, memo), 409, "output_memo_conversion")
    await session.rollback()
    assert detail["message"] == ("Complete this production run before converting this memo to an invoice. "
                                 "Its finished-goods cost is not final yet."), detail
    assert await snapshot(session, auth, order, lot, memo) == before

    # Still out on memo, the lot takes its final cost; the memo then converts.
    await completes_recosted(client, session, auth, order)
    assert await _cost(session, auth, lot) == 40.0
    assert (await _state(session, auth, lot))["status"] == "memo_out"
    await _bill(client, session, auth, memo, lot)
    assert await _supply(session, auth, made) == (2.0, 0.0)


async def test_taken_back_from_memo_it_is_recosted_at_completion(client, session, auth):
    made, order, lot = await open_output(client, session, auth)
    memo = await _memo_out(client, session, auth, lot)
    r = await client.post(f"/docs/{memo}/revert-lines", headers=auth["headers"], json={"line_entity_ids": [lot]})
    assert r.status_code == 200, r.text
    assert (await _state(session, auth, lot))["status"] == "available"
    assert await _supply(session, auth, made) == (2.0, 2.0)

    await completes_recosted(client, session, auth, order)

    assert await _cost(session, auth, lot) == 40.0
    assert await _supply(session, auth, made) == (4.0, 0.0)


async def test_output_of_a_completed_run_converts_from_memo(client, session, auth):
    made, order, lot = await open_output(client, session, auth)
    await completes_recosted(client, session, auth, order)
    assert await _cost(session, auth, lot) == 40.0
    memo = await _memo_out(client, session, auth, lot)

    await _bill(client, session, auth, memo, lot)

    assert await _supply(session, auth, made) == (2.0, 0.0)


async def test_a_conversion_already_recorded_still_replays(client, session, auth):
    """The hold applies to changes made now; a conversion already on the ledger rebuilds."""
    from celerp.projections.engine import ProjectionEngine

    _, order, lot = await open_output(client, session, auth)
    memo = await _memo_out(client, session, auth, lot)
    with older_release_rules():
        assert (await _convert(client, auth, memo)).status_code == 200

    await ProjectionEngine.rebuild(session, auth["company_id"])
    await session.commit()

    session.expire_all()
    assert (await _state(session, auth, lot))["status"] == "sold"
    assert (await _state(session, auth, order))["status"] != "completed"


async def test_a_return_of_open_output_sold_waits_for_completion(client, session, auth):
    """Goods back on a credit note come in at the cost of the lot that was sold; while the run
    is open that cost is not final, so the return waits and then comes back at 20.00 a unit."""
    made, order, lot = await open_output(client, session, auth)
    sku = (await _state(session, auth, lot))["sku"]
    invoice = await _sell(client, session, auth, lot)
    r = await client.post("/docs", headers=auth["headers"], json={
        "doc_type": "credit_note", "original_doc_id": invoice, "total": 500, "subtotal": 500, "tax": 0,
        "line_items": [{"name": "Lot", "sku": sku, "quantity": 2, "unit_price": 250, "sell_by": "piece"}]})
    assert r.status_code == 200, r.text
    note = r.json()["id"]
    assert (await client.post(f"/docs/{note}/finalize", headers=auth["headers"])).status_code == 200

    def back():
        return client.post(f"/docs/{note}/receive-return", headers=auth["headers"],
                           json={"items": [{"sku": sku, "quantity": 2, "item_id": lot}]})

    before = await snapshot(session, auth, order, lot, note)
    detail = refusal(await back(), 409, "output_cost_pending")
    await session.rollback()
    assert sku in detail["message"] and "still open" in detail["message"], detail
    assert await snapshot(session, auth, order, lot, note) == before
    assert await _supply(session, auth, made) == (0.0, 2.0)

    await completes_recosted(client, session, auth, order)
    r = await back()
    assert r.status_code == 200, r.text
    assert r.json()["total_cogs_reversed"] == 40.0
    await assert_settled(client, session, auth)
    assert await _supply(session, auth, made) == (4.0, 0.0)
