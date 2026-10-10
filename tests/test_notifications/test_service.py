# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Tests for celerp/notifications/service.py"""

from __future__ import annotations

import asyncio
import os
import uuid

os.environ.setdefault("ALLOW_INSECURE_JWT", "true")

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from unittest.mock import MagicMock, patch

from celerp.models.company import Company, User
from celerp.models.notification import Notification
from celerp.notifications import service as svc

# `session` (Postgres, rollback-isolated) comes from the root conftest.


@pytest_asyncio.fixture
async def company(session) -> Company:
    c = Company(name="TestCo", slug="testco", settings={})
    session.add(c)
    await session.commit()
    await session.refresh(c)
    return c


@pytest_asyncio.fixture
async def user(session, company) -> User:
    u = User(email="test@test.com", name="Test")
    session.add(u)
    await session.commit()
    await session.refresh(u)
    return u


@pytest_asyncio.fixture
async def user_b(session, company) -> User:
    u = User(email="b@test.com", name="UserB")
    session.add(u)
    await session.commit()
    await session.refresh(u)
    return u


@pytest_asyncio.fixture
async def company_b(session) -> Company:
    c = Company(name="OtherCo", slug="otherco", settings={})
    session.add(c)
    await session.commit()
    await session.refresh(c)
    return c


@pytest_asyncio.fixture
async def user_b_co(session, company_b) -> User:
    u = User(email="other@other.com", name="Other")
    session.add(u)
    await session.commit()
    await session.refresh(u)
    return u


# ── create ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_create_notification(session, company, user):
    with patch("celerp.notifications.service.deliver"):
        n = await svc.create(
            session, company.id, "ai", "Batch done", "5 files processed",
            user_id=user.id, action_url="/ai", priority="high",
        )
        await session.commit()

    assert n.id is not None
    assert n.category == "ai"
    assert n.title == "Batch done"
    assert n.body == "5 files processed"
    assert n.action_url == "/ai"
    assert n.priority == "high"
    assert n.company_id == company.id
    assert n.user_id == user.id


@pytest.mark.asyncio
async def test_create_notification_company_wide(session, company):
    with patch("celerp.notifications.service.deliver"):
        n = await svc.create(
            session, company.id, "system", "New version", "v2.1 available",
        )
        await session.commit()

    assert n.user_id is None
    assert n.priority == "medium"  # default


@pytest.mark.asyncio
async def test_create_notification_publishes_sse(session, company, user):
    mock_pub = MagicMock()
    with patch("celerp.notifications.service.deliver", mock_pub):
        await svc.create(session, company.id, "ai", "Done", "Body", user_id=user.id)
        await session.commit()

    mock_pub.assert_called_once()
    event = mock_pub.call_args[0][2]
    assert event["type"] == "notification"
    assert event["title"] == "Done"


# ── unread_count ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_unread_count(session, company, user):
    with patch("celerp.notifications.service.deliver"):
        await svc.create(session, company.id, "ai", "N1", "B1", user_id=user.id)
        await svc.create(session, company.id, "ai", "N2", "B2", user_id=user.id)
        await session.commit()

    count = await svc.get_unread_count(session, company.id, user.id)
    assert count == 2


@pytest.mark.asyncio
async def test_unread_count_includes_company_wide(session, company, user):
    with patch("celerp.notifications.service.deliver"):
        await svc.create(session, company.id, "system", "Update", "v2", user_id=None)
        await svc.create(session, company.id, "ai", "Personal", "B", user_id=user.id)
        await session.commit()

    count = await svc.get_unread_count(session, company.id, user.id)
    assert count == 2  # both personal + company-wide


@pytest.mark.asyncio
async def test_unread_count_excludes_other_users(session, company, user, user_b):
    with patch("celerp.notifications.service.deliver"):
        await svc.create(session, company.id, "ai", "ForB", "B", user_id=user_b.id)
        await session.commit()

    count = await svc.get_unread_count(session, company.id, user.id)
    assert count == 0


# ── list ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_notifications_newest_first(session, company, user):
    with patch("celerp.notifications.service.deliver"):
        await svc.create(session, company.id, "ai", "First", "B1", user_id=user.id)
        await svc.create(session, company.id, "ai", "Second", "B2", user_id=user.id)
        await session.commit()

    items = [n for n, _ in await svc.list_notifications(session, company.id, user.id)]
    assert len(items) == 2
    assert items[0].title == "Second"
    assert items[1].title == "First"


@pytest.mark.asyncio
async def test_the_bell_lists_notices_that_ask_for_action_first(session, company, user):
    """The bell (unread only) puts high-priority notices first, newest first within each
    priority, so one asking the user to act is not buried under later news. The full list
    stays newest first."""
    with patch("celerp.notifications.service.deliver"):
        await svc.create(session, company.id, "accounting", "Act", "B0", priority="high")
        await svc.create(session, company.id, "ai", "News 1", "B1", user_id=user.id)
        await svc.create(session, company.id, "ai", "News 2", "B2", user_id=user.id)
        await session.commit()

    bell = await svc.list_notifications(session, company.id, user.id, unread_only=True)
    assert [n.title for n, _ in bell] == ["Act", "News 2", "News 1"]
    every = await svc.list_notifications(session, company.id, user.id)
    assert [n.title for n, _ in every] == ["News 2", "News 1", "Act"]


