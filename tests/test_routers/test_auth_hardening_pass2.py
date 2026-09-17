# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Second-pass auth/session hardening - token contract and revocation.

Covers the genuinely-new behavior from plan section 5 items 5-14 (non-race).
Items 1-4, 6, 9-11, and 15 already hold at the merge base and are cited from
their existing tests in the cluster return, not duplicated here.

Every test in this file targets the POST-fix API so it goes green once the fix
lands. The refresh-only-logout (item 7), stale-refresh-cannot-logout (item 8),
and force-login-kills-dormant-refresh (item 13) tests are RED at the merge base
by design: logout still requires a live Bearer, and force-login only rotates
users with an active access JTI.
"""

from __future__ import annotations

import uuid

import pytest
from jose import jwt as _jwt
from sqlalchemy import delete, select

from celerp.config import settings
from celerp.models.accounting import UserCompany
from celerp.models.auth import SessionRegistry, UserAuthState


def _decode(token: str) -> dict:
    return _jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])


# ---------------------------------------------------------------------------
# Item 5 - a new access + refresh pair share the current per-user snonce.
# The pair is bound to one session generation; a mismatch between them would
# let one outlive a rotation the other honors. Behavior holds at merge base.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_issued_access_and_refresh_share_current_snonce(client, session):
    """The access and refresh tokens from a refresh exchange carry an identical
    snonce, and it equals the user's current DB nonce."""
    from celerp.services.session_tracker import get_nonce as _get_nonce

    reg = await client.post(
        "/auth/register",
        json={"company_name": "SnonceCo", "email": "snonce@example.com", "name": "Admin", "password": "pwvalid1"},
    )
    assert reg.status_code == 200
    refresh = reg.json()["refresh_token"]

    r = await client.post("/auth/token/refresh", json={"refresh_token": refresh})
    assert r.status_code == 200
    access_claims = _decode(r.json()["access_token"])
    refresh_claims = _decode(r.json()["refresh_token"])

    assert access_claims["snonce"], "access token must carry a non-empty snonce"
    assert access_claims["snonce"] == refresh_claims["snonce"], "pair must share one snonce"

    current = await _get_nonce(session, access_claims["sub"])
    assert access_claims["snonce"] == current, "pair snonce must equal the current DB nonce"


# ---------------------------------------------------------------------------
# Item 7 - server-side logout with a refresh-only credential (F3/A8).
# RED at merge base: logout depends on the auto_error Bearer scheme, so a
# request carrying no Bearer 401s and never revokes.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_logout_with_refresh_only_revokes_server_side(client):
    """POST /auth/logout with NO Bearer and a valid v2 refresh token in the JSON
    body revokes the session: the refresh can no longer be exchanged afterwards."""
    reg = await client.post(
        "/auth/register",
        json={"company_name": "RefLogoutCo", "email": "reflogout@example.com", "name": "Admin", "password": "pwvalid1"},
    )
    assert reg.status_code == 200
    refresh = reg.json()["refresh_token"]

    # The refresh works before logout.
    r_pre = await client.post("/auth/token/refresh", json={"refresh_token": refresh})
    assert r_pre.status_code == 200

    # Log out using only the refresh token (no Authorization header).
    r_logout = await client.post("/auth/logout", json={"refresh_token": refresh})
    assert r_logout.status_code == 200, r_logout.text

    # The refresh is now dead: the nonce rotated server-side.
    r_post = await client.post("/auth/token/refresh", json={"refresh_token": refresh})
    assert r_post.status_code == 401, "refresh must be revoked after refresh-only logout"


# ---------------------------------------------------------------------------
# Item 8 - a stale-generation credential cannot revoke a newer session
# generation (F3 expected_snonce gate). RED at merge base: invalidate_sessions
# has no expected_snonce parameter, so a stale credential rotates the current
# nonce unconditionally and grief-revokes the live session.
#
# This proves the gate at the service layer where it lives (the strongest
# proof). Its HTTP composition is item 7 (logout honors a refresh-only
# credential, red at base) plus this gate: logout threads the presented
# credential's snonce as expected_snonce, so a stale one skips rotation.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stale_snonce_cannot_revoke_newer_generation(session):
    """invalidate_sessions with a stale expected_snonce must NOT rotate a newer
    generation. After an authoritative revoke advances N0 -> N1, a credential
    still carrying N0 leaves N1 intact."""
    from celerp.services.session_tracker import invalidate_sessions, get_nonce
    from celerp.models.company import User

    user = User(id=uuid.uuid4(), email="stalegate@example.com", name="Stale", is_active=True)
    session.add(user)
    await session.flush()
    session.add(UserAuthState(user_id=user.id, nonce="generation-0"))
    await session.commit()

    # An authoritative revoke (expected_snonce=None) advances the generation.
    await invalidate_sessions(session, str(user.id))
    n1 = await get_nonce(session, str(user.id))
    assert n1 != "generation-0", "authoritative revoke must rotate the nonce"

    # A stale credential authenticated on generation-0 must not rotate N1.
    await invalidate_sessions(session, str(user.id), expected_snonce="generation-0")
    assert await get_nonce(session, str(user.id)) == n1, \
        "a stale expected_snonce must not rotate the newer generation"

    # A current credential (expected_snonce=N1) still revokes.
    await invalidate_sessions(session, str(user.id), expected_snonce=n1)
    assert await get_nonce(session, str(user.id)) != n1, \
        "a current expected_snonce must still rotate"


