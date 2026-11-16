# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""A resolve of decisions and accepted groups cut part way the way the assistant cuts a tool
at its step deadline, and the same key sent again while the first run is still going, on
real PostgreSQL connections against committed data: every line ends with one outcome and
both answers report every line."""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

import bank_statement_helpers as B
from test_bank_complete_race_pg import own_client  # noqa: F401  (a fixture)
from test_resolve_units import done_lines, every_line_once, mixed

pytestmark = pytest.mark.asyncio


async def _check(committed_engine, client, co, bk, body, before) -> None:
    async with async_sessionmaker(bind=committed_engine, expire_on_commit=False)() as session:
        await every_line_once(client, session, co, bk, body, before)


async def test_a_run_cut_at_the_step_deadline_is_finished_by_its_retry(committed_engine, own_client, monkeypatch):
    import celerp_accounting.bank_statements as BS
    co, bk, kw = await mixed(own_client)
    real = BS._decide

    async def paced(*a, **kw):
        await asyncio.sleep(0.3)
        return await real(*a, **kw)
    monkeypatch.setattr(BS, "_decide", paced)
    idem = B.key()
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(B.resolve(own_client, co, bk["id"], idem=idem, **kw), timeout=1.0)
    before = await done_lines(own_client, co, bk)
    assert 0 < len(before) < 5, "the deadline did not cut the run part way"
    again = await B.resolve(own_client, co, bk["id"], idem=idem, **kw)
    assert again.status_code == 200, again.text
    await _check(committed_engine, own_client, co, bk, again.json(), before)


@pytest.mark.parametrize("k", [1, 3, 4])
async def test_the_same_key_sent_while_the_first_run_is_part_way(committed_engine, own_client, monkeypatch, k):
    """The first run holds before its unit k + 1 with k units committed; the second request
    with the same key answers those k from their outcomes and runs the rest; the first then
    finds its remaining units recorded and answers the same."""
    import celerp_accounting.bank_statements as BS
    co, bk, kw = await mixed(own_client)
    real, calls = BS._decide, []
    between, go = asyncio.Event(), asyncio.Event()

    async def held(*a, **kw):
        calls.append(1)
        if len(calls) == k + 1:
            between.set()
            await go.wait()
        return await real(*a, **kw)
    monkeypatch.setattr(BS, "_decide", held)
    idem = B.key()
    first = asyncio.create_task(B.resolve(own_client, co, bk["id"], idem=idem, **kw))
    await asyncio.wait_for(between.wait(), timeout=20)
    before = await done_lines(own_client, co, bk)
    assert len(before) == k
    second = await asyncio.wait_for(B.resolve(own_client, co, bk["id"], idem=idem, **kw), timeout=30)
    go.set()
    one = await asyncio.wait_for(first, timeout=30)
    assert second.status_code == 200, second.text
    assert one.status_code == 200, one.text
    assert one.json() == second.json()
    await _check(committed_engine, own_client, co, bk, second.json(), before)
