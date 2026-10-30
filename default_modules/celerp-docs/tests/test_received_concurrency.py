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


@pytest.mark.asyncio
async def test_booking_while_a_new_revision_is_recorded_books_that_revision(_db_engine):
    """Booking reads the received document under the company lock: a booking made while a
    newer revision is being recorded waits for it and books the revision it left."""
    from types import SimpleNamespace

    from celerp_docs import received

    factory = async_sessionmaker(bind=_db_engine, class_=AsyncSession, expire_on_commit=False)
    company_id = uuid.uuid4()
    async with factory() as s:
        s.add(Company(id=company_id, name="BookCo", slug=f"book-{company_id.hex[:8]}"))
        await s.commit()
    async with factory() as s:
        rid = await _record(s, company_id, 1, "5.00")
        await s.commit()

    async def booked():
        async with factory() as s:
            await received.book(s, company_id, rid, role="owner", settings={},
                                user=SimpleNamespace(id=None))
        async with factory() as s:
            return (await s.get(Projection, (company_id, rid))).state["booked"]

    s1 = factory()
    try:
        await _record(s1, company_id, 2, "10.00")  # not committed yet
        task = asyncio.create_task(booked())
        await asyncio.sleep(0.5)
        assert not task.done(), "the booking read the received document without waiting for the holder"
        await s1.commit()
        assert (await asyncio.wait_for(task, timeout=10))["revision_seq"] == 2
    finally:
        await s1.close()
        async with factory() as s:
            await s.execute(delete(Projection).where(Projection.company_id == company_id))
            await s.execute(delete(LedgerEntry).where(LedgerEntry.company_id == company_id))
            await s.execute(delete(Company).where(Company.id == company_id))
            await s.commit()


async def _drafts(factory, company_id) -> list[dict]:
    async with factory() as s:
        return [p.state for p in (await s.execute(select(Projection).where(
            Projection.company_id == company_id, Projection.entity_type == "doc"))).scalars()]


@pytest.mark.asyncio
async def test_a_booking_whose_marker_fails_leaves_no_draft(_db_engine, monkeypatch):
    """The draft and the received document's booked marker are one unit: a booking that
    fails after the draft is made leaves neither, and booking again makes one draft."""
    from types import SimpleNamespace

    from celerp_docs import received

    factory = async_sessionmaker(bind=_db_engine, class_=AsyncSession, expire_on_commit=False)
    company_id = uuid.uuid4()
    user = SimpleNamespace(id=None)
    async with factory() as s:
        s.add(Company(id=company_id, name="BookCo", slug=f"book-{company_id.hex[:8]}"))
        await s.commit()
    try:
        async with factory() as s:
            rid = await _record(s, company_id, 1, "5.00")
            await s.commit()
        real_emit = received._emit

        async def failing_emit(session, cid, rid_, event_type, *args):
            if event_type == "received_doc.booked":
                raise RuntimeError("the marker failed")
            return await real_emit(session, cid, rid_, event_type, *args)

        monkeypatch.setattr(received, "_emit", failing_emit)
        async with factory() as s:
            with pytest.raises(RuntimeError, match="the marker failed"):
                await received.book(s, company_id, rid, role="owner", settings={}, user=user)
        assert await _drafts(factory, company_id) == [], "the draft committed without its booked marker"

        monkeypatch.setattr(received, "_emit", real_emit)
        async with factory() as s:
            await received.book(s, company_id, rid, role="owner", settings={}, user=user)
        assert len(await _drafts(factory, company_id)) == 1
    finally:
        async with factory() as s:
            await s.execute(delete(Projection).where(Projection.company_id == company_id))
            await s.execute(delete(LedgerEntry).where(LedgerEntry.company_id == company_id))
            await s.execute(delete(Company).where(Company.id == company_id))
            await s.commit()


