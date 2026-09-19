# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Tests for /auth/password-reset/request and /auth/password-reset/confirm.

The reset token is hashed at rest: only the SHA-256 digest is persisted, while the
raw high-entropy token is emailed. A database reader therefore cannot use the
stored value as a bearer credential.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
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


def _extract_raw_token(mock_send_email) -> str:
    """Pull the raw reset token out of the emailed link (never from the DB)."""
    assert mock_send_email.await_count or mock_send_email.call_count
    call = mock_send_email.call_args
    blob = " ".join(str(a) for a in call.args) + " " + " ".join(str(v) for v in call.kwargs.values())
    m = re.search(r"token=([A-Za-z0-9_\-]+)", blob)
    assert m, f"no reset token in emailed link: {blob}"
    return m.group(1)


async def _request_reset(client, email) -> str:
    """Request a reset and return the RAW emailed token."""
    with patch("celerp.services.email.send_email", new=AsyncMock(return_value=True)) as m:
        r = await client.post("/auth/password-reset/request", json={"email": email})
    assert r.status_code == 200
    return _extract_raw_token(m)


@pytest.mark.asyncio
async def test_password_reset_request_returns_200_for_existing_user(client):
    """Always returns 200 for existing users (no enumeration)."""
    await _register(client)
    with patch("celerp.services.email.send_email", new=AsyncMock(return_value=True)):
        r = await client.post("/auth/password-reset/request", json={"email": "reset@example.com"})
    assert r.status_code == 200
    assert "detail" in r.json()


@pytest.mark.asyncio
async def test_password_reset_delivery_task_is_retained_until_completion(client):
    """The response can return before delivery finishes, while the email task
    remains strongly referenced until the transport completes."""
    from celerp.services import background

    await _register(client, email="retained@example.com")
    started = asyncio.Event()
    release = asyncio.Event()
    baseline = set(background._BG_TASKS)

    async def _blocked_send(*args, **kwargs):
        started.set()
        await release.wait()
        return True, "sent"

    with patch("celerp.services.email.send_email", new=_blocked_send):
        r = await client.post(
            "/auth/password-reset/request",
            json={"email": "retained@example.com"},
        )
        assert r.status_code == 200
        await asyncio.wait_for(started.wait(), timeout=1)

        retained = background._BG_TASKS - baseline
        assert len(retained) == 1
        assert all(not task.done() for task in retained)

        release.set()
        await asyncio.gather(*retained)
        await asyncio.sleep(0)
        assert background._BG_TASKS == baseline


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
async def test_reset_token_stored_as_sha256_digest_not_raw(client, session):
    """The DB stores a 64-char SHA-256 hex digest, not the raw emailed token."""
    from celerp.models.company import User
    from sqlalchemy import select

    await _register(client, email="digest@example.com", password="oldpassword")
    raw = await _request_reset(client, "digest@example.com")

    user = (await session.execute(select(User).where(User.email == "digest@example.com"))).scalar_one()
    await session.refresh(user)
    assert user.reset_token != raw
    assert len(user.reset_token) == 64
    assert re.fullmatch(r"[0-9a-f]{64}", user.reset_token)
    assert user.reset_token == hashlib.sha256(raw.encode()).hexdigest()


@pytest.mark.asyncio
async def test_raw_token_confirms_successfully(client, session):
    """The raw emailed token confirms and updates the password."""
    from celerp.models.company import User
    from sqlalchemy import select

    await _register(client, email="rawok@example.com", password="oldpassword")
    raw = await _request_reset(client, "rawok@example.com")

    r = await client.post(
        "/auth/password-reset/confirm",
        json={"token": raw, "new_password": "newpassword1"},
    )
    assert r.status_code == 200
    assert r.json()["detail"] == "Password updated successfully."

    user = (await session.execute(select(User).where(User.email == "rawok@example.com"))).scalar_one()
    await session.refresh(user)
    assert user.reset_token is None
    assert user.reset_token_expires is None

    login = await client.post("/auth/login", json={"email": "rawok@example.com", "password": "newpassword1"})
    assert login.status_code == 200


@pytest.mark.asyncio
async def test_stored_digest_is_not_a_usable_bearer_token(client, session):
    """The value stored in the DB cannot itself be replayed to confirm a reset."""
    from celerp.models.company import User
    from sqlalchemy import select

    await _register(client, email="dbleak@example.com", password="oldpassword")
    await _request_reset(client, "dbleak@example.com")

    user = (await session.execute(select(User).where(User.email == "dbleak@example.com"))).scalar_one()
    await session.refresh(user)
    stored = user.reset_token
    assert stored and len(stored) == 64

    r = await client.post(
        "/auth/password-reset/confirm",
        json={"token": stored, "new_password": "newpassword1"},
    )
    assert r.status_code == 400
    assert "expired" in r.json()["detail"].lower() or "invalid" in r.json()["detail"].lower()


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
    raw = await _request_reset(client, "expired@example.com")

    user = (await session.execute(select(User).where(User.email == "expired@example.com"))).scalar_one()
    # Force expiry into the past
    user.reset_token_expires = datetime.now(timezone.utc) - timedelta(minutes=1)
    await session.commit()

    r = await client.post(
        "/auth/password-reset/confirm",
        json={"token": raw, "new_password": "newpassword1"},
    )
    assert r.status_code == 400
    assert "expired" in r.json()["detail"].lower() or "invalid" in r.json()["detail"].lower()


@pytest.mark.asyncio
async def test_password_reset_confirm_kills_old_access_and_refresh(client, session):
    """Completing a reset rotates the nonce, so the pre-reset access and refresh
    tokens both return 401 afterwards (a reset is a credential change)."""
    from celerp.models.company import User
    from sqlalchemy import select

    reg = await _register(client, email="killtok@example.com", password="oldpassword")
    old_access = reg["access_token"]
    old_refresh = reg["refresh_token"]

    raw = await _request_reset(client, "killtok@example.com")

    r = await client.post(
        "/auth/password-reset/confirm",
        json={"token": raw, "new_password": "newpassword1"},
    )
    assert r.status_code == 200

    r_acc = await client.get("/auth/my-companies", headers={"Authorization": f"Bearer {old_access}"})
    assert r_acc.status_code == 401
    r_ref = await client.post("/auth/token/refresh", json={"refresh_token": old_refresh})
    assert r_ref.status_code == 401


@pytest.mark.asyncio
async def test_password_reset_confirm_short_password(client, session):
    """Password shorter than 8 chars returns 400 (shared validator)."""
    await _register(client, email="short@example.com", password="oldpassword")
    raw = await _request_reset(client, "short@example.com")

    r = await client.post(
        "/auth/password-reset/confirm",
        json={"token": raw, "new_password": "abc"},
    )
    assert r.status_code == 400
    assert "8" in r.json()["detail"] or "characters" in r.json()["detail"].lower()