@pytest.mark.asyncio
async def test_list_notifications_pagination(session, company, user):
    with patch("celerp.notifications.service.deliver"):
        for i in range(5):
            await svc.create(session, company.id, "ai", f"N{i}", "B", user_id=user.id)
        await session.commit()

    page1 = [n for n, _ in await svc.list_notifications(session, company.id, user.id, limit=2, offset=0)]
    page2 = [n for n, _ in await svc.list_notifications(session, company.id, user.id, limit=2, offset=2)]
    assert len(page1) == 2
    assert len(page2) == 2
    assert page1[0].id != page2[0].id


# ── mark_read ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_mark_read(session, company, user):
    with patch("celerp.notifications.service.deliver"):
        n = await svc.create(session, company.id, "ai", "Test", "B", user_id=user.id)
        await session.commit()

    found = await svc.mark_read(session, n.id, company.id, user.id)
    await session.commit()
    assert found is True

    count = await svc.get_unread_count(session, company.id, user.id)
    assert count == 0


@pytest.mark.asyncio
async def test_mark_read_wrong_company(session, company, company_b, user):
    with patch("celerp.notifications.service.deliver"):
        n = await svc.create(session, company.id, "ai", "Test", "B", user_id=user.id)
        await session.commit()

    found = await svc.mark_read(session, n.id, company_b.id, user.id)
    assert found is False


@pytest.mark.asyncio
async def test_mark_read_nonexistent(session, company, user):
    found = await svc.mark_read(session, uuid.uuid4(), company.id, user.id)
    assert found is False


@pytest.mark.asyncio
async def test_mark_read_of_a_notice_deleted_meanwhile_is_not_found(committed_engine):
    """A notice deleted (pruned, or its company removed) while it is being marked
    read is simply not found."""
    mk = async_sessionmaker(committed_engine, expire_on_commit=False)
    async with mk() as s:
        co = Company(name="Race", slug="race", settings={})
        s.add(co)
        await s.flush()
        u = User(email="race@test.com", name="U")
        s.add(u)
        await s.flush()
        n = Notification(company_id=co.id, category="system", title="t", body="b")
        s.add(n)
        await s.commit()
        cid, uid, nid = co.id, u.id, n.id

    async with mk() as deleter:
        await deleter.execute(text("DELETE FROM notifications WHERE id = :i"), {"i": nid})

        async def reader():
            async with mk() as s:
                found = await svc.mark_read(s, nid, cid, uid)
                await s.commit()
                return found

        task = asyncio.create_task(reader())
        for _ in range(400):
            if task.done():
                break
            async with committed_engine.connect() as conn:
                waiting = (await conn.execute(text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = current_database() AND wait_event_type = 'Lock'"
                ))).scalar_one()
            if waiting:
                break
            await asyncio.sleep(0.05)
        await deleter.commit()
    assert await asyncio.wait_for(task, timeout=30) is False


# ── mark_all_read ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_mark_all_read(session, company, user):
    with patch("celerp.notifications.service.deliver"):
        await svc.create(session, company.id, "ai", "N1", "B1", user_id=user.id)
        await svc.create(session, company.id, "ai", "N2", "B2", user_id=user.id)
        await svc.create(session, company.id, "system", "N3", "B3", user_id=None)
        await session.commit()

    updated = await svc.mark_all_read(session, company.id, user.id)
    await session.commit()
    assert updated == 3

    count = await svc.get_unread_count(session, company.id, user.id)
    assert count == 0


# ── retention ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_retention_100_per_company(session, company, user):
    with patch("celerp.notifications.service.deliver"):
        for i in range(105):
            await svc.create(session, company.id, "ai", f"N{i}", "B", user_id=user.id)
        await session.commit()

    items = [n for n, _ in await svc.list_notifications(session, company.id, user.id, limit=200)]
    assert len(items) <= 100


# ── isolation ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_isolation_between_companies(session, company, company_b, user, user_b_co):
    with patch("celerp.notifications.service.deliver"):
        await svc.create(session, company.id, "ai", "CoA", "B", user_id=user.id)
        await svc.create(session, company_b.id, "ai", "CoB", "B", user_id=user_b_co.id)
        await session.commit()

    items_a = [n for n, _ in await svc.list_notifications(session, company.id, user.id)]
    items_b = [n for n, _ in await svc.list_notifications(session, company_b.id, user_b_co.id)]
    assert len(items_a) == 1
    assert items_a[0].title == "CoA"
    assert len(items_b) == 1
    assert items_b[0].title == "CoB"


@pytest.mark.asyncio
async def test_list_notifications_stable_order_when_created_at_ties(session, company, user):
    """Regression: notifications sharing a created_at must order deterministically by id, so
    OFFSET pagination can't skip or duplicate a tied row across pages."""
    from datetime import datetime, timezone
    from sqlalchemy import update

    ids = []
    with patch("celerp.notifications.service.deliver"):
        for i in range(6):
            n = await svc.create(session, company.id, "ai", f"N{i}", "B", user_id=user.id)
            ids.append(n.id)
        await session.commit()

    # Force a created_at tie so the id tiebreaker is the only thing ordering them.
    await session.execute(
        update(Notification).where(Notification.id.in_(ids)).values(created_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    )
    await session.commit()

    listed = [n for n, _ in await svc.list_notifications(session, company.id, user.id, limit=100)]
    assert [n.id for n in listed] == sorted(ids, reverse=True), \
        "created_at-tied notifications are not deterministically ordered by id (no tiebreaker)"

    # And a paged walk over the tie covers every row exactly once.
    paged = []
    for off in range(0, len(ids), 2):
        page = [n for n, _ in await svc.list_notifications(session, company.id, user.id, limit=2, offset=off)]
        paged += [n.id for n in page]
    assert sorted(paged) == sorted(ids)
    assert len(paged) == len(set(paged))