@pytest.mark.asyncio
async def test_a_draft_update_whose_marker_fails_leaves_the_draft_as_it_was(_db_engine, monkeypatch):
    """Following a new revision changes the draft and records the version followed in one
    unit: a follow that fails after the draft is patched leaves the draft as it was."""
    from types import SimpleNamespace

    from celerp_docs import received

    factory = async_sessionmaker(bind=_db_engine, class_=AsyncSession, expire_on_commit=False)
    company_id = uuid.uuid4()
    user = SimpleNamespace(id=None)
    async with factory() as s:
        s.add(Company(id=company_id, name="BookCo", slug=f"book-{company_id.hex[:8]}"))
        await s.commit()
    try:
        async with factory() as s:
            rid = await _record(s, company_id, 1, "5.00")
            await s.commit()
        async with factory() as s:
            await received.book(s, company_id, rid, role="owner", settings={}, user=user)
        async with factory() as s:
            await _record(s, company_id, 2, "10.00")
            await s.commit()
        before = await _drafts(factory, company_id)
        real_emit = received._emit

        async def failing_emit(session, cid, rid_, event_type, *args):
            if event_type == "received_doc.draft_updated":
                raise RuntimeError("the marker failed")
            return await real_emit(session, cid, rid_, event_type, *args)

        monkeypatch.setattr(received, "_emit", failing_emit)
        async with factory() as s:
            with pytest.raises(RuntimeError, match="the marker failed"):
                await received.update_draft(s, company_id, rid, role="owner", settings={}, user=user)
        assert await _drafts(factory, company_id) == before, "the draft changed without the version it follows"
    finally:
        async with factory() as s:
            await s.execute(delete(Projection).where(Projection.company_id == company_id))
            await s.execute(delete(LedgerEntry).where(LedgerEntry.company_id == company_id))
            await s.execute(delete(Company).where(Company.id == company_id))
            await s.commit()


async def _edit_notes(factory, company_id, target: str, user) -> None:
    from celerp_docs.routes import DocPatch, patch_doc

    async with factory() as s:
        await patch_doc(target, DocPatch(fields_changed={"notes": {"old": None, "new": "edited here"}}),
                        company_id, None, "owner", {}, user, s)


@pytest.mark.asyncio
@pytest.mark.parametrize("edit_first", [False, True], ids=["update-first", "edit-first"])
async def test_a_local_edit_and_a_draft_update_at_once_keep_the_local_edit(_db_engine, monkeypatch, edit_first):
    """Following a revision and a local edit of the same draft run one after the other under
    the company lock. An edit that waits for the update lands after it, on the updated draft;
    an update that waits for the edit is refused, so the edit is never overwritten and the
    draft needs reconciling."""
    from types import SimpleNamespace

    from celerp_docs import received

    factory = async_sessionmaker(bind=_db_engine, class_=AsyncSession, expire_on_commit=False)
    company_id = uuid.uuid4()
    user = SimpleNamespace(id=None)
    async with factory() as s:
        s.add(Company(id=company_id, name="BookCo", slug=f"book-{company_id.hex[:8]}"))
        await s.commit()
    try:
        async with factory() as s:
            rid = await _record(s, company_id, 1, "5.00")
            await s.commit()
        async with factory() as s:
            target = (await received.book(s, company_id, rid, role="owner", settings={}, user=user))["id"]
        async with factory() as s:
            await _record(s, company_id, 2, "10.00")
            await s.commit()

        async def update():
            async with factory() as s:
                return await received.update_draft(s, company_id, rid, role="owner", settings={}, user=user)

        reached, release = asyncio.Event(), asyncio.Event()
        real_fresh = received._fresh

        async def paused_fresh(session, cid, entity_id):
            if entity_id == target:
                reached.set()
                await release.wait()
            return await real_fresh(session, cid, entity_id)

        monkeypatch.setattr(received, "_fresh", paused_fresh)
        if edit_first:
            await _edit_notes(factory, company_id, target, user)
            release.set()
            with pytest.raises(HTTPException) as refused:
                await asyncio.wait_for(update(), timeout=10)
            assert refused.value.status_code == 409
        else:
            updating = asyncio.create_task(update())
            await asyncio.wait_for(reached.wait(), timeout=10)
            editing = asyncio.create_task(_edit_notes(factory, company_id, target, user))
            await asyncio.sleep(0.5)
            assert not editing.done(), "the local edit wrote while the update held the draft"
            release.set()
            await asyncio.wait_for(updating, timeout=10)
            await asyncio.wait_for(editing, timeout=10)
        monkeypatch.setattr(received, "_fresh", real_fresh)

        async with factory() as s:
            draft = await s.get(Projection, (company_id, target))
            source = await s.get(Projection, (company_id, rid))
            assert draft.state["notes"] == "edited here"
            assert float(draft.state["total"]) == (5.0 if edit_first else 10.0)
            assert received.revision_state(source.state, draft) == (
                received.NEEDS_RECONCILIATION if edit_first else received.BOOKED)
    finally:
        async with factory() as s:
            await s.execute(delete(Projection).where(Projection.company_id == company_id))
            await s.execute(delete(LedgerEntry).where(LedgerEntry.company_id == company_id))
            await s.execute(delete(Company).where(Company.id == company_id))
            await s.commit()
