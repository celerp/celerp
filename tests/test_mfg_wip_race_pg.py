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
from test_posting_roles_race_pg_draft import race  # noqa: F401  (a fixture)
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


async def _first_lot(s, job, run: int) -> str:
    """The first lot the run received, as the run stood when the request arrived."""
    row = await s.get(Projection, {"company_id": job["cid"], "entity_id": job["orders"][run]})
    return next(iter((row.state or {}).get("received_lots") or []), "no-lot")


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
        if name == "return":
            return await movements.return_materials(s, job["cid"], job["user"], order, None, key, at=AT)
        if name == "undo":
            return await movements.undo_receipt(s, job["cid"], job["user"], order, await _first_lot(s, job, run),
                                                key, at=AT)
        if name == "reopen":
            return await movements.reopen(s, job["cid"], job["user"], order, key, at=AT)
        if name in ("start", "hold", "resume", "schedule"):
            data = {"due_date": "2026-04-01"} if name == "schedule" else {}
            return await movements.transition(s, job["cid"], job["user"], order, name, data, key, at=AT)
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
    runs = [{k: rows[o].get(k) for k in ("status", "received_qty", "wip_issued", "wip_transferred", "wip_wasted",
                                         "due_date")}
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


# The run each pair starts from: as built, or after the steps named.
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
    ("return", "issue", ()),
    ("return", "issue", ("issue",)),
    ("return", "receive", ("issue",)),
    ("return", "return", ("issue",)),
    ("return", "complete", ("issue",)),
    ("cancel", "return", ("issue",)),
    ("undo", "undo", ("issue", "receive")),
    ("undo", "receive", ("issue", "receive")),
    ("undo", "return", ("issue", "receive")),
    ("undo", "complete", ("issue", "receive")),
    ("reopen", "reopen", ("issue", "complete")),
    ("reopen", "undo", ("issue", "complete")),
    ("reopen", "return", ("issue", "complete")),
    # Start, hold, resume and reschedule against the movements that close a run.
    ("hold", "complete", ("issue", "receive")),
    ("start", "cancel", ()),
    ("resume", "complete", ("issue", "receive", "hold")),
    ("schedule", "complete", ("issue", "receive")),
    ("hold", "cancel", ()),
    ("start", "complete", ("issue", "receive")),
]


@pytest.mark.parametrize("a, b, prep", PAIRS, ids=[f"{a}-{b}" + "".join(f"-after-{x}" for x in p) for a, b, p in PAIRS])
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


async def _sale(client, tok: str, job: dict, *, held: bool = False, hold=None) -> str:
    """Sell the run's first lot whole: an invoice for it, finalized and shipped. The answer is
    each step's status, up to the first refused. With ``held`` the invoice's creation is held
    at its commit until the hold is released."""
    lot = job["lot"]
    async with maker(job["engine"])() as s:
        state = (await s.get(Projection, {"company_id": job["cid"], "entity_id": lot})).state
    if hold is not None:
        hold.armed = held
    steps = []
    r = await client.post("/docs", headers=auth(tok), json={
        "doc_type": "invoice", "total": 500.0, "line_items": [
            {"entity_id": lot, "sku": state.get("sku"), "name": "Made", "quantity": job["qty"],
             "unit_price": 500.0, "sell_by": "piece"}]})
    steps.append(r.status_code)
    if r.status_code == 200:
        doc = r.json()["id"]
        for path, body in ((f"/docs/{doc}/finalize", None), (f"/docs/{doc}/fulfill-lines", {"line_entity_ids": [lot]})):
            r = await client.post(path, headers=auth(tok), json=body)
            steps.append(r.status_code)
            if r.status_code != 200:
                break
    return "sale " + " ".join(map(str, steps))


async def _sale_job(engine, client, user, n: int, prep) -> dict:
    job = await _job(engine, client, user, n) | {"engine": engine}
    await _prepare(engine, client, job, prep)
    async with maker(engine)() as s:
        lot = await _first_lot(s, job, 0)
        # The whole lot as received, read before the run takes it back.
        qty = (await s.get(Projection, {"company_id": job["cid"], "entity_id": lot})).state["quantity"]
    return job | {"tok": await token(engine, user, job["cid"]), "lot": lot, "qty": qty}


@pytest.mark.parametrize("name, prep, shipped", [
    ("undo", ("issue", "receive"), "sale 200 200 422"), ("reopen", ("issue", "complete"), "sale 200 200 200")],
    ids=["undo-receipt", "reopen"])
async def test_taking_output_back_while_it_is_being_sold_ends_as_if_one_ran_after_the_other(
        committed_engine, race, name, prep, shipped):
    """The run takes its lot back (Undo receipt, or Reopen) while an invoice for the same lot
    is being written: the invoice waits, and finds what the run left. An undone receipt has no
    stock left to ship; a reopened run keeps its lot, which then sells normally."""
    client, hold = race
    user = await owner(committed_engine)
    raced, serial = [await _sale_job(committed_engine, client, user, n, prep) for n in (1, 2)]
    take = _op(name, "1")

    async with maker(committed_engine)() as s1:
        held = await _attempt(s1, take, raced)
        task = asyncio.create_task(_sale(client, raced["tok"], raced))
        await _until_blocked(committed_engine, task)
        await _settle(s1, held)
        sold = await asyncio.wait_for(task, timeout=30)
    async with maker(committed_engine)() as s:
        out = await _attempt(s, take, serial)
        await _settle(s, out)
    assert (_answer(held), sold) == (_answer(out), await _sale(client, serial["tok"], serial))
    assert await _outcome(committed_engine, raced) == await _outcome(committed_engine, serial)
    assert sold == shipped, sold


@pytest.mark.parametrize("name, prep", [("undo", ("issue", "receive")), ("reopen", ("issue", "complete"))],
                         ids=["undo-receipt", "reopen"])
async def test_a_lot_put_on_an_invoice_first_cannot_be_taken_back(committed_engine, race, name, prep):
    """The invoice for the lot is being written when the run tries to take the lot back: the
    run waits for it, finds the lot on a document and refuses, changing nothing."""
    client, hold = race
    user = await owner(committed_engine)
    raced, serial = [await _sale_job(committed_engine, client, user, n, prep) for n in (1, 2)]
    take = _op(name, "1")

    selling = asyncio.create_task(_sale(client, raced["tok"], raced, held=True, hold=hold))
    await asyncio.wait_for(hold.reached.wait(), timeout=30)

    async def back():
        async with maker(committed_engine)() as s:
            out = await _attempt(s, take, raced)
            await _settle(s, out)
            return out

    task = asyncio.create_task(back())
    await _until_blocked(committed_engine, task)
    hold.release.set()
    sold, out = await asyncio.wait_for(selling, timeout=30), await asyncio.wait_for(task, timeout=30)

    expected = await _sale(client, serial["tok"], serial)
    async with maker(committed_engine)() as s:
        later = await _attempt(s, take, serial)
        await _settle(s, later)
    assert (sold, _answer(out)) == (expected, _answer(later)) == ("sale 200 200 200", "refused 409 mfg.output_changed")
    assert await _outcome(committed_engine, raced) == await _outcome(committed_engine, serial)
