# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Global-user tenant isolation and legacy-role normalization for patch_user.

Target (post-fix) API:
- UserPatch is restricted to role + is_active only, with Pydantic extra="forbid".
  password and name are no longer accepted on PATCH /companies/me/users/{id}, so a
  company admin cannot overwrite the global User.name or User.auth_hash that a user
  shares across every company they belong to.
- normalize_role() is public in celerp.services.auth; patch_user hierarchy
  comparisons and list_users output normalize a legacy salesperson membership to
  operator, so the "cannot modify a higher-ranked user" guard and the surfaced role
  are correct for legacy accounts.

At merge-base ea480c48 none of these fixes are present, so the new-behavior tests
are RED (currently 200 / raw role); test 25 and 26 assert behavior that must stay
green after extra="forbid" lands.
"""

from __future__ import annotations

import uuid as _uuid

import pytest

from celerp.models.accounting import UserCompany
from celerp.models.company import Company, User


async def _register_owner(client, company_name: str, email: str) -> dict:
    r = await client.post(
        "/auth/register",
        json={"company_name": company_name, "email": email, "name": "Owner", "password": "pw"},
    )
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def _create_member(client, admin_h: dict, email: str, role: str, name: str = "Target") -> str:
    r = await client.post(
        "/companies/me/users",
        json={"email": email, "name": name, "role": role, "password": "pw123"},
        headers=admin_h,
    )
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _add_second_company_membership(session, user_id: str) -> None:
    """Link the target user into a second (Company B) membership directly, so the
    User row it owns is genuinely shared across two tenants."""
    company_b = Company(id=_uuid.uuid4(), name="Company B", slug=f"company-b-{_uuid.uuid4().hex[:8]}", settings={})
    session.add(company_b)
    await session.flush()
    session.add(
        UserCompany(
            id=_uuid.uuid4(),
            user_id=_uuid.UUID(str(user_id)),
            company_id=company_b.id,
            role="operator",
        )
    )
    await session.flush()


# ---------------------------------------------------------------------------
# 23 / 24 - global credential + name overwrite blocked by extra="forbid"
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_cannot_patch_global_password_of_shared_user(client, session):
    """23: a user in Company A and Company B; admin A patching that user's password
    is rejected (422, extra fields forbidden) and the global auth_hash is unchanged."""
    admin_a = await _register_owner(client, "Company A", "admin-a@example.com")
    target_id = await _create_member(client, admin_a, "shared@example.com", "operator")
    await _add_second_company_membership(session, target_id)

    original = await session.get(User, _uuid.UUID(target_id))
    original_hash = original.auth_hash

    r = await client.patch(
        f"/companies/me/users/{target_id}", json={"password": "brandnew1"}, headers=admin_a
    )
    assert r.status_code == 422, r.text

    session.expire_all()
    after = await session.get(User, _uuid.UUID(target_id))
    assert after.auth_hash == original_hash


@pytest.mark.asyncio
async def test_admin_cannot_patch_global_name_of_shared_user(client, session):
    """24: admin A patching that user's global name is rejected (422) and the shared
    User.name is unchanged."""
    admin_a = await _register_owner(client, "Company A", "admin-a2@example.com")
    target_id = await _create_member(client, admin_a, "shared2@example.com", "operator", name="Original")
    await _add_second_company_membership(session, target_id)

    r = await client.patch(
        f"/companies/me/users/{target_id}", json={"name": "Renamed"}, headers=admin_a
    )
    assert r.status_code == 422, r.text

    session.expire_all()
    after = await session.get(User, _uuid.UUID(target_id))
    assert after.name == "Original"


# ---------------------------------------------------------------------------
# 25 - membership role/active still editable under hierarchy/last-owner rules
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_can_still_change_membership_role_and_active(client, session):
    """25: with password/name forbidden, admin A can still change the A-membership
    role and active flag (subject to hierarchy/last-owner rules)."""
    admin_a = await _register_owner(client, "Company A", "admin-a3@example.com")
    target_id = await _create_member(client, admin_a, "member3@example.com", "operator")

    r = await client.patch(
        f"/companies/me/users/{target_id}", json={"role": "manager"}, headers=admin_a
    )
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True

    r = await client.patch(
        f"/companies/me/users/{target_id}", json={"is_active": False}, headers=admin_a
    )
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True

    session.expire_all()
    link = (
        await session.execute(
            UserCompany.__table__.select().where(
                UserCompany.user_id == _uuid.UUID(target_id)
            )
        )
    ).first()
    assert link is not None
    assert link.role == "manager"
    assert link.is_active is False


# ---------------------------------------------------------------------------
# 26 - a membership change still invalidates the target's prior sessions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_membership_change_invalidates_prior_sessions(client, session):
    """26: changing the A membership (role/active) invalidates the target's prior
    sessions - their old access token is rejected on next use."""
    from celerp.services.session_tracker import clear as _clear_tracker

    admin_a = await _register_owner(client, "Company A", "admin-a4@example.com")
    target_email = "member4@example.com"
    target_id = await _create_member(client, admin_a, target_email, "operator")

    await _clear_tracker(session)
    r_login = await client.post("/auth/login", json={"email": target_email, "password": "pw123"})
    assert r_login.status_code == 200, r_login.text
    target_access = r_login.json()["access_token"]

    # Token works before the change.
    r_ok = await client.get("/auth/my-companies", headers={"Authorization": f"Bearer {target_access}"})
    assert r_ok.status_code == 200, r_ok.text

    r = await client.patch(
        f"/companies/me/users/{target_id}", json={"role": "manager"}, headers=admin_a
    )
    assert r.status_code == 200, r.text

    r_dead = await client.get("/auth/my-companies", headers={"Authorization": f"Bearer {target_access}"})
    assert r_dead.status_code == 401, r_dead.text


# ---------------------------------------------------------------------------
# 27 - legacy salesperson normalized in the target-role hierarchy comparison
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_legacy_salesperson_target_normalized_in_hierarchy(client, session):
    """27: a target whose membership role is the legacy 'salesperson' must be treated
    as 'operator' (level 2) in patch_user's "cannot modify a higher-ranked user"
    guard. A caller who is a viewer (level 1) holding manage_users must be blocked
    (403), because operator outranks viewer.

    Pre-fix, patch_user compares the raw 'salesperson' via ROLE_LEVELS.get(role, 0),
    scoring it level 0, so the guard passes and the viewer's PATCH returns 200. Post-
    fix normalizes salesperson to operator=2, so 2 > 1 blocks it (403). The two
    outcomes differ observably."""
    from test_helpers import grant_permission, make_authed_token

    admin_a = await _register_owner(client, "Company A", "admin-a5@example.com")

    # Resolve company id from the owner's own membership.
    me = await client.get("/companies/me/users", headers=admin_a)
    assert me.status_code == 200, me.text
    owner_row = next(u for u in me.json()["items"] if u["role"] == "owner")
    company_id = (
        await session.execute(
            UserCompany.__table__.select().where(
                UserCompany.user_id == _uuid.UUID(owner_row["id"])
            )
        )
    ).first().company_id

    # A viewer caller who is granted manage_users (its floor role is viewer).
    caller_id = await _create_member(client, admin_a, "viewer-caller@example.com", "viewer", name="Caller")
    await grant_permission(client, admin_a, "manage_users", "viewer")

    # A target whose membership role is the legacy 'salesperson' (seeded directly:
    # create_user rejects roles outside ROLE_LEVELS).
    target_user = User(id=_uuid.uuid4(), email="legacy-target@example.com", name="Legacy", is_active=True)
    session.add(target_user)
    await session.flush()
    session.add(
        UserCompany(
            id=_uuid.uuid4(),
            user_id=target_user.id,
            company_id=company_id,
            role="salesperson",
        )
    )
    await session.flush()

    caller_token = await make_authed_token(session, str(caller_id), str(company_id), "viewer")
    caller_h = {"Authorization": f"Bearer {caller_token}"}

    r = await client.patch(
        f"/companies/me/users/{target_user.id}", json={"is_active": False}, headers=caller_h
    )
    assert r.status_code == 403, r.text
    assert "above your own" in r.json()["detail"].lower()


# ---------------------------------------------------------------------------
# +R (a) - list_users returns the normalized role for a legacy membership
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_users_normalizes_legacy_salesperson_role(client, session):
    """+R(a): GET /companies/me/users returns 'operator' for a membership stored as
    the legacy 'salesperson', not the raw string. RED at merge-base, where
    list_users returns the raw UserCompany.role."""
    admin_a = await _register_owner(client, "Company A", "admin-a6@example.com")

    owner_row = next(
        u for u in (await client.get("/companies/me/users", headers=admin_a)).json()["items"]
        if u["role"] == "owner"
    )
    company_id = (
        await session.execute(
            UserCompany.__table__.select().where(
                UserCompany.user_id == _uuid.UUID(owner_row["id"])
            )
        )
    ).first().company_id

    legacy_user = User(id=_uuid.uuid4(), email="legacy-list@example.com", name="Legacy", is_active=True)
    session.add(legacy_user)
    await session.flush()
    session.add(
        UserCompany(
            id=_uuid.uuid4(),
            user_id=legacy_user.id,
            company_id=company_id,
            role="salesperson",
        )
    )
    await session.flush()

    r = await client.get("/companies/me/users", headers=admin_a)
    assert r.status_code == 200, r.text
    listed = next(u for u in r.json()["items"] if u["email"] == "legacy-list@example.com")
    assert listed["role"] == "operator"
