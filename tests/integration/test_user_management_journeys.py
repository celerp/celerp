# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1
"""
User management journey tests.

Covers: create user, change role, deactivate, multi-tenant isolation.
Uses journey_api fixture (writable dev.db copy, authenticated as admin@demo.test).
"""
from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient

from celerp.db import get_session
from celerp.main import app


def _u() -> str:
    return str(uuid.uuid4())


def _email() -> str:
    return f"user-{_u()[:8]}@test.example"


# ── Journey tests ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_create_user(journey_api: AsyncClient) -> None:
    """POST /companies/me/users creates a user and returns its id."""
    payload = {"email": _email(), "name": "Test User", "role": "operator", "password": "pw-secret"}
    r = await journey_api.post("/companies/me/users", json=payload)
    assert r.status_code == 200, r.text
    data = r.json()
    assert "id" in data
    uid = data["id"]
    # Verify it appears in the list
    r2 = await journey_api.get("/companies/me/users")
    assert r2.status_code == 200, r2.text
    ids = [u["id"] for u in r2.json()["items"]]
    assert uid in ids


@pytest.mark.asyncio
async def test_create_user_duplicate_email(journey_api: AsyncClient) -> None:
    """Creating two users with the same email returns 400."""
    email = _email()
    payload = {"email": email, "name": "Dup User", "role": "operator", "password": "pw"}
    r1 = await journey_api.post("/companies/me/users", json=payload)
    assert r1.status_code == 200, r1.text
    r2 = await journey_api.post("/companies/me/users", json=payload)
    assert r2.status_code == 400, r2.text


@pytest.mark.asyncio
async def test_change_role(journey_api: AsyncClient) -> None:
    """PATCH /companies/me/users/{id} changes a user's role."""
    email = _email()
    create = await journey_api.post(
        "/companies/me/users",
        json={"email": email, "name": "Role Changer", "role": "operator", "password": "pw"},
    )
    assert create.status_code == 200, create.text
    uid = create.json()["id"]

    patch = await journey_api.patch(f"/companies/me/users/{uid}", json={"role": "admin"})
    assert patch.status_code == 200, patch.text

    # Verify updated role in list
    r = await journey_api.get("/companies/me/users")
    assert r.status_code == 200, r.text
    user = next((u for u in r.json()["items"] if u["id"] == uid), None)
    assert user is not None, f"user {uid} not found in list"
    assert user["role"] == "admin"


@pytest.mark.asyncio
async def test_deactivate_user(journey_api: AsyncClient) -> None:
    """PATCH /companies/me/users/{id} with is_active=False deactivates a user."""
    email = _email()
    create = await journey_api.post(
        "/companies/me/users",
        json={"email": email, "name": "To Deactivate", "role": "operator", "password": "pw"},
    )
    assert create.status_code == 200, create.text
    uid = create.json()["id"]

    patch = await journey_api.patch(f"/companies/me/users/{uid}", json={"is_active": False})
    assert patch.status_code == 200, patch.text

    r = await journey_api.get("/companies/me/users")
    assert r.status_code == 200, r.text
    user = next((u for u in r.json()["items"] if u["id"] == uid), None)
    assert user is not None
    assert user["is_active"] is False


@pytest.mark.asyncio
async def test_patch_nonexistent_user_returns_404(journey_api: AsyncClient) -> None:
    """PATCH on a user id that doesn't exist returns 404."""
    fake_id = _u()
    r = await journey_api.patch(f"/companies/me/users/{fake_id}", json={"role": "admin"})
    assert r.status_code == 404, r.text


@pytest.mark.asyncio
async def test_list_users_returns_items_and_total(journey_api: AsyncClient) -> None:
    """GET /companies/me/users returns {items, total} shape."""
    r = await journey_api.get("/companies/me/users")
    assert r.status_code == 200, r.text
    data = r.json()
    assert "items" in data
    assert "total" in data
    assert data["total"] == len(data["items"])


@pytest.mark.asyncio
async def test_multi_tenant_isolation(journey_api: AsyncClient) -> None:
    """Users created under company A are invisible to company B.

    Uses POST /companies to create a second company scoped to the same user.
    Swaps journey_api's auth header to verify isolation, then restores it.
    """
    # Save company A's token
    token_a = journey_api.headers["Authorization"]

    # Create a second company — response includes a JWT scoped to company B
    r_new_co = await journey_api.post("/companies", json={"name": f"Co-B-{_u()[:6]}"})
    assert r_new_co.status_code == 200, r_new_co.text
    token_b = f"Bearer {r_new_co.json()['access_token']}"

    # Create a user under company A
    r_create = await journey_api.post(
        "/companies/me/users",
        json={"email": _email(), "name": "A's User", "role": "operator", "password": "pw"},
    )
    assert r_create.status_code == 200, r_create.text
    uid_a = r_create.json()["id"]

    # Switch to company B's token
    journey_api.headers["Authorization"] = token_b
    try:
        # Company B's user list must not contain company A's user
        r_list = await journey_api.get("/companies/me/users")
        assert r_list.status_code == 200, r_list.text
        ids_b = [u["id"] for u in r_list.json()["items"]]
        assert uid_a not in ids_b, "Company A's user leaked into Company B's list"

        # Company B cannot patch company A's user (different company_id → 404)
        r_patch = await journey_api.patch(f"/companies/me/users/{uid_a}", json={"role": "admin"})
        assert r_patch.status_code == 404, r_patch.text
    finally:
        journey_api.headers["Authorization"] = token_a
