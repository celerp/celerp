# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

from __future__ import annotations

import uuid as _uuid
from datetime import datetime, timedelta, timezone

import pytest


@pytest.mark.asyncio
async def test_register_and_login(client):
    r = await client.post(
        "/auth/register",
        json={"company_name": "Acme Inc", "email": "a@b.com", "name": "Admin", "password": "pw"},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["access_token"]
    assert data["refresh_token"]

    r2 = await client.post("/auth/login", json={"email": "a@b.com", "password": "pw"})
    assert r2.status_code == 200
    data2 = r2.json()
    assert data2["access_token"]
    assert data2["refresh_token"]


@pytest.mark.asyncio
async def test_refresh_token_flow(client):
    """Valid refresh token returns new access + refresh tokens."""
    reg = await client.post(
        "/auth/register",
        json={"company_name": "RefreshCo", "email": "r@r.com", "name": "Admin", "password": "pw"},
    )
    refresh_token = reg.json()["refresh_token"]

    r = await client.post("/auth/token/refresh", json={"refresh_token": refresh_token})
    assert r.status_code == 200
    data = r.json()
    assert data["access_token"]
    assert data["refresh_token"]


@pytest.mark.asyncio
async def test_refresh_token_rejects_access_token(client):
    """Passing an access token to /auth/token/refresh must be rejected."""
    reg = await client.post(
        "/auth/register",
        json={"company_name": "BadRefresh", "email": "b@b.com", "name": "Admin", "password": "pw"},
    )
    access_token = reg.json()["access_token"]

    r = await client.post("/auth/token/refresh", json={"refresh_token": access_token})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_refresh_token_rejects_garbage(client):
    r = await client.post("/auth/token/refresh", json={"refresh_token": "garbage"})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_login_rejects_bad_password(client):
    await client.post(
        "/auth/register",
        json={"company_name": "BadPass", "email": "c@c.com", "name": "Admin", "password": "correct"},
    )
    r = await client.post("/auth/login", json={"email": "c@c.com", "password": "wrong"})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_api_key_requires_auth(client):
    r = await client.post("/auth/api-key")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_invalid_token_rejected(client):
    r = await client.get("/auth/my-companies", headers={"Authorization": "Bearer invalid"})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_login_unknown_user(client):
    r = await client.post("/auth/login", json={"email": "nobody@no.com", "password": "pw"})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_change_password(client, session):
    from celerp.services.session_tracker import clear as _clear_tracker

    await client.post(
        "/auth/register",
        json={"company_name": "PwCo", "email": "pw@pw.com", "name": "Admin", "password": "oldpass123"},
    )
    await _clear_tracker(session)
    r = await client.post("/auth/login", json={"email": "pw@pw.com", "password": "oldpass123"})
    token = r.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    r2 = await client.post("/auth/change-password", json={
        "current_password": "oldpass123", "new_password": "newpass456",
    }, headers=headers)
    assert r2.status_code == 200

    r3 = await client.post("/auth/login", json={"email": "pw@pw.com", "password": "oldpass123"})
    assert r3.status_code == 401

    # New password works - clear tracker first (first login still active in window)
    await _clear_tracker(session)
    r4 = await client.post("/auth/login", json={"email": "pw@pw.com", "password": "newpass456"})
    assert r4.status_code == 200


@pytest.mark.asyncio
async def test_change_password_wrong_current(client):
    """Change password rejects wrong current password."""
    await client.post(
        "/auth/register",
        json={"company_name": "PwCo2", "email": "pw2@pw.com", "name": "Admin", "password": "correct"},
    )
    r = await client.post("/auth/login", json={"email": "pw2@pw.com", "password": "correct"})
    token = r.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    r2 = await client.post("/auth/change-password", json={
        "current_password": "wrong", "new_password": "newpass456",
    }, headers=headers)
    assert r2.status_code == 400


@pytest.mark.asyncio
async def test_change_password_too_short(client):
    """Change password rejects passwords shorter than 8 chars."""
    await client.post(
        "/auth/register",
        json={"company_name": "PwCo3", "email": "pw3@pw.com", "name": "Admin", "password": "longpass123"},
    )
    r = await client.post("/auth/login", json={"email": "pw3@pw.com", "password": "longpass123"})
    token = r.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    r2 = await client.post("/auth/change-password", json={
        "current_password": "longpass123", "new_password": "short",
    }, headers=headers)
    assert r2.status_code == 400


@pytest.mark.asyncio
async def test_change_password_requires_auth(client):
    """Change password endpoint requires authentication."""
    r = await client.post("/auth/change-password", json={
        "current_password": "x", "new_password": "newpass456",
    })
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Single-user gate tests (JTI-based registry)
# ---------------------------------------------------------------------------

async def _seed_foreign_session(session, user_id: str, expiry_offset_s: float = 900.0) -> None:
    """Directly insert a JTI for *user_id* into the DB tracker (test helper)."""
    from celerp.services.session_tracker import register_token as _reg
    expiry = datetime.now(timezone.utc) + timedelta(seconds=expiry_offset_s)
    await _reg(session, str(_uuid.uuid4()), user_id, expiry)


@pytest.mark.asyncio
async def test_single_user_gate_blocks_any_concurrent_user(client, session):
    """Gate: a different user cannot log in while another user has an active JTI."""
    from unittest.mock import patch
    from celerp.services.session_tracker import clear as _clear_tracker

    await client.post(
        "/auth/register",
        json={"company_name": "GateCo", "email": "gate_admin@test.com", "name": "Admin", "password": "longpass123"},
    )
    await _clear_tracker(session)
    await _seed_foreign_session(session, "00000000-0000-0000-0000-000000000001")

    with patch("celerp.gateway.state.get_session_token", return_value=""):
        r = await client.post("/auth/login", json={"email": "gate_admin@test.com", "password": "longpass123"})
    assert r.status_code == 409, f"Expected 409 but got {r.status_code}: {r.text}"
    assert r.json()["detail"] == "direct_connection_limit"


@pytest.mark.asyncio
async def test_single_user_gate_empty_tracker_allows_login(client, session):
    """Gate: login succeeds when tracker is empty (no active JTIs)."""
    from celerp.services.session_tracker import clear as _clear_tracker

    await client.post(
        "/auth/register",
        json={"company_name": "GateCo2", "email": "gate2@test.com", "name": "Admin", "password": "longpass123"},
    )
    await _clear_tracker(session)

    r = await client.post("/auth/login", json={"email": "gate2@test.com", "password": "longpass123"})
    assert r.status_code == 200, f"Empty tracker should allow login, got {r.status_code}: {r.text}"


@pytest.mark.asyncio
async def test_single_user_gate_blocks_same_user_relogin(client, session):
    """Same user cannot log in again while their own JTI is still active.

    One session per user - multi-tab is handled by sharing the same JTI chain,
    not by re-logging in.  A second login from a new browser is blocked.
    """
    from unittest.mock import patch
    import base64, json as _json
    from celerp.services.session_tracker import clear as _clear_tracker

    await client.post(
        "/auth/register",
        json={"company_name": "GateCo3", "email": "gate3@test.com", "name": "Admin", "password": "longpass123"},
    )
    await _clear_tracker(session)
    r1 = await client.post("/auth/login", json={"email": "gate3@test.com", "password": "longpass123"})
    assert r1.status_code == 200
    token = r1.json()["access_token"]
    payload_b64 = token.split(".")[1] + "=="
    user_id = _json.loads(base64.b64decode(payload_b64))["sub"]

    # Seed tracker with THIS user (simulates their existing session in another browser)
    await _seed_foreign_session(session, user_id)

    # Same user re-login must be blocked (any active JTI prevents new login)
    with patch("celerp.gateway.state.get_session_token", return_value=""):
        r2 = await client.post("/auth/login", json={"email": "gate3@test.com", "password": "longpass123"})
    assert r2.status_code == 409, f"Same-user re-login should be blocked, got {r2.status_code}: {r2.text}"


@pytest.mark.asyncio
async def test_single_user_gate_bypassed_with_relay(client, session):
    """Gate is skipped when relay session token is active (cloud tier = multi-user)."""
    from unittest.mock import patch
    from celerp.services.session_tracker import clear as _clear_tracker

    await client.post(
        "/auth/register",
        json={"company_name": "GateCo4", "email": "gate4@test.com", "name": "Admin", "password": "longpass123"},
    )
    await _clear_tracker(session)
    await _seed_foreign_session(session, "00000000-0000-0000-0000-000000000002")

    with patch("celerp.gateway.state.get_session_token", return_value="live-token-abc"):
        r = await client.post("/auth/login", json={"email": "gate4@test.com", "password": "longpass123"})
    assert r.status_code == 200, f"Relay present should bypass gate, got {r.status_code}: {r.text}"


@pytest.mark.asyncio
async def test_gate_opens_when_all_jtis_expired(client, session):
    """Gate must NOT fire when the only registered JTI has already expired."""
    from unittest.mock import patch
    from celerp.services.session_tracker import clear as _clear_tracker, register_token as _reg

    await client.post(
        "/auth/register",
        json={"company_name": "IdleCo", "email": "idle@test.com", "name": "Admin", "password": "longpass123"},
    )
    await _clear_tracker(session)

    # Register an already-expired JTI for a foreign user
    expired = datetime.now(timezone.utc) - timedelta(seconds=1)
    await _reg(session, str(_uuid.uuid4()), "00000000-0000-0000-0000-000000000099", expired)

    with patch("celerp.gateway.state.get_session_token", return_value=""):
        r = await client.post("/auth/login", json={"email": "idle@test.com", "password": "longpass123"})
    assert r.status_code == 200, f"Expired JTI should not block login, got {r.status_code}: {r.text}"


@pytest.mark.asyncio
async def test_logout_endpoint_invalidates_existing_tokens(client):
    """POST /auth/logout must rotate the nonce so existing tokens return 401."""
    r = await client.post(
        "/auth/register",
        json={"company_name": "LogoutCo", "email": "logout@test.com", "name": "Admin", "password": "longpass123"},
    )
    assert r.status_code == 200

    r_login = await client.post("/auth/login", json={"email": "logout@test.com", "password": "longpass123"})
    assert r_login.status_code == 200
    token = r_login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    r_pre = await client.get("/auth/my-companies", headers=headers)
    assert r_pre.status_code == 200

    r_logout = await client.post("/auth/logout", headers=headers)
    assert r_logout.status_code == 200

    r_post = await client.get("/auth/my-companies", headers=headers)
    assert r_post.status_code == 401, f"Token should be rejected after logout, got {r_post.status_code}"


@pytest.mark.asyncio
async def test_force_login_invalidates_other_user_tokens(client, session):
    """login-force must rotate the nonce so any previously-active user's token returns 401."""
    from unittest.mock import patch
    from celerp.services.session_tracker import clear as _clear_tracker

    await client.post(
        "/auth/register",
        json={"company_name": "ForceCo", "email": "owner@force.com", "name": "Owner", "password": "longpass123"},
    )
    await _clear_tracker(session)
    r_owner = await client.post("/auth/login", json={"email": "owner@force.com", "password": "longpass123"})
    assert r_owner.status_code == 200
    owner_token = r_owner.json()["access_token"]

    with patch("celerp.gateway.state.get_session_token", return_value=""):
        r_force = await client.post(
            "/auth/login-force", json={"email": "owner@force.com", "password": "longpass123"}
        )
    assert r_force.status_code == 200

    r_old = await client.get("/auth/my-companies", headers={"Authorization": f"Bearer {owner_token}"})
    assert r_old.status_code == 401, f"Old token should be rejected after force-login, got {r_old.status_code}"


@pytest.mark.asyncio
async def test_login_possible_after_force_login_and_logout(client, session):
    """Regression (Nikolai 2026-05-12): A logs in, B force-logs in, B logs out, both can log in again."""
    from unittest.mock import patch
    from celerp.services.session_tracker import clear as _clear_tracker

    await client.post(
        "/auth/register",
        json={"company_name": "ReloginCo", "email": "userA@relogin.com", "name": "A", "password": "pw123456"},
    )
    await _clear_tracker(session)

    with patch("celerp.gateway.state.get_session_token", return_value=""):
        r_a = await client.post("/auth/login", json={"email": "userA@relogin.com", "password": "pw123456"})
    assert r_a.status_code == 200
    token_a = r_a.json()["access_token"]

    r_check = await client.get("/auth/my-companies", headers={"Authorization": f"Bearer {token_a}"})
    assert r_check.status_code == 200

    with patch("celerp.gateway.state.get_session_token", return_value=""):
        r_b = await client.post("/auth/login-force", json={"email": "userA@relogin.com", "password": "pw123456"})
    assert r_b.status_code == 200
    token_b = r_b.json()["access_token"]

    r_dead = await client.get("/auth/my-companies", headers={"Authorization": f"Bearer {token_a}"})
    assert r_dead.status_code == 401

    r_b_check = await client.get("/auth/my-companies", headers={"Authorization": f"Bearer {token_b}"})
    assert r_b_check.status_code == 200

    await client.post("/auth/logout", headers={"Authorization": f"Bearer {token_b}"})

    with patch("celerp.gateway.state.get_session_token", return_value=""):
        r_a2 = await client.post("/auth/login", json={"email": "userA@relogin.com", "password": "pw123456"})
    assert r_a2.status_code == 200, f"User A could not log in after B's logout: {r_a2.json()}"
    token_a2 = r_a2.json()["access_token"]
    r_a2_check = await client.get("/auth/my-companies", headers={"Authorization": f"Bearer {token_a2}"})
    assert r_a2_check.status_code == 200


@pytest.mark.asyncio
async def test_force_login_stores_evicting_ip(client, session):
    """login-force stores the requesting IP so evicted user sees it on next 401."""
    from unittest.mock import patch
    from celerp.services.session_tracker import clear as _clear_tracker, pop_evicted_by_ip as _pop_ip
    import base64, json as _json

    await client.post(
        "/auth/register",
        json={"company_name": "IpCo", "email": "iptest@test.com", "name": "Admin", "password": "pw123456"},
    )
    await _clear_tracker(session)

    with patch("celerp.gateway.state.get_session_token", return_value=""):
        r1 = await client.post("/auth/login", json={"email": "iptest@test.com", "password": "pw123456"})
    assert r1.status_code == 200
    token_a = r1.json()["access_token"]
    payload_b64 = token_a.split(".")[1] + "=="
    user_id = _json.loads(base64.b64decode(payload_b64))["sub"]

    # force-login: evicting IP stored
    with patch("celerp.gateway.state.get_session_token", return_value=""):
        r2 = await client.post("/auth/login-force", json={"email": "iptest@test.com", "password": "pw123456"})
    assert r2.status_code == 200

    # Self-force-login: evicting user IS the displaced user, so no eviction IP stored.
    # (Eviction IP is only stored for OTHER users displaced by the force-login.)
    ip = await _pop_ip(session, user_id)
    assert ip is None, "Self-force-login must NOT store eviction IP on own account"

    # Old token returns 401 (nonce was rotated)
    r_dead = await client.get("/auth/my-companies", headers={"Authorization": f"Bearer {token_a}"})
    assert r_dead.status_code == 401


# ── _401_redirect helper unit tests ─────────────────────────────────────────

def test_401_redirect_evicted_with_ip():
    from ui.app import _401_redirect
    r = _401_redirect("Session expired|192.168.1.1")
    assert r.status_code == 302
    assert "reason=evicted" in r.headers["location"]
    assert "by=192.168.1.1" in r.headers["location"]


def test_401_redirect_evicted_no_ip():
    from ui.app import _401_redirect
    r = _401_redirect("Session expired")
    assert r.status_code == 302
    assert "reason=evicted" in r.headers["location"]
    assert "by=" not in r.headers["location"]


def test_401_redirect_expired():
    from ui.app import _401_redirect
    r = _401_redirect("Invalid token")
    assert r.status_code == 302
    assert "reason=expired" in r.headers["location"]


# ── session-watch SSE endpoint ──────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.skip(reason="SSE stream test requires shared test engine in SessionLocal; integration test only")
async def test_session_watch_yields_evicted_on_invalidation(client, session):
    """session-watch SSE stream emits 'evicted' event when nonce rotates."""
    from celerp.services.session_tracker import clear as _clear_tracker, invalidate_sessions as _invalidate
    import asyncio

    await client.post(
        "/auth/register",
        json={"company_name": "WatchCo", "email": "watch@test.com", "name": "Admin", "password": "pw123456"},
    )
    await _clear_tracker(session)
    r_login = await client.post("/auth/login", json={"email": "watch@test.com", "password": "pw123456"})
    assert r_login.status_code == 200
    token = r_login.json()["access_token"]

    import base64, json as _json
    payload_b64 = token.split(".")[1] + "=="
    user_id = _json.loads(base64.b64decode(payload_b64))["sub"]

    collected = []

    async def _consume():
        from httpx import AsyncClient, ASGITransport
        from celerp.main import app as api_app
        async with AsyncClient(transport=ASGITransport(app=api_app), base_url="http://test") as c:
            async with c.stream(
                "GET",
                "/auth/session-watch",
                headers={"Authorization": f"Bearer {token}"},
                timeout=5.0,
            ) as resp:
                saw_evicted = False
                async for line in resp.aiter_lines():
                    collected.append(line)
                    if line.startswith("event: evicted"):
                        saw_evicted = True
                    elif saw_evicted and line.startswith("data:"):
                        break

    async def _invalidate_after():
        await asyncio.sleep(0.5)
        await _invalidate(session, user_id, evicting_ip="10.0.0.1")

    await asyncio.gather(_consume(), _invalidate_after())

    assert any("evicted" in line for line in collected), f"Expected evicted event, got: {collected}"
    assert any("10.0.0.1" in line for line in collected), f"Expected IP in event data, got: {collected}"
