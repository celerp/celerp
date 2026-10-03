# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Demand Planning's Make selected under real concurrent transactions.

Two Make selected for the same short demand line arrive together, from two people or as
one action sent twice. The second waits for the first, then judges the shortfall from what
the first made: the demand is made once, never twice.
"""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

from celerp.models.projections import Projection
from company_backup_support import owner, token
from migration_support import maker
from test_mfg_wip_race_pg import _attempt, _job, _settle
from test_posting_roles_race_pg import _until_blocked
from test_posting_roles_race_pg_origin import _post, own_client  # noqa: F401  (own_client is a fixture)

pytestmark = pytest.mark.asyncio


def _make(doc: str, operation: str):
    from celerp_manufacturing import routes

    async def go(s, job):
        return await routes.make_selected(s, job["cid"], job["user"],
                                          [routes.WorkOrderLineRef(item_id=job["product"], doc_id=doc)],
                                          False, operation)
    return go


async def _runs(engine, job) -> list[tuple[float, str]]:
    async with maker(engine)() as s:
        rows = (await s.execute(select(Projection).where(
            Projection.company_id == job["cid"], Projection.entity_type == "mfg_order"))).scalars().all()
    return sorted((float(r.state["expected_outputs"][0]["quantity"]), r.state.get("source_doc_id")) for r in rows)


@pytest.mark.parametrize("flip", [False, True], ids=["ab", "ba"])
@pytest.mark.parametrize("same_action", [False, True], ids=["two-people", "one-action-sent-twice"])
async def test_two_make_selected_at_once_make_the_shortfall_once(committed_engine, own_client, same_action, flip):
    user = await owner(committed_engine)
    job = await _job(committed_engine, own_client, user, 1, runs=0)
    tok = await token(committed_engine, user, job["cid"])
    doc = (await _post(own_client, tok, "/docs", {"doc_type": "invoice", "total": 5, "line_items": [
        {"item_id": job["product"], "sku": "FG-1", "name": "Made", "quantity": 5, "unit_price": 1}]}))["id"]
    await _post(own_client, tok, f"/docs/{doc}/finalize")
    names = ("a", "a" if same_action else "b")
    first, second = _make(doc, names[1 if flip else 0]), _make(doc, names[0 if flip else 1])

    async with maker(committed_engine)() as s1, maker(committed_engine)() as s2:
        held = await _attempt(s1, first, job)

        async def run():
            out = await _attempt(s2, second, job)
            await _settle(s2, out)
            return out

        task = asyncio.create_task(run())
        await _until_blocked(committed_engine, task)
        await _settle(s1, held)
        out = await asyncio.wait_for(task, timeout=30)

    assert held["created"][0]["quantity"] == 5.0
    if same_action:
        assert out == held
    else:
        assert (out["created"], out["skipped"]) == ([], [{"item_id": job["product"], "doc_id": doc,
                                                          "reason": "nothing to make"}])
    assert await _runs(committed_engine, job) == [(5.0, doc)]
