# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""A notice is the last thing its transaction writes.

Notifications rank after every other lock (tests/lock_rank.py RANKS).
A writer that removed or updated a notice and then took its company's lock would hold the
notice row while waiting for the company, and a writer holding the company that then cleared
the same notice would wait for it: PostgreSQL aborts one of them. Each test runs one writer up
to the point between its notice and its company lock, starts the other until it waits, then
lets the first go on: both must finish."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

from test_import_preflight_invariants import _factory, _seed_race_company, _until_blocked_or_done
from test_lock_order_deadlocks import _both_finish

pytestmark = pytest.mark.asyncio

_URL = "/settings/accounting"


async def _company_with_notice(factory):
    from celerp.notifications import service

    company_id, _user_id = await _seed_race_company(factory)
    async with factory() as s:
        await service.notify_standing(s, company_id, "accounting", "Standing", "Before", action_url=_URL)
        await s.commit()
    return company_id


async def _race(engine, factory, company_id, first_notice, second_notice) -> None:
    """``first_notice`` before the company's lock against ``second_notice`` after it."""
    from celerp.services.company_lock import lock_company

    entered, go = asyncio.Event(), asyncio.Event()
    async with factory() as a, factory() as b:
        async def first():
            await first_notice(a)
            entered.set()
            await go.wait()
            await lock_company(a, company_id)
            await a.commit()

        async def second():
            await lock_company(b, company_id)
            await second_notice(b)
            await b.commit()

        one = asyncio.create_task(first())
        await asyncio.wait_for(entered.wait(), timeout=5)
        two = asyncio.create_task(second())
        await _until_blocked_or_done(engine, two)
        go.set()
        await _both_finish(one, two)


async def test_a_notice_marked_done_before_the_company_lock_waits_for_a_writer_holding_it(committed_engine):
    from celerp.notifications import service

    factory = _factory(committed_engine)
    company_id = await _company_with_notice(factory)
    await _race(committed_engine, factory, company_id,
                lambda s: service.mark_done(s, company_id, _URL),
                lambda s: service.clear_standing(s, company_id, "accounting", "Standing"))


async def test_a_standing_notice_kept_current_before_the_company_lock_waits_for_a_writer_holding_it(committed_engine):
    from celerp.models.notification import Notification
    from celerp.notifications import service

    factory = _factory(committed_engine)
    company_id = await _company_with_notice(factory)
    await _race(committed_engine, factory, company_id,
                lambda s: service.notify_standing(s, company_id, "accounting", "Standing", "First", action_url=_URL),
                lambda s: service.notify_standing(s, company_id, "accounting", "Standing", "Second", action_url=_URL))
    async with factory() as s:
        bodies = (await s.execute(select(Notification.body).where(Notification.company_id == company_id))).scalars()
        assert list(bodies) == ["First"]  # the writer that committed last kept it current


async def test_a_notice_written_in_a_rolled_back_savepoint_is_not_kept(committed_engine):
    from celerp.models.notification import Notification
    from celerp.notifications import service

    factory = _factory(committed_engine)
    company_id, _user_id = await _seed_race_company(factory)
    async with factory() as s:
        await service.create(s, company_id, "accounting", "Kept", "Outside the savepoint")
        try:
            async with s.begin_nested():
                await service.create(s, company_id, "accounting", "Dropped", "Inside the savepoint")
                raise RuntimeError("the savepoint fails")
        except RuntimeError:
            pass
        await s.commit()
    async with factory() as s:
        titles = (await s.execute(select(Notification.title).where(Notification.company_id == company_id))).scalars()
        assert list(titles) == ["Kept"]


async def test_two_commits_writing_two_notices_in_opposite_orders_both_finish(committed_engine):
    """One commit removes notice A then updates notice B; the other updates B then removes A.
    The first pauses between its two writes until the second waits: each holding the notice
    the other needs next is a deadlock, so the second must wait before its first write."""
    from sqlalchemy.util import await_only

    from celerp.notifications import service

    factory = _factory(committed_engine)
    company_id, _user_id = await _seed_race_company(factory)
    async with factory() as s:
        await service.notify_standing(s, company_id, "accounting", "A", "Before")
        await service.notify_standing(s, company_id, "accounting", "B", "Before")
        await s.commit()
    between, go = asyncio.Event(), asyncio.Event()

    async def pause():
        between.set()
        await go.wait()

    async with factory() as a, factory() as b:
        await service.clear_standing(a, company_id, "accounting", "A")
        service._defer(a, company_id, lambda sync: await_only(pause()))
        await service.notify_standing(a, company_id, "accounting", "B", "From the first")
        await service.notify_standing(b, company_id, "accounting", "B", "From the second")
        await service.clear_standing(b, company_id, "accounting", "A")
        one = asyncio.create_task(a.commit())
        await asyncio.wait_for(between.wait(), timeout=5)
        two = asyncio.create_task(b.commit())
        await _until_blocked_or_done(committed_engine, two)
        go.set()
        await _both_finish(one, two)
    async with factory() as s:
        from celerp.models.notification import Notification

        rows = (await s.execute(select(Notification.title, Notification.body)
                                .where(Notification.company_id == company_id))).all()
        assert rows == [("B", "From the second")]
