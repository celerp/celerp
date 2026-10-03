# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Production run movements under real concurrent transactions.

Each case holds one movement's transaction open after it has done all its work, starts a
second movement on its own connection, waits until Postgres reports the second blocked on
a lock, then lets the first commit (or roll back, when it was refused). The only
acceptable outcome is the one the same two movements produce run one after the other, in
that order, on a company of their own: the same answers, the same run, the same stock,
the same lots and the same books, with the stock and work in progress oracles holding.
Both orders of every pair are run.
"""
from __future__ import annotations

import asyncio
from collections import Counter
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from celerp.models.ledger import LedgerEntry
from celerp.models.projections import Projection
from company_backup_support import company, owner, token
from migration_support import auth, maker
from stock_books import assert_books_carry_stock, assert_wip_carried
from test_posting_roles_race_pg import _until_blocked
from test_posting_roles_race_pg_origin import _net, _post, own_client  # noqa: F401  (own_client is a fixture)

pytestmark = pytest.mark.asyncio

AT = "2026-03-02T10:00:00+00:00"


async def _job(engine, client, user, n: int, *, runs: int = 1, stock: float = 10.0) -> dict:
    """A company of its own: one component lot (``stock`` units costing 100), a product
    taking 5 of it each, and ``runs`` runs making 2 of it."""
    from test_helpers import provision_company_books

    cid = await company(engine, user, f"Run Race {n}", f"run-race-{n}", settings={"currency": "USD"})
    async with maker(engine)() as s:
        await provision_company_books(s, cid)
        await s.commit()
    tok = await token(engine, user, cid)
    raw = (await _post(client, tok, "/items", {"sku": f"RAW-{n}", "name": "Raw", "quantity": stock,
                                                "sell_by": "piece", "status": "available", "cost_total": 100.0}))["id"]
    product = (await _post(client, tok, "/items", {"sku": f"FG-{n}", "name": "Made", "quantity": 0,
                                                    "sell_by": "piece", "status": "available", "cost_total": 0.0}))["id"]
    r = await client.put(f"/manufacturing/items/{product}/recipe", headers=auth(tok), json={
        "output_qty": 1, "components": [{"item_id": raw, "quantity": 5}], "labor": [], "overhead": []})
    assert r.status_code == 200, r.text
    orders = [(await _post(client, tok, f"/manufacturing/items/{product}/build", {"quantity": 2}))["id"]
              for _ in range(runs)]
    return {"cid": cid, "user": user, "raw": raw, "product": product, "orders": orders}


def _op(name: str, tag: str, run: int = 0):
    """Movement ``name`` on run ``run`` of a job, with its own request key."""
    from celerp_manufacturing import movements

    async def go(s, job):
        order, key = job["orders"][run], f"race-{name}-{tag}"
        if name == "issue":
            return await movements.issue(s, job["cid"], job["user"], order, None, key, at=AT)
        if name == "receive":
            return await movements.receive(s, job["cid"], job["user"], order, 1.0, key, at=AT)
        if name == "complete":
            return await movements.complete(s, job["cid"], job["user"], order, {}, key, at=AT)
        return await movements.cancel(s, job["cid"], job["user"], order, None, key, at=AT)
    return go


def _answer(out) -> str:
    if isinstance(out, HTTPException):
        detail = out.detail if isinstance(out.detail, dict) else {}
        return f"refused {out.status_code} {detail.get('message_key', out.detail)}"
    assert not isinstance(out, BaseException), repr(out)
    return "done"


async def _attempt(s, op, job):
    try:
        return await op(s, job)
    except HTTPException as exc:
        return exc


async def _settle(s, out) -> None:
    await (s.rollback() if isinstance(out, HTTPException) else s.commit())


async def _raced(engine, job, first, second) -> tuple[str, str]:
    """``first`` holds its transaction open; ``second`` runs until it blocks; then ``first``
    ends and ``second`` finishes."""
    async with maker(engine)() as s1, maker(engine)() as s2:
        held = await _attempt(s1, first, job)

        async def run():
            out = await _attempt(s2, second, job)
            await _settle(s2, out)
            return out

        task = asyncio.create_task(run())
        await _until_blocked(engine, task)
        await _settle(s1, held)
        out = await asyncio.wait_for(task, timeout=30)
    return _answer(held), _answer(out)


async def _serial(engine, job, first, second) -> tuple[str, str]:
    answers = []
    for op in (first, second):
        async with maker(engine)() as s:
            out = await _attempt(s, op, job)
            await _settle(s, out)
        answers.append(_answer(out))
    return tuple(answers)


async def _outcome(engine, job) -> dict:
    """Everything the two movements decide, free of ids: each run, the component, the lots
    made, the books and the events written."""
    cid = job["cid"]
    async with maker(engine)() as s:
        rows = {r.entity_id: r.state or {} for r in (await s.execute(select(Projection).where(
            Projection.company_id == cid, Projection.entity_type.in_(("item", "mfg_order"))))).scalars()}
        events = Counter((await s.execute(select(LedgerEntry.event_type).where(
            LedgerEntry.company_id == cid))).scalars())
        # company() seeds item:1 as a marker with no stock; it is no lot.
        books = await assert_books_carry_stock(s, cid, unplaced=("item:1",))
        wip = await assert_wip_carried(s, cid)
    runs = [{k: rows[o].get(k) for k in ("status", "received_qty", "wip_issued", "wip_transferred", "wip_wasted")}
            | {"issued": [float(i.get("issued_qty") or 0) for i in rows[o].get("inputs", [])],
               "lots": sorted((float(rows[x].get("quantity") or 0), float(rows[x].get("cost_total") or 0))
                              for x in rows[o].get("received_lots") or [])}
            for o in job["orders"]]
    raw, product = rows[job["raw"]], rows[job["product"]]
    return {"runs": runs, "raw": (raw.get("quantity"), raw.get("cost_total")),
            "product": product.get("quantity"), "events": dict(events),
            "books": {k: v for k, v in (books | wip).items() if v != Decimal("0")}}


async def _prepare(engine, client, job, steps) -> None:
    for name in steps:
        async with maker(engine)() as s:
            await _op(name, "prep")(s, job)
            await s.commit()


# The run each pair starts from: as built, or with every component issued.
PAIRS = [
    ("issue", "issue", ()),
    ("issue", "receive", ()),
    ("receive", "receive", ("issue",)),
    ("receive", "complete", ("issue",)),
    ("complete", "complete", ()),
    ("cancel", "issue", ()),
    ("cancel", "receive", ()),
    ("cancel", "complete", ()),
    ("cancel", "cancel", ()),
    ("cancel", "receive", ("issue",)),
]


@pytest.mark.parametrize("a, b, prep", PAIRS, ids=[f"{a}-{b}{'-issued' if p else ''}" for a, b, p in PAIRS])
@pytest.mark.parametrize("flip", [False, True], ids=["ab", "ba"])
async def test_two_movements_on_one_run_end_as_if_one_ran_after_the_other(
        committed_engine, own_client, a, b, prep, flip):
    first, second = (_op(b, "2"), _op(a, "1")) if flip else (_op(a, "1"), _op(b, "2"))
    user = await owner(committed_engine)
    raced, serial = [await _job(committed_engine, own_client, user, n) for n in (1, 2)]
    for job in (raced, serial):
        await _prepare(committed_engine, own_client, job, prep)

    answers = await _raced(committed_engine, raced, first, second)

    assert answers == await _serial(committed_engine, serial, first, second)
    assert await _outcome(committed_engine, raced) == await _outcome(committed_engine, serial)


@pytest.mark.parametrize("flip", [False, True], ids=["first-run-holds", "second-run-holds"])
async def test_two_runs_issuing_the_last_of_a_component_issue_it_once(committed_engine, own_client, flip):
    """Two runs each need all 10 units on hand: whichever issues first takes them and its
    work in progress carries the 100; the other is refused and moves nothing."""
    user = await owner(committed_engine)
    job = await _job(committed_engine, own_client, user, 1, runs=2)
    first, second = (_op("issue", "1", 1), _op("issue", "2", 0)) if flip else (_op("issue", "1", 0),
                                                                                _op("issue", "2", 1))

    assert await _raced(committed_engine, job, first, second) == ("done", "refused 409 mfg.insufficient_stock")

    outcome = await _outcome(committed_engine, job)
    winner, loser = (outcome["runs"][1], outcome["runs"][0]) if flip else outcome["runs"]
    assert (winner["issued"], winner["wip_issued"]) == ([10.0], "100.00")
    assert (loser["issued"], loser.get("wip_issued")) == ([0.0], None)
    assert outcome["raw"][0] == 0
    assert outcome["events"]["item.consumed"] == 1
