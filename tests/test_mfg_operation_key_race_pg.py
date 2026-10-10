# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""One request key is one action, under real concurrent transactions.

Two Make selected or two bulk actions arrive together with the same key: the same action
sent twice gives one answer, and the key sent with another selection is refused whichever
of the two arrives first. Nothing is made or changed beyond what the action that ran asked for.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import text

from celerp.services.company_lock import lock_company
from company_backup_support import owner, token
from migration_support import maker
from test_mfg_make_selected_race_pg import _runs
from test_mfg_wip_race_pg import _job
from test_posting_roles_race_pg_origin import _post, own_client  # noqa: F401  (own_client is a fixture)

pytestmark = pytest.mark.asyncio


async def _waiting(engine) -> int:
    async with engine.connect() as conn:
        return (await conn.execute(text(
            "SELECT count(*) FROM pg_stat_activity "
            "WHERE datname = current_database() AND wait_event_type = 'Lock'"))).scalar_one()


async def _together(engine, job, *calls):
    """Hold the company lock, send each call in its own transaction until all of them wait
    for it, then let them run; returns each call's answer or refusal."""
    async def one(call):
        async with maker(engine)() as s:
            try:
                return await call(s)
            except HTTPException as exc:
                await s.rollback()
                return exc

    async with maker(engine)() as gate:
        await lock_company(gate, job["cid"])
        tasks = []
        for n, call in enumerate(calls, start=1):
            tasks.append(asyncio.create_task(one(call)))
            for _ in range(400):
                assert not tasks[-1].done(), f"the request did not wait: {tasks[-1].result()!r}"
                if await _waiting(engine) >= n:
                    break
                await asyncio.sleep(0.05)
            else:
                raise AssertionError("the request never waited")
        await gate.rollback()
    return await asyncio.wait_for(asyncio.gather(*tasks), timeout=30)


def _make(job, lines, key):
    from celerp_manufacturing import routes

    def call(s):
        body = routes.MakeWorkOrdersBody(lines=[routes.WorkOrderLineRef(item_id=i, doc_id=d) for i, d in lines],
                                         idempotency_key=key)
        return routes.make_work_orders(body, company_id=job["cid"], user=SimpleNamespace(id=job["user"]),
                                       _=None, session=s)
    return call


def _bulk(job, runs, action, key):
    from celerp_manufacturing import routes

    def call(s):
        body = routes.BulkRunActionBody(run_ids=runs, action=action, idempotency_key=key)
        return routes.bulk_run_action(body, company_id=job["cid"], user=SimpleNamespace(id=job["user"]),
                                      _=None, session=s)
    return call


def _one_refused(answers) -> dict:
    refused = [a for a in answers if isinstance(a, HTTPException)]
    assert len(refused) == 1, answers
    assert refused[0].status_code == 409 and refused[0].detail["message_key"] == "mfg.key_reused"
    return next(a for a in answers if not isinstance(a, HTTPException))


async def _setup(engine, client, n: int, runs: int) -> tuple[dict, str]:
    user = await owner(engine)
    job = await _job(engine, client, user, n, runs=runs, stock=100.0)
    tok = await token(engine, user, job["cid"])
    doc = (await _post(client, tok, "/docs", {"doc_type": "invoice", "total": 5, "line_items": [
        {"item_id": job["product"], "sku": f"FG-{n}", "name": "Made", "quantity": 5, "unit_price": 1}]}))["id"]
    await _post(client, tok, f"/docs/{doc}/finalize")
    return job, doc


async def test_make_selected_sent_twice_at_once_gives_one_answer(committed_engine, own_client):
    job, doc = await _setup(committed_engine, own_client, 1, 0)

    a, b = await _together(committed_engine, job, _make(job, [(job["product"], doc)], "op"),
                           _make(job, [(job["product"], doc)], "op"))

    assert a == b and len(a["created"]) == 1
    assert await _runs(committed_engine, job) == [(5.0, doc)]


@pytest.mark.parametrize("wider_first", [False, True], ids=["narrow-first", "wider-first"])
async def test_make_selected_key_with_another_selection_at_once_is_refused(committed_engine, own_client,
                                                                          wider_first):
    job, doc = await _setup(committed_engine, own_client, 2, 0)
    narrow, wider = [(job["product"], doc)], [(job["product"], doc), (job["product"], "")]
    calls = (_make(job, wider, "op"), _make(job, narrow, "op")) if wider_first else \
        (_make(job, narrow, "op"), _make(job, wider, "op"))

    ran = _one_refused(await _together(committed_engine, job, *calls))

    assert len(ran["created"]) == 1
    assert await _runs(committed_engine, job) == [(5.0, doc)]


@pytest.mark.parametrize("wider_first", [False, True], ids=["narrow-first", "wider-first"])
async def test_bulk_action_key_with_another_selection_at_once_is_refused(committed_engine, own_client, wider_first):
    job, _doc = await _setup(committed_engine, own_client, 3, 2)
    one, two = job["orders"]
    calls = (_bulk(job, [one, two], "hold", "op"), _bulk(job, [one], "hold", "op")) if wider_first else \
        (_bulk(job, [one], "hold", "op"), _bulk(job, [one, two], "hold", "op"))

    ran = _one_refused(await _together(committed_engine, job, *calls))

    async with maker(committed_engine)() as s:
        from celerp.models.projections import Projection
        status = {o: (await s.get(Projection, {"company_id": job["cid"], "entity_id": o})).state["status"]
                  for o in (one, two)}
    assert status == {o: ("on_hold" if o in ran["done"] else "planned") for o in (one, two)}
    assert ran["done"] == ([one, two] if wider_first else [one])
