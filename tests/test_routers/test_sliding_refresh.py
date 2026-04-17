# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Tests for SlidingTokenRefreshMiddleware and _maybe_refresh_bearer."""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest


def _make_token(subject: str, company_id: str, role: str, expire_minutes: int) -> str:
    from celerp.services.auth import create_access_token
    from unittest.mock import patch
    with patch("celerp.services.auth.settings") as mock_settings:
        mock_settings.jwt_secret = "test-secret"
        mock_settings.jwt_algorithm = "HS256"
        mock_settings.access_token_expire_minutes = expire_minutes
        return create_access_token(subject, company_id, role)


@pytest.mark.asyncio
async def test_no_refresh_when_token_fresh(client):
    """X-Refreshed-Token must NOT appear when token is fresh (< half TTL consumed)."""
    reg = await client.post(
        "/auth/register",
        json={"company_name": "SlideTest", "email": "slide@test.com", "name": "Admin", "password": "pw"},
    )
    token = reg.json()["access_token"]

    r = await client.get("/items", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert "X-Refreshed-Token" not in r.headers


@pytest.mark.asyncio
async def test_refresh_header_set_when_token_past_half_life(client):
    """X-Refreshed-Token IS set when the token has consumed > 50% of its TTL."""
    import uuid
    from jose import jwt as _jwt

    reg = await client.post(
        "/auth/register",
        json={"company_name": "SlideHalf", "email": "half@half.com", "name": "Admin", "password": "pw"},
    )
    data = reg.json()
    # Manually craft a token that was issued 61 minutes ago (past half of 120-min TTL)
    # by setting exp = now + 59 min (120 - 61 = 59 remaining)
    from celerp.config import settings
    now = time.time()
    total_ttl = int(settings.access_token_expire_minutes) * 60
    # Decode original to get sub/company_id/role
    claims = _jwt.decode(data["access_token"], settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    stale_payload = {
        "sub": claims["sub"],
        "company_id": claims["company_id"],
        "role": claims["role"],
        "exp": int(now + total_ttl * 0.49),  # only 49% TTL remaining => past half-life
    }
    stale_token = _jwt.encode(stale_payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)

    r = await client.get("/items", headers={"Authorization": f"Bearer {stale_token}"})
    assert r.status_code == 200
    assert "X-Refreshed-Token" in r.headers
    new_token = r.headers["X-Refreshed-Token"]
    assert new_token != stale_token
    # New token should be valid
    new_claims = _jwt.decode(new_token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    assert new_claims["sub"] == claims["sub"]
    assert new_claims["company_id"] == claims["company_id"]


@pytest.mark.asyncio
async def test_no_refresh_header_on_error_responses(client):
    """X-Refreshed-Token must NOT be set on 4xx responses."""
    r = await client.get("/items", headers={"Authorization": "Bearer invalid.token.here"})
    assert r.status_code == 401
    assert "X-Refreshed-Token" not in r.headers


@pytest.mark.asyncio
async def test_no_refresh_header_without_auth(client):
    """X-Refreshed-Token must NOT be set when no Authorization header."""
    r = await client.get("/health")
    assert "X-Refreshed-Token" not in r.headers


def test_maybe_refresh_bearer_returns_none_for_fresh_token():
    from celerp.middleware import _maybe_refresh_bearer
    from celerp.config import settings
    from celerp.services.auth import create_access_token
    token = create_access_token("user-1", "company-1", "admin")
    # Fresh token: should not refresh
    result = _maybe_refresh_bearer(token)
    assert result is None


def test_maybe_refresh_bearer_returns_none_for_garbage():
    from celerp.middleware import _maybe_refresh_bearer
    assert _maybe_refresh_bearer("not.a.jwt") is None
    assert _maybe_refresh_bearer("") is None
    assert _maybe_refresh_bearer("x.y.z") is None


def test_maybe_refresh_bearer_issues_new_token_when_stale():
    from celerp.middleware import _maybe_refresh_bearer
    from celerp.config import settings
    from jose import jwt as _jwt

    now = time.time()
    total_ttl = int(settings.access_token_expire_minutes) * 60
    stale_payload = {
        "sub": "user-abc",
        "company_id": "company-xyz",
        "role": "admin",
        "exp": int(now + total_ttl * 0.49),
    }
    stale_token = _jwt.encode(stale_payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    result = _maybe_refresh_bearer(stale_token)
    assert result is not None
    new_claims = _jwt.decode(result, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    assert new_claims["sub"] == "user-abc"
    assert new_claims["company_id"] == "company-xyz"
    assert new_claims["role"] == "admin"
    # New token should have a longer remaining TTL
    assert new_claims["exp"] > now + total_ttl * 0.49
