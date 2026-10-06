# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Tests for celerp/routers/notifications.py"""

from __future__ import annotations

import os
import secrets
import uuid
from unittest.mock import AsyncMock, patch

os.environ.setdefault("ALLOW_INSECURE_JWT", "true")

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.db import get_session
from celerp.main import app
import celerp.gateway.state as gw_state

# `session` (Postgres, rollback-isolated) comes from the root conftest.


@pytest_asyncio.fixture
async def auth_client(session: AsyncSession):
    from celerp.services.session_tracker import clear as _clear_tracker
    await _clear_tracker(session)
    app.dependency_overrides[get_session] = lambda: session
    app.state.limiter.enabled = False
    app.state.limiter._storage.reset()
    token = secrets.token_hex(32)
    gw_state.set_session_token(token)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        await c.post("/auth/register", json={
            "company_name": "NotifCo", "email": "notif@test.example",
            "name": "Admin", "password": "pwvalid1",
        })
        r = await c.post("/auth/login", json={"email": "notif@test.example", "password": "pwvalid1"})
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
    from celerp.models.accounting import UserCompany
    user = (await session.execute(select(User).where(User.email == "notif@test.example"))).scalars().first()
    uc = (await session.execute(select(UserCompany).where(UserCompany.user_id == user.id))).scalars().first()

    n = Notification(
        company_id=uc.company_id, user_id=user.id,
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


@pytest.mark.asyncio
async def test_unread_only_hides_read_notifications(auth_client, session):
    """The bell fetches ?unread_only=true, so a dismissed notification must
    not come back on the next fetch (the reported 'comes back on refresh' bug)."""
    c, headers = auth_client
    from celerp.models.notification import Notification
    from celerp.models.company import User
    from celerp.models.accounting import UserCompany
    from sqlalchemy import select

    user = (await session.execute(select(User).where(User.email == "notif@test.example"))).scalars().first()
    uc = (await session.execute(select(UserCompany).where(UserCompany.user_id == user.id))).scalars().first()
    keep = Notification(company_id=uc.company_id, user_id=user.id, category="email", title="Keep", body="B")
    dismiss = Notification(company_id=uc.company_id, user_id=user.id, category="email", title="Dismiss", body="B")
    session.add_all([keep, dismiss])
    await session.commit()
    await session.refresh(dismiss)

    # Dismiss one, then the unread-only bell shows only the other.
    assert (await c.post(f"/notifications/{dismiss.id}/read", headers=headers)).status_code == 204
    data = (await c.get("/notifications?unread_only=true", headers=headers)).json()
    titles = [i["title"] for i in data["items"]]
    assert titles == ["Keep"]
    assert data["unread_count"] == 1
    # The full list still has both (read history is retained server-side).
    alld = (await c.get("/notifications", headers=headers)).json()
    assert {i["title"] for i in alld["items"]} == {"Keep", "Dismiss"}


# ── POST /notifications/{id}/read ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_mark_read_success(auth_client, session):
    c, headers = auth_client
    from celerp.models.notification import Notification
    from celerp.models.company import User
    from sqlalchemy import select

    from celerp.models.accounting import UserCompany
    user = (await session.execute(select(User).where(User.email == "notif@test.example"))).scalars().first()
    uc = (await session.execute(select(UserCompany).where(UserCompany.user_id == user.id))).scalars().first()
    n = Notification(
        company_id=uc.company_id, user_id=user.id,
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

    from celerp.models.accounting import UserCompany
    user = (await session.execute(select(User).where(User.email == "notif@test.example"))).scalars().first()
    uc = (await session.execute(select(UserCompany).where(UserCompany.user_id == user.id))).scalars().first()
    for i in range(3):
        session.add(Notification(
            company_id=uc.company_id, user_id=user.id,
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


# ── Per-user read state ──────────────────────────────────────────────────────

async def _caller(session):
    """(user_id, company_id) of the user the auth_client fixture registered."""
    from sqlalchemy import select

    from celerp.models.accounting import UserCompany
    from celerp.models.company import User

    user = (await session.execute(select(User).where(User.email == "notif@test.example"))).scalars().first()
    uc = (await session.execute(select(UserCompany).where(UserCompany.user_id == user.id))).scalars().first()
    return user.id, uc.company_id


async def _second_user(c, session, headers) -> tuple[uuid.UUID, dict]:
    """A second member of the same company, signed in: (user_id, headers)."""
    from sqlalchemy import select

    from celerp.models.company import User
    from celerp.services.session_tracker import clear as _clear_tracker

    r = await c.post("/companies/me/users", headers=headers, json={
        "name": "Second", "email": "second@test.example", "password": "pwvalid1", "role": "operator"})
    assert r.status_code == 200, r.text
    await _clear_tracker(session)
    r = await c.post("/auth/login", json={"email": "second@test.example", "password": "pwvalid1"})
    assert r.status_code == 200, r.text
    uid = (await session.execute(select(User.id).where(User.email == "second@test.example"))).scalar_one()
    return uid, {**headers, "Authorization": f"Bearer {r.json()['access_token']}"}


async def _add_notice(session, company_id, user_id=None, title="Notice"):
    from celerp.models.notification import Notification

    n = Notification(company_id=company_id, user_id=user_id, category="system", title=title, body="B")
    session.add(n)
    await session.commit()
    await session.refresh(n)
    return n


@pytest.mark.asyncio
async def test_company_wide_notice_read_by_one_user_stays_unread_for_others(auth_client, session):
    """Each user closes a company-wide notice for themselves only."""
    c, headers = auth_client
    _, company_id = await _caller(session)
    _, headers_b = await _second_user(c, session, headers)
    n = await _add_notice(session, company_id, title="Everyone")

    assert (await c.post(f"/notifications/{n.id}/read", headers=headers)).status_code == 204

    mine = (await c.get("/notifications", headers=headers)).json()
    assert mine["unread_count"] == 0
    assert [i["read"] for i in mine["items"]] == [True]
    assert (await c.get("/notifications?unread_only=true", headers=headers)).json()["items"] == []

    theirs = (await c.get("/notifications?unread_only=true", headers=headers_b)).json()
    assert [i["title"] for i in theirs["items"]] == ["Everyone"]
    assert [i["read"] for i in theirs["items"]] == [False]
    assert theirs["unread_count"] == 1


@pytest.mark.asyncio
async def test_mark_all_read_only_affects_caller(auth_client, session):
    c, headers = auth_client
    _, company_id = await _caller(session)
    uid_b, headers_b = await _second_user(c, session, headers)
    await _add_notice(session, company_id, title="Everyone")
    await _add_notice(session, company_id, user_id=uid_b, title="For B")

    assert (await c.post("/notifications/read-all", headers=headers)).status_code == 204

    assert (await c.get("/notifications", headers=headers)).json()["unread_count"] == 0
    theirs = (await c.get("/notifications?unread_only=true", headers=headers_b)).json()
    assert sorted(i["title"] for i in theirs["items"]) == ["Everyone", "For B"]
    assert theirs["unread_count"] == 2


@pytest.mark.asyncio
async def test_user_cannot_mark_another_users_notice_read(auth_client, session):
    c, headers = auth_client
    uid_a, company_id = await _caller(session)
    _, headers_b = await _second_user(c, session, headers)
    n = await _add_notice(session, company_id, user_id=uid_a, title="For A")

    r = await c.post(f"/notifications/{n.id}/read", headers=headers_b)
    assert r.status_code == 404

    mine = (await c.get("/notifications?unread_only=true", headers=headers)).json()
    assert [i["title"] for i in mine["items"]] == ["For A"]
    assert mine["unread_count"] == 1


@pytest.mark.asyncio
async def test_mark_read_twice_is_idempotent(auth_client, session):
    """Re-marking is a no-op: one receipt, no error, the notice stays read."""
    from sqlalchemy import text

    c, headers = auth_client
    uid_a, company_id = await _caller(session)
    n = await _add_notice(session, company_id, title="Twice")

    assert (await c.post(f"/notifications/{n.id}/read", headers=headers)).status_code == 204
    assert (await c.post(f"/notifications/{n.id}/read", headers=headers)).status_code == 204
    assert (await c.post("/notifications/read-all", headers=headers)).status_code == 204

    receipts = (await session.execute(text(
        "SELECT count(*) FROM notification_reads WHERE notification_id = :n AND user_id = :u"),
        {"n": n.id, "u": uid_a})).scalar_one()
    assert receipts == 1
    assert (await c.get("/notifications", headers=headers)).json()["unread_count"] == 0
