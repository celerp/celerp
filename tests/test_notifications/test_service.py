# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Tests for celerp/notifications/service.py"""

from __future__ import annotations

import os
import uuid

os.environ.setdefault("ALLOW_INSECURE_JWT", "true")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from unittest.mock import AsyncMock, patch

from celerp.models.base import Base
from celerp.models.company import Company, User
from celerp.models.notification import Notification
from celerp.notifications import service as svc

_DB_URL = "sqlite+aiosqlite:///:memory:"


@pytest_asyncio.fixture
async def session() -> AsyncSession:
    engine = create_async_engine(_DB_URL)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as sess:
        yield sess
    await engine.dispose()


@pytest_asyncio.fixture
async def company(session) -> Company:
    c = Company(name="TestCo", slug="testco", settings={})
    session.add(c)
    await session.commit()
    await session.refresh(c)
    return c


@pytest_asyncio.fixture
async def user(session, company) -> User:
    u = User(company_id=company.id, email="test@test.com", name="Test", role="owner")
    session.add(u)
    await session.commit()
    await session.refresh(u)
    return u


@pytest_asyncio.fixture
async def user_b(session, company) -> User:
    u = User(company_id=company.id, email="b@test.com", name="UserB", role="user")
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
    u = User(company_id=company_b.id, email="other@other.com", name="Other", role="owner")
    session.add(u)
    await session.commit()
    await session.refresh(u)
    return u


# ── create ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_create_notification(session, company, user):
    with patch("celerp.notifications.service.publish", new_callable=AsyncMock):
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
    assert n.read is False
    assert n.company_id == company.id
    assert n.user_id == user.id


@pytest.mark.asyncio
async def test_create_notification_company_wide(session, company):
    with patch("celerp.notifications.service.publish", new_callable=AsyncMock):
        n = await svc.create(
            session, company.id, "system", "New version", "v2.1 available",
        )
        await session.commit()

    assert n.user_id is None
    assert n.priority == "medium"  # default


@pytest.mark.asyncio
async def test_create_notification_publishes_sse(session, company, user):
    mock_pub = AsyncMock()
    with patch("celerp.notifications.service.publish", mock_pub):
        await svc.create(session, company.id, "ai", "Done", "Body", user_id=user.id)
        await session.commit()

    mock_pub.assert_called_once()
    event = mock_pub.call_args[0][2]
    assert event["type"] == "notification"
    assert event["title"] == "Done"


# ── unread_count ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_unread_count(session, company, user):
    with patch("celerp.notifications.service.publish", new_callable=AsyncMock):
        await svc.create(session, company.id, "ai", "N1", "B1", user_id=user.id)
        await svc.create(session, company.id, "ai", "N2", "B2", user_id=user.id)
        await session.commit()

    count = await svc.get_unread_count(session, company.id, user.id)
    assert count == 2


@pytest.mark.asyncio
async def test_unread_count_includes_company_wide(session, company, user):
    with patch("celerp.notifications.service.publish", new_callable=AsyncMock):
        await svc.create(session, company.id, "system", "Update", "v2", user_id=None)
        await svc.create(session, company.id, "ai", "Personal", "B", user_id=user.id)
        await session.commit()

    count = await svc.get_unread_count(session, company.id, user.id)
    assert count == 2  # both personal + company-wide


@pytest.mark.asyncio
async def test_unread_count_excludes_other_users(session, company, user, user_b):
    with patch("celerp.notifications.service.publish", new_callable=AsyncMock):
        await svc.create(session, company.id, "ai", "ForB", "B", user_id=user_b.id)
        await session.commit()

    count = await svc.get_unread_count(session, company.id, user.id)
    assert count == 0


# ── list ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_notifications_newest_first(session, company, user):
    with patch("celerp.notifications.service.publish", new_callable=AsyncMock):
        await svc.create(session, company.id, "ai", "First", "B1", user_id=user.id)
        await svc.create(session, company.id, "ai", "Second", "B2", user_id=user.id)
        await session.commit()

    items = await svc.list_notifications(session, company.id, user.id)
    assert len(items) == 2
    assert items[0].title == "Second"
    assert items[1].title == "First"


@pytest.mark.asyncio
async def test_list_notifications_pagination(session, company, user):
    with patch("celerp.notifications.service.publish", new_callable=AsyncMock):
        for i in range(5):
            await svc.create(session, company.id, "ai", f"N{i}", "B", user_id=user.id)
        await session.commit()

    page1 = await svc.list_notifications(session, company.id, user.id, limit=2, offset=0)
    page2 = await svc.list_notifications(session, company.id, user.id, limit=2, offset=2)
    assert len(page1) == 2
    assert len(page2) == 2
    assert page1[0].id != page2[0].id


# ── mark_read ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_mark_read(session, company, user):
    with patch("celerp.notifications.service.publish", new_callable=AsyncMock):
        n = await svc.create(session, company.id, "ai", "Test", "B", user_id=user.id)
        await session.commit()

    found = await svc.mark_read(session, n.id, company.id)
    await session.commit()
    assert found is True

    count = await svc.get_unread_count(session, company.id, user.id)
    assert count == 0


@pytest.mark.asyncio
async def test_mark_read_wrong_company(session, company, company_b, user):
    with patch("celerp.notifications.service.publish", new_callable=AsyncMock):
        n = await svc.create(session, company.id, "ai", "Test", "B", user_id=user.id)
        await session.commit()

    found = await svc.mark_read(session, n.id, company_b.id)
    assert found is False


@pytest.mark.asyncio
async def test_mark_read_nonexistent(session, company):
    found = await svc.mark_read(session, uuid.uuid4(), company.id)
    assert found is False


# ── mark_all_read ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_mark_all_read(session, company, user):
    with patch("celerp.notifications.service.publish", new_callable=AsyncMock):
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
    with patch("celerp.notifications.service.publish", new_callable=AsyncMock):
        for i in range(105):
            await svc.create(session, company.id, "ai", f"N{i}", "B", user_id=user.id)
        await session.commit()

    items = await svc.list_notifications(session, company.id, user.id, limit=200)
    assert len(items) <= 100


# ── isolation ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_isolation_between_companies(session, company, company_b, user, user_b_co):
    with patch("celerp.notifications.service.publish", new_callable=AsyncMock):
        await svc.create(session, company.id, "ai", "CoA", "B", user_id=user.id)
        await svc.create(session, company_b.id, "ai", "CoB", "B", user_id=user_b_co.id)
        await session.commit()

    items_a = await svc.list_notifications(session, company.id, user.id)
    items_b = await svc.list_notifications(session, company_b.id, user_b_co.id)
    assert len(items_a) == 1
    assert items_a[0].title == "CoA"
    assert len(items_b) == 1
    assert items_b[0].title == "CoB"
