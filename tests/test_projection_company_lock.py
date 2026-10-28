# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Every writer of records takes its company's lock before a record's row lock
(company_lock.lock_company), whatever kind of record it writes. Two writers holding one
company's lock never interleave, so the order in which each takes records orders nothing;
a writer taking records without it could hold the record the other waits on."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from company_backup_support import company, owner
from migration_support import maker, real_engine  # noqa: F401

pytestmark = pytest.mark.asyncio


async def _company_locked(engine, company_id) -> bool:
    async with engine.connect() as probe:
        try:
            await probe.execute(text("SELECT 1 FROM companies WHERE id = :c FOR NO KEY UPDATE NOWAIT"),
                                {"c": company_id})
        except DBAPIError:
            return True
        finally:
            await probe.rollback()
    return False


async def test_an_emit_of_a_document_holds_its_companys_lock(real_engine):
    from celerp.events.engine import emit_event

    cid = await company(real_engine, await owner(real_engine), "Lock Co", "lock")
    async with maker(real_engine)() as s:
        await emit_event(s, company_id=cid, entity_id=f"doc:Q-{uuid.uuid4().hex[:8]}", entity_type="doc",
                         event_type="doc.created",
                         data={"doc_type": "quotation", "status": "draft", "line_items": [], "total": 0.0},
                         actor_id=None, location_id=None, source="test",
                         idempotency_key=str(uuid.uuid4()), metadata_={})
        assert await _company_locked(real_engine, cid)
        await s.rollback()


@pytest.mark.parametrize("one_company", [True, False])
async def test_a_rebuild_holds_the_lock_of_every_company_it_replays(real_engine, one_company):
    from celerp.projections.engine import ProjectionEngine

    user = await owner(real_engine)
    first = await company(real_engine, user, "Lock One", "one")
    second = await company(real_engine, user, "Lock Two", "two")
    async with maker(real_engine)() as s:
        await ProjectionEngine.rebuild(s, first if one_company else None)
        assert await _company_locked(real_engine, first)
        assert await _company_locked(real_engine, second) is not one_company
        await s.rollback()
