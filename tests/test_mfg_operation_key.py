# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""One request key is one action, with one answer.

Make selected and the runs list's bulk actions each act on many lines or runs at once. Sent
again with the same key (its answer lost, say), the action gives back the answer it first
gave and changes nothing, even when a line or run it passed over could now be acted on. The
same key sent with a different selection is refused. Concurrent requests are covered in
test_mfg_operation_key_race_pg.
"""
from __future__ import annotations

import pytest

from mfg_runs import product, refusal, run
from test_cost_restatement import _item, _state
from test_mfg_creation_contract import _count
from test_mfg_make_selected import _demand

pytestmark = pytest.mark.asyncio


async def _make(client, auth, lines: list[tuple[str, str]], key: str):
    return await client.post("/manufacturing/to-make/make", headers=auth["headers"], json={
        "lines": [{"item_id": i, "doc_id": d} for i, d in lines], "idempotency_key": key})


async def _bulk(client, auth, runs: list[str], action: str, key: str):
    return await client.post("/manufacturing/bulk-action", headers=auth["headers"], json={
        "run_ids": runs, "action": action, "idempotency_key": key})


async def _made_for(session, auth, item_id: str) -> int:
    from sqlalchemy import select
    from celerp.models.projections import Projection
    session.expire_all()
    rows = (await session.execute(select(Projection.state).where(
        Projection.company_id == auth["company_id"], Projection.entity_type == "mfg_order"))).scalars().all()
    return sum(1 for st in rows if st.get("output_item_id") == item_id)


async def _two_products(client, auth) -> tuple[str, str, str]:
    """One product short on an order, and one made to stock that nothing yet asks for."""
    raw = await _item(client, auth, 100.0, qty=100)
    short = await product(client, auth, [(raw, 1)])
    stock = await product(client, auth, [(raw, 1)])
    return short, stock, await _demand(client, auth, short, 5, "2026-05-01")


async def test_make_selected_retry_never_reevaluates_a_skipped_line(client, session, auth):
    """The answer is lost; an order now asks for the product the action passed over; the
    action sent again gives back its first answer and makes nothing for that product."""
    short, stock, order = await _two_products(client, auth)
    first = await _make(client, auth, [(short, order), (stock, "")], "op")
    assert first.status_code == 200, first.text
    assert [s["item_id"] for s in first.json()["skipped"]] == [stock]
    await _demand(client, auth, stock, 3, "2026-05-02")

    again = await _make(client, auth, [(short, order), (stock, "")], "op")

    assert again.status_code == 200, again.text
    assert again.json() == first.json()
    assert await _made_for(session, auth, stock) == 0
    assert await _count(session, auth, entity_type="mfg_order") == 1


async def test_make_selected_same_key_other_selection_is_refused(client, session, auth):
    short, stock, order = await _two_products(client, auth)
    assert (await _make(client, auth, [(short, order)], "op")).status_code == 200
    await _demand(client, auth, stock, 3, "2026-05-02")

    refusal(await _make(client, auth, [(short, order), (stock, "")], "op"), 409, "key_reused")

    assert await _made_for(session, auth, stock) == 0


async def test_bulk_retry_never_reevaluates_a_skipped_run(client, session, auth):
    """Resume passes over a planned run; the run is then put on hold; resume sent again gives
    back its first answer and leaves that run on hold."""
    raw = await _item(client, auth, 100.0, qty=100)
    made = await product(client, auth, [(raw, 1)])
    held, planned = await run(client, auth, made, 1), await run(client, auth, made, 1)
    assert (await _bulk(client, auth, [held], "hold", "h1")).status_code == 200
    first = await _bulk(client, auth, [held, planned], "resume", "op")
    assert first.status_code == 200, first.text
    assert first.json()["done"] == [held] and [s["id"] for s in first.json()["skipped"]] == [planned]
    assert (await _bulk(client, auth, [planned], "hold", "h2")).status_code == 200

    again = await _bulk(client, auth, [held, planned], "resume", "op")

    assert again.status_code == 200, again.text
    assert again.json() == first.json()
    assert (await _state(session, auth, planned))["status"] == "on_hold"


async def test_bulk_same_key_other_selection_is_refused(client, session, auth):
    raw = await _item(client, auth, 100.0, qty=100)
    made = await product(client, auth, [(raw, 1)])
    one, two = await run(client, auth, made, 1), await run(client, auth, made, 1)
    assert (await _bulk(client, auth, [one], "hold", "op")).status_code == 200

    refusal(await _bulk(client, auth, [one, two], "hold", "op"), 409, "key_reused")
    refusal(await _bulk(client, auth, [one], "cancel", "op"), 409, "key_reused")

    assert (await _state(session, auth, two))["status"] == "planned"
    assert (await _state(session, auth, one))["status"] == "on_hold"


async def test_one_key_is_never_two_kinds_of_action(client, session, auth):
    short, _stock, order = await _two_products(client, auth)
    assert (await _make(client, auth, [(short, order)], "op")).status_code == 200
    raw = await _item(client, auth, 100.0, qty=100)
    planned = await run(client, auth, await product(client, auth, [(raw, 1)]), 1)

    refusal(await _bulk(client, auth, [planned], "hold", "op"), 409, "key_reused")

    assert (await _state(session, auth, planned))["status"] == "planned"
