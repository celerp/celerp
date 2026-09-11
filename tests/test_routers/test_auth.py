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
async def test_change_password_kills_old_access_and_refresh(client, session):
    """A self password change rotates the nonce, so the caller's own pre-change
    access and refresh tokens both return 401 afterwards."""
    from celerp.services.session_tracker import clear as _clear_tracker

    await client.post(
        "/auth/register",
        json={"company_name": "PwKill", "email": "pwkill@t.com", "name": "Admin", "password": "oldpass123"},
    )
    await _clear_tracker(session)
    r = await client.post("/auth/login", json={"email": "pwkill@t.com", "password": "oldpass123"})
    assert r.status_code == 200
    old_access = r.json()["access_token"]
    old_refresh = r.json()["refresh_token"]

    r2 = await client.post(
        "/auth/change-password",
        json={"current_password": "oldpass123", "new_password": "newpass456"},
        headers={"Authorization": f"Bearer {old_access}"},
    )
    assert r2.status_code == 200

    r_acc = await client.get("/auth/my-companies", headers={"Authorization": f"Bearer {old_access}"})
    assert r_acc.status_code == 401
    r_ref = await client.post("/auth/token/refresh", json={"refresh_token": old_refresh})
    assert r_ref.status_code == 401


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
    """Directly insert a JTI for *user_id* into the DB tracker (test helper).

    Ensures a real users row exists first: Postgres enforces the
    session_registry.user_id foreign key (SQLite did not).
    """
    from celerp.models.company import User
    from celerp.services.session_tracker import register_token as _reg
    uid = _uuid.UUID(str(user_id))
    if await session.get(User, uid) is None:
        session.add(User(id=uid, email=f"foreign-{uid}@test.local", name="Foreign User"))
        await session.flush()
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
    await _seed_foreign_session(session, "00000000-0000-0000-0000-000000000099", expiry_offset_s=-1)

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


# ---------------------------------------------------------------------------
# Token-format v2 invariants (section 7 test matrix)
#
# These prove the security contract of the v2 cutover directly against the
# frozen public interface of celerp.services.auth. They do not depend on any
# workstream-2 surface (middleware sliding-refresh, SSE), so they must be green
# on this branch.
# ---------------------------------------------------------------------------


def _decode(token: str) -> dict:
    from jose import jwt as _jwt
    from celerp.config import settings
    return _jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])


@pytest.mark.asyncio
async def test_new_access_token_carries_v2_contract(client):
    """A freshly issued access token has auth_ver=2, type=access and a non-empty snonce."""
    from celerp.services.auth import AUTH_TOKEN_VERSION
    reg = await client.post(
        "/auth/register",
        json={"company_name": "FmtA", "email": "fmta@t.com", "name": "Admin", "password": "pw"},
    )
    claims = _decode(reg.json()["access_token"])
    assert claims["auth_ver"] == AUTH_TOKEN_VERSION == 2
    assert claims["type"] == "access"
    assert claims["snonce"], "access token must carry a non-empty snonce"


@pytest.mark.asyncio
async def test_new_refresh_token_carries_v2_contract(client):
    """A refresh token has auth_ver=2, type=refresh, a non-empty snonce, and no
    authoritative role claim (the role is read from DB on refresh, never trusted)."""
    from celerp.services.auth import AUTH_TOKEN_VERSION
    reg = await client.post(
        "/auth/register",
        json={"company_name": "FmtR", "email": "fmtr@t.com", "name": "Admin", "password": "pw"},
    )
    claims = _decode(reg.json()["refresh_token"])
    assert claims["auth_ver"] == AUTH_TOKEN_VERSION == 2
    assert claims["type"] == "refresh"
    assert claims["snonce"], "refresh token must carry a non-empty snonce"
    assert "role" not in claims, "refresh token must NOT embed an authoritative role"


