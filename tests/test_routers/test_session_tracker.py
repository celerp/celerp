# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

from __future__ import annotations

import time
import uuid
from unittest.mock import patch

import pytest

from celerp.services import session_tracker


@pytest.fixture(autouse=True)
def _clean_tracker():
    """Ensure tracker is clean before and after each test."""
    session_tracker.clear()
    yield
    session_tracker.clear()


# ── Unit tests for session_tracker ──────────────────────────────────────


def test_record_and_active():
    session_tracker.record("u1")
    assert "u1" in session_tracker.active_user_ids()


def test_active_excludes_self():
    session_tracker.record("u1")
    assert session_tracker.active_user_ids(exclude="u1") == set()


def test_active_multiple_users():
    session_tracker.record("u1")
    session_tracker.record("u2")
    assert session_tracker.active_user_ids() == {"u1", "u2"}
    assert session_tracker.active_user_ids(exclude="u1") == {"u2"}


def test_expired_entries_excluded():
    session_tracker.record("u1")
    session_tracker._activity["u1"] = time.monotonic() - session_tracker._WINDOW_SECONDS - 1
    assert session_tracker.active_user_ids() == set()


def test_evict():
    session_tracker.record("u1")
    session_tracker.record("u2")
    session_tracker.evict("u1")
    assert session_tracker.active_user_ids() == {"u2"}


def test_clear():
    session_tracker.record("u1")
    session_tracker.record("u2")
    session_tracker.clear()
    assert session_tracker.active_user_ids() == set()


# ── Integration tests for login gate ────────────────────────────────────


async def _bootstrap_and_seed(client, session):
    """Register the first user (bootstrap) and create a second user in the same company."""
    from celerp.services.auth import hash_password
    from celerp.models.company import User
    from celerp.models.accounting import UserCompany

    # Bootstrap: register first user + company
    r = await client.post(
        "/auth/register",
        json={"company_name": "TestCo", "email": "owner@test.com", "name": "Owner", "password": "pw"},
    )
    assert r.status_code == 200
    owner_token = r.json()["access_token"]

    # Get the company_id from the owner
    from sqlalchemy import select
    owner = (await session.execute(select(User).where(User.email == "owner@test.com"))).scalar_one()
    company_id = owner.company_id

    # Create second user directly in DB
    user2_id = uuid.uuid4()
    user2 = User(
        id=user2_id, company_id=company_id, email="staff@test.com",
        name="Staff", role="staff", auth_hash=hash_password("pw"), is_active=True,
    )
    session.add(user2)
    session.add(UserCompany(id=uuid.uuid4(), user_id=user2_id, company_id=company_id, role="staff"))
    await session.commit()

    return owner_token


@pytest.mark.asyncio
async def test_login_blocked_when_other_user_active(client, session):
    """Second user login returns 409 when no relay connected."""
    owner_token = await _bootstrap_and_seed(client, session)
    # Owner makes an authenticated request -> populates tracker
    await client.get("/items", headers={"Authorization": f"Bearer {owner_token}"})

    with patch("celerp.gateway.client._client", None):
        r = await client.post("/auth/login", json={"email": "staff@test.com", "password": "pw"})
    assert r.status_code == 409
    assert r.json()["detail"] == "direct_connection_limit"


@pytest.mark.asyncio
async def test_login_allowed_when_relay_connected(client, session):
    """Login is not blocked when relay is connected."""
    owner_token = await _bootstrap_and_seed(client, session)
    await client.get("/items", headers={"Authorization": f"Bearer {owner_token}"})

    with patch("celerp.gateway.client._client", object()):
        r = await client.post("/auth/login", json={"email": "staff@test.com", "password": "pw"})
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_same_user_relogin_allowed(client, session):
    """Same user can log in again (e.g. from a different browser)."""
    owner_token = await _bootstrap_and_seed(client, session)
    await client.get("/items", headers={"Authorization": f"Bearer {owner_token}"})

    # Same user logs in again - should succeed even without relay
    with patch("celerp.gateway.client._client", None):
        r = await client.post("/auth/login", json={"email": "owner@test.com", "password": "pw"})
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_force_login_evicts_and_succeeds(client, session):
    """Force login clears tracker and succeeds for second user."""
    owner_token = await _bootstrap_and_seed(client, session)
    await client.get("/items", headers={"Authorization": f"Bearer {owner_token}"})

    r = await client.post("/auth/login-force", json={"email": "staff@test.com", "password": "pw"})
    assert r.status_code == 200
    assert r.json()["access_token"]
    assert session_tracker.active_user_ids() == set()


@pytest.mark.asyncio
async def test_force_login_rejects_bad_password(client, session):
    """Force login still validates credentials."""
    await _bootstrap_and_seed(client, session)
    r = await client.post("/auth/login-force", json={"email": "staff@test.com", "password": "wrong"})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_no_active_users_allows_login(client, session):
    """Login works normally when nobody is active (fresh start)."""
    await _bootstrap_and_seed(client, session)
    with patch("celerp.gateway.client._client", None):
        r = await client.post("/auth/login", json={"email": "owner@test.com", "password": "pw"})
    assert r.status_code == 200
