# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""A notice told once (notify_once) or kept standing (notify_standing) is deduped on what
the company already has, a read-check-write run when the session commits. Two commits that
check at the same time would each find nothing and each add the notice, so each takes the
company's notice key first: the second waits for the first to commit and then finds its
notice.

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


ROUNDS = 25


@pytest.mark.parametrize("notify", [_once, _standing], ids=["once", "standing"])
async def test_two_writers_at_once_leave_one_notice(factory, notify):
    async def write(cid, start: asyncio.Event) -> None:
        async with factory() as s:
            await notify(s, cid, "Same")
            await start.wait()
            await s.commit()

    for _ in range(ROUNDS):
        cid = await _company(factory)
        start = asyncio.Event()
        writers = [asyncio.create_task(write(cid, start)) for _ in range(2)]
        await asyncio.sleep(0)
        start.set()
        await asyncio.wait_for(asyncio.gather(*writers), 10)
        async with factory() as s:
            assert (await s.execute(select(func.count()).select_from(Notification)
                                    .where(Notification.company_id == cid))).scalar_one() == 1
