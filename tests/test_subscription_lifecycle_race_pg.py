# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""A subscription lifecycle change (activate, pause, resume, cancel) checks the
template's status and writes the next one. A change made at the same time by another
user commits first or waits: the second change judges the status the first left, so a
cancelled subscription is never paused and a paused one is still cancelled. Each case
runs on real PostgreSQL across two connections, in both orders."""

from __future__ import annotations

import asyncio
import types
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from celerp.events.engine import emit_event
from celerp.models.company import Company
from celerp.models.projections import Projection
from celerp.services.company_lock import lock_projections
from celerp_subscriptions.routes import _build_router

pytestmark = pytest.mark.asyncio

_SUB = "doc:SUB-1"
_ROUTES = {r.path: r.endpoint for r in _build_router().routes}


@pytest.fixture
def factory(committed_engine):
    return async_sessionmaker(bind=committed_engine, class_=AsyncSession, expire_on_commit=False)


async def _seed(factory) -> uuid.UUID:
    cid = uuid.uuid4()
    async with factory() as s:
        s.add(Company(id=cid, name="Subs", slug=f"subs-{cid.hex[:8]}", settings={}))
        await s.flush()
        await emit_event(s, company_id=cid, entity_id=_SUB, entity_type="doc", event_type="doc.created",
                         data={"doc_type": "subscription_invoice", "status": "active", "frequency": "monthly"},
                         actor_id=None, location_id=None, source="test", idempotency_key=str(uuid.uuid4()))
        await s.commit()
    return cid


async def _change(factory, cid, action: str):
    async with factory() as s:
        return await _ROUTES[f"/{{entity_id}}/{action}"](
            _SUB, company_id=cid, _=None, user=types.SimpleNamespace(id=None), session=s)


async def _status(factory, cid) -> str:
    async with factory() as s:
        return (await s.get(Projection, {"company_id": cid, "entity_id": _SUB})).state["status"]


async def _while_held(factory, cid, first: str, second: str):
    """*second* starts while *first* holds the template and has written, then *first* commits."""
    async with factory() as s:
        await lock_projections(s, cid, [_SUB])
        await emit_event(s, company_id=cid, entity_id=_SUB, entity_type="doc", event_type="doc.updated",
                         data={"fields_changed": {"status": {"new": first}}}, actor_id=None,
                         location_id=None, source="test", idempotency_key=str(uuid.uuid4()))
        task = asyncio.create_task(_change(factory, cid, second))
        await asyncio.sleep(0.5)
        assert not task.done(), "the change checked the status without waiting for the holder"
        await s.commit()
    return await asyncio.wait_for(task, 10)


async def test_a_pause_waiting_on_a_cancel_is_refused(factory):
    cid = await _seed(factory)
    with pytest.raises(HTTPException) as refused:
        await _while_held(factory, cid, "cancelled", "pause")
    assert refused.value.status_code == 409
    assert await _status(factory, cid) == "cancelled"


async def test_a_cancel_waiting_on_a_pause_cancels(factory):
    cid = await _seed(factory)
    assert await _while_held(factory, cid, "paused", "cancel") == {"ok": True}
    assert await _status(factory, cid) == "cancelled"
