# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""A notice told once (notify_once) or kept standing (notify_standing) is deduped on what
the company already has, a read-check-write. Two writers that check at the same time would
each find nothing and each add the notice, so the writers hold the company lock: the
second waits for the first to commit and then finds its notice.

Each case runs on real PostgreSQL across two connections."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from celerp.models.company import Company
from celerp.models.notification import Notification
from celerp.notifications import service as notification_service

pytestmark = pytest.mark.asyncio


@pytest.fixture
def factory(committed_engine):
    return async_sessionmaker(bind=committed_engine, class_=AsyncSession, expire_on_commit=False)


async def _company(factory) -> uuid.UUID:
    cid = uuid.uuid4()
    async with factory() as s:
        s.add(Company(id=cid, name="Notice Co", slug=f"nt-{cid.hex[:8]}", settings={}))
        await s.commit()
    return cid


async def _once(s, cid, body):
    return await notification_service.notify_once(s, cid, "system", "Told once", body)


async def _standing(s, cid, body):
    return await notification_service.notify_standing(s, cid, "system", "Standing", body)


@pytest.mark.parametrize("notify", [_once, _standing], ids=["once", "standing"])
async def test_two_writers_at_once_leave_one_notice(factory, notify):
    cid = await _company(factory)
    async with factory() as first:
        assert await notify(first, cid, "Same")

        async def second() -> bool:
            async with factory() as s:
                created = await notify(s, cid, "Same")
                await s.commit()
                return created

        task = asyncio.create_task(second())
        await asyncio.sleep(0.5)
        assert not task.done(), "the second writer checked without waiting for the first"
        await first.commit()
    assert await asyncio.wait_for(task, 10) is False
    async with factory() as s:
        assert (await s.execute(select(func.count()).select_from(Notification)
                                .where(Notification.company_id == cid))).scalar_one() == 1


async def test_clearing_waits_for_a_writer_that_holds_the_company(factory):
    cid = await _company(factory)
    async with factory() as first:
        assert await _standing(first, cid, "Holds")

        async def clear() -> int:
            async with factory() as s:
                removed = await notification_service.clear_standing(s, cid, "system", "Standing")
                await s.commit()
                return removed

        task = asyncio.create_task(clear())
        await asyncio.sleep(0.5)
        assert not task.done(), "clearing did not wait for the writer holding the company"
        await first.commit()
    assert await asyncio.wait_for(task, 10) == 1
