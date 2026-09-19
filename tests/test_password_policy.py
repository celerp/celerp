# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Centralised backend password-length policy.

One rule, enforced in one place: passwords must be at least MIN_PASSWORD_LENGTH
characters. hash_password() is the final backend invariant; every password-setting
path routes through the shared validator, and the UI preflight reads the same
constant. No composition rules, no new dependency.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from test_helpers import make_authed_token


def test_validate_password_rejects_short():
    from celerp.services.auth import MIN_PASSWORD_LENGTH, validate_password
    assert MIN_PASSWORD_LENGTH == 8
    with pytest.raises(ValueError):
        validate_password("short7x"[:7])


def test_validate_password_accepts_min_length():
    from celerp.services.auth import validate_password
    # exactly 8 characters is valid
    validate_password("12345678")


def test_hash_password_rejects_short():
    """hash_password is the final backend invariant: it validates before bcrypt."""
    from celerp.services.auth import hash_password
    with pytest.raises(ValueError):
        hash_password("short")


@pytest.mark.asyncio
async def test_first_admin_rejects_short_password(client):
    """First-admin registration rejects a short password with a clean 400, not a
    500 from an internal invariant."""
    r = await client.post(
        "/auth/register",
        json={"company_name": "PolCo", "email": "owner@example.com", "name": "Owner", "password": "short7x"[:7]},
    )
    assert r.status_code == 400
    assert "8" in r.json()["detail"] or "characters" in r.json()["detail"].lower()
    assert "password_too_short" not in r.json()["detail"]


@pytest.mark.asyncio
async def test_company_user_creation_rejects_short_password_for_new_global_user(client):
    """Creating a brand-new global user via POST /companies/me/users enforces the
    length policy."""
    reg = await client.post(
        "/auth/register",
        json={"company_name": "PolCo", "email": "owner@example.com", "name": "Owner", "password": "validpass1"},
    )
    assert reg.status_code == 200
    token = reg.json()["access_token"]
    r = await client.post(
        "/companies/me/users",
        json={"email": "newbie@example.com", "name": "Newbie", "role": "operator", "password": "short"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 400
    assert "8" in r.json()["detail"] or "characters" in r.json()["detail"].lower()


@pytest.mark.asyncio
async def test_company_user_link_does_not_validate_unused_password(client, session):
    """Linking an already-existing global user into the current company must not
    reject on the otherwise-unused password field."""
    from celerp.models.company import Company, User
    from celerp.models.accounting import UserCompany

    # Owner A bootstraps company 1.
    reg = await client.post(
        "/auth/register",
        json={"company_name": "LinkCo1", "email": "ownera@example.com", "name": "Owner A", "password": "validpass1"},
    )
    assert reg.status_code == 200
    token1 = reg.json()["access_token"]

    # Owner A creates a new global user B in company 1 (valid password).
    r_new = await client.post(
        "/companies/me/users",
        json={"email": "userb@example.com", "name": "User B", "role": "operator", "password": "validpass2"},
        headers={"Authorization": f"Bearer {token1}"},
    )
    assert r_new.status_code == 200

    owner_a = (await session.execute(select(User).where(User.email == "ownera@example.com"))).scalar_one()

    # A second company owned by A, created directly (A is a shared owner).
    company2 = Company(id=uuid.uuid4(), name="LinkCo2", slug=f"linkco2-{uuid.uuid4().hex[:8]}",
                       settings={"fiscal_year_start": "01-01"})
    session.add(company2)
    await session.flush()
    session.add(UserCompany(id=uuid.uuid4(), user_id=owner_a.id, company_id=company2.id, role="owner"))
    await session.flush()

    token2 = await make_authed_token(session, owner_a.id, company2.id, "owner")

    # Link existing global user B into company 2 with a SHORT unused password.
    r_link = await client.post(
        "/companies/me/users",
        json={"email": "userb@example.com", "name": "User B", "role": "operator", "password": "x"},
        headers={"Authorization": f"Bearer {token2}"},
    )
    assert r_link.status_code == 200, r_link.text

    # B is now a member of company 2.
    userb = (await session.execute(select(User).where(User.email == "userb@example.com"))).scalar_one()
    link = (await session.execute(
        select(UserCompany).where(UserCompany.user_id == userb.id, UserCompany.company_id == company2.id)
    )).scalar_one_or_none()
    assert link is not None


def test_cli_reset_password_uses_shared_validator():
    """CLI reset-password enforces the length policy through the shared constant,
    not a private literal, and rejects a short password before any DB work.

    The behavioural rejection already existed; the red-first fact is that the CLI
    now sources the threshold from the single MIN_PASSWORD_LENGTH constant.
    """
    import celerp.cli as cli_mod
    from celerp.services.auth import MIN_PASSWORD_LENGTH
    assert getattr(cli_mod, "MIN_PASSWORD_LENGTH", None) == MIN_PASSWORD_LENGTH

    from click.testing import CliRunner
    runner = CliRunner()
    result = runner.invoke(cli_mod.main, ["reset-password", "--email", "x@example.com", "--password", "short"])
    assert result.exit_code == 1
    assert "8" in result.output or "characters" in result.output.lower()


def test_setup_ui_preflight_uses_shared_constant():
    """The setup UI preflight imports the single MIN_PASSWORD_LENGTH constant rather
    than a hard-coded literal."""
    import ui.routes.auth as setup_mod
    from celerp.services.auth import MIN_PASSWORD_LENGTH
    assert getattr(setup_mod, "MIN_PASSWORD_LENGTH", None) == MIN_PASSWORD_LENGTH


def test_settings_ui_preflight_uses_shared_constant():
    """The settings change-password UI preflight imports the shared constant."""
    import ui.routes.settings as settings_mod
    from celerp.services.auth import MIN_PASSWORD_LENGTH
    assert getattr(settings_mod, "MIN_PASSWORD_LENGTH", None) == MIN_PASSWORD_LENGTH
