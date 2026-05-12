# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

from __future__ import annotations

import time
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



# ---------------------------------------------------------------------------
# Single-user gate tests (JTI-based registry)
# ---------------------------------------------------------------------------

def _seed_foreign_session(user_id: str, expiry_offset: float = 900.0) -> None:
    """Directly insert a JTI for *user_id* into the tracker (test helper)."""
    import uuid as _uuid
    import time as _time
    from celerp.services.session_tracker import register_token as _reg
    _reg(str(_uuid.uuid4()), user_id, _time.time() + expiry_offset)


@pytest.mark.asyncio
async def test_single_user_gate_blocks_any_concurrent_user(client):
    """Gate: a different user cannot log in while another user has an active JTI."""
    from unittest.mock import patch
    from celerp.services.session_tracker import clear as _clear_tracker

    await client.post(
        "/auth/register",
        json={"company_name": "GateCo", "email": "gate_admin@test.com", "name": "Admin", "password": "longpass123"},
    )
    _clear_tracker()
    _seed_foreign_session("00000000-0000-0000-0000-000000000001")

    with patch("celerp.gateway.state.get_session_token", return_value=""):
        r = await client.post("/auth/login", json={"email": "gate_admin@test.com", "password": "longpass123"})
    assert r.status_code == 409, f"Expected 409 but got {r.status_code}: {r.text}"
    assert r.json()["detail"] == "direct_connection_limit"


@pytest.mark.asyncio
async def test_single_user_gate_empty_tracker_allows_login(client):
    """Gate: login succeeds when tracker is empty (no active JTIs)."""
    from celerp.services.session_tracker import clear as _clear_tracker

    await client.post(
        "/auth/register",
        json={"company_name": "GateCo2", "email": "gate2@test.com", "name": "Admin", "password": "longpass123"},
    )
    _clear_tracker()

    r = await client.post("/auth/login", json={"email": "gate2@test.com", "password": "longpass123"})
    assert r.status_code == 200, f"Empty tracker should allow login, got {r.status_code}: {r.text}"


@pytest.mark.asyncio
async def test_single_user_gate_allows_same_user_relogin(client):
    """Same user may log in again even while their own JTI is still registered (multi-tab)."""
    from unittest.mock import patch
    import base64, json as _json
    from celerp.services.session_tracker import clear as _clear_tracker

    await client.post(
        "/auth/register",
        json={"company_name": "GateCo3", "email": "gate3@test.com", "name": "Admin", "password": "longpass123"},
    )
    _clear_tracker()
    r1 = await client.post("/auth/login", json={"email": "gate3@test.com", "password": "longpass123"})
    assert r1.status_code == 200
    token = r1.json()["access_token"]
    payload_b64 = token.split(".")[1] + "=="
    user_id = _json.loads(base64.b64decode(payload_b64))["sub"]

    # Seed tracker with THIS user (simulates their existing session - e.g. another tab)
    _seed_foreign_session(user_id)

    # Same user re-login must be allowed (only OTHER user_ids block)
    with patch("celerp.gateway.state.get_session_token", return_value=""):
        r2 = await client.post("/auth/login", json={"email": "gate3@test.com", "password": "longpass123"})
    assert r2.status_code == 200, f"Same-user re-login should be allowed, got {r2.status_code}: {r2.text}"


@pytest.mark.asyncio
async def test_single_user_gate_bypassed_with_relay(client):
    """Gate is skipped when relay session token is active (cloud tier = multi-user)."""
    from unittest.mock import patch
    from celerp.services.session_tracker import clear as _clear_tracker

    await client.post(
        "/auth/register",
        json={"company_name": "GateCo4", "email": "gate4@test.com", "name": "Admin", "password": "longpass123"},
    )
    _clear_tracker()
    _seed_foreign_session("00000000-0000-0000-0000-000000000002")

    with patch("celerp.gateway.state.get_session_token", return_value="live-token-abc"):
        r = await client.post("/auth/login", json={"email": "gate4@test.com", "password": "longpass123"})
    assert r.status_code == 200, f"Relay present should bypass gate, got {r.status_code}: {r.text}"


@pytest.mark.asyncio
async def test_single_user_gate_survives_tracker_reload(client, tmp_path):
    """Gate persists across in-memory wipe when the file is populated.

    Regression: uvicorn --reload wipes in-process state; gate must still fire
    because session_tracker loads JTIs from .active_sessions.json on next use.
    """
    import celerp.services.session_tracker as _tracker
    from unittest.mock import patch
    import uuid as _uuid

    sessions_file = tmp_path / ".active_sessions.json"

    await client.post(
        "/auth/register",
        json={"company_name": "ReloadCo", "email": "reload@test.com", "name": "Admin", "password": "longpass123"},
    )

    with patch("celerp.services.session_tracker._sessions_path", return_value=sessions_file):
        r1 = await client.post("/auth/login", json={"email": "reload@test.com", "password": "longpass123"})
        assert r1.status_code == 200

        # Inject a foreign session directly into the tracker and save to file
        foreign_id = str(_uuid.uuid4())
        _seed_foreign_session(foreign_id)
        _tracker._save()

        # Simulate process reload: wipe in-memory state, force re-read from file
        old_sessions = dict(_tracker._sessions)
        old_nonce = _tracker._nonce
        _tracker._sessions.clear()
        _tracker._loaded = False

        try:
            with patch("celerp.gateway.state.get_session_token", return_value=""):
                r2 = await client.post("/auth/login", json={"email": "reload@test.com", "password": "longpass123"})
            assert r2.status_code == 409, f"Gate should persist after tracker reload, got {r2.status_code}: {r2.text}"
        finally:
            _tracker._sessions.update(old_sessions)
            _tracker._nonce = old_nonce
            _tracker._loaded = True


@pytest.mark.asyncio
async def test_gate_opens_when_all_jtis_expired(client):
    """Gate must NOT fire when the only registered JTI has already expired."""
    from unittest.mock import patch
    import uuid as _uuid
    import time as _time
    from celerp.services.session_tracker import clear as _clear_tracker, register_token as _reg

    await client.post(
        "/auth/register",
        json={"company_name": "IdleCo", "email": "idle@test.com", "name": "Admin", "password": "longpass123"},
    )
    _clear_tracker()

    # Register an already-expired JTI for a foreign user
    _reg(str(_uuid.uuid4()), "00000000-0000-0000-0000-000000000099", _time.time() - 1)

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
async def test_force_login_invalidates_other_user_tokens(client):
    """login-force must rotate the nonce so any previously-active user's token returns 401."""
    from unittest.mock import patch
    from celerp.services.session_tracker import clear as _clear_tracker

    await client.post(
        "/auth/register",
        json={"company_name": "ForceCo", "email": "owner@force.com", "name": "Owner", "password": "longpass123"},
    )
    _clear_tracker()
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
async def test_login_possible_after_force_login_and_logout(client):
    """Regression (Nikolai 2026-05-12): A logs in, B force-logs in, B logs out, both can log in again."""
    from unittest.mock import patch
    from celerp.services.session_tracker import clear as _clear_tracker

    await client.post(
        "/auth/register",
        json={"company_name": "ReloginCo", "email": "userA@relogin.com", "name": "A", "password": "pw123456"},
    )
    _clear_tracker()

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
