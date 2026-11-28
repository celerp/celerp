# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Runs with extras on a company holding its demo records: rebuilding every record from the
ledger gives back exactly what was stored, and the books carry the stock and work in progress.

The company is set up the way a user does it (register, pick a business type, load the demo
records), then presses oil with press cake as an extra: one run completed at the shown
quantities, one with the cake quantity entered, one reopened, its cake receipt undone and
completed again, and part of a cake lot sold.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from celerp.models.projections import Projection
from celerp.projections.engine import ProjectionEngine
from celerp.services.company_lock import lock_company
from mfg_runs import complete, reopen, undo_receipt
from stock_books import assert_settled
from test_co_products import lots_of, press, start
from test_cost_restatement import _sell

pytestmark = pytest.mark.asyncio


async def _ok(r) -> dict:
    assert r.status_code == 200, r.text
    return r.json()


async def _demo_company(client) -> dict:
    email = f"press-{uuid.uuid4().hex[:8]}@test.example"
    r = await _ok(await client.post("/auth/register", json={
        "company_name": "Press House", "email": email, "name": "Owner", "password": "pwvalid1"}))
    h = {"Authorization": f"Bearer {r['access_token']}"}
    await _ok(await client.post("/companies/me/business-type", headers=h, json={"vertical": "gemstones"}))
    await _ok(await client.post("/companies/me/demo/reseed", headers=h))
    cid = uuid.UUID((await _ok(await client.get("/companies/me", headers=h)))["id"])
    return {"headers": h, "company_id": cid}


async def _stored(session, company_id) -> dict:
    session.expire_all()
    rows = (await session.execute(select(Projection.entity_id, Projection.entity_type, Projection.state)
                                  .where(Projection.company_id == company_id))).all()
    return {r.entity_id: (r.entity_type, r.state) for r in rows}


async def test_rebuild_equals_stored_after_runs_with_extras(client, session):
    auth = await _demo_company(client)
    demo = await _stored(session, auth["company_id"])
    assert sum(1 for t, _ in demo.values() if t == "item") >= 10
    seed, oil, cake = await press(client, auth)

    shown = await start(client, auth, oil, 10)
    await _ok(await complete(client, auth, shown, key="shown"))

    entered = await start(client, auth, oil, 5)
    await _ok(await complete(client, auth, entered, key="entered",
                             also_produces=[{"item_id": cake, "quantity": 7}]))

    again = await start(client, auth, oil, 4)
    await _ok(await complete(client, auth, again, key="again-1"))
    await _ok(await reopen(client, auth, again, key="again-reopen"))
    cake_lot = (await lots_of(session, auth, again))[cake][0]["lot"]
    await _ok(await undo_receipt(client, auth, again, cake_lot, key="again-undo"))
    await _ok(await complete(client, auth, again, key="again-2",
                             also_produces=[{"item_id": cake, "quantity": 6}]))

    await _sell(client, session, auth, (await lots_of(session, auth, entered))[cake][0]["lot"])

    lots = {o: await lots_of(session, auth, o) for o in (shown, entered, again)}
    assert [l["cost"] for l in lots[shown][cake]] == [12.0]
    assert [l["quantity"] for l in lots[again][cake]] == [6.0]

    before = await _stored(session, auth["company_id"])
    assert demo.keys() < before.keys()
    await lock_company(session, auth["company_id"])
    await ProjectionEngine.rebuild(session, auth["company_id"])
    await session.commit()
    after = await _stored(session, auth["company_id"])
    assert after.keys() == before.keys()
    assert {k: v for k, v in after.items() if v != before[k]} == {}
    await assert_settled(client, session, auth)