# ---------------------------------------------------------------------------
# Item 12 - deactivate then reactivate a membership does NOT revive old tokens.
# Behavior holds at merge base: deactivation rotates the nonce, and reactivation
# never rolls it back, so the pre-deactivation tokens stay dead.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reactivation_does_not_revive_old_tokens(client, session):
    """Deactivating a user's membership kills their tokens (nonce rotation);
    reactivating the membership does not resurrect the pre-deactivation tokens."""
    from celerp.services.session_tracker import clear as _clear_tracker

    reg = await client.post(
        "/auth/register",
        json={"company_name": "ReactCo", "email": "reactowner@example.com", "name": "Owner", "password": "pwvalid1"},
    )
    owner_h = {"Authorization": f"Bearer {reg.json()['access_token']}"}
    target_email = "reacttarget@example.com"
    r_new = await client.post(
        "/companies/me/users",
        json={"email": target_email, "name": "Target", "role": "manager", "password": "pw123val"},
        headers=owner_h,
    )
    assert r_new.status_code == 200
    target_id = r_new.json()["id"]
    await _clear_tracker(session)
    r_login = await client.post("/auth/login", json={"email": target_email, "password": "pw123val"})
    assert r_login.status_code == 200
    old_access = r_login.json()["access_token"]
    old_refresh = r_login.json()["refresh_token"]

    # Deactivate, then reactivate the membership.
    r_off = await client.patch(
        f"/companies/me/users/{target_id}", json={"is_active": False}, headers=owner_h
    )
    assert r_off.status_code == 200
    r_on = await client.patch(
        f"/companies/me/users/{target_id}", json={"is_active": True}, headers=owner_h
    )
    assert r_on.status_code == 200

    # The old tokens must remain dead - reactivation did not roll the nonce back.
    r_acc = await client.get("/auth/my-companies", headers={"Authorization": f"Bearer {old_access}"})
    assert r_acc.status_code == 401, "reactivation must not revive the old access token"
    r_ref = await client.post("/auth/token/refresh", json={"refresh_token": old_refresh})
    assert r_ref.status_code == 401, "reactivation must not revive the old refresh token"


# ---------------------------------------------------------------------------
# Item 13 - force-login kills a valid refresh whose access JTI is expired/absent
# (F4/A5). RED at merge base: invalidate_all_sessions derives its rotate set from
# live SessionRegistry JTIs, so a dormant refresh (no live JTI) is not rotated
# and can refresh back in after a force-login by another actor.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_force_login_kills_dormant_refresh_without_live_jti(client, session):
    """A user with a still-valid refresh token but no live access JTI must be
    rotated by another actor's force-login: the dormant refresh can no longer
    refresh afterwards."""
    from celerp.services.session_tracker import clear as _clear_tracker

    # Displaced user: registers, then we drop their access JTI to simulate an
    # expired/absent access session while the refresh token stays valid.
    reg = await client.post(
        "/auth/register",
        json={"company_name": "DormantCo", "email": "dormant@example.com", "name": "Dormant", "password": "pw123456"},
    )
    assert reg.status_code == 200
    dormant_refresh = reg.json()["refresh_token"]
    dormant_sub = uuid.UUID(_decode(dormant_refresh)["sub"])
    owner_h = {"Authorization": f"Bearer {reg.json()['access_token']}"}

    # A second, different actor. Registration is single-bootstrap, so the owner
    # creates the actor through the admin API rather than a second /register.
    r_actor = await client.post(
        "/companies/me/users",
        json={"email": "actor@example.com", "name": "Actor", "role": "admin", "password": "pw123456"},
        headers=owner_h,
    )
    assert r_actor.status_code == 200, r_actor.text

    # Remove every SessionRegistry JTI for the dormant user: no live access token
    # remains, only the valid refresh. Its UserAuthState nonce still matches.
    await session.execute(delete(SessionRegistry).where(SessionRegistry.user_id == dormant_sub))
    await session.commit()
    assert await session.scalar(
        select(UserAuthState).where(UserAuthState.user_id == dormant_sub)
    ) is not None, "dormant user keeps a valid auth-state nonce"

    # The actor force-logs-in. Clear the tracker so the login gate opens.
    await _clear_tracker(session)
    from unittest.mock import patch

    with patch("celerp.gateway.state.get_session_token", return_value=""):
        r_force = await client.post(
            "/auth/login-force", json={"email": "actor@example.com", "password": "pw123456"}
        )
    assert r_force.status_code == 200

    # The dormant refresh must now be dead: force-login rotated every auth-state
    # nonce, not merely users with a live access JTI.
    r_ref = await client.post("/auth/token/refresh", json={"refresh_token": dormant_refresh})
    assert r_ref.status_code == 401, "force-login must rotate a dormant refresh with no live JTI"


# ---------------------------------------------------------------------------
# Item 14 - a permission-only route (/system/restart) rejects a revoked access
# token. Behavior holds at merge base: the require_permission dependency resolves
# the auth context first, so a rotated-nonce token 401s before any 403.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_system_restart_rejects_revoked_access_token(client):
    """POST /system/restart, guarded solely by require_permission, rejects a
    syntactically valid access token whose session was revoked (nonce rotated)."""
    reg = await client.post(
        "/auth/register",
        json={"company_name": "RestartCo", "email": "restart@example.com", "name": "Owner", "password": "pwvalid1"},
    )
    assert reg.status_code == 200
    access = reg.json()["access_token"]

    # Revoke the session: logout rotates the nonce so the access token is dead.
    r_logout = await client.post("/auth/logout", headers={"Authorization": f"Bearer {access}"})
    assert r_logout.status_code == 200

    r = await client.post("/system/restart", headers={"Authorization": f"Bearer {access}"})
    assert r.status_code == 401, "a revoked access token must be rejected before the permission check"
