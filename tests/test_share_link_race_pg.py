# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""A document has one share link for its lifetime. Opening Share mints it the first time,
and Share and Revoke turn that same link on and off. A request made while another is
minting or changing the link waits for it under the company lock and then works on the
link and the document it left, so two first opens never mint two links (after which the
document's link could no longer be found) and a document that stopped being shareable
meanwhile is refused. Each case runs on real PostgreSQL across two connections."""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from celerp.events.engine import emit_event
from celerp.models.company import Company
from celerp.models.projections import Projection
from celerp.models.share import DocShareToken
from celerp.services.company_lock import lock_company
from celerp_docs.routes_share import create_share_link, revoke_share_link, share_status

pytestmark = pytest.mark.asyncio

_DOC = "doc:INV-1"


@pytest.fixture
def factory(committed_engine):
    return async_sessionmaker(bind=committed_engine, class_=AsyncSession, expire_on_commit=False)


async def _seed(factory) -> uuid.UUID:
    cid = uuid.uuid4()
    async with factory() as s:
        s.add(Company(id=cid, name="Share", slug=f"share-{cid.hex[:8]}", settings={}))
        await s.flush()
        await emit_event(s, company_id=cid, entity_id=_DOC, entity_type="doc", event_type="doc.created",
                         data={"doc_type": "invoice", "status": "draft"}, actor_id=None,
                         location_id=None, source="test", idempotency_key=str(uuid.uuid4()))
        await s.commit()
    return cid


async def _links(factory, cid) -> list[DocShareToken]:
    async with factory() as s:
        return list((await s.scalars(select(DocShareToken).where(
            DocShareToken.company_id == cid, DocShareToken.entity_id == _DOC))).all())


async def _while_minting(factory, cid, request):
    """*request* starts while another request holds the company and has minted the link,
    then that request commits. Returns the minted token and *request*'s answer."""
    async with factory() as s:
        await lock_company(s, cid)
        minted = DocShareToken(company_id=cid, entity_id=_DOC, token=uuid.uuid4().hex,
                               revoked_at=datetime.now(timezone.utc))
        s.add(minted)
        await s.flush()
        task = asyncio.create_task(request())
        await asyncio.sleep(0.5)
        assert not task.done(), "the request read the share link without waiting for the holder"
        await s.commit()
    return minted.token, await asyncio.wait_for(task, 10)


async def test_two_first_opens_share_one_link(factory):
    cid = await _seed(factory)

    async def opened():
        async with factory() as s:
            return await share_status(_DOC, company_id=cid, _=None, session=s)

    token, answer = await _while_minting(factory, cid, opened)
    assert answer["token"] == token
    assert [link.token for link in await _links(factory, cid)] == [token]


async def test_sharing_a_document_made_unshareable_meanwhile_is_refused(factory):
    cid = await _seed(factory)

    async def shared():
        async with factory() as s:
            try:
                return await create_share_link(_DOC, body=None, company_id=cid, _=None, session=s)
            except HTTPException as exc:
                return exc.status_code

    async with factory() as s:
        await lock_company(s, cid)
        await s.execute(update(Projection).where(Projection.company_id == cid, Projection.entity_id == _DOC)
                        .values(state={"doc_type": "production_order", "status": "draft"}))
        await s.flush()
        task = asyncio.create_task(shared())
        await asyncio.sleep(0.5)
        assert not task.done(), "the share judged the document without waiting for the holder"
        await s.commit()
    assert await asyncio.wait_for(task, 10) == 422
    assert await _links(factory, cid) == []


async def test_sharing_while_the_link_is_minted_turns_that_link_on(factory):
    cid = await _seed(factory)

    async def shared():
        async with factory() as s:
            return await create_share_link(_DOC, body=None, company_id=cid, _=None, session=s)

    token, answer = await _while_minting(factory, cid, shared)
    assert (answer["token"], answer["active"]) == (token, True)
    assert [(link.token, link.revoked_at) for link in await _links(factory, cid)] == [(token, None)]


async def test_revoking_while_the_link_is_shared_turns_it_off_after(factory):
    cid = await _seed(factory)
    async with factory() as s:
        await create_share_link(_DOC, body=None, company_id=cid, _=None, session=s)
    async with factory() as s:
        await lock_company(s, cid)
        link = (await s.scalars(select(DocShareToken).where(DocShareToken.company_id == cid))).one()
        link.revoked_at = None

        async def revoked():
            async with factory() as other:
                return await revoke_share_link(_DOC, company_id=cid, _=None, session=other)

        task = asyncio.create_task(revoked())
        await asyncio.sleep(0.5)
        assert not task.done(), "the revoke read the share link without waiting for the holder"
        await s.commit()
    assert (await asyncio.wait_for(task, 10))["active"] is False
    async with factory() as s:
        assert await s.scalar(select(func.count()).select_from(DocShareToken).where(
            DocShareToken.company_id == cid, DocShareToken.revoked_at.is_(None))) == 0
