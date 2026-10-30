# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""A company's projection rebuild deletes every record and replays the ledger. The
operation that asks for it holds the company lock for the whole replay, so a write made
meanwhile waits and is kept instead of landing mid-replay, and the rebuild's own
authority, carried from the request onto the lifecycle session, is judged again under
that lock. The engine refuses a company replay its caller did not lock.

Each case runs on real PostgreSQL across two connections."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from celerp.models.accounting import UserCompany
from celerp.models.company import Company, User
from celerp.models.ledger import LedgerEntry
from celerp.models.projections import Projection
from celerp.projections.engine import ProjectionEngine
from celerp.services.company_lock import lock_company
from celerp.services.permissions import authorize_request, carry_authority

pytestmark = pytest.mark.asyncio


@pytest.fixture
def factory(committed_engine):
    return async_sessionmaker(bind=committed_engine, class_=AsyncSession, expire_on_commit=False)


async def _company(factory) -> tuple[uuid.UUID, uuid.UUID]:
    cid, uid = uuid.uuid4(), uuid.uuid4()
    async with factory() as s:
        s.add(Company(id=cid, name="Rebuild Co", slug=f"rb-{cid.hex[:8]}", settings={}))
        s.add(User(id=uid, email=f"rb-{uid.hex[:8]}@test.co", name="Owner", auth_hash="x", is_active=True))
        await s.flush()
        s.add(UserCompany(id=uuid.uuid4(), user_id=uid, company_id=cid, role="owner", is_active=True))
        await _write(s, cid)
        await s.commit()
    return cid, uid


async def _write(s, cid) -> None:
    from celerp.events.engine import emit_event
    await emit_event(s, company_id=cid, entity_id=f"item:{uuid.uuid4()}", entity_type="item",
                     event_type="item.created", data={"sku": f"SKU-{uuid.uuid4().hex[:6]}", "name": "Thing"},
                     actor_id=None, location_id=None, source="test",
                     idempotency_key=f"test:{uuid.uuid4()}", metadata_={})


async def _count(factory, model, cid) -> int:
    async with factory() as s:
        return (await s.execute(select(func.count()).select_from(model).where(model.company_id == cid))).scalar_one()


async def test_a_write_during_a_rebuild_waits_and_is_kept(factory):
    cid, _ = await _company(factory)

    async def write():
        async with factory() as s:
            await lock_company(s, cid)
            await _write(s, cid)
            await s.commit()

    async with factory() as rebuilding:
        await lock_company(rebuilding, cid)
        await ProjectionEngine.rebuild(rebuilding, company_id=cid)
        writing = asyncio.create_task(write())
        await asyncio.sleep(0.5)
        assert not writing.done(), "a write landed while the rebuild replayed the company"
        await rebuilding.commit()
    await asyncio.wait_for(writing, 10)
    assert await _count(factory, Projection, cid) == await _count(factory, LedgerEntry, cid) == 2


async def test_a_rebuild_whose_access_was_revoked_meanwhile_is_refused(factory):
    cid, uid = await _company(factory)
    async with factory() as request_session, factory() as s:
        authorize_request(request_session, cid, uid, "owner").keys.add("manage_company_lifecycle")
        carry_authority(request_session, s)
        async with factory() as revoking:
            await revoking.execute(text("SET LOCAL lock_timeout = '2s'"))
            await revoking.execute(update(UserCompany).where(
                UserCompany.company_id == cid, UserCompany.user_id == uid).values(is_active=False))
            await revoking.commit()
        with pytest.raises(HTTPException) as refused:
            await lock_company(s, cid)
            await ProjectionEngine.rebuild(s, company_id=cid)
            await s.commit()
        assert refused.value.status_code == 403
    assert await _count(factory, Projection, cid) == 1, "the refused rebuild deleted the records"


async def test_a_rebuild_its_caller_did_not_lock_is_refused_before_anything_is_deleted(factory):
    cid, _ = await _company(factory)
    async with factory() as s:
        with pytest.raises(RuntimeError, match="company lock"):
            await ProjectionEngine.rebuild(s, company_id=cid)
        await s.commit()
    assert await _count(factory, Projection, cid) == 1
