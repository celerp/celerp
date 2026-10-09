# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Taking goods back on an invoice issued before invoices kept their cost allocation.

Such an invoice booked its cost of goods sold in one entry: at fulfilment, or later
by the one-time backfill. Goods it takes back return to stock carrying their cost, so
the invoice gives back that lot's share of the entry exactly once, and a lot sold
again elsewhere is costed once in total. When the share cannot be worked out from
what was posted, the take-back is refused and nothing changes.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from celerp.models.projections import Projection
from gl_support import gl_totals
from test_sold_cost_correction import (
    _new_company, _set_backfill_marker, cogs_backfill, doc_action, fulfil, invoice, legacy_fulfil,
    legacy_invoice, make, revert_lines,
)

pytestmark = pytest.mark.asyncio

COGS = "5100"


def _inventory(gl: dict[str, float]) -> float:
    return round(sum(v for code, v in gl.items() if code.startswith("1130")), 2)


async def _play(client, session, steps) -> tuple[dict, dict, dict[str, float]]:
    auth = await _new_company(session)
    ctx: dict = {}
    try:
        for step in steps:
            await step(client, session, auth, ctx)
    finally:
        await _set_backfill_marker(session)
    return auth, ctx, await gl_totals(session, auth["company_id"])


def set_available(*names: str, doc: str = "doc"):
    async def step(client, session, auth, ctx):
        r = await client.post(f"/docs/{ctx[doc]}/set-available", headers=auth["headers"],
                              json={"line_entity_ids": [ctx[n] for n in names]})
        assert r.status_code == 200, r.text
    return step


BACKFILLED = [make("a", 100.0), legacy_invoice("a"), fulfil("a"), cogs_backfill()]
FULFILMENT_BOOKED = [make("a", 100.0), legacy_invoice("a"), legacy_fulfil("a")]


@pytest.mark.parametrize("history", ["backfilled", "fulfilment_booked"])
async def test_goods_taken_back_and_sold_again_are_costed_once(client, session, history):
    start = BACKFILLED if history == "backfilled" else FULFILMENT_BOOKED
    auth, ctx, gl = await _play(client, session, [
        *start, revert_lines("a"), invoice("a", doc="B"), fulfil("a", doc="B")])
    assert gl[COGS] == 100.0
    assert COGS not in await gl_totals(session, auth["company_id"], entry_id_part=f":{ctx['doc']}:")
    assert _inventory(gl) == 0.0


@pytest.mark.parametrize("history", ["backfilled", "fulfilment_booked"])
async def test_set_as_available_gives_the_cost_back(client, session, history):
    start = BACKFILLED if history == "backfilled" else FULFILMENT_BOOKED
    _, _, gl = await _play(client, session, [*start, set_available("a")])
    assert gl.get(COGS) is None
    assert _inventory(gl) == 100.0


@pytest.mark.parametrize("history", ["backfilled", "fulfilment_booked"])
async def test_goods_taken_back_and_shipped_again_on_the_same_invoice(client, session, history):
    start = BACKFILLED if history == "backfilled" else FULFILMENT_BOOKED
    _, _, gl = await _play(client, session, [*start, revert_lines("a"), fulfil("a")])
    assert gl[COGS] == 100.0
    assert _inventory(gl) == 0.0


async def test_void_after_the_take_back_leaves_one_sale(client, session):
    auth, ctx, gl = await _play(client, session, [
        *BACKFILLED, revert_lines("a"), invoice("a", doc="B"), fulfil("a", doc="B"), doc_action("void")])
    assert gl[COGS] == 100.0
    await doc_action("unvoid")(client, session, auth, ctx)
    assert (await gl_totals(session, auth["company_id"]))[COGS] == 100.0


async def test_modern_invoice_control(client, session):
    _, _, gl = await _play(client, session, [
        make("a", 100.0), invoice("a"), fulfil("a"), revert_lines("a"), invoice("a", doc="B"), fulfil("a", doc="B")])
    assert gl[COGS] == 100.0
    assert _inventory(gl) == 0.0


async def test_share_that_cannot_be_worked_out_refuses_and_changes_nothing(client, session):
    """The lot's cost changed after its sale was booked, by a release that recorded no
    adjustment, so what the entry holds no longer matches the goods."""
    auth, ctx, before = await _play(client, session, FULFILMENT_BOOKED)
    row = await session.get(Projection, {"company_id": auth["company_id"], "entity_id": ctx["a"]})
    row.state = {**row.state, "cost_total": 130.0}
    await session.commit()
    r = await client.post(f"/docs/{ctx['doc']}/revert-lines", headers=auth["headers"],
                          json={"line_entity_ids": [ctx["a"]]})
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["message_key"] == "documents.cogs_share_unknown"
    # The request's own session ends uncommitted; the test client shares this one.
    await session.rollback()
    lot = (await session.execute(select(Projection).where(
        Projection.company_id == auth["company_id"], Projection.entity_id == ctx["a"]))).scalars().one()
    assert (lot.state["status"], lot.state.get("status_doc_id")) == ("sold", ctx["doc"])
    assert await gl_totals(session, auth["company_id"]) == before
