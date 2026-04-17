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

    # New password works
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
