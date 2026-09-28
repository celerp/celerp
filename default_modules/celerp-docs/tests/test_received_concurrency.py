# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""One sender revision holds one content. Two imports of the same revision with
different content arriving at the same moment must not both be recorded: the
second waits for the first, then sees the revision and is rejected.

These use independent sessions on the shared engine with real commits: the race
only exists between separately committed transactions."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from celerp.models.company import Company
from celerp.models.ledger import LedgerEntry
from celerp.models.projections import Projection


def _bundle(revision: int, price: str) -> tuple[dict, dict]:
    from celerp_docs.routes_share import _sanitize_bundle_doc

    doc = {
        "doc_type": "invoice", "ref_id": "INV-9", "currency": "USD",
        "line_items": [{"description": "Widget", "quantity": 1, "unit_price": price}],
    }
    bundle = {"version": 1, "doc": doc,
              "source": {"installation": "inst-race", "document": "doc:R-1", "revision": revision}}
    return bundle, _sanitize_bundle_doc(doc)


async def _record(session, company_id, revision: int, price: str) -> str:
    from celerp_docs import received

    bundle, document = _bundle(revision, price)
    return await received.record_received(
        session, company_id, None, bundle=bundle, document=document, link=None,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("already_received", [False, True], ids=["first-import", "revision"])
async def test_same_revision_with_different_content_at_once_records_only_one(_db_engine, already_received):
    factory = async_sessionmaker(bind=_db_engine, class_=AsyncSession, expire_on_commit=False)
    company_id = uuid.uuid4()
    async with factory() as s:
        s.add(Company(id=company_id, name="RaceCo", slug=f"race-{company_id.hex[:8]}"))
        await s.commit()
    revision = 1
    if already_received:
        async with factory() as s:
            await _record(s, company_id, 1, "5.00")
            await s.commit()
        revision = 2

    s1, s2 = factory(), factory()
    try:
        rid = await _record(s1, company_id, revision, "10.00")  # not committed yet
        task = asyncio.create_task(_record(s2, company_id, revision, "99.00"))
        await asyncio.sleep(0.3)
        await s1.commit()
        with pytest.raises(HTTPException) as exc:
            await asyncio.wait_for(task, timeout=10)
        assert exc.value.status_code == 422
        await s2.rollback()

        async with factory() as s:
            events = (await s.execute(
                select(func.count()).select_from(LedgerEntry).where(
                    LedgerEntry.company_id == company_id, LedgerEntry.entity_id == rid)
            )).scalar_one()
            state = (await s.get(Projection, (company_id, rid))).state
        assert events == (2 if already_received else 1)
        assert float(state["total"]) == 10.0
    finally:
        await s1.close()
        await s2.close()
        async with factory() as s:
            await s.execute(delete(Projection).where(Projection.company_id == company_id))
            await s.execute(delete(LedgerEntry).where(LedgerEntry.company_id == company_id))
            await s.execute(delete(Company).where(Company.id == company_id))
            await s.commit()
