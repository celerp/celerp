# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

"""Tests for celerp/routers/notifications.py"""

from __future__ import annotations

import os
import secrets
import uuid
from unittest.mock import AsyncMock, patch

os.environ.setdefault("ALLOW_INSECURE_JWT", "true")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from celerp.db import get_session
from celerp.main import app
from celerp.models.base import Base
import celerp.gateway.state as gw_state

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
async def auth_client(session: AsyncSession):
    from celerp.services.session_tracker import clear as _clear_tracker
    _clear_tracker()
    app.dependency_overrides[get_session] = lambda: session
    app.state.limiter.enabled = False
    app.state.limiter._storage.reset()
    token = secrets.token_hex(32)
    gw_state.set_session_token(token)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        await c.post("/auth/register", json={
            "company_name": "NotifCo", "email": "notif@test.com",
            "name": "Admin", "password": "pw",
        })
        r = await c.post("/auth/login", json={"email": "notif@test.com", "password": "pw"})
        jwt = r.json()["access_token"]
        headers = {
            "Authorization": f"Bearer {jwt}",
            "X-Session-Token": token,
        }
        yield c, headers

    app.dependency_overrides.clear()
    gw_state.set_session_token("")


# ── GET /notifications ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_notifications_empty(auth_client):
    c, headers = auth_client
    r = await c.get("/notifications", headers=headers)
    assert r.status_code == 200
    data = r.json()
    assert data["items"] == []
    assert data["unread_count"] == 0


@pytest.mark.asyncio
async def test_list_notifications_with_data(auth_client, session):
    c, headers = auth_client

    from celerp.models.notification import Notification
    from celerp.models.company import User
    from sqlalchemy import select

    # Get user_id and company_id from the registered user
    user = (await session.execute(select(User).where(User.email == "notif@test.com"))).scalars().first()

    n = Notification(
        company_id=user.company_id, user_id=user.id,
        category="ai", title="Test", body="Body",
        priority="high",
    )
    session.add(n)
    await session.commit()

    r = await c.get("/notifications", headers=headers)
    assert r.status_code == 200
    data = r.json()
    assert len(data["items"]) == 1
    assert data["items"][0]["title"] == "Test"
    assert data["unread_count"] == 1


# ── POST /notifications/{id}/read ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_mark_read_success(auth_client, session):
    c, headers = auth_client
    from celerp.models.notification import Notification
    from celerp.models.company import User
    from sqlalchemy import select

    user = (await session.execute(select(User).where(User.email == "notif@test.com"))).scalars().first()
    n = Notification(
        company_id=user.company_id, user_id=user.id,
        category="ai", title="Read me", body="B",
    )
    session.add(n)
    await session.commit()
    await session.refresh(n)

    r = await c.post(f"/notifications/{n.id}/read", headers=headers)
    assert r.status_code == 204

    # Verify unread count is now 0
    r2 = await c.get("/notifications", headers=headers)
    assert r2.json()["unread_count"] == 0


@pytest.mark.asyncio
async def test_mark_read_404(auth_client):
    c, headers = auth_client
    r = await c.post(f"/notifications/{uuid.uuid4()}/read", headers=headers)
    assert r.status_code == 404


# ── POST /notifications/read-all ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_mark_all_read(auth_client, session):
    c, headers = auth_client
    from celerp.models.notification import Notification
    from celerp.models.company import User
    from sqlalchemy import select

    user = (await session.execute(select(User).where(User.email == "notif@test.com"))).scalars().first()
    for i in range(3):
        session.add(Notification(
            company_id=user.company_id, user_id=user.id,
            category="ai", title=f"N{i}", body="B",
        ))
    await session.commit()

    r = await c.post("/notifications/read-all", headers=headers)
    assert r.status_code == 204

    r2 = await c.get("/notifications", headers=headers)
    assert r2.json()["unread_count"] == 0


# ── Auth guard ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_auth_required(auth_client):
    c, headers = auth_client
    # No auth headers
    r = await c.get("/notifications")
    assert r.status_code == 401


# ── GET /notifications/stream ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_stream_endpoint_exists(auth_client):
    """Verify the SSE endpoint is registered and returns 200.

    ASGITransport doesn't support real HTTP timeouts, so we test
    the endpoint exists by checking the route is registered.
    Functional SSE tests are in test_sse.py (unit level).
    """
    c, headers = auth_client
    # Verify the route is registered by checking a non-streaming endpoint still works
    # (The SSE test is covered in test_sse.py at the unit level)
    from celerp.main import app as _app
    route_paths = [r.path for r in _app.routes if hasattr(r, "path")]
    assert "/notifications/stream" in route_paths
