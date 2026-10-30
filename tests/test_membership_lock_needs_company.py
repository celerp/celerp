# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""A membership row is locked only by a transaction that holds its company's row first.
A company writer (a role change, a reset) takes the company row and then the membership
row, so a membership lock taken without the company row could be held while its taker waits
on that writer. read_authority holds the membership only under the company's lock or its
deletion lock; without either it reads the membership as committed and holds nothing.

Each case runs on real PostgreSQL across two connections."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from celerp.models.accounting import UserCompany
from celerp.models.company import Company, User
from celerp.services.company_lock import hold_company, lock_company, lock_company_for_deletion
from celerp.services.permissions import read_authority

pytestmark = pytest.mark.asyncio


@pytest.fixture
def factory(committed_engine):
    return async_sessionmaker(bind=committed_engine, class_=AsyncSession, expire_on_commit=False)


async def _member(factory) -> tuple[uuid.UUID, uuid.UUID]:
    cid, uid = uuid.uuid4(), uuid.uuid4()
    async with factory() as s:
        s.add(User(id=uid, email=f"ml-{uid.hex[:8]}@test.co", name="Owner", auth_hash="x", is_active=True))
        await s.flush()
        s.add(Company(id=cid, name="Member Co", slug=f"ml-{cid.hex[:8]}", settings={"k": 1}))
        await s.flush()
        s.add(UserCompany(id=uuid.uuid4(), user_id=uid, company_id=cid, role="owner", is_active=True))
        await s.commit()
    return cid, uid


async def _held_by(factory, cid):
    """A session holding the company's membership rows, as a role change in flight does."""
    writer = factory()
    await writer.execute(select(UserCompany).where(UserCompany.company_id == cid).with_for_update())
    return writer


async def _no_hold(session, company_id):
    return True


@pytest.mark.parametrize("hold", [_no_hold, hold_company], ids=["nothing", "hold_company"])
async def test_without_the_company_lock_the_membership_is_read_and_not_held(factory, hold):
    cid, uid = await _member(factory)
    writer = await _held_by(factory, cid)
    async with factory() as s:
        await hold(s, cid)
        assert await asyncio.wait_for(read_authority(s, cid, uid), 5) == ("owner", {"k": 1})
        await s.rollback()
    await writer.rollback()
    await writer.close()


async def test_another_companys_lock_does_not_hold_the_membership(factory):
    cid, uid = await _member(factory)
    other, _ = await _member(factory)
    writer = await _held_by(factory, cid)
    async with factory() as s:
        await lock_company(s, other)
        assert await asyncio.wait_for(read_authority(s, cid, uid), 5) == ("owner", {"k": 1})
        await s.rollback()
    await writer.rollback()
    await writer.close()


@pytest.mark.parametrize("hold", [lock_company, lock_company_for_deletion])
async def test_under_the_company_row_the_membership_is_held_to_commit(factory, hold):
    cid, uid = await _member(factory)
    async with factory() as s, factory() as changing:
        await hold(s, cid)
        assert await read_authority(s, cid, uid) == ("owner", {"k": 1})
        change = asyncio.create_task(changing.execute(
            update(UserCompany).where(UserCompany.company_id == cid).values(role="admin")))
        await asyncio.sleep(0.5)
        assert not change.done(), "the membership was not held under the company row"
        await s.rollback()
        await asyncio.wait_for(change, 5)
        await changing.rollback()


async def test_a_lost_membership_reads_as_no_role(factory):
    cid, _ = await _member(factory)
    async with factory() as s:
        assert await read_authority(s, cid, uuid.uuid4()) == ("", {"k": 1})
