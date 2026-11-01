# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Draws racing each other and the writers they share documents with, on real PostgreSQL
across two connections. The first writer holds its commit until the second is waiting on
(or past) the lock; every pair finishes without a deadlock and keeps every unit counted."""
from __future__ import annotations

import pytest

from memo_draw_support import back_to_memo, draft, draw, drawn, drawn_lines, memo_out, owner, pay, state
from test_draft_delete_races_pg import _factory, _seed
from test_fresh_authority_races_pg import _app_client, _http, _race

pytestmark = pytest.mark.asyncio


def _status(outcome) -> int:
    assert not isinstance(outcome, BaseException), outcome
    return outcome.status_code


def _no_deadlock(*outcomes) -> None:
    for o in outcomes:
        assert "40P01" not in o.text and "deadlock" not in o.text.lower(), o.text


async def _racing(engine, pending, one, two):
    a, b = await _race(engine, lambda held: _http(pending, held, one), lambda held: _http(pending, held, two))
    _no_deadlock(a, b)
    return a, b


async def _setup(engine):
    factory = _factory(engine)
    return await _seed(factory)


async def test_concurrent_draws_from_two_memos_onto_one_draft(committed_engine):
    c = await _setup(committed_engine)
    h = c["h"]["owner"]
    async with _app_client(_factory(committed_engine)) as (client, pending):
        a, la, _ = await memo_out(client, h, [(2, 10.0)])
        b, lb, _ = await memo_out(client, h, [(3, 20.0)])
        pf = await draft(client, h)
        v = (await state(client, h, pf))["version"]
        one, two = await _racing(committed_engine, pending,
                                 lambda: draw(client, h, a, line_ids=la, target=pf),
                                 lambda: draw(client, h, b, line_ids=lb, target=pf))
        assert (_status(one), _status(two)) == (200, 200)
        doc = await state(client, h, pf)
        assert len(drawn_lines(doc, a)) == 1 and len(drawn_lines(doc, b)) == 1
        # A version is the record's latest ledger entry id, so each draw moves it on.
        assert doc["version"] > v


async def test_concurrent_draws_take_shipping_once(committed_engine):
    c = await _setup(committed_engine)
    h = c["h"]["owner"]
    async with _app_client(_factory(committed_engine)) as (client, pending):
        memo, lids, _ = await memo_out(client, h, [(2, 10.0), (2, 10.0)], shipping=50.0)
        one, two = await _racing(committed_engine, pending,
                                 lambda: draw(client, h, memo, {lids[0]: 1}),
                                 lambda: draw(client, h, memo, {lids[1]: 1}))
        assert (_status(one), _status(two)) == (200, 200)
        shipping = [(await state(client, h, r.json()["target_doc_id"])).get("shipping") or 0 for r in (one, two)]
        assert sorted(shipping) == [0, 50.0]


async def test_payment_races_draw_one_winner(committed_engine):
    c = await _setup(committed_engine)
    h = c["h"]["owner"]
    async with _app_client(_factory(committed_engine)) as (client, pending):
        memo, lids, lots = await memo_out(client, h, [(2, 100.0)], lots_per_line=2)
        one, two = await _racing(committed_engine, pending,
                                 lambda: draw(client, h, memo, {lids[0]: 1}),
                                 lambda: pay(client, h, memo, 50.0))
        assert sorted((_status(one), _status(two))) == [200, 409]
        m = await state(client, h, memo)
        on_invoice = [i for i in lots[0] if (await owner(client, h, i))[1] != memo]
        assert bool(m.get("amount_paid")) != bool(on_invoice)


@pytest.mark.parametrize("onto_draft", [False, True], ids=["new_invoice", "existing_draft"])
async def test_concurrent_draws_same_lot_one_winner(committed_engine, onto_draft):
    c = await _setup(committed_engine)
    h = c["h"]["owner"]
    async with _app_client(_factory(committed_engine)) as (client, pending):
        memo, lids, lots = await memo_out(client, h, [(1, 10.0)])
        targets = [await draft(client, h) if onto_draft else None for _ in range(2)]
        one, two = await _racing(committed_engine, pending,
                                 lambda: draw(client, h, memo, line_ids=lids, target=targets[0]),
                                 lambda: draw(client, h, memo, line_ids=lids, target=targets[1]))
        assert sorted((_status(one), _status(two))) == [200, 409]
        winner = (one if one.status_code == 200 else two).json()["target_doc_id"]
        assert await owner(client, h, lots[0][0]) == ("memo_out", winner)
        loser = [t for t in targets if t and t != winner]
        for t in loser:
            assert not drawn_lines(await state(client, h, t))


async def test_concurrent_disjoint_draws_one_draft_keep_both(committed_engine):
    c = await _setup(committed_engine)
    h = c["h"]["owner"]
    async with _app_client(_factory(committed_engine)) as (client, pending):
        a, la, _ = await memo_out(client, h, [(2, 10.0)], lots_per_line=2)
        b, lb, _ = await memo_out(client, h, [(2, 10.0)], lots_per_line=2)
        pf = await draft(client, h)
        one, two = await _racing(committed_engine, pending,
                                 lambda: draw(client, h, a, {la[0]: 1}, target=pf),
                                 lambda: draw(client, h, b, {lb[0]: 1}, target=pf))
        assert (_status(one), _status(two)) == (200, 200)
        doc = await state(client, h, pf)
        assert len(drawn_lines(doc, a)) == 1 and len(drawn_lines(doc, b)) == 1


async def test_lock_order_company_first_no_deadlock(committed_engine):
    c = await _setup(committed_engine)
    h = c["h"]["owner"]
    async with _app_client(_factory(committed_engine)) as (client, pending):
        memo, lids, _ = await memo_out(client, h, [(4, 10.0)], lots_per_line=4)
        sold = await drawn(client, h, memo, {lids[0]: 1})
        await client.post(f"/docs/{sold}/finalize", headers=h)
        pf = await draft(client, h)
        one, two = await _racing(committed_engine, pending,
                                 lambda: draw(client, h, memo, {lids[0]: 1}, target=pf),
                                 lambda: client.post(f"/docs/{sold}/void", headers=h, json={}))
        assert (_status(one), _status(two)) == (200, 200)

        back = await drawn(client, h, memo, {lids[0]: 1})
        back_line = drawn_lines(await state(client, h, back), memo)[0]["line_id"]
        one, two = await _racing(committed_engine, pending,
                                 lambda: back_to_memo(client, h, back, [back_line]),
                                 lambda: draw(client, h, memo, {lids[0]: 1}))
        assert (_status(one), _status(two)) == (200, 200)


@pytest.mark.parametrize("finalize_first", [True, False], ids=["finalize_wins", "draw_wins"])
async def test_draw_races_finalize_of_target_draft(committed_engine, finalize_first):
    c = await _setup(committed_engine)
    h = c["h"]["owner"]
    async with _app_client(_factory(committed_engine)) as (client, pending):
        memo, lids, lots = await memo_out(client, h, [(2, 10.0)], lots_per_line=2)
        pf = await draft(client, h, lines=[{"description": "Fee", "quantity": 1, "unit_price": 5, "sell_by": "piece"}])
        fin = lambda: client.post(f"/docs/{pf}/finalize", headers=h)  # noqa: E731
        drw = lambda: draw(client, h, memo, line_ids=lids, target=pf)  # noqa: E731
        first, second = (fin, drw) if finalize_first else (drw, fin)
        a, b = await _racing(committed_engine, pending, first, second)
        f, d = (a, b) if finalize_first else (b, a)
        assert _status(f) == 200
        if finalize_first:
            assert _status(d) == 409 and d.json()["detail"]["message_key"] == "docs.draw_target_not_draft"
            assert [await owner(client, h, i) for i in lots[0]] == [("memo_out", memo)] * 2
        else:
            assert _status(d) == 200
            assert [(await owner(client, h, i))[0] for i in lots[0]] == ["sold"] * 2


async def test_concurrent_bulk_deletes_sharing_memos_no_deadlock(committed_engine):
    c = await _setup(committed_engine)
    h = c["h"]["owner"]
    async with _app_client(_factory(committed_engine)) as (client, pending):
        a, la, lots_a = await memo_out(client, h, [(2, 10.0)], lots_per_line=2)
        b, lb, lots_b = await memo_out(client, h, [(2, 10.0)], lots_per_line=2)
        x, y = await draft(client, h), await draft(client, h)
        for memo, lid in ((a, la[0]), (b, lb[0])):
            await drawn(client, h, memo, {lid: 1}, target=x)
            await drawn(client, h, memo, {lid: 1}, target=y)

        def bulk(ids):
            return lambda: client.delete("/docs/bulk-draft", params={"doc_ids": ",".join(ids)}, headers=h)
        one, two = await _racing(committed_engine, pending, bulk([x]), bulk([y]))
        assert (_status(one), _status(two)) == (200, 200)
        for memo, lots in ((a, lots_a), (b, lots_b)):
            assert [await owner(client, h, i) for i in lots[0]] == [("memo_out", memo)] * 2


@pytest.mark.parametrize("back_first", [True, False], ids=["back_wins", "finalize_wins"])
async def test_back_to_memo_races_finalize_one_winner(committed_engine, back_first):
    c = await _setup(committed_engine)
    h = c["h"]["owner"]
    async with _app_client(_factory(committed_engine)) as (client, pending):
        memo, lids, lots = await memo_out(client, h, [(1, 10.0)])
        inv = await drawn(client, h, memo, line_ids=lids)
        line = drawn_lines(await state(client, h, inv), memo)[0]["line_id"]
        back = lambda: back_to_memo(client, h, inv, [line])  # noqa: E731
        fin = lambda: client.post(f"/docs/{inv}/finalize", headers=h)  # noqa: E731
        a, b = await _racing(committed_engine, pending, *((back, fin) if back_first else (fin, back)))
        assert [_status(a), _status(b)].count(200) == 1
        where = await owner(client, h, lots[0][0])
        assert where == (("memo_out", memo) if back_first else ("sold", inv))


async def test_close_races_draw_one_winner(committed_engine):
    c = await _setup(committed_engine)
    h = c["h"]["owner"]
    async with _app_client(_factory(committed_engine)) as (client, pending):
        memo, lids, lots = await memo_out(client, h, [(1, 10.0)])
        one, two = await _racing(committed_engine, pending,
                                 lambda: draw(client, h, memo, line_ids=lids),
                                 lambda: client.post(f"/docs/{memo}/close", headers=h, json={}))
        assert sorted((_status(one), _status(two))) == [200, 409]
        m = await state(client, h, memo)
        on_invoice = [i for i in lots[0] if (await owner(client, h, i))[1] != memo]
        assert (m["status"] == "closed") != bool(on_invoice)
