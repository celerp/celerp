# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

"""Tests for /auth/password-reset/request and /auth/password-reset/confirm."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest


async def _register(client, email="reset@example.com", password="securepass"):
    r = await client.post(
        "/auth/register",
        json={"company_name": "ResetCo", "email": email, "name": "Admin", "password": password},
    )
    assert r.status_code == 200
    return r.json()


@pytest.mark.asyncio
async def test_password_reset_request_returns_200_for_existing_user(client):
    """Always returns 200 for existing users (no enumeration)."""
    await _register(client)
    with patch("celerp.services.email.send_email", new=AsyncMock(return_value=True)):
        r = await client.post("/auth/password-reset/request", json={"email": "reset@example.com"})
    assert r.status_code == 200
    assert "detail" in r.json()


@pytest.mark.asyncio
async def test_password_reset_request_returns_200_for_nonexistent_user(client):
    """Always returns 200 even when email doesn't exist (prevent user enumeration)."""
    r = await client.post("/auth/password-reset/request", json={"email": "nobody@example.com"})
    assert r.status_code == 200
    assert "detail" in r.json()


@pytest.mark.asyncio
async def test_password_reset_request_response_is_identical(client):
    """Response body must be identical whether user exists or not."""
    await _register(client)
    with patch("celerp.services.email.send_email", new=AsyncMock(return_value=True)):
        r_exists = await client.post("/auth/password-reset/request", json={"email": "reset@example.com"})
    r_missing = await client.post("/auth/password-reset/request", json={"email": "missing@example.com"})
    assert r_exists.json() == r_missing.json()


@pytest.mark.asyncio
async def test_password_reset_confirm_happy_path(client, session):
    """Valid token → password updated, token cleared, returns 200."""
    from celerp.models.company import User
    from sqlalchemy import select

    await _register(client, email="happy@example.com", password="oldpassword")

    with patch("celerp.services.email.send_email", new=AsyncMock(return_value=True)):
        await client.post("/auth/password-reset/request", json={"email": "happy@example.com"})

    user = (await session.execute(select(User).where(User.email == "happy@example.com"))).scalar_one()
    assert user.reset_token is not None
    token = user.reset_token

    r = await client.post(
        "/auth/password-reset/confirm",
        json={"token": token, "new_password": "newpassword1"},
    )
    assert r.status_code == 200
    assert r.json()["detail"] == "Password updated successfully."

    await session.refresh(user)
    assert user.reset_token is None
    assert user.reset_token_expires is None

    # Can log in with new password
    login = await client.post("/auth/login", json={"email": "happy@example.com", "password": "newpassword1"})
    assert login.status_code == 200


@pytest.mark.asyncio
async def test_password_reset_confirm_wrong_token(client):
    """Wrong token returns 400."""
    r = await client.post(
        "/auth/password-reset/confirm",
        json={"token": "totally-wrong-token", "new_password": "newpassword1"},
    )
    assert r.status_code == 400
    assert "expired" in r.json()["detail"].lower() or "invalid" in r.json()["detail"].lower()


@pytest.mark.asyncio
async def test_password_reset_confirm_expired_token(client, session):
    """Expired token returns 400."""
    from celerp.models.company import User
    from sqlalchemy import select

    await _register(client, email="expired@example.com", password="oldpassword")

    with patch("celerp.services.email.send_email", new=AsyncMock(return_value=True)):
        await client.post("/auth/password-reset/request", json={"email": "expired@example.com"})

    user = (await session.execute(select(User).where(User.email == "expired@example.com"))).scalar_one()
    # Force expiry into the past
    user.reset_token_expires = datetime.now(timezone.utc) - timedelta(minutes=1)
    await session.commit()

    r = await client.post(
        "/auth/password-reset/confirm",
        json={"token": user.reset_token, "new_password": "newpassword1"},
    )
    assert r.status_code == 400
    assert "expired" in r.json()["detail"].lower() or "invalid" in r.json()["detail"].lower()


@pytest.mark.asyncio
async def test_password_reset_confirm_short_password(client, session):
    """Password shorter than 8 chars returns 400."""
    from celerp.models.company import User
    from sqlalchemy import select

    await _register(client, email="short@example.com", password="oldpassword")

    with patch("celerp.services.email.send_email", new=AsyncMock(return_value=True)):
        await client.post("/auth/password-reset/request", json={"email": "short@example.com"})

    user = (await session.execute(select(User).where(User.email == "short@example.com"))).scalar_one()

    r = await client.post(
        "/auth/password-reset/confirm",
        json={"token": user.reset_token, "new_password": "abc"},
    )
    assert r.status_code == 400
    assert "8" in r.json()["detail"] or "characters" in r.json()["detail"].lower()
