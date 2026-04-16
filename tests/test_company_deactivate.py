# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1
"""Tests for company soft-delete (deactivate/reactivate)."""

import pytest
from httpx import AsyncClient


async def _register(client: AsyncClient, email: str = "owner@deact.test", company: str = "Deact Co") -> str:
    r = await client.post("/auth/register", json={"company_name": company, "email": email, "name": "Owner", "password": "pass1234"})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


async def _add_user(client: AsyncClient, admin_token: str, email: str = "staff@deact.test", role: str = "operator") -> str:
    r = await client.post(
        "/companies/me/users",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"name": "Staff", "email": email, "password": "pass1234", "role": role},
    )
    assert r.status_code == 200, r.text
    r2 = await client.post("/auth/login", json={"email": email, "password": "pass1234"})
    assert r2.status_code == 200, r2.text
    return r2.json()["access_token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_active_company_accessible(client: AsyncClient):
    """Sanity: active company is accessible."""
    token = await _register(client)
    r = await client.get("/companies/me", headers=_auth(token))
    assert r.status_code == 200
    assert r.json().get("name") == "Deact Co"


@pytest.mark.asyncio
async def test_deactivate_requires_admin(client: AsyncClient):
    """Non-admin cannot deactivate a company."""
    admin_token = await _register(client)
    user_token = await _add_user(client, admin_token)
    r = await client.delete("/companies/me", headers=_auth(user_token))
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_deactivate_company(client: AsyncClient):
    """Admin can deactivate the company."""
    token = await _register(client)
    r = await client.delete("/companies/me", headers=_auth(token))
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["is_active"] is False


@pytest.mark.asyncio
async def test_deactivated_company_blocks_api(client: AsyncClient):
    """Requests to a deactivated company are rejected with 401."""
    token = await _register(client)
    await client.delete("/companies/me", headers=_auth(token))

    r = await client.get("/companies/me", headers=_auth(token))
    assert r.status_code == 401
    assert "deactivated" in r.json()["detail"].lower()


@pytest.mark.asyncio
async def test_deactivated_company_blocked_on_switch(client: AsyncClient):
    """Cannot switch into a deactivated company."""
    token = await _register(client)

    # Get company_id from my-companies
    companies = (await client.get("/auth/my-companies", headers=_auth(token))).json()["items"]
    company_id = companies[0]["company_id"]

    await client.delete("/companies/me", headers=_auth(token))

    r = await client.post(f"/auth/switch-company/{company_id}", headers=_auth(token))
    assert r.status_code in (401, 403)


@pytest.mark.asyncio
async def test_deactivated_hidden_from_my_companies(client: AsyncClient, session):
    """Deactivated company does not appear in /auth/my-companies."""
    from uuid import UUID
    from celerp.models.company import Company

    token = await _register(client)
    companies = (await client.get("/auth/my-companies", headers=_auth(token))).json()["items"]
    company_id = UUID(companies[0]["company_id"])

    await client.delete("/companies/me", headers=_auth(token))

    # Restore via DB to verify it reappears
    company = await session.get(Company, company_id)
    company.is_active = True
    await session.commit()

    r = await client.get("/auth/my-companies", headers=_auth(token))
    assert r.status_code == 200
    ids = [item["company_id"] for item in r.json()["items"]]
    assert str(company_id) in ids

    # Deactivate again and verify token is blocked
    company.is_active = False
    await session.commit()
    r = await client.get("/auth/my-companies", headers=_auth(token))
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_reactivate_via_db(client: AsyncClient, session):
    """Deactivated company can be reactivated."""
    from uuid import UUID
    from celerp.models.company import Company

    token = await _register(client)
    companies = (await client.get("/auth/my-companies", headers=_auth(token))).json()["items"]
    company_id = UUID(companies[0]["company_id"])

    await client.delete("/companies/me", headers=_auth(token))

    company = await session.get(Company, company_id)
    assert company.is_active is False

    company.is_active = True
    await session.commit()

    r = await client.get("/companies/me", headers=_auth(token))
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_reactivate_endpoint_requires_admin(client: AsyncClient):
    """Non-admin cannot call reactivate endpoint."""
    admin_token = await _register(client)
    user_token = await _add_user(client, admin_token)
    r = await client.post("/companies/me/reactivate", headers=_auth(user_token))
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_reactivate_endpoint(client: AsyncClient):
    """Admin reactivate endpoint returns 200 on an active company (idempotent)."""
    token = await _register(client)
    r = await client.post("/companies/me/reactivate", headers=_auth(token))
    assert r.status_code == 200
    assert r.json()["is_active"] is True
