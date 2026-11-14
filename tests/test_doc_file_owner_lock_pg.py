# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""A file change on a document or List takes the document's row lock before it records the
file, so it waits for any other writer holding that document; a download only reads it."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from celerp.events.engine import emit_event
from celerp.models.company import Company
from celerp_docs import routes as docs

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("entity_type,entity_id,event_type,data", [
    ("doc", "doc:INV-1", "doc.created", {"doc_type": "invoice", "status": "draft"}),
    ("list", "list:QUO-1", "list.created", {"list_type": "quotation", "status": "draft"}),
])
async def test_a_file_change_waits_for_the_document_lock(committed_engine, entity_type, entity_id, event_type, data):
    factory = async_sessionmaker(bind=committed_engine, class_=AsyncSession, expire_on_commit=False)
    company_id = uuid.uuid4()
    async with factory() as s:
        s.add(Company(id=company_id, name="Files", slug=f"files-{company_id.hex[:8]}", settings={}))
        await s.flush()
        await emit_event(s, company_id=company_id, entity_id=entity_id, entity_type=entity_type,
                         event_type=event_type, data=data, actor_id=None, location_id=None,
                         source="test", idempotency_key=str(uuid.uuid4()))
        await s.commit()
    async def writer_side() -> None:
        async with factory() as writer:
            await writer.execute(text("SET LOCAL lock_timeout = '200ms'"))
            assert (await docs._get_file_owner(writer, company_id, entity_id)).entity_type == entity_type
            with pytest.raises(DBAPIError, match="lock"):
                await docs._get_file_owner(writer, company_id, entity_id, for_update=True)
            await writer.rollback()

    async with factory() as holder:
        await holder.execute(text("SELECT 1 FROM projections WHERE company_id = :c AND entity_id = :e FOR UPDATE"),
                             {"c": company_id, "e": entity_id})
        await asyncio.create_task(writer_side())
        await holder.rollback()
