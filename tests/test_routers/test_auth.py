# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

from __future__ import annotations

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
    r = await client.post("/auth/token/refresh", json={"refresh_token": "not.a.token"})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_login_rejects_bad_password(client):
    await client.post(
        "/auth/register",
        json={"company_name": "Acme Inc", "email": "x@y.com", "name": "Admin", "password": "pw"},
    )

    r = await client.post("/auth/login", json={"email": "x@y.com", "password": "wrong"})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_api_key_requires_auth(client):
    r = await client.post("/auth/api-key")
    assert r.status_code == 401

    reg = await client.post(
        "/auth/register",
        json={"company_name": "Acme Inc", "email": "k@k.com", "name": "Admin", "password": "pw"},
    )
    token = reg.json()["access_token"]

    r2 = await client.post("/auth/api-key", headers={"Authorization": f"Bearer {token}"})
    assert r2.status_code == 200
    assert r2.json()["api_key"]


@pytest.mark.asyncio
async def test_invalid_token_rejected(client):
    r = await client.get("/items", headers={"Authorization": "Bearer not.a.token"})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_login_unknown_user(client):
    r = await client.post("/auth/login", json={"email": "nobody@x.com", "password": "pw"})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_change_password(client):
    """Authenticated user can change their password."""
    await client.post(
        "/auth/register",
        json={"company_name": "PwCo", "email": "pw@pw.com", "name": "Admin", "password": "oldpass123"},
    )
    r = await client.post("/auth/login", json={"email": "pw@pw.com", "password": "oldpass123"})
    token = r.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    # Change password
    r2 = await client.post("/auth/change-password", json={
        "current_password": "oldpass123", "new_password": "newpass456",
    }, headers=headers)
    assert r2.status_code == 200

    # Old password no longer works
    r3 = await client.post("/auth/login", json={"email": "pw@pw.com", "password": "oldpass123"})
    assert r3.status_code == 401

    # New password works - clear tracker first (first login still active in window)
    from celerp.services.session_tracker import clear as _clear_tracker
    _clear_tracker()
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



@pytest.mark.asyncio
async def test_single_user_gate_blocks_any_concurrent_user(client):
    """Gate: any second user is blocked when relay is not connected.

    The gate is global (not company-scoped): if ANY user has been active
    in the past 15 minutes, a different user cannot log in without relay.

    This test directly seeds the tracker with a fake other_user_id to
    simulate an active session, then verifies a real user login returns 409.
    """
    from unittest.mock import patch
    from celerp.services.session_tracker import clear as _clear_tracker, record as _record

    await client.post(
        "/auth/register",
        json={"company_name": "GateCo", "email": "gate_admin@test.com", "name": "Admin", "password": "longpass123"},
    )

    _clear_tracker()

    # Directly seed tracker with a different user (simulates another active session)
    _record("00000000-0000-0000-0000-000000000001", company_id="")

    # Login attempt -> 409 because another user is active
    r = await client.post("/auth/login", json={"email": "gate_admin@test.com", "password": "longpass123"})
    assert r.status_code == 409, f"Expected 409 but got {r.status_code}: {r.text}"
    assert r.json()["detail"] == "direct_connection_limit"


@pytest.mark.asyncio
async def test_single_user_gate_empty_tracker_allows_login(client):
    """Gate: login succeeds when tracker is empty (no active sessions)."""
    from unittest.mock import patch
    from celerp.services.session_tracker import clear as _clear_tracker

    await client.post(
        "/auth/register",
        json={"company_name": "GateCo2", "email": "gate2@test.com", "name": "Admin", "password": "longpass123"},
    )

    _clear_tracker()

    r = await client.post("/auth/login", json={"email": "gate2@test.com", "password": "longpass123"})
    assert r.status_code == 200, f"Empty tracker should allow login, got {r.status_code}: {r.text}"


@pytest.mark.asyncio
async def test_single_user_gate_blocks_same_user_relogin(client):
    """Gate: same user attempting re-login while already active is blocked (no exclude=self)."""
    from celerp.services.session_tracker import clear as _clear_tracker, record as _record
    import base64, json as _json

    await client.post(
        "/auth/register",
        json={"company_name": "GateCo3", "email": "gate3@test.com", "name": "Admin", "password": "longpass123"},
    )

    _clear_tracker()
    r1 = await client.post("/auth/login", json={"email": "gate3@test.com", "password": "longpass123"})
    assert r1.status_code == 200
    # Decode token to get real user id
    token = r1.json()["access_token"]
    payload_b64 = token.split(".")[1] + "=="
    user_id = _json.loads(base64.b64decode(payload_b64))["sub"]

    # Seed tracker with THIS user (simulates their own existing session)
    _record(user_id, company_id="")

    # Same user re-login -> now blocked (universal gate, no self-exclusion)
    r2 = await client.post("/auth/login", json={"email": "gate3@test.com", "password": "longpass123"})
    assert r2.status_code == 409, f"Same-user re-login should be blocked, got {r2.status_code}: {r2.text}"


@pytest.mark.asyncio
async def test_single_user_gate_fires_regardless_of_relay(client):
    """Gate always fires when another user is active - relay connection does not bypass it."""
    from celerp.services.session_tracker import clear as _clear_tracker, record as _record

    await client.post(
        "/auth/register",
        json={"company_name": "GateCo4", "email": "gate4@test.com", "name": "Admin", "password": "longpass123"},
    )

    _clear_tracker()
    # Seed tracker with another user
    _record("00000000-0000-0000-0000-000000000002", company_id="")

    # Gate fires even when relay is connected
    r = await client.post("/auth/login", json={"email": "gate4@test.com", "password": "longpass123"})
    assert r.status_code == 409, f"Gate should fire regardless of relay, got {r.status_code}: {r.text}"


@pytest.mark.asyncio
async def test_single_user_gate_survives_tracker_reload(client, tmp_path):
    """Gate persists across tracker in-memory wipe (simulates process reload in dev mode).

    Regression: uvicorn --reload wipes in-memory _activity on config.toml write
    (e.g. after cloud disconnect). Gate must still fire after reload because
    session_tracker persists to .active_sessions.json next to config.toml.
    """
    import celerp.services.session_tracker as _tracker

    await client.post(
        "/auth/register",
        json={"company_name": "ReloadCo", "email": "reload@test.com", "name": "Admin", "password": "longpass123"},
    )

    # First login - seeds the file-backed tracker
    r1 = await client.post("/auth/login", json={"email": "reload@test.com", "password": "longpass123"})
    assert r1.status_code == 200

    # Simulate process reload: wipe in-memory state but keep the file
    old_loaded = _tracker._loaded
    old_activity = dict(_tracker._activity)
    _tracker._activity.clear()
    _tracker._loaded = False  # force re-load from file on next call

    try:
        # Gate must still fire after in-memory wipe (reads from file)
        r2 = await client.post("/auth/login", json={"email": "reload@test.com", "password": "longpass123"})
        assert r2.status_code == 409, f"Gate should persist after tracker reload, got {r2.status_code}: {r2.text}"
    finally:
        # Restore so teardown clear() works correctly
        _tracker._activity.update(old_activity)
        _tracker._loaded = True
