# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""A notification reaches the event stream only once its row is committed.

The browser answers a high-priority event by fetching /notifications for the
translated text, on another connection; an event published before the commit
sends it looking for a row it cannot see yet. Each test uses its own database
(committed_engine) so commits are real.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from celerp.models.company import Company
from celerp.models.notification import Notification
from celerp.notifications import service as svc
from celerp.notifications.sse import subscribe, unsubscribe


@pytest.fixture
async def setup(committed_engine):
    factory = async_sessionmaker(committed_engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        company = Company(name="Co", slug=f"co-{uuid.uuid4().hex[:6]}", settings={})
        s.add(company)
        await s.commit()
    viewer = uuid.uuid4()
    q = subscribe(company.id, viewer)
    try:
        yield factory, company.id, q
    finally:
        unsubscribe(company.id, viewer, q)


async def _visible_events(factory, q) -> list[dict]:
    """Drain ``q`` the way the browser does: each event triggers a fetch of its
    row on a separate connection. Returns the events whose row was found."""
    events = []
    while not q.empty():
        event = q.get_nowait()
        async with factory() as other:
            row = (await other.execute(
                select(Notification).where(Notification.id == uuid.UUID(event["id"])))).scalar_one_or_none()
        assert row is not None, "an event was published before its row was committed"
        events.append(event)
    return events


async def test_event_follows_the_commit_and_its_row_is_readable(setup):
    factory, company_id, q = setup
    async with factory() as s:
        await svc.create(s, company_id, "system", "Update failed", "Body", priority="high")
        assert await _visible_events(factory, q) == []
        await s.commit()
    events = await _visible_events(factory, q)
    assert [e["title"] for e in events] == ["Update failed"]


async def test_rolled_back_notification_is_never_published(setup):
    factory, company_id, q = setup
    async with factory() as s:
        await svc.create(s, company_id, "system", "Gone", "Body", priority="high")
        await s.rollback()
        await s.commit()
    assert q.empty()


async def test_notification_in_a_rolled_back_savepoint_is_never_published(setup):
    factory, company_id, q = setup
    async with factory() as s:
        await svc.create(s, company_id, "system", "Kept", "Body")
        savepoint = await s.begin_nested()
        await svc.create(s, company_id, "system", "Undone", "Body")
        await savepoint.rollback()
        await s.commit()
    assert [e["title"] for e in await _visible_events(factory, q)] == ["Kept"]


async def test_notification_in_a_released_savepoint_is_published_at_commit(setup):
    factory, company_id, q = setup
    async with factory() as s:
        async with s.begin_nested():
            await svc.create(s, company_id, "system", "Nested", "Body")
        assert q.empty()
        await s.commit()
    assert [e["title"] for e in await _visible_events(factory, q)] == ["Nested"]


async def test_session_closed_without_commit_publishes_nothing_later(setup):
    factory, company_id, q = setup
    s = factory()
    await svc.create(s, company_id, "system", "Abandoned", "Body")
    await s.close()
    await svc.create(s, company_id, "system", "Second", "Body")
    await s.commit()
    await s.close()
    assert [e["title"] for e in await _visible_events(factory, q)] == ["Second"]
