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
        token, _ = create_access_token(subject, company_id, role)
        return token


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
    # Craft a token that is past half of its TTL by copying the freshly issued
    # v2 token's full claim set and only shortening its expiry. Preserving
    # auth_ver, type, jti and snonce is required now: the request path validates
    # the whole v2 contract and the per-user nonce before any sliding re-mint, so
    # a token that dropped those fields would be rejected 401, not refreshed.
    from celerp.config import settings
    now = time.time()
    total_ttl = int(settings.access_token_expire_minutes) * 60
    claims = _jwt.decode(data["access_token"], settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    stale_payload = dict(claims)
    stale_payload["exp"] = int(now + total_ttl * 0.49)  # only 49% TTL remaining => past half-life
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
    token, _ = create_access_token("user-1", "company-1", "admin")
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
    import uuid

    now = time.time()
    total_ttl = int(settings.access_token_expire_minutes) * 60
    stale_jti = str(uuid.uuid4())
    stale_payload = {
        "sub": "user-abc",
        "company_id": "company-xyz",
        "role": "admin",
        "jti": stale_jti,
        "snonce": "test-nonce-value",  # arbitrary; refresh copies it forward as-is
        "exp": int(now + total_ttl * 0.49),
    }
    stale_token = _jwt.encode(stale_payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    result = _maybe_refresh_bearer(stale_token)
    assert result is not None, "Stale token should trigger a refresh"
    new_token, returned_jti, new_expiry = result
    assert returned_jti == stale_jti, "Refresh must reuse the original JTI"
    new_claims = _jwt.decode(new_token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    assert new_claims["sub"] == "user-abc"
    assert new_claims["company_id"] == "company-xyz"
    assert new_claims["role"] == "admin"
    assert new_claims["jti"] == stale_jti, "Refreshed token must carry the same JTI"
    # New token should have a longer remaining TTL
    assert new_claims["exp"] > now + total_ttl * 0.49


def test_maybe_refresh_bearer_returns_none_for_token_without_jti():
    """Token without jti cannot be refreshed (no registry row to update)."""
    from celerp.middleware import _maybe_refresh_bearer
    from celerp.config import settings
    from jose import jwt as _jwt

    now = time.time()
    total_ttl = int(settings.access_token_expire_minutes) * 60
    payload = {
        "sub": "user-abc",
        "company_id": "company-xyz",
        "role": "admin",
        # no jti
        "exp": int(now + total_ttl * 0.49),
    }
    token = _jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    assert _maybe_refresh_bearer(token) is None


@pytest.mark.asyncio
async def test_switch_company_updates_refresh_token():
    """POST /switch-company/{id} must set a new refresh token cookie scoped to the new company.

    Regression: previously switch_company in api_client.py discarded the new refresh token
    returned by the API. After 15 min idle the stale refresh token (company A) would
    silently move the user back to company A.
    """
    from unittest.mock import AsyncMock, patch
    from httpx import AsyncClient
    from httpx._transports.asgi import ASGITransport
    from celerp.services.auth import create_access_token, create_refresh_token
    from ui.app import app as ui_app
    from test_helpers import authed_cookies as _authed

    company_b_id = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    new_access = create_access_token("user-1", company_b_id, "admin")[0]
    new_refresh = create_refresh_token("user-1", company_b_id, "admin")

    # Patch at the api_client module level (where it's imported inside the handler)
    with patch("ui.api_client.switch_company", new=AsyncMock(return_value=(new_access, new_refresh))):
        async with AsyncClient(
            transport=ASGITransport(app=ui_app),
            base_url="http://ui",
            follow_redirects=False,
        ) as c:
            r = await c.post(f"/switch-company/{company_b_id}", cookies=_authed())

    assert r.status_code in (302, 303)
    cookies_header = r.headers.get("set-cookie", "")
    # Both cookies must be set
    assert "celerp_token=" in cookies_header
    assert "celerp_refresh=" in cookies_header
    # The refresh cookie must contain the new refresh token value
    assert new_refresh in cookies_header


# ---------------------------------------------------------------------------
# A7 sliding-bearer security matrix (section 7 rows 24, 25, 27)
#
# These pin the sliding re-mint to the same validation the request path uses:
# it must never re-sign a token it would not itself accept. The re-mint becoming
# signature/session/DB-validating is workstream-2 work (A7); until it lands the
# refresh token, revoked nonce, and stale-role cases below are RED by design.
# Row 23 (signature-tampered) lives in tests/test_routers/test_auth.py.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_token_as_bearer_never_refreshed(client):
    """A v2 refresh token presented as a bearer never receives an X-Refreshed-Token.

    It is the wrong token type for the bearer path; a validating re-mint refuses
    to re-sign it. DEFERRED (A7): the current claims-only re-mint may still stamp
    a header, so this is RED until the helper validates the token type."""
    reg = await client.post(
        "/auth/register",
        json={"company_name": "SlideRefB", "email": "sliderefb@example.com", "name": "Admin", "password": "pw"},
    )
    refresh = reg.json()["refresh_token"]
    r = await client.get("/items", headers={"Authorization": f"Bearer {refresh}"})
    assert "X-Refreshed-Token" not in r.headers


@pytest.mark.asyncio
async def test_revoked_nonce_never_refreshed(client, session):
    """A past-half-life access token whose nonce was rotated (logout) never receives
    an X-Refreshed-Token. DEFERRED (A7): the current re-mint does not check the
    session nonce, so it may re-sign a dead token; RED until it validates."""
    import time as _time
    import uuid
    from jose import jwt as _jwt
    from celerp.config import settings
    from celerp.services.session_tracker import clear as _clear_tracker

    reg = await client.post(
        "/auth/register",
        json={"company_name": "SlideRev", "email": "sliderev@example.com", "name": "Admin", "password": "pw"},
    )
    claims = _jwt.decode(reg.json()["access_token"], settings.jwt_secret, algorithms=[settings.jwt_algorithm])

    now = _time.time()
    total_ttl = int(settings.access_token_expire_minutes) * 60
    stale_payload = {
        "auth_ver": claims["auth_ver"],
        "type": "access",
        "sub": claims["sub"],
        "company_id": claims["company_id"],
        "role": claims["role"],
        "jti": claims["jti"],
        "snonce": claims["snonce"],
        "exp": int(now + total_ttl * 0.49),  # past half-life
    }
    stale = _jwt.encode(stale_payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)

    # Revoke: log out so the user's nonce rotates and the stale token's snonce is dead.
    r_logout = await client.post(
        "/auth/logout", headers={"Authorization": f"Bearer {reg.json()['access_token']}"}
    )
    assert r_logout.status_code == 200

    r = await client.get("/items", headers={"Authorization": f"Bearer {stale}"})
    assert "X-Refreshed-Token" not in r.headers


@pytest.mark.asyncio
async def test_refreshed_token_uses_current_db_role(client, session):
    """When a past-half-life token is refreshed, the new token carries the current DB
    role, not the stale role claim. DEFERRED (A7): the current re-mint copies the
    role claim forward verbatim, so a demoted user keeps the old role; RED until the
    re-mint derives the role from DB membership."""
    import time as _time
    import uuid as _uuid
    from jose import jwt as _jwt
    from sqlalchemy import select
    from celerp.config import settings
    from celerp.models.accounting import UserCompany
    from celerp.services.session_tracker import clear as _clear_tracker

    owner_reg = await client.post(
        "/auth/register",
        json={"company_name": "SlideRole", "email": "slideowner@example.com", "name": "Owner", "password": "pw"},
    )
    owner_h = {"Authorization": f"Bearer {owner_reg.json()['access_token']}"}
    await client.post(
        "/companies/me/users",
        json={"email": "slidetarget@example.com", "name": "Target", "role": "manager", "password": "pw123"},
        headers=owner_h,
    )
    await _clear_tracker(session)
    r_login = await client.post("/auth/login", json={"email": "slidetarget@example.com", "password": "pw123"})
    claims = _jwt.decode(r_login.json()["access_token"], settings.jwt_secret, algorithms=[settings.jwt_algorithm])

    # Demote the target in DB.
    link = await session.scalar(
        select(UserCompany).where(
            UserCompany.user_id == _uuid.UUID(claims["sub"]),
            UserCompany.company_id == _uuid.UUID(claims["company_id"]),
        )
    )
    link.role = "operator"
    await session.commit()

    now = _time.time()
    total_ttl = int(settings.access_token_expire_minutes) * 60
    stale_payload = dict(claims)
    stale_payload["exp"] = int(now + total_ttl * 0.49)  # past half-life, still valid nonce
    stale = _jwt.encode(stale_payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)

    r = await client.get("/items", headers={"Authorization": f"Bearer {stale}"})
    assert r.status_code == 200
    assert "X-Refreshed-Token" in r.headers, "a valid near-half-life token must still be refreshed"
    new_claims = _jwt.decode(r.headers["X-Refreshed-Token"], settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    assert new_claims["role"] == "operator", "refreshed token must reflect the current DB role"


def test_maybe_refresh_bearer_preserves_email_and_modules():
    """The Bearer sliding re-mint must carry the source token's email and modules
    claims through, not drop them.

    Regression: _maybe_refresh_bearer re-minted via create_access_token without
    email or modules, so the refreshed token decoded to email == "" and
    modules == []. An empty modules claim makes the UI sidebar fall back to
    showing every nav entry, and a dropped email loses identity for the session.
    """
    from celerp.middleware import _maybe_refresh_bearer
    from celerp.config import settings
    from jose import jwt as _jwt
    import uuid

    now = time.time()
    total_ttl = int(settings.access_token_expire_minutes) * 60
    stale_jti = str(uuid.uuid4())
    stale_payload = {
        "sub": "user-abc",
        "email": "admin@example.com",
        "company_id": "company-xyz",
        "role": "admin",
        "jti": stale_jti,
        "snonce": "test-nonce-value",
        "modules": ["acme-maintenance", "acme-crm"],
        "exp": int(now + total_ttl * 0.49),  # past half-life
    }
    stale_token = _jwt.encode(stale_payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)

    result = _maybe_refresh_bearer(stale_token)
    assert result is not None, "Stale token should trigger a refresh"
    new_token, _returned_jti, _new_expiry = result
    new_claims = _jwt.decode(new_token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    assert new_claims["email"] == "admin@example.com", "email claim must survive the re-mint"
    assert new_claims["modules"] == ["acme-maintenance", "acme-crm"], "modules claim must survive the re-mint"