@pytest.mark.asyncio
async def test_pre_v2_access_token_rejected(client, session):
    """A token minted in the pre-v2 shape (no auth_ver/type/snonce) is rejected 401."""
    from jose import jwt as _jwt
    from celerp.config import settings
    reg = await client.post(
        "/auth/register",
        json={"company_name": "CutA", "email": "cuta@t.com", "name": "Admin", "password": "pw"},
    )
    good = _decode(reg.json()["access_token"])
    # Same signing secret, same subject/company, but the old claim set: no
    # auth_ver, no type, no snonce. This is exactly what an old build issued.
    legacy_payload = {
        "sub": good["sub"],
        "company_id": good["company_id"],
        "role": "owner",
        "jti": good["jti"],
        "exp": good["exp"],
    }
    legacy = _jwt.encode(legacy_payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    r = await client.get("/auth/my-companies", headers={"Authorization": f"Bearer {legacy}"})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_pre_v2_refresh_token_rejected(client):
    """A pre-v2-shaped refresh token (no auth_ver/type/snonce) is rejected 401."""
    from jose import jwt as _jwt
    from celerp.config import settings
    reg = await client.post(
        "/auth/register",
        json={"company_name": "CutR", "email": "cutr@t.com", "name": "Admin", "password": "pw"},
    )
    good = _decode(reg.json()["refresh_token"])
    legacy_payload = {
        "sub": good["sub"],
        "company_id": good["company_id"],
        "role": "owner",
        "exp": good["exp"],
    }
    legacy = _jwt.encode(legacy_payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    r = await client.post("/auth/token/refresh", json={"refresh_token": legacy})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_refresh_token_rejected_as_bearer(client):
    """A refresh token presented as a bearer access token is rejected 401
    (type separation): it carries type=refresh, which decode_access_token refuses."""
    reg = await client.post(
        "/auth/register",
        json={"company_name": "SepB", "email": "sepb@t.com", "name": "Admin", "password": "pw"},
    )
    refresh = reg.json()["refresh_token"]
    r = await client.get("/auth/my-companies", headers={"Authorization": f"Bearer {refresh}"})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_access_token_missing_snonce_rejected(client):
    """An otherwise-valid access token with an empty snonce fails closed (401).

    There is no legacy accept path for a missing nonce; every access token must
    prove per-user nonce equality, and an empty one can never match."""
    from jose import jwt as _jwt
    from celerp.config import settings
    reg = await client.post(
        "/auth/register",
        json={"company_name": "NonceA", "email": "noncea@t.com", "name": "Admin", "password": "pw"},
    )
    good = _decode(reg.json()["access_token"])
    good["snonce"] = ""
    tampered = _jwt.encode(good, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    r = await client.get("/auth/my-companies", headers={"Authorization": f"Bearer {tampered}"})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_refresh_token_missing_snonce_rejected(client):
    """A refresh token with an empty snonce is rejected 401 at the refresh endpoint."""
    from jose import jwt as _jwt
    from celerp.config import settings
    reg = await client.post(
        "/auth/register",
        json={"company_name": "NonceR", "email": "noncer@t.com", "name": "Admin", "password": "pw"},
    )
    good = _decode(reg.json()["refresh_token"])
    good["snonce"] = ""
    tampered = _jwt.encode(good, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    r = await client.post("/auth/token/refresh", json={"refresh_token": tampered})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_old_refresh_token_rejected_after_logout(client):
    """After logout, a refresh token issued before it is dead (nonce rotated)."""
    reg = await client.post(
        "/auth/register",
        json={"company_name": "LogoutR", "email": "logoutr@t.com", "name": "Admin", "password": "pw"},
    )
    access = reg.json()["access_token"]
    refresh = reg.json()["refresh_token"]

    # The refresh token works before logout.
    r_pre = await client.post("/auth/token/refresh", json={"refresh_token": refresh})
    assert r_pre.status_code == 200
    # Refresh rotated the nonce and returned a fresh pair; log out with the fresh access.
    fresh = r_pre.json()
    r_logout = await client.post(
        "/auth/logout", headers={"Authorization": f"Bearer {fresh['access_token']}"}
    )
    assert r_logout.status_code == 200

    # Both the original and the just-issued refresh tokens are now dead.
    r_old = await client.post("/auth/token/refresh", json={"refresh_token": refresh})
    assert r_old.status_code == 401
    r_new = await client.post("/auth/token/refresh", json={"refresh_token": fresh["refresh_token"]})
    assert r_new.status_code == 401
    # And the pre-logout access token is dead too.
    r_acc = await client.get("/auth/my-companies", headers={"Authorization": f"Bearer {access}"})
    assert r_acc.status_code == 401


@pytest.mark.asyncio
async def test_displaced_user_refresh_token_rejected_after_force_login(client, session):
    """After a force-login rotates the nonce, the displaced session's refresh token is dead."""
    from unittest.mock import patch
    from celerp.services.session_tracker import clear as _clear_tracker

    await client.post(
        "/auth/register",
        json={"company_name": "ForceR", "email": "forcer@t.com", "name": "Owner", "password": "pw123456"},
    )
    await _clear_tracker(session)
    r_login = await client.post("/auth/login", json={"email": "forcer@t.com", "password": "pw123456"})
    assert r_login.status_code == 200
    old_refresh = r_login.json()["refresh_token"]

    with patch("celerp.gateway.state.get_session_token", return_value=""):
        r_force = await client.post(
            "/auth/login-force", json={"email": "forcer@t.com", "password": "pw123456"}
        )
    assert r_force.status_code == 200

    r_old = await client.post("/auth/token/refresh", json={"refresh_token": old_refresh})
    assert r_old.status_code == 401


@pytest.mark.asyncio
async def test_refresh_uses_current_db_role_not_jwt_claim(client, session):
    """Refreshing after a DB role change reflects the new role, never the old JWT.

    The refresh token embeds no role, and the issuer reads the role from the
    current UserCompany membership, so the reissued access token carries the
    current DB role."""
    import base64, json as _json, uuid as _uuid
    from sqlalchemy import select
    from celerp.models.accounting import UserCompany
    from celerp.services.session_tracker import clear as _clear_tracker

    owner_reg = await client.post(
        "/auth/register",
        json={"company_name": "RoleCo", "email": "roleowner@t.com", "name": "Owner", "password": "pw"},
    )
    owner_h = {"Authorization": f"Bearer {owner_reg.json()['access_token']}"}
    # Create a manager user under the same company.
    await client.post(
        "/companies/me/users",
        json={"email": "target@t.com", "name": "Target", "role": "manager", "password": "pw123"},
        headers=owner_h,
    )
    await _clear_tracker(session)
    r_login = await client.post("/auth/login", json={"email": "target@t.com", "password": "pw123"})
    assert r_login.status_code == 200
    refresh = r_login.json()["refresh_token"]
    access = r_login.json()["access_token"]
    old_claims = _decode(access)
    assert old_claims["role"] == "manager"
    user_id = old_claims["sub"]
    company_id = old_claims["company_id"]

    # Demote the target in DB directly (membership role is authoritative).
    link = await session.scalar(
        select(UserCompany).where(
            UserCompany.user_id == _uuid.UUID(user_id),
            UserCompany.company_id == _uuid.UUID(company_id),
        )
    )
    link.role = "operator"
    await session.commit()

    r = await client.post("/auth/token/refresh", json={"refresh_token": refresh})
    assert r.status_code == 200
    new_claims = _decode(r.json()["access_token"])
    assert new_claims["role"] == "operator", "refreshed token must carry the current DB role"


@pytest.mark.asyncio
async def test_inactive_user_rejected_on_access_and_refresh(client, session):
    """A user marked inactive in DB is rejected on both a bearer access token and refresh."""
    import uuid as _uuid
    from celerp.models.company import User
    from celerp.services.session_tracker import clear as _clear_tracker

    reg = await client.post(
        "/auth/register",
        json={"company_name": "InactCo", "email": "inact@t.com", "name": "Admin", "password": "pw"},
    )
    access = reg.json()["access_token"]
    refresh = reg.json()["refresh_token"]
    user_id = _decode(access)["sub"]

    user = await session.get(User, _uuid.UUID(user_id))
    user.is_active = False
    await session.commit()

    r_acc = await client.get("/auth/my-companies", headers={"Authorization": f"Bearer {access}"})
    assert r_acc.status_code == 401
    r_ref = await client.post("/auth/token/refresh", json={"refresh_token": refresh})
    assert r_ref.status_code == 401


@pytest.mark.asyncio
async def test_deactivated_membership_rejected_on_access_and_refresh(client, session):
    """When a user's company membership is deactivated, both access and refresh are rejected."""
    import base64, json as _json, uuid as _uuid
    from sqlalchemy import select
    from celerp.models.accounting import UserCompany
    from celerp.services.session_tracker import clear as _clear_tracker

    owner_reg = await client.post(
        "/auth/register",
        json={"company_name": "MemCo", "email": "memowner@t.com", "name": "Owner", "password": "pw"},
    )
    owner_h = {"Authorization": f"Bearer {owner_reg.json()['access_token']}"}
    await client.post(
        "/companies/me/users",
        json={"email": "member@t.com", "name": "Member", "role": "manager", "password": "pw123"},
        headers=owner_h,
    )
    await _clear_tracker(session)
    r_login = await client.post("/auth/login", json={"email": "member@t.com", "password": "pw123"})
    access = r_login.json()["access_token"]
    refresh = r_login.json()["refresh_token"]
    claims = _decode(access)

    link = await session.scalar(
        select(UserCompany).where(
            UserCompany.user_id == _uuid.UUID(claims["sub"]),
            UserCompany.company_id == _uuid.UUID(claims["company_id"]),
        )
    )
    link.is_active = False
    await session.commit()

    r_acc = await client.get("/auth/my-companies", headers={"Authorization": f"Bearer {access}"})
    assert r_acc.status_code == 401
    r_ref = await client.post("/auth/token/refresh", json={"refresh_token": refresh})
    assert r_ref.status_code == 401


@pytest.mark.asyncio
async def test_permission_only_route_rejects_invalid_token(client):
    """A route guarded solely by require_permission() rejects an unauthenticated or
    invalid token with 401 - the permission guard resolves the auth context first,
    so no valid session means no route, before any 403 role decision.

    Registered on the live app as a throwaway route rather than driving a
    destructive real endpoint (per the test-placement note)."""
    from fastapi import APIRouter
    from celerp.main import app
    from celerp.services.permissions import require_permission

    probe = APIRouter()

    @probe.get("/__perm_probe__", dependencies=[require_permission("manage_company_settings")])
    async def _probe() -> dict:
        return {"ok": True}

    app.include_router(probe)
    try:
        # No Authorization header at all.
        r_none = await client.get("/__perm_probe__")
        assert r_none.status_code == 401
        # A syntactically invalid bearer.
        r_bad = await client.get("/__perm_probe__", headers={"Authorization": "Bearer not.a.token"})
        assert r_bad.status_code == 401
    finally:
        app.router.routes = [
            rt for rt in app.router.routes if getattr(rt, "path", None) != "/__perm_probe__"
        ]


def test_a7_tampered_signature_helper_returns_no_refresh():
    """Exploit regression (A7): the sliding-refresh helper must not re-mint a token
    whose signature does not verify against the real secret.

    A forged bearer is built by signing a past-half-life payload with a DIFFERENT
    secret, so jwt.decode against settings.jwt_secret raises. The current
    fail-open helper re-signs from unverified claims and returns a token; the
    fixed helper validates the signature first and returns None. This test is RED
    on the merge base and only passes once A7 lands (workstream 2)."""
    import time as _time
    import uuid as _uuid
    from jose import jwt as _jwt
    from celerp.config import settings
    from celerp.middleware import _maybe_refresh_bearer

    now = _time.time()
    total_ttl = int(settings.access_token_expire_minutes) * 60
    forged_payload = {
        "sub": "attacker",
        "company_id": "victim-co",
        "role": "owner",
        "jti": str(_uuid.uuid4()),
        "snonce": "anything",
        "exp": int(now + total_ttl * 0.49),  # past half-life so the old path would re-mint
    }
    # Signed with the WRONG secret: verifying against the real secret must fail.
    forged = _jwt.encode(forged_payload, settings.jwt_secret + "-wrong", algorithm=settings.jwt_algorithm)
    # Sanity: the forged token does NOT verify against the real secret.
    with pytest.raises(Exception):
        _jwt.decode(forged, settings.jwt_secret, algorithms=[settings.jwt_algorithm])

    result = _maybe_refresh_bearer(forged)
    assert result is None, "A tampered-signature bearer must never be re-minted"


@pytest.mark.asyncio
async def test_a7_tampered_signature_no_refresh_header(client):
    """Exploit regression (A7), end to end: a forged bearer (valid-looking claims,
    invalid signature) sent to a harmless path never comes back with an
    X-Refreshed-Token header. RED on merge base; green once A7 validates the
    signature before re-signing."""
    import time as _time
    import uuid as _uuid
    from jose import jwt as _jwt
    from celerp.config import settings

    now = _time.time()
    total_ttl = int(settings.access_token_expire_minutes) * 60
    forged_payload = {
        "sub": str(_uuid.uuid4()),
        "company_id": str(_uuid.uuid4()),
        "role": "owner",
        "jti": str(_uuid.uuid4()),
        "snonce": "anything",
        "exp": int(now + total_ttl * 0.49),
    }
    forged = _jwt.encode(forged_payload, settings.jwt_secret + "-wrong", algorithm=settings.jwt_algorithm)
    r = await client.get("/health", headers={"Authorization": f"Bearer {forged}"})
    assert "X-Refreshed-Token" not in r.headers
